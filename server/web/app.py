"""FastAPI app factory: JSON board API + the static no-build frontend."""
from __future__ import annotations

import ipaddress
import mimetypes
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from server import config
from server.core import app_liveness
from server.core import lifecycle
from server.web.routes_board import router as board_router
from server.web.routes_chat import router as chat_router
from server.web.routes_history import router as history_router
from server.web.routes_settings import router as settings_router
from server.web.routes_updates import router as updates_router


# Force a JavaScript MIME type for .js/.mjs regardless of the host's mimetypes registry. The board
# loads `main.js` as an ES module (`<script type="module">`) and that module imports the vendored
# chessground/chess.js from `/vendor/*.min.js`; browsers REFUSE a module script unless it's served
# with a JS MIME. On Windows, `mimetypes.guess_type` reads the registry, where some installed apps
# map `.js` -> `text/plain`, which would break the offline board ("Failed to load module script").
# `add_type` runs init() first then overrides, so our value wins over any registry entry.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


# --- Local-only request guard (CSRF / DNS-rebinding defence) ------------------------------------
# The board binds to loopback, but loopback is NOT a security boundary for a *browser*: any website
# the user visits can make their browser send requests to 127.0.0.1, and a DNS-rebinding attack can
# even turn that into a same-origin read. Our endpoints spawn `claude -p` (burns the user's Claude
# quota) and expose game history + the Lichess token, so we reject requests that don't originate
# from the board itself. Two checks, both standard for localhost apps:
#   * Host header must be a loopback name — defeats DNS rebinding (the browser sends the attacker's
#     hostname in Host even after the IP rebinds to 127.0.0.1).
#   * Origin header (when present) must be loopback — blocks ordinary cross-site fetch/POST.
# "testserver" is allowed so Starlette's TestClient works; a real browser can't be coerced into
# sending that Host while connecting to the user's machine, so it doesn't widen the attack surface.
_LOCAL_HOSTNAMES = {"localhost", "testserver"}


def _authority_host(value: str) -> str:
    """Extract the bare host from a Host or Origin header value (drop scheme, port, [] brackets)."""
    v = value.strip().lower()
    if "://" in v:  # Origin is scheme://host[:port]; Host is host[:port]
        v = v.split("://", 1)[1]
    if v.startswith("["):  # bracketed IPv6, e.g. [::1]:8765
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if ":" in v else v


def _is_local_host(host: str) -> bool:
    if host in _LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard_is_active() -> bool:
    """Guard only when bound to loopback. Binding to 0.0.0.0/a routable IP is an explicit opt-in to
    network access, where a fixed Host allowlist would be wrong — so we don't second-guess it."""
    host = (config.WEB_HOST or "").strip()
    if host in ("", "0.0.0.0", "::"):
        return False
    return _is_local_host(_authority_host(host))


class _NoCacheStaticFiles(StaticFiles):
    """Serve the no-build frontend with `Cache-Control: no-cache` so browsers always REVALIDATE.

    Plain `StaticFiles` emits an ETag/Last-Modified but no Cache-Control, so a browser may serve a
    stale `main.js`/`styles.css` from heuristic cache without checking — meaning a JS/CSS edit
    silently doesn't take effect until a hard refresh. `no-cache` (not `no-store`) keeps the cache
    but forces revalidation: an unchanged file still returns a cheap 304, an edited one is refetched.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _resolve_frontend_dir() -> Path | None:
    """Locate the static frontend, working for BOTH a source checkout and an installed wheel.

    A plain wheel install (e.g. `uv run` for the MCP server) ships `frontend/` inside the package
    as `server/_frontend/` (see pyproject force-include); a source/editable run uses the repo-root
    `frontend/` sibling. Try the packaged copy first, then the source layout. Returning None means
    the UI genuinely wasn't shipped — `create_app` logs loudly rather than silently 404-ing at `/`.
    """
    here = Path(__file__).resolve()
    packaged = here.parent.parent / "_frontend"        # server/web/app.py -> server/_frontend
    source = here.parents[2] / "frontend"              # <repo>/frontend (source/editable checkout)
    for candidate in (packaged, source):
        if candidate.is_dir():
            return candidate
    return None


_FRONTEND_DIR = _resolve_frontend_dir()


def create_app() -> FastAPI:
    app = FastAPI(title="Chess Review board", docs_url="/api/docs")

    # In app mode (double-click launcher), self-exit shortly after the browser tab is closed.
    # No-op for the MCP-driven board and tests (config.APP_MODE is off there).
    app_liveness.start()

    guard_active = _guard_is_active()

    @app.middleware("http")
    async def _guard_and_mark_activity(request: Request, call_next):
        # Reject cross-site / rebound requests before they can spend Claude quota or read game data.
        if guard_active:
            host = request.headers.get("host", "")
            if host and not _is_local_host(_authority_host(host)):
                return PlainTextResponse("Forbidden: non-local Host header.", status_code=403)
            origin = request.headers.get("origin")
            if origin is not None:
                if origin.lower() == "null":
                    # file:// (the loading splash) and sandboxed iframes both send Origin: null.
                    # Allow it only for side-effect-free reads; never for state-changing methods.
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        return PlainTextResponse("Forbidden: opaque origin.", status_code=403)
                elif not _is_local_host(_authority_host(origin)):
                    return PlainTextResponse("Forbidden: cross-origin request.", status_code=403)
        # Any board interaction keeps the session alive (resets the idle watchdog).
        lifecycle.touch()
        return await call_next(request)

    app.include_router(board_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(history_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")
    app.include_router(updates_router, prefix="/api")

    # Mount the raw frontend last so /api/* routes win. html=True serves index.html at /.
    if _FRONTEND_DIR is not None:
        app.mount("/", _NoCacheStaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
    else:
        # The UI wasn't packaged with this install — the board would 404 at `/`. Don't fail silently:
        # this is a packaging bug (see _resolve_frontend_dir), and a bare 404 is impossible to debug.
        print(
            "[chess-web] WARNING: frontend assets not found; the board UI is unavailable and '/' "
            "will 404. This usually means the package was installed without 'server/_frontend'.",
            file=sys.stderr,
            flush=True,
        )

    return app
