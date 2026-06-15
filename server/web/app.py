"""FastAPI app factory: JSON board API + the static no-build frontend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.web.routes_board import router as board_router
from server.web.routes_chat import router as chat_router

# Repo root: server/web/app.py -> server/web -> server -> <root>
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIR = _REPO_ROOT / "frontend"


def create_app() -> FastAPI:
    app = FastAPI(title="Chess Review board", docs_url="/api/docs")
    app.include_router(board_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")

    # Mount the raw frontend last so /api/* routes win. html=True serves index.html at /.
    if _FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app
