"""Settings routes: read/update the user-editable config so the app is standalone.

`GET /api/settings` returns the current effective values for the Settings panel; `POST /api/settings`
persists a patch to `<DATA_DIR>/settings.json`, applies it to the live `config`, and handles the one
change with a side effect — a new Stockfish path, which is validated then triggers an engine restart.
"""
from __future__ import annotations

import shutil

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.core import engine
from server.core import settings as settings_mod

router = APIRouter()


class SettingsPatch(BaseModel):
    username: str | None = None
    aliases: str | None = None
    lichess_token: str | None = None
    profile_recent: str | None = None
    profile_lifetime: str | None = None
    stockfish_path: str | None = None


def _stockfish_ok(path: str) -> bool:
    return bool(shutil.which((path or "").strip()))


@router.get("/settings")
def get_settings() -> dict:
    """Current effective settings + a couple of read-only status flags for the panel."""
    eff = settings_mod.effective()
    return {
        "settings": eff,
        "stockfish_ok": _stockfish_ok(eff["stockfish_path"]),
        "data_dir": config.DATA_DIR,
    }


@router.post("/settings")
def post_settings(patch: SettingsPatch) -> JSONResponse:
    """Persist + apply a settings patch. Returns the new effective settings (or a 400 on bad input)."""
    data = {k: v for k, v in patch.model_dump().items() if v is not None}

    # A new Stockfish path is the only setting with a side effect: validate it, then restart the
    # engine pool so the next analysis uses it. An unusable path is rejected before anything changes.
    new_path = (data.get("stockfish_path") or "").strip()
    restart_engine = bool(new_path) and (shutil.which(new_path) or new_path) != config.STOCKFISH_PATH
    if new_path and not _stockfish_ok(new_path):
        return JSONResponse(
            {"error": f"Stockfish not found or not executable at '{new_path}'."}, status_code=400
        )

    eff = settings_mod.update(data)
    if restart_engine:
        try:
            engine.restart()
        except Exception:  # pragma: no cover - defensive; next analysis would surface a real error
            pass
    return JSONResponse({"settings": eff, "stockfish_ok": _stockfish_ok(eff["stockfish_path"])})
