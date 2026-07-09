#!/usr/bin/env python3
"""
Home Perfect — Live Dev Cockpit (3 tabs)
========================================
A local dashboard that sits next to Claude Code and updates in real time.

TABS (bottom bar, like a phone app):
  1. Terminal    — a REAL shell running in your repo, in the browser. Run
                   `claude` inside it and it's the genuine interactive
                   session; type in the message box to send it a line while
                   it works. Run xcodebuild, git, anything. Works from your
                   phone too.
  2. Cockpit     — commits streaming in, files being edited, structure growing
  3. Claude Code — EXACTLY what Claude Code is doing right now: every tool
                   call, file edit, bash command, and subagent (Task) launch,
                   read live from Claude Code's own session logs
  4. Galaxy      — an auto-generated galaxy of YOUR app. Planets drift
                   slowly; click a feature to zoom into its own sub-galaxy
                   of files. Fullscreen it and show your friends.

SECURITY
--------
Each launch prints a one-time access key. Your Mac (localhost) connects with
no key. Every other device must open the exact "Phone:" link, which carries
the key and sets a cookie — anyone else on the Wi-Fi is refused. The Terminal
is a real shell on this Mac, so keep that link private.

No installs, no dependencies — just Python 3 (already on your Mac). The
Terminal tab loads xterm.js from a CDN, so it needs internet the first time.

HOW TO RUN
----------
1. Open a NEW terminal tab (keep Claude Code running in its own tab).
2. cd into your Home Perfect repo folder.
3. Run:   python3 dev_cockpit.py
4. Your browser opens http://127.0.0.1:4321 — pick a tab at the bottom.

Stop with Ctrl+C.

HOW THE CLAUDE CODE TAB WORKS
-----------------------------
Claude Code keeps a live transcript of each session in
~/.claude/projects/<your-project>/. This cockpit tails the newest transcript
and shows tool calls, edits, bash commands, Claude's commentary, and subagent
activity as they happen. If the tab says "no session found", just start
Claude Code inside this folder once — the log appears immediately.

HOOKS (macOS desktop notifications)
-----------------------------------
The cockpit watches for key moments and pings you through Notification
Center, even when this window is hidden:
  - Error alerts       : a build or tool call fails            (default ON)
  - Task finished      : Claude Code goes working -> idle      (default ON)
  - New commits        : every commit that lands               (default ON)
  - Subagent launches  : a Task subagent spins up              (default OFF)
Toggle them in the Claude Code tab. Notifications use macOS's built-in
osascript — nothing to install. The first one may ask you to allow
notifications from "Script Editor" in System Settings.

GALAXY EXTRAS
-------------
- Infinite zoom: ringed planets are folders — click to dive deeper,
  level after level, all the way down to single files. Esc / Back /
  empty-space click goes up one level.
- Time-lapse: replays your git history; watch the galaxy grow commit
  by commit with a play button and a scrubber.
- Build health: after a failing build/test run, the affected feature's
  planet glows red; it turns green again when the build passes.

PHONE ACCESS
------------
The cockpit is also served on your local network. The terminal prints a
"Phone:" URL — open it on any device on the same Wi-Fi (run
`pip3 install qrcode` to also get a scannable QR code in the terminal).
Note: anyone on your Wi-Fi can view the cockpit while it runs.
"""

import glob
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
import base64
import secrets
import struct
import select
import signal
import queue
import urllib.parse
from collections import Counter

try:
    import pty
    import termios
    import fcntl
    PTY_OK = True
except Exception:               # non-unix; terminal tab will be disabled
    PTY_OK = False

PORT = 4321
ROOT = os.getcwd()
IGNORE = {".git", "node_modules", "build", "DerivedData", ".next", "dist",
          "Pods", ".venv", "venv", ".idea", "__pycache__", ".dart_tool"}
SELF_FILES = {"dev_cockpit.py"}
LAN_URL = None       # set at startup in main()
TOKEN = None         # access token; set at startup in main()


def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# --------------------------------------------------------------------------
# shell + repo state
# --------------------------------------------------------------------------
def sh(args):
    try:
        r = subprocess.run(args, cwd=ROOT, capture_output=True,
                           text=True, timeout=6)
        return r.stdout.strip()
    except Exception:
        return ""


def is_git():
    return sh(["git", "rev-parse", "--is-inside-work-tree"]) == "true"


def list_files():
    if is_git():
        out = sh(["git", "ls-files"])
        files = [f for f in out.splitlines() if f]
        if files:
            return files
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in IGNORE and not d.startswith(".")]
        for f in fn:
            if f.startswith("."):
                continue
            files.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return files


def build_tree(files):
    """Full project tree: folders all the way down to single files,
    capped at 12 children per level for readability."""
    root = {"name": os.path.basename(ROOT) or "project", "dir": True,
            "count": 0, "children": {}}
    for f in files:
        if f in SELF_FILES:
            continue
        parts = f.split(os.sep)
        root["count"] += 1
        node = root
        for i, part in enumerate(parts):
            isdir = i < len(parts) - 1
            ch = node["children"].setdefault(
                part, {"name": part, "dir": isdir, "count": 0, "children": {}})
            ch["count"] += 1
            ch["dir"] = ch["dir"] or isdir
            node = ch

    def finalize(n):
        kids = sorted(n["children"].values(),
                      key=lambda c: (-c["count"], c["name"]))[:12]
        return {"name": n["name"], "dir": n["dir"], "count": n["count"],
                "children": [finalize(k) for k in kids]}
    return finalize(root)


def build_timelapse(max_commits=80):
    """Galaxy snapshots per commit for the time-lapse scrubber."""
    if not is_git():
        return {"commits": []}
    log = sh(["git", "log", "--reverse", "--pretty=format:%h\x1f%ct\x1f%s"])
    lines = [ln for ln in log.splitlines() if ln.strip()]
    if len(lines) > max_commits:
        step = (len(lines) - 1) / (max_commits - 1)
        keep = sorted({round(i * step) for i in range(max_commits)})
        lines = [lines[i] for i in keep]
    commits = []
    for line in lines:
        p = line.split("\x1f")
        if len(p) != 3:
            continue
        h, ct, msg = p
        files = sh(["git", "ls-tree", "-r", "--name-only", h]).splitlines()
        folders = Counter()
        total = 0
        for f in files:
            f = f.strip()
            if not f or f in SELF_FILES:
                continue
            total += 1
            folders[f.split("/")[0]] += 1
        commits.append({"hash": h, "t": int(ct) if ct.isdigit() else 0,
                        "msg": msg, "total": total,
                        "clusters": [{"name": k, "count": v}
                                     for k, v in folders.most_common(12)]})
    return {"commits": commits}


def build_state():
    git = is_git()
    files = list_files()

    folders = Counter()
    for f in files:
        parts = f.split(os.sep)
        folders[parts[0] if len(parts) > 1 else "(root)"] += 1

    state = {
        "time": time.strftime("%H:%M:%S"),
        "project": os.path.basename(ROOT) or "project",
        "git": git,
        "fileCount": len(files),
        "folders": folders.most_common(12),
        "branch": "",
        "commits": [],
        "changed": [],
        "tree": build_tree(files),
        "lanUrl": LAN_URL,
        "activity": claude_activity(files),
    }

    if git:
        state["branch"] = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "?"
        log = sh(["git", "log", "-30", "--pretty=format:%h\x1f%an\x1f%ar\x1f%s"])
        for line in log.splitlines():
            p = line.split("\x1f")
            if len(p) == 4:
                state["commits"].append(
                    {"hash": p[0], "author": p[1], "when": p[2], "msg": p[3]})
        for line in sh(["git", "status", "--porcelain"]).splitlines():
            # NOTE: don't slice by index — sh() strips leading whitespace,
            # and porcelain's status codes are position-sensitive.
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            code, path = parts
            if path in SELF_FILES or path.startswith("__pycache__"):
                continue
            state["changed"].append({"code": code, "path": path})
    return state


# --------------------------------------------------------------------------
# Claude Code live activity (reads ~/.claude/projects session transcripts)
# --------------------------------------------------------------------------
def encode_project_dir(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_sessions_dir():
    base = os.path.expanduser("~/.claude/projects")
    exact = os.path.join(base, encode_project_dir(ROOT))
    if os.path.isdir(exact):
        return exact
    # fallback: any project dir ending with this folder's encoded name
    tail = encode_project_dir(os.path.basename(ROOT))
    if os.path.isdir(base) and tail:
        cands = [os.path.join(base, x) for x in os.listdir(base)
                 if x.endswith(tail)]
        if cands:
            return max(cands, key=os.path.getmtime)
    return None


def tail_lines(path, max_bytes=400_000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, 2)
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        return lines[1:] if size > max_bytes else lines
    except Exception:
        return []


def short_path(p):
    if not p:
        return ""
    if p.startswith(ROOT):
        p = p[len(ROOT):].lstrip(os.sep)
    return p if len(p) <= 90 else "…" + p[-89:]


def summarize_tool(name, inp):
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return short_path(inp.get("file_path") or inp.get("path") or "")
    if name == "Bash":
        return (inp.get("command") or "")[:130]
    if name == "Task":
        sub = inp.get("subagent_type") or "subagent"
        desc = inp.get("description") or (inp.get("prompt") or "")[:90]
        return f"{sub} — {desc}"
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        doing = [t.get("content", "") for t in todos
                 if t.get("status") == "in_progress"]
        return ("now: " + doing[0][:110]) if doing else f"{len(todos)} todos updated"
    try:
        return json.dumps(inp)[:110]
    except Exception:
        return ""


def _iso_secs(a, b):
    """Seconds between two ISO timestamps, or None."""
    try:
        from datetime import datetime
        def p(x):
            return datetime.fromisoformat(x.replace("Z", "+00:00"))
        return max(0, int((p(b) - p(a)).total_seconds()))
    except Exception:
        return None


def _first_text(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                return (b.get("text") or "").strip()
    return ""


def _top_of(p, file_map):
    """Top-level folder (or root file name) a path belongs to."""
    p = short_path(p).replace("\\", "/")
    if not p:
        return None
    if "/" in p:
        return p.split("/", 1)[0]
    return file_map.get(os.path.basename(p), p)


def compute_health(records, file_map):
    """Pair each build/test command with its result; failing builds mark
    the involved top-level folders 'err', a passing build flips them 'ok'."""
    health, pend, edits = {}, {}, []
    valid = set(file_map.values())
    build_re = re.compile(
        r"(xcodebuild|swift\s+build|swift\s+test|\bbuild\b|\btest\b|pytest|"
        r"npm\s+run|xcrun)")
    path_re = re.compile(r"[\w@~/.\\-]+\.[A-Za-z0-9]{1,6}")
    for r in sorted(records, key=lambda x: x.get("timestamp", "")):
        c = (r.get("message") or {}).get("content")
        if r.get("type") == "assistant" and isinstance(c, list):
            for b in c:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                nm, inp = b.get("name"), b.get("input") or {}
                if nm in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    t = _top_of(inp.get("file_path") or "", file_map)
                    if t:
                        edits.append(t)
                    if b.get("id"):
                        pend[b["id"]] = {"n": nm,
                                         "p": inp.get("file_path", "")}
                elif nm == "Bash" and b.get("id"):
                    pend[b["id"]] = {"n": "Bash",
                                     "c": inp.get("command") or ""}
        elif r.get("type") == "user" and isinstance(c, list):
            for b in c:
                if not (isinstance(b, dict)
                        and b.get("type") == "tool_result"):
                    continue
                info = pend.pop(b.get("tool_use_id"), None)
                if not info:
                    continue
                iserr = bool(b.get("is_error"))
                if info["n"] == "Bash" and build_re.search(info.get("c", "")):
                    if iserr:
                        tops = set()
                        for m in path_re.findall(str(b.get("content"))):
                            t = _top_of(m, file_map)
                            if t in valid:
                                tops.add(t)
                        if not tops:
                            tops = set(edits[-6:])
                        for t in tops:
                            health[t] = "err"
                    else:
                        for k in list(health):
                            health[k] = "ok"
                        edits = []
                elif iserr and info.get("p"):
                    t = _top_of(info["p"], file_map)
                    if t:
                        health[t] = "err"
    return health


def claude_activity(files=None, limit=80):
    d = claude_sessions_dir()
    out = {"found": bool(d), "events": [], "active": False,
           "ageSec": None, "sessions": 0, "agents": [], "now": "",
           "plan": [], "health": {}}
    if not d:
        return out
    files = sorted(glob.glob(os.path.join(d, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:2]
    out["sessions"] = len(files)

    # -- pass 1: parse every record --------------------------------------
    records = []
    for fp in files:
        for line in tail_lines(fp):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("isMeta"):
                continue
            records.append(rec)
    byid = {r["uuid"]: r for r in records if r.get("uuid")}
    file_map = {}
    for f in (files or []):
        parts = f.split(os.sep)
        file_map.setdefault(os.path.basename(f),
                            parts[0] if len(parts) > 1 else f)

    # -- pass 2: register subagents (Task launches + their results) ------
    tasks, task_by_toolid = [], {}
    for r in records:
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") == "Task"):
                    inp = b.get("input") or {}
                    t = {"toolid": b.get("id"),
                         "type": inp.get("subagent_type") or "subagent",
                         "desc": inp.get("description") or "",
                         "prompt": (inp.get("prompt") or "").strip(),
                         "start": r.get("timestamp", ""), "end": "",
                         "done": False, "actions": 0, "last": ""}
                    tasks.append(t)
                    if t["toolid"]:
                        task_by_toolid[t["toolid"]] = t
        elif r.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    t = task_by_toolid.get(b.get("tool_use_id"))
                    if t:
                        t["done"] = True
                        t["end"] = r.get("timestamp", "")

    # -- attribute every sidechain record to the Task that launched it ---
    root_cache = {}

    def chain_root(r):
        u = r.get("uuid")
        if u in root_cache:
            return root_cache[u]
        cur, hops = r, 0
        while hops < 200:
            p = cur.get("parentUuid")
            if not p or p not in byid or not byid[p].get("isSidechain"):
                break
            cur = byid[p]
            hops += 1
        if u:
            root_cache[u] = cur
        return cur

    def agent_for(r):
        if not tasks:
            return None
        txt = _first_text(chain_root(r).get("message"))
        for t in tasks:
            if t["prompt"] and txt and (txt == t["prompt"]
                                        or txt[:100] == t["prompt"][:100]):
                return t
        # fallback: latest Task launched at or before this record
        ts = r.get("timestamp", "")
        prior = [t for t in tasks if t["start"] and t["start"] <= ts]
        return prior[-1] if prior else tasks[-1]

    # -- pass 3: build the event feed -------------------------------------
    events = []
    for rec in records:
        ts = rec.get("timestamp", "")
        sub = bool(rec.get("isSidechain"))
        content = (rec.get("message") or {}).get("content")
        rtype = rec.get("type")
        agent = agent_for(rec) if sub else None
        aname = agent["type"] if agent else ""

        if rtype == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    detail = summarize_tool(b.get("name", ""), b.get("input"))
                    events.append({"ts": ts, "kind": "tool",
                                   "name": b.get("name", "tool"),
                                   "detail": detail, "sub": sub,
                                   "agent": aname})
                    if agent is not None:
                        agent["actions"] += 1
                        agent["last"] = (b.get("name", "tool")
                                         + " · " + detail)[:110]
                elif bt == "text":
                    txt = (b.get("text") or "").strip()
                    if txt and not txt.startswith("<"):
                        events.append({"ts": ts, "kind": "say",
                                       "name": aname or "Claude",
                                       "detail": txt[:170], "sub": sub,
                                       "agent": aname})
        elif rtype == "user":
            if isinstance(content, list):
                results = [b for b in content if isinstance(b, dict)
                           and b.get("type") == "tool_result"]
                for b in results:
                    if b.get("is_error"):
                        events.append({"ts": ts, "kind": "err",
                                       "name": "error",
                                       "detail": str(b.get("content"))[:150],
                                       "sub": sub, "agent": aname})
                if sub or results:
                    continue  # sidechain user turns = subagent internals
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = (b.get("text") or "").strip()
                        if txt and not txt.startswith("<"):
                            events.append({"ts": ts, "kind": "you",
                                           "name": "You",
                                           "detail": txt[:170],
                                           "sub": False, "agent": ""})
            elif isinstance(content, str) and not sub:
                txt = content.strip()
                if txt and not txt.startswith("<"):
                    events.append({"ts": ts, "kind": "you", "name": "You",
                                   "detail": txt[:170], "sub": False,
                                   "agent": ""})

    events.sort(key=lambda e: e.get("ts") or "")
    out["events"] = events[-limit:][::-1]  # newest first

    # -- plan: Claude Code's latest TodoWrite checklist ---------------------
    plan, plan_ts = [], ""
    for r in records:
        if r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") == "TodoWrite"):
                    ts = r.get("timestamp", "")
                    if ts >= plan_ts:
                        plan_ts = ts
                        todos = (b.get("input") or {}).get("todos") or []
                        plan = [{"content": t.get("content", ""),
                                 "status": t.get("status", "pending")}
                                for t in todos if isinstance(t, dict)][:20]
    out["plan"] = plan
    out["health"] = compute_health(records, file_map)

    # -- subagent summary + "doing right now" line ------------------------
    for t in tasks[-8:]:
        out["agents"].append({
            "type": t["type"], "desc": t["desc"], "done": t["done"],
            "secs": _iso_secs(t["start"], t["end"]) if t["done"] else None,
            "actions": t["actions"], "last": t["last"]})

    if files:
        age = time.time() - os.path.getmtime(files[0])
        out["active"] = age < 25
        out["ageSec"] = int(age)
        if out["active"]:
            out["now"] = _now_line(out["events"])
    return out


def _now_line(events):
    verbs = {"Edit": "editing", "Write": "writing", "MultiEdit": "editing",
             "NotebookEdit": "editing", "Read": "reading", "Bash": "running",
             "Grep": "searching for", "Glob": "searching for",
             "WebFetch": "fetching", "WebSearch": "searching the web for",
             "Task": "delegating:", "TodoWrite": "planning —"}
    for e in events:                              # newest first
        if e["kind"] == "tool":
            v = verbs.get(e["name"], "using " + e["name"] + " on")
            who = (e["agent"] + " subagent is ") if e.get("agent") else ""
            return (who + v + " " + (e["detail"] or "")).strip()[:120]
    return ""


# --------------------------------------------------------------------------
# hooks — desktop notifications fired from repo + Claude Code events
# --------------------------------------------------------------------------
HOOKS = {"errors": True, "done": True, "commits": True, "subagents": False}
HOOKS_LOCK = threading.Lock()
NOTIFY_OK = (sys.platform == "darwin")


def notify(title, msg, sound="Glass"):
    """macOS desktop notification via built-in osascript (no installs)."""
    if not NOTIFY_OK:
        return
    script = ("display notification " + json.dumps(str(msg)[:180]) +
              " with title " + json.dumps(str(title)[:80]))
    if sound:
        script += f' sound name "{sound}"'
    try:
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def hooks_watcher():
    """Background thread: watches state transitions and fires notifications."""
    prev_hashes = None
    prev_active = None
    seen = set()
    startup = True
    while True:
        try:
            s = build_state()
            a = s.get("activity") or {}
            with HOOKS_LOCK:
                h = dict(HOOKS)

            # event hooks: errors + subagent launches (oldest first)
            for e in reversed(a.get("events") or []):
                key = (e.get("ts"), e.get("name"), e.get("detail"))
                if key in seen:
                    continue
                seen.add(key)
                if startup:
                    continue          # don't replay history on launch
                if e.get("kind") == "err" and h["errors"]:
                    notify("Claude Code hit an error",
                           e.get("detail") or "check the cockpit", "Basso")
                elif e.get("name") == "Task" and h["subagents"]:
                    notify("Subagent launched",
                           e.get("detail") or "Task started", "Submarine")
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])

            # working -> idle transition = Claude Code finished
            if a.get("found"):
                act = bool(a.get("active"))
                if prev_active is True and act is False and h["done"]:
                    n = len(s.get("changed") or [])
                    notify("Claude Code finished",
                           f"{n} uncommitted change(s) in {s['project']}",
                           "Glass")
                prev_active = act

            # new commits
            hashes = {c["hash"] for c in s.get("commits") or []}
            if prev_hashes is not None and h["commits"]:
                for c in s.get("commits") or []:
                    if c["hash"] not in prev_hashes:
                        notify("New commit — " + s["project"], c["msg"], "Pop")
            prev_hashes = hashes
            startup = False
        except Exception:
            pass
        time.sleep(3)


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Home Perfect — Dev Cockpit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/xterm/5.3.0/xterm.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/xterm/5.3.0/xterm.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/xterm-addon-fit/0.8.0/xterm-addon-fit.min.js"></script>
<style>
:root{--bg:#05060d;--panel:#0c0f1c;--ink:#eef1ff;--dim:#8a90b8;
  --amber:#ffb020;--teal:#37d6c0;--violet:#8a7bff;--green:#4ade80;--red:#ff8a97;
  --line:rgba(160,170,220,.12)}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Inter",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:20px 22px 96px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;
  margin-bottom:18px;flex-wrap:wrap}
.title{font-family:"Space Grotesk";font-weight:700;font-size:22px;
  letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:12px}
.pulse{width:9px;height:9px;border-radius:50%;background:var(--green);
  box-shadow:0 0 0 0 rgba(74,222,128,.6);animation:p 2s infinite;flex:none}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(74,222,128,.5)}70%{box-shadow:0 0 0 10px rgba(74,222,128,0)}100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}}
.meta{color:var(--dim);font-size:13px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.meta b{color:var(--ink);font-weight:600}
.branch{font-family:"JetBrains Mono";background:rgba(138,123,255,.14);color:#c7bcff;
  border:1px solid rgba(138,123,255,.3);padding:3px 10px;border-radius:999px;font-size:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px}
.panel h2{font-family:"Space Grotesk";font-size:12px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--dim);margin:0 0 14px;font-weight:600}
.empty{color:var(--dim);font-size:13px;padding:8px 2px;line-height:1.55}
::-webkit-scrollbar{width:8px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:99px}
section[hidden]{display:none!important}

/* ---- tab bar ---- */
nav.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:50;
  display:flex;justify-content:center;gap:6px;padding:10px 14px calc(10px + env(safe-area-inset-bottom));
  background:rgba(7,9,18,.82);backdrop-filter:blur(14px);
  border-top:1px solid var(--line)}
nav.tabbar button{flex:0 1 220px;display:flex;flex-direction:column;align-items:center;gap:3px;
  background:none;border:0;color:var(--dim);cursor:pointer;padding:8px 10px;
  border-radius:12px;font-family:"Space Grotesk";font-weight:600;font-size:12.5px;
  letter-spacing:.02em;transition:.16s}
nav.tabbar button .ico{font-size:17px;line-height:1}
nav.tabbar button:hover{color:var(--ink);background:rgba(255,255,255,.05)}
nav.tabbar button.on{color:var(--ink);background:rgba(255,176,32,.1)}
nav.tabbar button.on .ico{filter:none}
nav.tabbar button:focus-visible{outline:2px solid var(--amber);outline-offset:2px}

/* ---- tab 1: cockpit ---- */
.stats{display:flex;gap:14px;margin-bottom:16px;flex-wrap:wrap}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:14px 18px;flex:1;min-width:120px}
.stat .n{font-family:"Space Grotesk";font-weight:700;font-size:30px;line-height:1}
.stat .l{color:var(--dim);font-size:12px;margin-top:6px}
.stat.a .n{color:var(--amber)}.stat.t .n{color:var(--teal)}.stat.v .n{color:var(--violet)}
.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.feed{display:flex;flex-direction:column;gap:2px;max-height:340px;overflow:auto}
.commit{display:flex;gap:12px;padding:9px 8px;border-radius:10px;align-items:baseline}
.commit:hover{background:rgba(255,255,255,.03)}
.commit.new{animation:in .6s ease}
@keyframes in{from{background:rgba(255,176,32,.16)}to{background:transparent}}
.commit .h{font-family:"JetBrains Mono";font-size:12px;color:var(--amber);flex:none}
.commit .m{flex:1;font-size:14px}
.commit .w{color:var(--dim);font-size:12px;flex:none}
.changed{display:flex;flex-direction:column;gap:6px;max-height:260px;overflow:auto}
.chip{display:flex;gap:10px;align-items:center;font-family:"JetBrains Mono";font-size:12.5px}
.code{width:26px;text-align:center;border-radius:6px;padding:2px 0;font-weight:600;flex:none}
.code.M{background:rgba(255,176,32,.16);color:var(--amber)}
.code.A{background:rgba(74,222,128,.16);color:var(--green)}
.code.D{background:rgba(255,90,110,.16);color:var(--red)}
.code.R{background:rgba(138,123,255,.16);color:#c7bcff}
.chip .path{color:#c8cced;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars{display:flex;flex-direction:column;gap:9px}
.bar{display:grid;grid-template-columns:120px 1fr 34px;gap:10px;align-items:center;font-size:13px}
.bar .fn{color:#c8cced;font-family:"JetBrains Mono";font-size:12px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{height:8px;background:rgba(255,255,255,.05);border-radius:99px;overflow:hidden}
.bar .fill{height:100%;background:linear-gradient(90deg,var(--teal),var(--violet));
  border-radius:99px;transition:width .5s}
.bar .c{color:var(--dim);font-size:12px;text-align:right}
.prev input{width:100%;background:#070a14;border:1px solid var(--line);color:var(--ink);
  border-radius:10px;padding:10px 12px;font-family:"JetBrains Mono";font-size:12.5px;margin-bottom:10px}
.prev iframe{width:100%;height:300px;border:1px solid var(--line);border-radius:12px;background:#000}
.prev .ph{color:var(--dim);font-size:13px;line-height:1.5}

/* ---- tab 2: claude code activity ---- */
.status{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.lamp{display:flex;align-items:center;gap:8px;font-family:"Space Grotesk";
  font-weight:600;font-size:14px;padding:8px 16px;border-radius:999px;
  border:1px solid var(--line);background:var(--panel)}
.lamp .d{width:10px;height:10px;border-radius:50%;background:#5a6080}
.lamp.live .d{background:var(--green);box-shadow:0 0 0 0 rgba(74,222,128,.6);animation:p 1.6s infinite}
.lamp.live{border-color:rgba(74,222,128,.35)}
.status .note{color:var(--dim);font-size:13px}
.hrow{display:flex;gap:18px;flex-wrap:wrap;align-items:center}
.hk{display:flex;align-items:center;gap:9px;font-size:13px;color:#c8cced;cursor:pointer}
.hk input{accent-color:var(--amber);width:15px;height:15px;cursor:pointer;flex:none}
.hk em{color:var(--dim);font-style:normal;font-size:11.5px;display:block;margin-top:1px}
.hk span{line-height:1.2}
#hkTest{font-family:"Space Grotesk";font-weight:600;font-size:12px;color:var(--ink);
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);
  padding:7px 13px;border-radius:999px;cursor:pointer;transition:.16s;margin-left:auto}
#hkTest:hover{background:rgba(255,255,255,.12)}
#hkTest:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.hnote{color:var(--dim);font-size:12px;margin:12px 0 0;line-height:1.5}
#tab-term{display:flex;flex-direction:column;gap:10px}
.termbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
#tState{font-family:"Space Grotesk";font-weight:600;font-size:13px;color:var(--dim)}
.tlamp{width:9px;height:9px;border-radius:50%;background:#5a6080;flex:none}
.tlamp.on{background:var(--green);box-shadow:0 0 0 0 rgba(74,222,128,.6);animation:p 1.8s infinite}
.tquick{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.tquick button{font-family:"JetBrains Mono";font-weight:500;font-size:12px;color:#c8cced;
  background:rgba(255,255,255,.05);border:1px solid var(--line);
  padding:6px 12px;border-radius:8px;cursor:pointer;transition:.15s}
.tquick button:hover{background:rgba(255,255,255,.12);color:var(--ink)}
#term{flex:1;min-height:320px;height:calc(100vh - 250px);
  background:#04050b;border:1px solid var(--line);border-radius:14px;
  padding:10px 12px;overflow:hidden}
#term .xterm{height:100%}
.sayrow{display:flex;gap:8px}
.sayrow input{flex:1;background:#070a14;border:1px solid var(--line);color:var(--ink);
  border-radius:10px;padding:12px 14px;font-family:"JetBrains Mono";font-size:13px}
.sayrow input:focus{outline:none;border-color:rgba(255,176,32,.5)}
.sayrow button{font-family:"Space Grotesk";font-weight:700;font-size:13px;color:#05060d;
  background:var(--amber);border:0;padding:0 22px;border-radius:10px;cursor:pointer}
.sayrow button:hover{filter:brightness(1.08)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:-2px 0 12px}
.fc{font-family:"Space Grotesk";font-weight:600;font-size:11.5px;color:var(--dim);
  background:rgba(255,255,255,.04);border:1px solid var(--line);
  padding:5px 12px;border-radius:999px;cursor:pointer;transition:.15s}
.fc:hover{color:var(--ink)}
.fc.on{color:var(--ink);background:rgba(255,176,32,.12);border-color:rgba(255,176,32,.3)}
.fc:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.agent{display:flex;gap:12px;align-items:flex-start;padding:10px 8px;border-radius:10px}
.agent:hover{background:rgba(255,255,255,.03)}
.agent .ad{width:10px;height:10px;border-radius:50%;margin-top:5px;flex:none;background:#5a6080}
.agent.run .ad{background:var(--violet);box-shadow:0 0 0 0 rgba(138,123,255,.6);animation:p 1.6s infinite}
.agent .an{font-family:"Space Grotesk";font-weight:600;font-size:14px}
.agent .an em{color:var(--dim);font-weight:400;font-style:normal;font-size:12.5px;margin-left:8px}
.agent .am{color:var(--dim);font-size:12px;font-family:"JetBrains Mono";margin-top:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.agent .st{margin-left:auto;flex:none;font-size:11px;font-family:"Space Grotesk";
  font-weight:600;padding:3px 10px;border-radius:999px}
.st.run{background:rgba(138,123,255,.16);color:#c7bcff;border:1px solid rgba(138,123,255,.35)}
.st.done{background:rgba(74,222,128,.1);color:#9df3ba;border:1px solid rgba(74,222,128,.24)}
.pitem{display:flex;gap:10px;align-items:flex-start;padding:7px 8px;border-radius:10px;
  font-size:13.5px;color:#c8cced}
.pitem .pi{width:17px;height:17px;border-radius:50%;flex:none;margin-top:1px;
  display:flex;align-items:center;justify-content:center;font-size:11px}
.pitem.todo .pi{border:1.5px solid rgba(160,170,220,.4)}
.pitem.now{color:var(--ink);font-weight:600}
.pitem.now .pi{background:var(--amber);box-shadow:0 0 0 0 rgba(255,176,32,.5);animation:p 1.6s infinite}
.pitem.done{color:var(--dim)}
.pitem.done .pt{text-decoration:line-through}
.pitem.done .pi{background:rgba(74,222,128,.15);color:#9df3ba}
#tlTop{display:flex;align-items:center;gap:10px;padding:4px 8px 4px 4px;
  border-radius:999px;background:rgba(12,14,26,.6);border:1px solid rgba(255,255,255,.12)}
#tlTop input[type=range]{accent-color:var(--amber);width:clamp(120px,20vw,260px);cursor:pointer}
#tlTop #tlCount{font-family:"JetBrains Mono";font-size:11.5px;color:var(--dim);
  min-width:44px;text-align:center}
.tlcallout{position:absolute;z-index:7;pointer-events:none;
  font-family:"Space Grotesk";font-weight:600;font-size:13px;color:#fff;
  white-space:nowrap;opacity:0;transition:opacity .35s}
.tlcallout.on{opacity:1}
.tlcallout .cmsg{display:block;font-family:"Inter";font-weight:400;
  font-size:11px;color:var(--dim);margin-top:1px;max-width:230px;
  overflow:hidden;text-overflow:ellipsis}
.afeed{display:flex;flex-direction:column;gap:2px;max-height:calc(100vh - 300px);
  min-height:200px;overflow:auto}
.ev{display:flex;gap:10px;align-items:baseline;padding:8px;border-radius:10px}
.ev:hover{background:rgba(255,255,255,.03)}
.ev .t{font-family:"JetBrains Mono";font-size:11px;color:var(--dim);flex:none;width:62px}
.badge{font-family:"Space Grotesk";font-weight:600;font-size:11px;
  padding:2px 9px;border-radius:999px;flex:none;letter-spacing:.02em}
.badge.tool{background:rgba(255,176,32,.14);color:#ffcf7a;border:1px solid rgba(255,176,32,.28)}
.badge.bash{background:rgba(55,214,192,.12);color:#8ceadd;border:1px solid rgba(55,214,192,.28)}
.badge.task{background:rgba(138,123,255,.14);color:#c7bcff;border:1px solid rgba(138,123,255,.32)}
.badge.say{background:rgba(255,255,255,.06);color:var(--dim);border:1px solid var(--line)}
.badge.you{background:rgba(74,222,128,.12);color:#9df3ba;border:1px solid rgba(74,222,128,.26)}
.badge.err{background:rgba(255,90,110,.14);color:var(--red);border:1px solid rgba(255,90,110,.3)}
.badge.sub{background:rgba(138,123,255,.22);color:#ded7ff;border:1px solid rgba(138,123,255,.45)}
.ev .d2{flex:1;font-size:13px;color:#c8cced;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-family:"JetBrains Mono";font-size:12.5px}
.ev.say .d2{font-family:"Inter";font-style:italic;color:var(--dim)}
.ev.new{animation:in .6s ease}

/* ---- tab 3: galaxy ---- */
#tab-galaxy{position:fixed;inset:0;bottom:0;background:var(--bg)}
#tab-galaxy canvas{position:absolute;inset:0;display:block}
.goverlay{position:absolute;top:0;left:0;right:0;padding:20px 24px;
  display:flex;justify-content:space-between;align-items:flex-start;pointer-events:none;gap:14px}
.goverlay .gt{font-family:"Space Grotesk";font-weight:700;
  font-size:clamp(24px,4.5vw,42px);letter-spacing:-.02em;margin:0;line-height:1}
.goverlay .gs{color:var(--dim);font-size:13px;margin:8px 0 0;max-width:44ch}
.gbtns{display:flex;gap:8px;pointer-events:auto}
.gbtns button{font-family:"Space Grotesk";font-weight:600;font-size:12px;
  color:var(--ink);background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);
  padding:8px 14px;border-radius:999px;cursor:pointer;transition:.18s;backdrop-filter:blur(6px)}
.gbtns button:hover{background:rgba(255,255,255,.13)}
.gbtns button:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
#gtip{position:absolute;pointer-events:none;z-index:10;opacity:0;
  transform:translateY(5px);transition:.15s;max-width:250px;
  background:rgba(12,14,26,.9);border:1px solid rgba(255,255,255,.15);
  border-radius:12px;padding:11px 14px;backdrop-filter:blur(12px)}
#gtip.on{opacity:1;transform:none}
#gtip .n{font-family:"Space Grotesk";font-weight:700;font-size:15px;margin:0 0 2px;
  display:flex;align-items:center;gap:8px}
#gtip .n i{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
#gtip .c{color:var(--dim);font-size:12px;margin:0}
</style></head><body>

<div class="wrap" id="chrome">
  <header>
    <h1 class="title"><span class="pulse"></span><span id="proj">…</span> · dev cockpit</h1>
    <div class="meta">
      <span class="branch" id="branch">—</span>
      <span>updated <b id="clock">—</b></span>
      <span id="phone" title="Open on your phone (same Wi-Fi)" style="font-family:'JetBrains Mono';font-size:12px;color:var(--teal)"></span>
    </div>
  </header>

  <!-- TAB 1 : COCKPIT -->
  <section id="tab-cockpit" hidden>
    <div class="stats">
      <div class="stat a"><div class="n" id="sFiles">—</div><div class="l">files</div></div>
      <div class="stat t"><div class="n" id="sCommits">—</div><div class="l">commits</div></div>
      <div class="stat v"><div class="n" id="sChanged">—</div><div class="l">uncommitted changes</div></div>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>Commit stream — the app taking shape</h2>
        <div class="feed" id="feedEl"></div>
      </div>
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="panel">
          <h2>Working right now</h2>
          <div class="changed" id="changedEl"></div>
        </div>
        <div class="panel">
          <h2>Structure</h2>
          <div class="bars" id="barsEl"></div>
        </div>
      </div>
    </div>
    <div class="panel prev" style="margin-top:16px">
      <h2>Live preview (optional)</h2>
      <input id="url" placeholder="Paste a localhost URL to embed your running app, e.g. http://localhost:3000">
      <div id="prevHost"><div class="ph">Web app running locally? Paste its URL above to watch it live here.<br>
        Building native iOS? Keep the Xcode simulator open next to this window — it updates on every save.</div></div>
    </div>
  </section>

  <!-- TAB 2 : CLAUDE CODE -->
  <section id="tab-claude" hidden>
    <div class="status">
      <div class="lamp" id="lamp"><span class="d"></span><span id="lampTxt">looking for a session…</span></div>
      <span class="note" id="actNote"></span>
    </div>
    <div class="panel" style="margin-bottom:16px">
      <h2>Hooks — desktop notifications</h2>
      <div class="hrow">
        <label class="hk"><input type="checkbox" data-hook="errors"><span>Error alerts<em>build / tool failures</em></span></label>
        <label class="hk"><input type="checkbox" data-hook="done"><span>Task finished<em>working → idle</em></span></label>
        <label class="hk"><input type="checkbox" data-hook="commits"><span>New commits<em>each commit lands</em></span></label>
        <label class="hk"><input type="checkbox" data-hook="subagents"><span>Subagent launches<em>can be chatty</em></span></label>
        <button id="hkTest">Send test notification</button>
      </div>
      <p class="hnote" id="hkNote"></p>
    </div>
    <div class="panel" style="margin-bottom:16px" id="planPanel" hidden>
      <h2>Plan — Claude Code's checklist</h2>
      <div id="plan"></div>
    </div>
    <div class="panel" style="margin-bottom:16px" id="agentsPanel" hidden>
      <h2>Subagents — who's doing what</h2>
      <div id="agents"></div>
    </div>
    <div class="panel">
      <h2>Live activity — every tool call, edit &amp; subagent</h2>
      <div class="filters">
        <button class="fc on" data-f="all">All</button>
        <button class="fc" data-f="edits">Edits</button>
        <button class="fc" data-f="bash">Commands</button>
        <button class="fc" data-f="sub">Subagents</button>
        <button class="fc" data-f="say">Conversation</button>
        <button class="fc" data-f="err">Errors</button>
      </div>
      <div class="afeed" id="afeed"></div>
    </div>
  </section>

  <!-- TAB : TERMINAL -->
  <section id="tab-term" hidden>
    <div class="termbar">
      <span class="tlamp" id="tLamp"></span>
      <span id="tState">terminal</span>
      <div class="tquick">
        <button data-cmd="claude" title="Start Claude Code here">claude</button>
        <button data-cmd="xcodebuild build" title="Build the Xcode project">Build</button>
        <button data-cmd="xcodebuild test" title="Run tests">Test</button>
        <button data-cmd="git status" title="git status">git status</button>
        <button id="tClear" title="Clear screen">Clear</button>
        <button id="tRestart" title="Kill &amp; restart the shell">Restart</button>
      </div>
    </div>
    <div id="term"></div>
    <div class="sayrow">
      <input id="sayIn" placeholder="Message Claude Code while it works — type here and press Enter (sends the line + Enter to the terminal)">
      <button id="sayBtn">Send ⏎</button>
    </div>
  </section>
</div>

<!-- TAB 3 : GALAXY (fullscreen layer) -->
<section id="tab-galaxy" hidden>
  <canvas id="gc"></canvas>
  <div class="goverlay">
    <div>
      <p class="gt" id="gTitle">Home Perfect</p>
      <p class="gs" id="gSub">Your app, drawn live from the codebase. Planets drift slowly — click one to zoom into its own galaxy.</p>
    </div>
    <div class="gbtns">
      <button id="gBack" hidden>← Back</button>
      <div id="tlTop" hidden>
        <button id="tlPlay" title="Play / pause">⏸</button>
        <input type="range" id="tlRange" min="0" max="0" value="0" step="1">
        <span id="tlCount"></span>
      </div>
      <button id="gTl">Time-lapse</button>
      <button id="gPause">Pause orbit</button>
      <button id="gFull">Fullscreen</button>
    </div>
  </div>
  <div class="tlcallout" id="tlCallout"></div>
  <div id="gtip"><p class="n"><i id="gtc"></i><span id="gtn"></span></p><p class="c" id="gtd"></p></div>
</section>

<nav class="tabbar" role="tablist">
  <button id="tb-term" class="on" role="tab" aria-selected="true"><span class="ico">▟</span>Terminal</button>
  <button id="tb-cockpit" role="tab" aria-selected="false"><span class="ico">◉</span>Cockpit</button>
  <button id="tb-claude" role="tab" aria-selected="false"><span class="ico">⌘</span>Claude Code</button>
  <button id="tb-galaxy" role="tab" aria-selected="false"><span class="ico">✦</span>Galaxy</button>
</nav>

<script>
// ------------------------------------------------------------- shared state
let S = null;                 // latest /state payload
let seenCommits = new Set(), seenEv = new Set(), first = true;
let tab = 'term';
const $ = id => document.getElementById(id);
const esc = x => String(x).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function tick(){
  try{
    const r = await fetch('/state',{cache:'no-store'});
    S = await r.json();
    render();
  }catch(e){ /* server stopped; keep last view */ }
}
tick(); setInterval(tick, 2000);

// ------------------------------------------------------------------- tabs
const tabs = ['term','cockpit','claude','galaxy'];
tabs.forEach(t=>{
  $('tb-'+t).addEventListener('click', ()=>setTab(t));
});
function setTab(t){
  tab = t;
  tabs.forEach(x=>{
    $('tb-'+x).classList.toggle('on', x===t);
    $('tb-'+x).setAttribute('aria-selected', x===t);
  });
  $('tab-term').hidden    = t!=='term';
  $('tab-cockpit').hidden = t!=='cockpit';
  $('tab-claude').hidden  = t!=='claude';
  $('tab-galaxy').hidden  = t!=='galaxy';
  $('chrome').style.display = (t==='galaxy') ? 'none' : '';
  if(t==='galaxy'){ gResize(); }
  if(t==='term'){ initTerm(); setTimeout(fitTerm, 40); }
}

// ------------------------------------------------------------- terminal
let term=null, fitAddon=null, termES=null, termReady=false;
function initTerm(){
  if(termReady) return;
  if(typeof Terminal==='undefined' || typeof FitAddon==='undefined'){
    // xterm.js still loading from CDN — retry shortly
    $('tState').textContent='loading terminal…';
    setTimeout(initTerm, 200); return;
  }
  termReady=true;
  term = new Terminal({
    cursorBlink:true, fontFamily:'"JetBrains Mono", monospace', fontSize:13,
    scrollback:5000, allowProposedApi:true,
    theme:{background:'#04050b', foreground:'#eef1ff', cursor:'#ffb020',
           selectionBackground:'rgba(255,176,32,.3)'}
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($('term'));
  fitTerm();
  term.onData(d=> sendBytes(d));
  connectTermStream();
  window.addEventListener('resize', ()=>{ if(tab==='term') fitTerm(); });
}
function utf8b64(str){ return btoa(unescape(encodeURIComponent(str))); }
function sendBytes(str){
  fetch('/term/input',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({d:utf8b64(str)})}).catch(()=>{});
}
function fitTerm(){
  if(!fitAddon) return;
  try{
    fitAddon.fit();
    fetch('/term/resize',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cols:term.cols, rows:term.rows})}).catch(()=>{});
  }catch(e){}
}
function connectTermStream(){
  if(termES) termES.close();
  termES = new EventSource('/term/stream');
  termES.onmessage = e=>{
    const bytes = Uint8Array.from(atob(e.data), c=>c.charCodeAt(0));
    term.write(bytes);
    $('tLamp').className='tlamp on'; $('tState').textContent='live shell — running in your repo';
  };
  termES.onerror = ()=>{ $('tLamp').className='tlamp'; $('tState').textContent='reconnecting…'; };
}
// send a whole line (message box + quick buttons). Claude Code's TUI treats a
// fast burst of characters as a *paste*, so a trailing "\r" in the SAME write
// becomes a literal newline in the composer instead of submitting — the message
// gets typed but never sent. Directly-typed keys work because xterm sends each
// one separately, so Enter arrives as its own keystroke. Mirror that here: send
// the text first, then a separate Enter a beat later so it registers as a real
// submit. 80ms is comfortably outside any paste-detection window.
function sendLine(text){
  sendBytes(text);
  setTimeout(()=> sendBytes('\r'), 80);
}
function wireTerm(){
  const say=$('sayIn'), sayBtn=$('sayBtn');
  if(!say) return;
  const send=()=>{ const v=say.value; if(v){ sendLine(v); say.value=''; } if(term) term.focus(); };
  sayBtn.addEventListener('click', send);
  say.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); send(); } });
  document.querySelectorAll('.tquick [data-cmd]').forEach(b=>
    b.addEventListener('click', ()=>{ sendLine(b.dataset.cmd); if(term) term.focus(); }));
  $('tClear').addEventListener('click', ()=>{ if(term) term.clear(); });
  $('tRestart').addEventListener('click', ()=>{
    fetch('/term/restart',{method:'POST'}).then(()=>{ if(term){ term.reset(); } });
  });
}
wireTerm();
// terminal is the default tab — bring it up now
initTerm(); setTimeout(fitTerm, 60);

// ------------------------------------------------------------- render all
function render(){
  if(!S) return;
  $('proj').textContent = S.project;
  gChrome();
  $('clock').textContent = S.time;
  $('branch').textContent = S.git ? S.branch : 'no git yet';

  renderCockpit();
  renderClaude();
  gData(S.tree || null);
  $('phone').textContent = S.lanUrl ? ('phone: ' + S.lanUrl.replace('http://','')) : '';
  first = false;
}

// ------------------------------------------------------------- tab 1
function renderCockpit(){
  $('sFiles').textContent = S.fileCount;
  $('sCommits').textContent = S.git ? S.commits.length : '0';
  $('sChanged').textContent = S.git ? S.changed.length : '—';

  const feed = $('feedEl');
  if(!S.git){
    feed.innerHTML = '<div class="empty">No git repo here yet. Once you <code>git init</code> and commit, progression shows up live.</div>';
  } else if(!S.commits.length){
    feed.innerHTML = '<div class="empty">No commits yet — make your first commit and watch it land here.</div>';
  } else {
    feed.innerHTML = '';
    S.commits.forEach(c=>{
      const isNew = !first && !seenCommits.has(c.hash);
      seenCommits.add(c.hash);
      const row = document.createElement('div');
      row.className = 'commit' + (isNew ? ' new' : '');
      row.innerHTML = `<span class="h">${esc(c.hash)}</span><span class="m">${esc(c.msg)}</span><span class="w">${esc(c.when)}</span>`;
      feed.appendChild(row);
    });
  }

  const ch = $('changedEl');
  if(!S.git){ ch.innerHTML = '<div class="empty">—</div>'; }
  else if(!S.changed.length){ ch.innerHTML = '<div class="empty">Working tree clean. Nothing being edited right now.</div>'; }
  else{
    ch.innerHTML = '';
    S.changed.forEach(f=>{
      const cls = f.code === '??' ? 'A' : (f.code[0] || 'M');
      const el = document.createElement('div');
      el.className = 'chip';
      el.innerHTML = `<span class="code ${esc(cls)}">${esc(f.code)}</span><span class="path">${esc(f.path)}</span>`;
      ch.appendChild(el);
    });
  }

  const bars = $('barsEl');
  const max = S.folders.length ? S.folders[0][1] : 1;
  bars.innerHTML = '';
  S.folders.forEach(([name,count])=>{
    const el = document.createElement('div');
    el.className = 'bar';
    el.innerHTML = `<span class="fn">${esc(name)}</span>`+
      `<span class="track"><span class="fill" style="width:${Math.max(6,count/max*100)}%"></span></span>`+
      `<span class="c">${count}</span>`;
    bars.appendChild(el);
  });
}

$('url').addEventListener('change', e=>{
  const u = e.target.value.trim();
  if(u){ $('prevHost').innerHTML = `<iframe src="${esc(u)}" title="live app preview"></iframe>`; }
});

// ------------------------------------------------------------- tab 2
function badgeFor(e){
  if(e.kind==='say') return 'say';
  if(e.kind==='you') return 'you';
  if(e.kind==='err') return 'err';
  if(e.name==='Bash') return 'bash';
  if(e.name==='Task') return 'task';
  return 'tool';
}
let filt = 'all';
document.querySelectorAll('.fc').forEach(b=>b.addEventListener('click',()=>{
  filt = b.dataset.f;
  document.querySelectorAll('.fc').forEach(x=>x.classList.toggle('on', x===b));
  if(S) renderClaude();
}));
function passes(e){
  if(filt==='all') return true;
  if(filt==='edits') return ['Edit','Write','MultiEdit','NotebookEdit'].includes(e.name);
  if(filt==='bash') return e.name==='Bash';
  if(filt==='sub') return e.sub || e.name==='Task';
  if(filt==='say') return e.kind==='say' || e.kind==='you';
  if(filt==='err') return e.kind==='err';
  return true;
}
function renderClaude(){
  const a = S.activity || {};
  const lamp = $('lamp');
  if(!a.found){
    lamp.className = 'lamp';
    $('lampTxt').textContent = 'no session found';
    $('actNote').textContent = 'Start Claude Code inside this folder once — its live log appears here automatically.';
  } else if(a.active){
    lamp.className = 'lamp live';
    $('lampTxt').textContent = 'Claude Code is working';
    $('actNote').textContent = a.now ? a.now : ('last activity ' + a.ageSec + 's ago');
  } else {
    lamp.className = 'lamp';
    $('lampTxt').textContent = 'idle';
    $('actNote').textContent = a.ageSec!=null ? ('last activity ' + fmtAge(a.ageSec) + ' ago') : '';
  }

  // plan panel (Claude Code's TodoWrite checklist)
  const plan = a.plan || [];
  $('planPanel').hidden = !plan.length;
  const pl = $('plan'); pl.innerHTML='';
  plan.forEach(it=>{
    const st = it.status==='completed' ? 'done' : (it.status==='in_progress' ? 'now' : 'todo');
    const row = document.createElement('div');
    row.className = 'pitem '+st;
    row.innerHTML = '<span class="pi">'+(st==='done'?'✓':'')+'</span><span class="pt">'+esc(it.content||'')+'</span>';
    pl.appendChild(row);
  });

  // subagents panel
  const agents = a.agents || [];
  $('agentsPanel').hidden = !agents.length;
  const ag = $('agents');
  ag.innerHTML = '';
  agents.slice().reverse().forEach(x=>{
    const row = document.createElement('div');
    row.className = 'agent' + (x.done ? '' : ' run');
    const meta = [x.actions + ' action' + (x.actions===1?'':'s')];
    if(x.secs!=null) meta.push('ran ' + fmtAge(x.secs));
    if(x.last) meta.push('last: ' + x.last);
    row.innerHTML = `<span class="ad"></span><div style="min-width:0;flex:1">`+
      `<div class="an">${esc(x.type)}<em>${esc(x.desc)}</em></div>`+
      `<div class="am">${esc(meta.join(' · '))}</div></div>`+
      `<span class="st ${x.done?'done':'run'}">${x.done?'done':'running'}</span>`;
    ag.appendChild(row);
  });

  const feed = $('afeed');
  const evs = (a.events || []).filter(passes);
  if(!a.found){
    feed.innerHTML = '<div class="empty">This tab reads Claude Code\u2019s own session transcript (in ~/.claude/projects) and shows every tool call, file edit, bash command and subagent launch in real time.</div>';
    return;
  }
  if(!evs.length){
    feed.innerHTML = '<div class="empty">' + (filt==='all'
      ? 'Session found, no recent events yet. Give Claude Code a task and watch this feed light up.'
      : 'Nothing in this filter yet.') + '</div>';
    return;
  }
  feed.innerHTML = '';
  evs.forEach(e=>{
    const key = (e.ts||'') + '|' + e.name + '|' + e.detail;
    const isNew = !first && !seenEv.has(key);
    seenEv.add(key);
    const row = document.createElement('div');
    row.className = 'ev ' + e.kind + (isNew ? ' new' : '');
    const t = e.ts ? new Date(e.ts).toLocaleTimeString([], {hour12:false}) : '';
    const subBadge = (e.sub && e.kind!=='say')
      ? '<span class="badge sub">' + esc(e.agent || 'subagent') + '</span>' : '';
    row.innerHTML = `<span class="t">${esc(t)}</span>`+
      `<span class="badge ${badgeFor(e)}">${esc(e.name)}</span>${subBadge}`+
      `<span class="d2">${esc(e.detail)}</span>`;
    feed.appendChild(row);
  });
}
function fmtAge(s){
  if(s<90) return s+'s';
  if(s<5400) return Math.round(s/60)+'m';
  return Math.round(s/3600)+'h';
}

// hooks panel
async function loadHooks(){
  try{
    const r = await fetch('/hooks',{cache:'no-store'});
    const j = await r.json();
    document.querySelectorAll('.hk input').forEach(cb=>{
      cb.checked = !!(j.hooks && j.hooks[cb.dataset.hook]);
    });
    $('hkNote').textContent = j.supported
      ? 'Notifications land in macOS Notification Center — even when this window is hidden. If the test does nothing, allow notifications from "Script Editor" in System Settings → Notifications.'
      : 'Desktop notifications need macOS — toggles are saved, but nothing will pop up on this system.';
  }catch(e){}
}
loadHooks();
document.querySelectorAll('.hk input').forEach(cb=>{
  cb.addEventListener('change', ()=>{
    const body = {}; body[cb.dataset.hook] = cb.checked;
    fetch('/hooks', {method:'POST', body: JSON.stringify(body)});
  });
});
$('hkTest').addEventListener('click', ()=>{
  fetch('/hooks/test');
  $('hkTest').textContent = 'Sent ✓';
  setTimeout(()=>{ $('hkTest').textContent = 'Send test notification'; }, 1600);
});

// ------------------------------------------------------------- tab 3: galaxy
const gcv = $('gc'), gctx = gcv.getContext('2d');
const PAL = ['#ffb020','#37d6c0','#8a7bff','#4ade80','#ff8a97','#f472b6','#38bdf8','#facc15'];
let GW=0, GH=0, DPR=Math.min(window.devicePixelRatio||1,2);
let gStars = [], gT=0, gPlaying=true, gLast=performance.now();
let gMouse={x:-999,y:-999}, gHot=null, gNodes=[];
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
if(reduce) gPlaying=false;

let TABH = 70;
function gResize(){
  GW = window.innerWidth; GH = window.innerHeight;
  const bar = document.querySelector('nav.tabbar');
  TABH = document.fullscreenElement ? 0 : ((bar && bar.offsetHeight) || 70);
  gcv.width=GW*DPR; gcv.height=GH*DPR;
  gcv.style.width=GW+'px'; gcv.style.height=GH+'px';
  gctx.setTransform(DPR,0,0,DPR,0,0);
  gStars=[];
  const n=Math.round((GW*GH)/9000);
  for(let i=0;i<n;i++) gStars.push({x:Math.random()*GW,y:Math.random()*GH,
    r:Math.random()*1.3+.2,base:Math.random()*.5+.15,tw:Math.random()*6.28,sp:Math.random()*.9+.2});
}
window.addEventListener('resize', ()=>{ if(tab==='galaxy') gResize(); });
document.addEventListener('fullscreenchange', ()=>{ if(tab==='galaxy') gResize(); });

let gTree=null;          // full project tree from /state
let gPath=[];            // [] = project root; e.g. ['Views','Components']
let gTr=null;            // zoom transition {dir, ph, name, focus, did}
let speedMul=1;          // eases toward ~0 while hovering so clicking is easy
let TL=null;             // time-lapse state {commits,i,playing,colors,disp,acc}

function gData(tree){
  gTree=tree||null;
  if(gPath.length && !nodeAt(gPath)){ gPath=[]; gTr=null; }
  gChrome();
}
function nodeAt(path){
  let n=gTree; if(!n) return null;
  for(const nm of path){
    n=(n.children||[]).find(c=>c.name===nm);
    if(!n) return null;
  }
  return n;
}
function colorOfPath(path){
  let n=gTree, col='#aab6ff';
  for(const nm of path){
    const kids=n?(n.children||[]):[];
    const i=kids.findIndex(c=>c.name===nm);
    if(i<0) return col;
    col=PAL[i%PAL.length]; n=kids[i];
  }
  return col;
}
function gRect(){
  const top = 88;                                  // title overlay zone
  const h = Math.max(220, GH - TABH - top - 18);   // usable height
  return {top, h};
}
function gCenter(){ const r=gRect(); return {x:GW*.5, y:r.top + r.h*.5}; }
function gF(){
  const r=gRect();
  // outermost orbit + planet + label is ~375 design units; budget 400 with margin
  return Math.max(.28, Math.min(GW*.5, r.h*.5)/400);
}

gcv.addEventListener('pointermove',e=>{gMouse.x=e.clientX;gMouse.y=e.clientY;});
gcv.addEventListener('pointerleave',()=>{gMouse.x=gMouse.y=-999;});
$('gPause').addEventListener('click',e=>{
  gPlaying=!gPlaying; e.target.textContent = gPlaying?'Pause orbit':'Resume orbit';
});
$('gFull').addEventListener('click',()=>{
  const el=$('tab-galaxy');
  if(document.fullscreenElement) document.exitFullscreen();
  else if(el.requestFullscreen) el.requestFullscreen();
});

function hexA(hex,a){
  hex=hex.replace('#','');
  const n=parseInt(hex,16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}
function glow(x,y,r,color,a){
  const g=gctx.createRadialGradient(x,y,0,x,y,r);
  g.addColorStop(0,hexA(color,a)); g.addColorStop(1,hexA(color,0));
  gctx.fillStyle=g; gctx.beginPath(); gctx.arc(x,y,r,0,7); gctx.fill();
}

function lerpPt(a,b,k){ return {x:a.x+(b.x-a.x)*k, y:a.y+(b.y-a.y)*k}; }
function camDraw(fn, focus, s, alpha){
  gctx.save();
  gctx.globalAlpha=Math.max(0,Math.min(1,alpha));
  gctx.translate(GW/2,GH/2); gctx.scale(s,s); gctx.translate(-focus.x,-focus.y);
  fn();
  gctx.restore();
}
function gChrome(){
  const proj=S?S.project:'Home Perfect';
  if(TL){
    $('gTitle').textContent=proj+' — time-lapse';
    $('gSub').textContent='Replaying the app being built, commit by commit. Drag the slider or press play.';
    $('gBack').hidden=true;
    return;
  }
  const node=nodeAt(gPath);
  if(gPath.length&&node){
    $('gTitle').textContent=node.name;
    $('gSub').textContent=[proj].concat(gPath).join(' / ')+' — '+(node.count||0)+' files · ringed planets are folders, click to go deeper';
  } else {
    $('gTitle').textContent=proj;
    $('gSub').textContent='Your app, drawn live from the codebase. Ringed planets are folders — click to zoom in, all the way down to single files.';
  }
  $('gBack').hidden=!gPath.length;
}

function layoutKids(kids){
  const C=gCenter(), F=gF(), n=kids.length||1;
  const two=n>6;
  return kids.map((k,i)=>{
    const inner=!two||i<6;
    const ring=two?(inner?185:310):245;
    const cnt=two?(inner?Math.min(n,6):Math.max(n-6,1)):n;
    const j=inner?i:i-6;
    const speed=(.012+(j%3)*.004)*(reduce?0:1)*(inner?1:-1);
    const a=j*(6.283/cnt)+(inner?-.5:.15)+gT*speed;
    const cap=k.dir?32:15;
    const pr=Math.min(cap,(k.dir?11:6)+Math.sqrt(k.count||1)*2.2)*F;
    return {k,i,x:C.x+Math.cos(a)*ring*F,y:C.y+Math.sin(a)*ring*F,pr};
  });
}

function drawSystem(interactive){
  const node=nodeAt(gPath); if(!node) return;
  const C=gCenter(), F=gF();
  const kids=node.children||[];
  const health=(gPath.length===0&&S&&S.activity&&S.activity.health)||{};
  gctx.lineWidth=1; gctx.strokeStyle='rgba(160,170,220,.08)';
  (kids.length>6?[185,310]:[245]).forEach(r=>{gctx.beginPath();gctx.arc(C.x,C.y,r*F,0,7);gctx.stroke();});
  const pos=layoutKids(kids);
  pos.forEach(p=>{gctx.strokeStyle='rgba(160,170,220,.11)';gctx.beginPath();gctx.moveTo(C.x,C.y);gctx.lineTo(p.x,p.y);gctx.stroke();});
  pos.forEach(p=>{
    const col=PAL[p.i%PAL.length];
    const st=health[p.k.name];
    if(st==='err') glow(p.x,p.y,p.pr*3.2,'#ff5a6e',.5);
    glow(p.x,p.y,p.pr*2.4,col,.42);
    if(p.k.dir){
      const g=gctx.createRadialGradient(p.x-3,p.y-3,1,p.x,p.y,p.pr);
      g.addColorStop(0,'#ffffff');g.addColorStop(.28,col);g.addColorStop(1,hexA(col,.5));
      gctx.fillStyle=g;gctx.beginPath();gctx.arc(p.x,p.y,p.pr,0,7);gctx.fill();
      gctx.strokeStyle='rgba(255,255,255,.6)';gctx.lineWidth=1.4;
      gctx.beginPath();gctx.ellipse(p.x,p.y,p.pr*1.65,p.pr*.52,-.5,0,7);gctx.stroke();
    } else {
      gctx.fillStyle=col;gctx.beginPath();gctx.arc(p.x,p.y,p.pr,0,7);gctx.fill();
      gctx.fillStyle='rgba(255,255,255,.9)';gctx.beginPath();gctx.arc(p.x,p.y,p.pr*.38,0,7);gctx.fill();
    }
    if(st==='err'){gctx.strokeStyle='rgba(255,90,110,.95)';gctx.lineWidth=2;gctx.beginPath();gctx.arc(p.x,p.y,p.pr+5*F,0,7);gctx.stroke();}
    else if(st==='ok'){gctx.strokeStyle='rgba(74,222,128,.55)';gctx.lineWidth=1.4;gctx.beginPath();gctx.arc(p.x,p.y,p.pr+5*F,0,7);gctx.stroke();}
    const label=p.k.name.length>18?p.k.name.slice(0,17)+'…':p.k.name;
    gctx.font=p.k.dir?`600 ${13*Math.max(1,F)}px "Space Grotesk", sans-serif`:`500 ${11.5*Math.max(1,F)}px "JetBrains Mono", monospace`;
    gctx.fillStyle='rgba(235,238,255,.9)';gctx.textAlign='center';
    gctx.fillText(label,p.x,p.y+p.pr+15*F);
    if(p.k.dir){
      gctx.font=`500 ${10.5*Math.max(1,F)}px "Inter", sans-serif`;
      gctx.fillStyle='rgba(200,205,237,.55)';
      gctx.fillText((p.k.count||0)+' files',p.x,p.y+p.pr+29*F);
    }
    if(interactive){
      let info=p.k.dir?((p.k.count||0)+' files inside · click to zoom in'):'file';
      if(st==='err') info+=' · build failing here';
      else if(st==='ok') info+=' · build passing';
      gNodes.push({x:p.x,y:p.y,r:Math.max(p.pr+10*F,16*F),color:col,
        kind:p.k.dir?'dir':'file',name:p.k.name,info});
    }
  });
  const isRoot=gPath.length===0;
  const coreCol=isRoot?'#aab6ff':colorOfPath(gPath);
  const pr=(isRoot?20:Math.min(40,16+Math.sqrt(node.count||1)*2.4))*F;
  glow(C.x,C.y,pr*2.8,coreCol,.6);
  const cg=gctx.createRadialGradient(C.x-5,C.y-5,2,C.x,C.y,pr);
  cg.addColorStop(0,'#ffffff');
  cg.addColorStop(isRoot?.5:.3,isRoot?'#cdd6ff':coreCol);
  cg.addColorStop(1,hexA(isRoot?'#6f7bff':coreCol,.55));
  gctx.fillStyle=cg;gctx.beginPath();gctx.arc(C.x,C.y,pr,0,7);gctx.fill();
  gctx.font=`700 ${(isRoot?14:15)*Math.max(1,F)}px "Space Grotesk", sans-serif`;
  gctx.fillStyle='#fff';gctx.textAlign='center';
  gctx.fillText(node.name,C.x,C.y+pr+20*F);
  gctx.font=`500 ${11*Math.max(1,F)}px "Inter", sans-serif`;
  gctx.fillStyle='rgba(200,205,237,.6)';
  gctx.fillText((node.count||0)+' files'+((isRoot&&S&&S.git)?(' · '+S.commits.length+' commits'):''),C.x,C.y+pr+35*F);
  if(interactive) gNodes.push({x:C.x,y:C.y,r:pr+10*F,color:coreCol,kind:'core',
    name:node.name,info:(node.count||0)+' files'+(gPath.length?' · click empty space to go up':'')});
}

function childFocus(name){
  const node=nodeAt(gPath); if(!node) return gCenter();
  const p=layoutKids(node.children||[]).find(x=>x.k.name===name);
  return p?{x:p.x,y:p.y}:gCenter();
}
function zoomIn(name,focus){ if(gTr||TL) return; gTr={dir:1,ph:reduce?1:0,name,focus,did:false}; }
function zoomOut(){
  if(gTr||TL||!gPath.length) return;
  const name=gPath[gPath.length-1];
  const saved=gPath.slice();
  gPath=gPath.slice(0,-1);
  const f=childFocus(name);
  gPath=saved;
  gTr={dir:-1,ph:reduce?1:0,name,focus:f,did:false};
}

gcv.addEventListener('click', e=>{
  gMouse.x=e.clientX; gMouse.y=e.clientY;      // makes taps work on touch too
  if(gTr||TL) return;
  let hit=null;
  for(let i=gNodes.length-1;i>=0;i--){
    const n=gNodes[i];
    if((e.clientX-n.x)**2+(e.clientY-n.y)**2<=n.r*n.r){hit=n;break;}
  }
  if(hit&&hit.kind==='dir'){ zoomIn(hit.name,{x:hit.x,y:hit.y}); }
  else if(!hit&&gPath.length){ zoomOut(); }
});
$('gBack').addEventListener('click', zoomOut);
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'&&tab==='galaxy'){ if(TL) tlExit(); else zoomOut(); }
});

// ---- time-lapse ----------------------------------------------------------
$('gTl').addEventListener('click', async ()=>{
  if(TL){ tlExit(); return; }
  $('gTl').textContent='Loading…';
  try{
    const r=await fetch('/timelapse',{cache:'no-store'});
    const j=await r.json();
    const commits=j.commits||[];
    if(!commits.length){ $('gTl').textContent='Time-lapse'; return; }
    const colors={};
    commits.forEach(c=>(c.clusters||[]).forEach(cl=>{
      if(!(cl.name in colors)) colors[cl.name]=PAL[Object.keys(colors).length%PAL.length];
    }));
    TL={commits,i:0,playing:true,colors,disp:{},acc:0,
        callout:null,calloutUntil:0};
    gPath=[]; gTr=null;
    $('tlTop').hidden=false;
    $('tlRange').max=commits.length-1; $('tlRange').value=0;
    $('tlPlay').textContent='⏸'; $('gTl').textContent='Exit';
    tlLabel(); gChrome();
  }catch(err){ $('gTl').textContent='Time-lapse'; }
});
function tlExit(){
  TL=null; $('tlTop').hidden=true; $('gTl').textContent='Time-lapse';
  $('tlCallout').classList.remove('on'); gChrome();
}
$('tlPlay').addEventListener('click', ()=>{
  if(!TL) return;
  TL.playing=!TL.playing; $('tlPlay').textContent=TL.playing?'⏸':'▶';
});
$('tlRange').addEventListener('input', e=>{
  if(!TL) return;
  TL.playing=false; $('tlPlay').textContent='▶';
  const to=+e.target.value;
  TL.i=to; TL.acc=0; tlLabel();
});
function tlLabel(){
  const c=TL.commits[TL.i];
  $('tlCount').textContent=(TL.i+1)+'/'+TL.commits.length;
  // arm a callout for whatever folder this commit introduces (if any)
  const prev = TL.i>0 ? new Set((TL.commits[TL.i-1].clusters||[]).map(x=>x.name)) : new Set();
  const added = (c.clusters||[]).map(x=>x.name).filter(n=>!prev.has(n));
  TL.callout = {msg:c.msg, when:c.t?new Date(c.t*1000).toLocaleDateString():'',
                names:added};
  TL.calloutUntil = performance.now()/1000 + 2.6;
}

function drawTimelapse(dt){
  if(TL.playing){
    TL.acc+=dt;
    if(TL.acc>2.0){                       // ~2s per commit so each lands visibly
      TL.acc=0;
      if(TL.i<TL.commits.length-1){ TL.i++; $('tlRange').value=TL.i; tlLabel(); }
      else { TL.playing=false; $('tlPlay').textContent='▶'; }
    }
  }
  const snap=TL.commits[TL.i];
  const target={};
  (snap.clusters||[]).forEach(c=>target[c.name]=c.count);
  Object.keys(TL.disp).forEach(n=>{ if(!(n in target)) target[n]=0; });
  Object.keys(target).forEach(n=>{
    const cur=TL.disp[n]==null?0:TL.disp[n];
    const v=cur+(target[n]-cur)*Math.min(1,dt*2.4);   // gentler grow-in
    if(v<.4&&target[n]===0) delete TL.disp[n]; else TL.disp[n]=v;
  });
  const kids=Object.entries(TL.disp).map(([name,count])=>({name,count,dir:true}))
    .sort((a,b)=>b.count-a.count).slice(0,12);
  const C=gCenter(), F=gF();
  gctx.lineWidth=1; gctx.strokeStyle='rgba(160,170,220,.08)';
  (kids.length>6?[185,310]:[245]).forEach(r=>{gctx.beginPath();gctx.arc(C.x,C.y,r*F,0,7);gctx.stroke();});
  const pos=layoutKids(kids);
  pos.forEach(p=>{gctx.strokeStyle='rgba(160,170,220,.11)';gctx.beginPath();gctx.moveTo(C.x,C.y);gctx.lineTo(p.x,p.y);gctx.stroke();});
  const spots={};
  pos.forEach(p=>{
    const col=TL.colors[p.k.name]||PAL[p.i%PAL.length];
    glow(p.x,p.y,p.pr*2.4,col,.42);
    const g=gctx.createRadialGradient(p.x-3,p.y-3,1,p.x,p.y,p.pr);
    g.addColorStop(0,'#ffffff');g.addColorStop(.28,col);g.addColorStop(1,hexA(col,.5));
    gctx.fillStyle=g;gctx.beginPath();gctx.arc(p.x,p.y,p.pr,0,7);gctx.fill();
    gctx.font=`600 ${12.5*Math.max(1,F)}px "Space Grotesk", sans-serif`;
    gctx.fillStyle='rgba(235,238,255,.9)';gctx.textAlign='center';
    gctx.fillText(p.k.name,p.x,p.y+p.pr+15*F);
    spots[p.k.name]={x:p.x,y:p.y,pr:p.pr};
    gNodes.push({x:p.x,y:p.y,r:Math.max(p.pr+10*F,16*F),color:col,kind:'file',
      name:p.k.name,info:Math.round(p.k.count)+' file'+(Math.round(p.k.count)===1?'':'s')+' at this commit'});
  });
  glow(C.x,C.y,56*F,'#aab6ff',.6);
  const cg=gctx.createRadialGradient(C.x-6,C.y-6,2,C.x,C.y,22*F);
  cg.addColorStop(0,'#ffffff');cg.addColorStop(.5,'#cdd6ff');cg.addColorStop(1,'#6f7bff');
  gctx.fillStyle=cg;gctx.beginPath();gctx.arc(C.x,C.y,20*F,0,7);gctx.fill();
  gctx.font=`700 ${14*Math.max(1,F)}px "Space Grotesk", sans-serif`;
  gctx.fillStyle='#fff';gctx.textAlign='center';
  gctx.fillText(S?S.project:'',C.x,C.y+40*F);
  gctx.font=`500 ${11*Math.max(1,F)}px "Inter", sans-serif`;
  gctx.fillStyle='rgba(200,205,237,.6)';
  gctx.fillText((snap.total||0)+' files',C.x,C.y+55*F);

  drawCallout(spots, C, F);
}

function drawCallout(spots, C, F){
  const el=$('tlCallout');
  const co=TL.callout;
  const nowS=performance.now()/1000;
  if(!co || nowS>TL.calloutUntil){ el.classList.remove('on'); return; }
  // anchor to the first newly-added planet that's on screen; else the core
  let anchor=null;
  for(const n of (co.names||[])){ if(spots[n]){ anchor=spots[n]; break; } }
  const a = anchor || {x:C.x, y:C.y, pr:22*F};
  // draw a short dash from the planet outward
  const up = a.y > GH*0.5 ? -1 : 1;
  const x1=a.x, y1=a.y - up*(a.pr+6*F);
  const x2=a.x, y2=a.y - up*(a.pr+40*F);
  gctx.strokeStyle='rgba(255,255,255,.7)'; gctx.lineWidth=1.3;
  gctx.setLineDash([4,4]);
  gctx.beginPath(); gctx.moveTo(x1,y1); gctx.lineTo(x2,y2); gctx.stroke();
  gctx.setLineDash([]);
  gctx.fillStyle='rgba(255,255,255,.9)';
  gctx.beginPath(); gctx.arc(x2,y2,2.4,0,7); gctx.fill();
  // place the HTML label near the dash end
  const label = (co.names && co.names.length)
    ? ('+ ' + co.names[0] + (co.names.length>1 ? ' +'+(co.names.length-1)+' more' : ''))
    : co.msg;
  el.innerHTML = esc(label) + '<span class="cmsg">'+esc(co.msg)+'</span>';
  let lx=x2+8, ly=(up<0)? (y2-8) : (y2+8);
  el.style.left=Math.min(GW-240, Math.max(12,lx))+'px';
  el.style.top=Math.max(96, ly)+'px';
  el.classList.add('on');
}

function hitAndTip(){
  gHot=null;
  for(let i=gNodes.length-1;i>=0;i--){
    const n=gNodes[i];
    if((gMouse.x-n.x)**2+(gMouse.y-n.y)**2<=n.r*n.r){gHot=n;break;}
  }
  const tip=$('gtip');
  if(gHot){
    gctx.strokeStyle=hexA(gHot.color,.9); gctx.lineWidth=2;
    gctx.beginPath(); gctx.arc(gHot.x,gHot.y,gHot.r*.72,0,7); gctx.stroke();
    gcv.style.cursor = gHot.kind==='dir' ? 'pointer' : 'default';
    tip.classList.add('on');
    $('gtc').style.background=gHot.color;
    $('gtn').textContent=gHot.name;
    $('gtd').textContent=gHot.info;
    let x=gHot.x+20,y=gHot.y-8;
    if(x+260>GW-10)x=gHot.x-270;
    if(y+80>GH-10)y=GH-90; if(y<10)y=10;
    tip.style.left=x+'px'; tip.style.top=y+'px';
  } else {
    gcv.style.cursor='default';
    tip.classList.remove('on');
  }
}

function gFrame(now){
  requestAnimationFrame(gFrame);
  if(tab!=='galaxy') return;             // don't burn CPU when hidden
  const dt=Math.min((now-gLast)/1000,.05); gLast=now;
  const sT=now/1000;
  const tgt=(gHot||gTr)?.12:1;           // ease to near-freeze while hovering
  speedMul += (tgt-speedMul)*Math.min(1,dt*6);
  if(gPlaying && !gTr) gT += dt*speedMul;
  gctx.clearRect(0,0,GW,GH);

  for(const s of gStars){
    const tw=reduce?s.base:s.base+Math.sin(sT*s.sp+s.tw)*.22;
    gctx.fillStyle=`rgba(210,220,255,${Math.max(0,tw)})`;
    gctx.beginPath(); gctx.arc(s.x,s.y,s.r,0,7); gctx.fill();
  }

  gNodes=[];
  const C=gCenter();

  if(TL){ drawTimelapse(dt); hitAndTip(); return; }

  if(gTr){
    gTr.ph=Math.min(1,gTr.ph+dt*1.7);
    const ph=gTr.ph;
    if(gTr.dir===1){
      if(ph<.5){
        const k=ph*2;
        camDraw(()=>drawSystem(false), lerpPt(C,gTr.focus,k), 1+1.8*k, 1-k);
      } else {
        if(!gTr.did){ gPath.push(gTr.name); gTr.did=true; gChrome(); }
        const k=(ph-.5)*2;
        camDraw(()=>drawSystem(false), C, .6+.4*k, k);
      }
    } else {
      if(ph<.5){
        const k=ph*2;
        camDraw(()=>drawSystem(false), C, 1-.4*k, 1-k);
      } else {
        if(!gTr.did){ gPath.pop(); gTr.did=true; gChrome(); }
        const k=(ph-.5)*2;
        camDraw(()=>drawSystem(false), lerpPt(gTr.focus,C,k), 2.8-1.8*k, k);
      }
    }
    if(ph>=1){ gTr=null; }
    gHot=null; $('gtip').classList.remove('on'); gcv.style.cursor='default';
    return;
  }

  drawSystem(true);
  hitAndTip();
}
requestAnimationFrame(gFrame);
</script>
</body></html>"""


# --------------------------------------------------------------------------
# terminal — a real shell over a pty, streamed to the browser
# --------------------------------------------------------------------------
class Term:
    """Owns one pseudo-terminal running the user's shell. The browser opens
    it (xterm.js), so running `claude` inside is the genuine interactive
    session — typing while it works just writes to its stdin."""

    def __init__(self):
        self.fd = None
        self.pid = None
        self.alive = False
        self.scroll = bytearray()      # capped scrollback for late joiners
        self.subs = []                 # queues for connected SSE clients
        self.lock = threading.Lock()

    def start(self):
        if not PTY_OK:
            return
        with self.lock:
            if self.alive:
                return
            shell = os.environ.get("SHELL", "/bin/bash")
            pid, fd = pty.fork()
            if pid == 0:                # child → become the shell
                try:
                    os.chdir(ROOT)
                except Exception:
                    pass
                os.environ["TERM"] = "xterm-256color"
                os.environ["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
                try:
                    os.execvp(shell, [shell, "-l"])
                except Exception:
                    os._exit(1)
            self.pid, self.fd, self.alive = pid, fd, True
            self.scroll = bytearray()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while self.alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.4)
                if self.fd in r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    self._broadcast(data)
            except OSError:
                break
        self.alive = False
        self._broadcast(b"\r\n\x1b[90m[shell exited - press Restart]\x1b[0m\r\n")

    def _broadcast(self, data):
        with self.lock:
            self.scroll += data
            if len(self.scroll) > 240000:
                del self.scroll[:len(self.scroll) - 240000]
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            snap = bytes(self.scroll)
            self.subs.append(q)
        return q, snap

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def write(self, data):
        if self.alive and self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, cols, rows):
        if self.fd is not None and PTY_OK:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:
                pass

    def restart(self):
        with self.lock:
            pid = self.pid
            self.alive = False
            self.fd = self.pid = None
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(0.15)
        self.start()


TERM = Term()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- auth: localhost (the Mac) is trusted; everyone else needs token --
    def _is_local(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _token_ok(self):
        if self._is_local():
            return True
        if not TOKEN:
            return False
        ck = self.headers.get("Cookie", "")
        for part in ck.split(";"):
            if part.strip().startswith("ck=") and part.strip()[3:] == TOKEN:
                return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("k", [""])[0] == TOKEN:
            return True
        if self.headers.get("X-Token") == TOKEN:
            return True
        return False

    def _deny(self):
        body = (b"<body style='font-family:sans-serif;background:#05060d;"
                b"color:#eef1ff;padding:40px'><h3>Cockpit locked</h3>"
                b"<p>Open it using the exact link shown in your terminal "
                b"(it carries your access key).</p></body>")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if not self._token_ok():
            return self._deny()
        path = urllib.parse.urlparse(self.path).path

        if path == "/term/stream":
            return self._term_stream()
        if self.path.startswith("/state"):
            return self._json(build_state())
        if self.path.startswith("/timelapse"):
            return self._json(build_timelapse())
        if self.path.startswith("/hooks/test"):
            notify("Cockpit hooks are live",
                   "This is what a hook notification looks like.", "Glass")
            return self._json({"ok": True, "supported": NOTIFY_OK})
        if self.path.startswith("/hooks"):
            with HOOKS_LOCK:
                return self._json({"hooks": dict(HOOKS),
                                   "supported": NOTIFY_OK})

        # the page
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if TOKEN and q.get("k", [""])[0] == TOKEN:
            self.send_header("Set-Cookie",
                             f"ck={TOKEN}; Path=/; SameSite=Lax; Max-Age=604800")
        self.end_headers()
        try:
            self.wfile.write(PAGE.encode("utf-8"))
        except OSError:
            pass

    def _term_stream(self):
        if not PTY_OK:
            self.send_response(501)
            self.end_headers()
            return
        TERM.start()
        q, snap = TERM.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            if snap:
                self._sse(snap)
            while True:
                try:
                    self._sse(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            TERM.unsubscribe(q)

    def _sse(self, data):
        self.wfile.write(b"data: " + base64.b64encode(data) + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        if not self._token_ok():
            return self._deny()
        path = urllib.parse.urlparse(self.path).path

        if path == "/term/input":
            d = self._read_body().get("d", "")
            try:
                TERM.write(base64.b64decode(d))
            except Exception:
                pass
            return self._json({"ok": True})
        if path == "/term/resize":
            b = self._read_body()
            TERM.resize(int(b.get("cols", 80)), int(b.get("rows", 24)))
            return self._json({"ok": True})
        if path == "/term/restart":
            TERM.restart()
            return self._json({"ok": True})

        if self.path.startswith("/hooks"):
            data = self._read_body()
            with HOOKS_LOCK:
                for k in HOOKS:
                    if k in data:
                        HOOKS[k] = bool(data[k])
                return self._json({"hooks": dict(HOOKS),
                                   "supported": NOTIFY_OK})
        self.send_response(404)
        self.end_headers()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global LAN_URL, TOKEN
    TOKEN = secrets.token_urlsafe(12)
    ip = lan_ip()
    url = f"http://127.0.0.1:{PORT}"
    LAN_URL = f"http://{ip}:{PORT}/?k={TOKEN}" if ip else None
    print("\n  Home Perfect — Dev Cockpit")
    print("  --------------------------")
    print(f"  Watching:    {ROOT}")
    sess = claude_sessions_dir()
    print(f"  Claude Code: {'session log found' if sess else 'no session log yet (start Claude Code here once)'}")
    hk = "desktop notifications ON" if NOTIFY_OK else "notifications unavailable (not macOS)"
    print(f"  Hooks:       {hk} — toggle in the Claude Code tab")
    print(f"  Terminal:    {'live shell ready' if PTY_OK else 'unavailable on this OS'}")
    print(f"  Open (Mac):  {url}   (this machine, no key needed)")
    if LAN_URL:
        print(f"  Phone:       {LAN_URL}")
        print("               ^ open this exact link on your phone (same Wi-Fi).")
        print("               Only devices with this key can connect — others are refused.")
        try:
            import qrcode
            q = qrcode.QRCode(border=1)
            q.add_data(LAN_URL)
            q.make(fit=True)
            q.print_ascii(invert=True)
        except ImportError:
            print("               tip: run `pip3 install qrcode` and restart for a scannable QR here")
        except Exception:
            pass
    print("  SECURITY:    the Terminal tab is a real shell on this Mac. Keep the")
    print("               phone link private — anyone with it can run commands here.")
    print("  Tabs:        Terminal · Cockpit · Claude Code · Galaxy")
    print("  Stop with Ctrl+C.\n")
    threading.Thread(target=hooks_watcher, daemon=True).start()
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        with Server(("0.0.0.0", PORT), Handler) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n  Cockpit stopped.\n")
    except OSError:
        print(f"  Port {PORT} is busy — is another cockpit already running?")
        print(f"  Close it, or open {url} in your browser.\n")


if __name__ == "__main__":
    main()
