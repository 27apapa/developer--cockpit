# Dev Cockpit

A single-file, zero-dependency local dashboard that sits next to
[Claude Code](https://claude.com/claude-code) while you build. Point it at any
repo and it shows you — live, in the browser, and on your phone — what your
project and your AI agent are doing right now.

![tabs](https://img.shields.io/badge/tabs-Terminal%20·%20Cockpit%20·%20Claude%20Code%20·%20Galaxy-ffb020)

## The four tabs

| Tab | What it shows |
|---|---|
| **Terminal** | A *real* shell running in your repo, streamed to the browser via a pty + xterm.js. Run `claude` inside it and it's the genuine interactive session — use the message box to send it a prompt while it works, even from your phone. |
| **Cockpit** | Commits streaming in as they land, files being edited right now, project structure, and an optional embedded live preview of your running app. |
| **Claude Code** | Exactly what Claude Code is doing, read live from its own session transcripts in `~/.claude/projects`: every tool call, file edit, bash command, subagent launch, its current plan, and desktop-notification hooks (errors, task finished, new commits, subagent launches). |
| **Galaxy** | Your codebase drawn as a galaxy. Folders are ringed planets — click to zoom in, level after level, down to single files. Includes a git time-lapse (watch the app grow commit by commit) and build health (a failing build makes the affected planet glow red). |

## Run it

Requires only Python 3 — no installs, no pip packages. (The Terminal tab loads
xterm.js from a CDN, so it needs internet the first time.)

```sh
cd your-project/
python3 /path/to/dev_cockpit.py
```

Your browser opens `http://127.0.0.1:4321`. Stop with `Ctrl+C`.

Keep Claude Code running in its own terminal tab (or start it from the
cockpit's Terminal tab) — the Claude Code tab picks up its session log
automatically.

## Phone access

The cockpit is also served on your local network. The terminal prints a
`Phone:` URL carrying a one-time access key — open that exact link on any
device on the same Wi-Fi. Devices without the key are refused.
`pip3 install qrcode` to get a scannable QR code in the terminal.

## Security model

- Each launch generates a fresh access token. Non-localhost devices must
  present it (via the `Phone:` link, which sets a cookie).
- Every request must carry a matching `Host`, and any `Origin` must be
  same-origin — so web pages you happen to visit can't reach the cockpit
  (or its shell) through your browser, and DNS-rebinding is blocked.
- The Terminal tab is a **real shell on your machine**. Keep the phone link
  private; anyone holding it can run commands.

## Desktop notifications (macOS)

The cockpit pings you through Notification Center even when the window is
hidden: build/tool errors, Claude Code going working → idle, new commits, and
(optionally) subagent launches. Toggle them in the Claude Code tab. Uses the
built-in `osascript` — if the test button does nothing, allow notifications
from "Script Editor" in System Settings → Notifications.

## How the Claude Code tab works

Claude Code writes a live JSONL transcript of each session under
`~/.claude/projects/<encoded-project-path>/`. The cockpit tails the newest
transcripts, parses tool calls / results / subagent sidechains, and renders
them as a filterable feed — no hooks into Claude Code itself, so it can never
slow your session down.
