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

# Set when we re-exec ourselves after applying a git update (see the apply block below). On a relaunch
# we skip the splash (already open) and the update step (already done), and just start the board.
RELAUNCHED=""
[ "${1:-}" = "--updated" ] && RELAUNCHED=1

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

# Kill whatever is listening on our port (an old app instance, or a Claude Code MCP-hosted board that
# squats the same port and never exits on tab-close). Used in a dev checkout so a relaunch always
# runs the CURRENT code instead of re-attaching to a stale instance.
free_port() {
  command -v lsof >/dev/null 2>&1 || return 0
  local pids
  pids="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
  [ -n "$pids" ] || return 0
  kill $pids 2>/dev/null || true
  for _ in 1 2 3 4 5 6; do
    lsof -ti "tcp:${PORT}" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
  kill -9 $(lsof -ti "tcp:${PORT}" 2>/dev/null) 2>/dev/null || true
  sleep 0.5
}

# Make a uv installed in the usual spots visible without a fresh login shell.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Already running?
if curl -fsS "${URL}/api/app-config" >/dev/null 2>&1; then
  if [ -d ".git" ]; then
    # Dev checkout: don't re-attach to the old instance — stop it and start fresh so this launch runs
    # the latest code (and a clean Snowie chat). End-user installs (no .git) keep the reuse shortcut.
    echo "Restarting Tintin's AI Chess Analysis with the latest code…"
    free_port
  else
    echo "Tintin's AI Chess Analysis is already running — opening ${URL}"
    open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || true
    close_window
    exit 0
  fi
fi

# Open a loading splash in the browser RIGHT NOW so first-time users see immediate progress while
# the (slow, one-time) install + engine download run — instead of a blank screen that looks frozen.
# The splash polls the board URL and swaps itself for the real app the moment the server is up; so we
# set CHESS_WEB_OPEN=0 below to avoid the server opening a second, duplicate tab. (Spaces in the path
# → %20 for a valid file:// URL; the #host:port lets the splash know where the board will be.)
# Copy the splash into a writable scratch dir so the installer can drop a sibling progress.js beside
# it — the splash re-loads that file (as a <script>) to advance a real progress bar during the slow
# first-run install. A failed copy just opens the in-place splash (spinner only, no bar; no regression).
SPLASH_DIR="${TMPDIR:-/tmp}/tintin-chess-splash"
SPLASH="file://${PWD// /%20}/frontend/loading.html#${HOST}:${PORT}"
if mkdir -p "$SPLASH_DIR" 2>/dev/null && cp "frontend/loading.html" "$SPLASH_DIR/loading.html" 2>/dev/null; then
  : > "$SPLASH_DIR/progress.js" 2>/dev/null || true   # clear any stale progress from a prior run
  export CHESS_INSTALL_PROGRESS="$SPLASH_DIR/progress.js"
  SPLASH="file://${SPLASH_DIR// /%20}/loading.html#${HOST}:${PORT}"
fi
[ -n "$RELAUNCHED" ] || open "$SPLASH" 2>/dev/null || xdg-open "$SPLASH" 2>/dev/null || true

# Apply a staged update before starting. The in-app "Update now" button drops a `.update-requested`
# sentinel here; we apply it on the NEXT launch, then remove it. Best-effort — any failure just
# starts the existing code. git checkout → git pull; source-zip download → fetch+extract the latest
# release. (We only get here when no server is already running, so nothing has the venv files open.)
# Skipped on a relaunch (the update is already applied).
if [ -z "$RELAUNCHED" ] && [ -f ".update-requested" ]; then
  echo "Applying a staged update…"
  RELAUNCH=""
  if [ -d ".git" ] && command -v git >/dev/null 2>&1; then
    git stash --include-untracked -q 2>/dev/null || true   # keep a fast-forward possible (e.g. edited .mcp.json)
    git pull --ff-only -q 2>/dev/null || echo "  (couldn't fast-forward; keeping current version)"
    git stash pop -q 2>/dev/null || true
    uv sync >/dev/null 2>&1 || true
    RELAUNCH=1   # git pull may have rewritten THIS launcher — re-exec the fresh copy below
  elif command -v uv >/dev/null 2>&1; then
    # No git (or not a git checkout) → update by downloading the latest release instead. The zip
    # updater never touches the launcher scripts, so no re-exec is needed here.
    [ -d ".git" ] && echo "  Git isn't installed, so updating by download instead — install Git from https://git-scm.com/downloads for faster updates."
    if uv run python scripts/apply_update.py; then uv sync >/dev/null 2>&1 || true; else echo "  (update skipped — download the latest from the Releases page)"; fi
  else
    echo "  Couldn't update automatically (neither Git nor uv is available). Download the latest version from the Releases page."
  fi
  rm -f ".update-requested" 2>/dev/null || true
  # Replace this (possibly now-stale) process with a fresh run of the updated script. The splash is
  # already open and the sentinel is gone, so the relaunch starts cleanly and won't re-update.
  [ -n "$RELAUNCH" ] && exec "$0" --updated
fi

# First-run install: no uv, or the project env hasn't been built yet.
if ! command -v uv >/dev/null 2>&1 || [ ! -d ".venv" ]; then
  echo "First-time setup — installing Tintin's AI Chess Analysis (this happens only once)…"
  # Non-interactive: the user is watching the browser splash, not this window, so the installer must
  # NOT stop at its username prompt (that would hang first-run — the server would never start). The
  # username is collected on the app's first-run screen instead.
  CHESS_NONINTERACTIVE=1 ./install.sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "Starting Tintin's AI Chess Analysis… close the browser tab (or this window) to quit."
# Run in the foreground (not exec) so we can close this window once the server exits — which the
# server does automatically a few seconds after the browser tab is closed (app-liveness watchdog).
# CHESS_WEB_OPEN=0: the splash tab above redirects to the board itself, so don't open another.
CHESS_APP_MODE=1 CHESS_WEB_OPEN=0 uv run python scripts/run_web.py --serve || true
close_window
