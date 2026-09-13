"""Start the FastAPI web server in a background daemon thread.

Called from `mcp_server.main()` so the board shares the MCP process's engine pool and
session. Idempotent and best-effort: a port collision (a stale instance still bound) logs
to stderr and never crashes the MCP server, since stdout is owned by the MCP protocol.
"""
from __future__ import annotations

import socket
import sys
import threading
import webbrowser

import uvicorn

from server import config
from server.web.app import create_app

_thread: threading.Thread | None = None
_lock = threading.Lock()
_opened = False
_open_lock = threading.Lock()


def open_board_once() -> None:
    """Open the board in the default browser, at most once per process.

    Called when a game is analysed (not at server boot) so the tab only appears once
    there is actually a game to look at. Best-effort: a headless box or a missing
    browser just logs to stderr and never raises. Disable with CHESS_WEB_OPEN=0.
    """
    global _opened
    if not config.WEB_OPEN:
        return
    with _open_lock:
        if _opened:
            return
        _opened = True
    url = config.board_url()
    try:
        if webbrowser.open(url):
            print(f"[chess-web] opened board in browser: {url}", file=sys.stderr, flush=True)
        else:
            print(
                f"[chess-web] no browser to open; board is at {url}",
                file=sys.stderr,
                flush=True,
            )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[chess-web] could not open browser ({exc}); board is at {url}",
              file=sys.stderr, flush=True)


def board_port_in_use(host: str, port: int) -> bool:
    """True if a board (or anything) already answers on this computer's loopback at `port`.

    Checked before binding: a wildcard (0.0.0.0) listener and a loopback listener can share one port
    on macOS/Windows, silently splitting traffic between two servers instead of the second one
    failing to bind. Only relevant when `host` covers loopback (wildcard or a loopback name).
    """
    host = (host or "").strip()
    if host not in config.WILDCARD_HOSTS and host not in ("", "127.0.0.1", "localhost"):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _serve() -> None:
    if board_port_in_use(config.WEB_HOST, config.WEB_PORT):
        print(
            f"[chess-web] port {config.WEB_PORT} is already in use on this computer (another board "
            "still running?); board disabled for this process.",
            file=sys.stderr,
            flush=True,
        )
        return
    config.WEB_BOUND_HOST = config.WEB_HOST  # what this process actually serves on (see phone_access)
    try:
        cfg = uvicorn.Config(
            create_app(),
            host=config.WEB_HOST,
            port=config.WEB_PORT,
            log_level="warning",
            access_log=False,
        )
        uvicorn.Server(cfg).run()  # blocks (runs its own event loop)
    except OSError as exc:
        print(
            f"[chess-web] could not bind {config.WEB_HOST}:{config.WEB_PORT} ({exc}); "
            "board disabled for this process.",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[chess-web] web server stopped: {exc}", file=sys.stderr, flush=True)


def start_in_thread() -> None:
    """Start the web server once. Safe to call multiple times."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_serve, name="chess-web", daemon=True)
        _thread.start()
        lan_url = config.lan_board_url()
        print(
            f"[chess-web] serving board at {config.board_url()}"
            + (f" (other devices on your network: {lan_url})" if lan_url else ""),
            file=sys.stderr,
            flush=True,
        )
