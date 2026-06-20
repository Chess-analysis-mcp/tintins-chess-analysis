"""FastAPI app factory: JSON board API + the static no-build frontend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from server.core import app_liveness
from server.core import lifecycle
from server.web.routes_board import router as board_router
from server.web.routes_chat import router as chat_router
from server.web.routes_history import router as history_router
from server.web.routes_settings import router as settings_router

# Repo root: server/web/app.py -> server/web -> server -> <root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIR = _REPO_ROOT / "frontend"


def create_app() -> FastAPI:
    app = FastAPI(title="Chess Review board", docs_url="/api/docs")

    # In app mode (double-click launcher), self-exit shortly after the browser tab is closed.
    # No-op for the MCP-driven board and tests (config.APP_MODE is off there).
    app_liveness.start()

    @app.middleware("http")
    async def _mark_activity(request: Request, call_next):
        # Any board interaction keeps the session alive (resets the idle watchdog).
        lifecycle.touch()
        return await call_next(request)

    app.include_router(board_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(history_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")

    # Mount the raw frontend last so /api/* routes win. html=True serves index.html at /.
    if _FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app
