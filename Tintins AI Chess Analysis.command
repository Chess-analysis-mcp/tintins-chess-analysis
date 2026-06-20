#!/usr/bin/env bash
#
# Tintin's AI Chess Analysis — double-click launcher (macOS / Linux).
#
# First run: installs everything (uv + Stockfish + deps) via ./install.sh.
# Every run: starts the board and opens your most recent Lichess game in the browser.
#
# Closing the browser tab quits the app — the server stops and this window closes itself
# (macOS). You can also close this window or press Ctrl-C to quit.
# (First time on macOS: if double-click is blocked, right-click → Open, then "Open" again.)
set -euo pipefail

cd "$(dirname "$0")"

HOST="${CHESS_WEB_HOST:-127.0.0.1}"
PORT="${CHESS_WEB_PORT:-8765}"
URL="http://${HOST}:${PORT}"

# Close THIS Terminal window (macOS Terminal only; best-effort, never fails the script). We spawn a
# DETACHED helper (its own session, no controlling tty) that waits for this script's shell to exit,
# then closes the window matched by tty. Detaching matters: if we closed the window while this shell
# / osascript were still running, Terminal would pop a "terminate running processes?" prompt.
close_window() {
  [ "${TERM_PROGRAM:-}" = "Apple_Terminal" ] || return 0
  local win_tty py
  win_tty=$(tty 2>/dev/null) || return 0
  [ -n "$win_tty" ] || return 0
  py="$(command -v python3 2>/dev/null || true)"
  [ -n "$py" ] || { [ -x ".venv/bin/python" ] && py="$(pwd)/.venv/bin/python"; }
  [ -n "$py" ] || return 0
  nohup "$py" - "$win_tty" >/dev/null 2>&1 <<'PY' &
import os, sys, time, subprocess
# Daemonize: fork (so we're not a process-group leader) then setsid to drop the controlling tty.
if os.fork() > 0:
    os._exit(0)
os.setsid()
win_tty = sys.argv[1]
time.sleep(1.5)  # let the launcher's shell fully exit so the window has no running processes
script = (
    'tell application "Terminal"\n'
    '  set toClose to {}\n'
    '  repeat with w in windows\n'
    '    repeat with t in tabs of w\n'
    '      if tty of t is "%s" then set end of toClose to id of w\n'
    '    end repeat\n'
    '  end repeat\n'
    '  repeat with wid in toClose\n'
    '    try\n'
    '      close (every window whose id is wid) saving no\n'
    '    end try\n'
    '  end repeat\n'
    'end tell'
) % win_tty
subprocess.run(["osascript", "-e", script])
PY
  disown 2>/dev/null || true
}

# Make a uv installed in the usual spots visible without a fresh login shell.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Already running? Just open the browser and stop — don't start a second server.
if curl -fsS "${URL}/api/app-config" >/dev/null 2>&1; then
  echo "Tintin's AI Chess Analysis is already running — opening ${URL}"
  open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || true
  close_window
  exit 0
fi

# First-run install: no uv, or the project env hasn't been built yet.
if ! command -v uv >/dev/null 2>&1 || [ ! -d ".venv" ]; then
  echo "First-time setup — installing Tintin's AI Chess Analysis (this happens only once)…"
  ./install.sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "Starting Tintin's AI Chess Analysis… close the browser tab (or this window) to quit."
# Run in the foreground (not exec) so we can close this window once the server exits — which the
# server does automatically a few seconds after the browser tab is closed (app-liveness watchdog).
CHESS_APP_MODE=1 uv run python scripts/run_web.py --serve || true
close_window
