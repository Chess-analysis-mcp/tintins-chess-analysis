"""User-editable settings, so the app is standalone (no hand-editing of .mcp.json).

A small JSON file at `<DATA_DIR>/settings.json` holds the knobs a user would otherwise set as env
vars in `.mcp.json` (username, alt accounts, Lichess token, profile windows, Stockfish path).
`apply_saved()` is called at startup by both entry points (the MCP server and the standalone web
app) to override the env-derived `config` values — so **settings.json wins over the environment**,
which wins over the built-in defaults. Because the rest of the code reads `config.*` at call-time,
writing settings live (via the Settings panel) takes effect immediately without a restart, and
persists across runs and across the MCP server / app processes (both read the same file).
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from server import config

# The keys the Settings panel can edit, stored as the raw strings a user would type (parsed into
# config the same way the matching env vars are).
KEYS = (
    "username",
    "chesscom_username",
    "chesscom_sync",
    "chesscom_sync_max",
    "aliases",
    "lichess_token",
    "profile_recent",
    "profile_lifetime",
    "player_elo",
    "stockfish_path",
    "coach_ai_auto",
    "coach_ai_persist",
    "personalize_history",
    "local_llm_base_url",
    "local_llm_model",
)


def _path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir if data_dir is not None else config.DATA_DIR, "settings.json")


def load(data_dir: Optional[str] = None) -> dict:
    """Read settings.json (missing/garbled -> {}, so the app still runs on env defaults)."""
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(settings: dict, data_dir: Optional[str] = None) -> None:
    path = _path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)


def apply(settings: dict) -> None:
    """Override live `config` values from a settings dict (only the keys that are present)."""
    # Identity is composed from three fields together (Lichess handle, chess.com handle, other
    # accounts) so the canonical player_id + aliases stay coherent. Any field absent from the patch
    # keeps its current live value.
    if any(k in settings for k in ("username", "chesscom_username", "aliases")):
        config._compose_identity(
            settings.get("username", config.LICHESS_USERNAME),
            settings.get("chesscom_username", config.CHESSCOM_USERNAME),
            settings.get("aliases", config.USERNAME_ALIASES_RAW),
        )
    if "chesscom_sync" in settings:
        config.CHESSCOM_SYNC_ENABLED = bool(settings["chesscom_sync"])
    if "chesscom_sync_max" in settings:
        try:
            n = int(settings["chesscom_sync_max"])
            if n > 0:
                config.CHESSCOM_SYNC_MAX = n
        except (ValueError, TypeError):
            pass
    if "lichess_token" in settings:
        config.LICHESS_TOKEN = (settings["lichess_token"] or "").strip()
    if "profile_recent" in settings:
        try:
            config.PROFILE_RECENT_WINDOW = int(settings["profile_recent"])
        except (ValueError, TypeError):
            pass
    if "profile_lifetime" in settings:
        config.PROFILE_LIFETIME = config._parse_lifetime(str(settings["profile_lifetime"]))
    if "player_elo" in settings:
        config.PLAYER_ELO = config._parse_elo(str(settings["player_elo"]))
    if "stockfish_path" in settings:
        sp = config.clean_path(settings["stockfish_path"])
        if sp:
            config.STOCKFISH_PATH = shutil.which(sp) or sp
    if "coach_ai_auto" in settings:
        config.COACH_AI_AUTO = bool(settings["coach_ai_auto"])
    if "coach_ai_persist" in settings:
        config.COACH_AI_PERSIST = bool(settings["coach_ai_persist"])
    if "personalize_history" in settings:
        config.PERSONALIZE_HISTORY = bool(settings["personalize_history"])
    if "local_llm_base_url" in settings:
        config.LOCAL_LLM_BASE_URL = (settings["local_llm_base_url"] or "").strip()
    if "local_llm_model" in settings:
        config.LOCAL_LLM_MODEL = (settings["local_llm_model"] or "").strip()


def apply_saved(data_dir: Optional[str] = None) -> dict:
    """Load + apply settings.json at startup. Returns the loaded settings (possibly empty)."""
    settings = load(data_dir)
    apply(settings)
    return settings


def effective() -> dict:
    """The current effective values (as raw strings) for the Settings form."""
    return {
        "username": config.LICHESS_USERNAME or "",
        "chesscom_username": config.CHESSCOM_USERNAME or "",
        "chesscom_sync": config.CHESSCOM_SYNC_ENABLED,
        "chesscom_sync_max": str(config.CHESSCOM_SYNC_MAX),
        "aliases": config.USERNAME_ALIASES_RAW,
        "lichess_token": config.LICHESS_TOKEN or "",
        "profile_recent": str(config.PROFILE_RECENT_WINDOW),
        "profile_lifetime": "all" if config.PROFILE_LIFETIME is None else str(config.PROFILE_LIFETIME),
        "player_elo": "" if config.PLAYER_ELO is None else str(config.PLAYER_ELO),
        "stockfish_path": config.STOCKFISH_PATH or "",
        "coach_ai_auto": config.COACH_AI_AUTO,
        "coach_ai_persist": config.COACH_AI_PERSIST,
        "personalize_history": config.PERSONALIZE_HISTORY,
        "local_llm_base_url": config.LOCAL_LLM_BASE_URL or "",
        "local_llm_model": config.LOCAL_LLM_MODEL or "",
    }


def update(patch: dict, data_dir: Optional[str] = None) -> dict:
    """Merge a partial settings patch into the store, persist it, apply it live, return effective."""
    settings = load(data_dir)
    for key in KEYS:
        if key in patch:
            settings[key] = patch[key]
    # Normalise the Stockfish path before persisting so a quoted "Copy as path" paste
    # (common on Windows) is stored clean, not just applied clean.
    if "stockfish_path" in settings and settings["stockfish_path"]:
        settings["stockfish_path"] = config.clean_path(settings["stockfish_path"])
    save(settings, data_dir)
    apply(settings)
    return effective()
