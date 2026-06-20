"""App-mode liveness: quit the server shortly after the app's browser tab is *really* closed.

Only active in **app mode** (CHESS_APP_MODE=1 — the double-click launcher), so the MCP-driven board
never self-exits when you close a tab.

The hard part is telling "tab closed" apart from "tab lost focus / went to the background", because
browsers heavily throttle background-tab timers (down to ~once a minute). So we use two signals:

  - An explicit **close beacon**: the page fires `navigator.sendBeacon('/api/closing')` on `pagehide`
    (tab close / navigation / refresh). After `CLOSE_GRACE` seconds with no new heartbeat we exit —
    fast. A *refresh* also fires `pagehide`, but the reloaded page sends a heartbeat within ~1s,
    which cancels the pending close, so a refresh doesn't kill the server.
  - A slow **heartbeat backstop**: the page POSTs `/api/ping` periodically; if none arrives for
    `BEAT_TIMEOUT` (generous — minutes — so background throttling never trips it) we exit anyway,
    covering the rare case where `pagehide` never fires (e.g. the browser is killed).

Exit mirrors lifecycle.py: `os._exit` after `engine.shutdown()`.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from server import config
from server.core import engine

# Generous heartbeat backstop (seconds): long enough that background-tab timer throttling
# (as slow as ~1/min) never trips it. Only catches a close that didn't fire the beacon.
BEAT_TIMEOUT: float = float(os.environ.get("CHESS_APP_BEAT_TIMEOUT", "120"))
# After an explicit close beacon, wait this long for a heartbeat to resume (a refresh) before
# exiting. Short, so a real close quits promptly.
CLOSE_GRACE: float = float(os.environ.get("CHESS_APP_CLOSE_GRACE", "3"))

_lock = threading.Lock()
_armed = False  # becomes True after the first heartbeat (browser actually connected)
_last_beat = 0.0
_closing_at: float | None = None  # monotonic time the tab signalled it's unloading
_started = False
_stop = threading.Event()


def beat() -> None:
    """Heartbeat from the open tab. Arms the watchdog and cancels any pending close (e.g. refresh)."""
    global _armed, _last_beat, _closing_at
    with _lock:
        _armed = True
        _last_beat = time.monotonic()
        _closing_at = None


def closing() -> None:
    """The tab signalled it's unloading (pagehide). Starts the short close countdown."""
    global _closing_at
    with _lock:
        if _armed:
            _closing_at = time.monotonic()


def _expired() -> bool:
    with _lock:
        if not _armed:
            return False  # browser never connected yet — never exit
        now = time.monotonic()
        if _closing_at is not None and (now - _closing_at) >= CLOSE_GRACE:
            return True  # explicit close, and no heartbeat came back (not a refresh)
        return (now - _last_beat) >= BEAT_TIMEOUT  # backstop


def _run() -> None:
    while not _stop.wait(1):
        if _expired():
            print(
                "[chess-app] browser closed — shutting the app down.",
                file=sys.stderr,
                flush=True,
            )
            try:
                engine.shutdown()
            finally:
                os._exit(0)


def start() -> None:
    """Start the liveness watchdog once. No-op unless in app mode."""
    global _started
    if not config.APP_MODE:
        return
    with _lock:
        if _started:
            return
        _started = True
    _stop.clear()
    threading.Thread(target=_run, name="chess-app-liveness", daemon=True).start()


def stop() -> None:
    """Stop the watchdog without exiting (clean shutdown / tests)."""
    global _started
    _stop.set()
    with _lock:
        _started = False
