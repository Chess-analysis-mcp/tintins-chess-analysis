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
import sys
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
    "puzzle_animations",
    "puzzle_auto_advance",
    "puzzle_mistake_interleave",
    "local_llm_base_url",
    "local_llm_model",
    "local_llm_api_key",
    "local_llm_api_key_header",
    "anthropic_api_key",
    "anthropic_model",
    "web_host",
)


# The settings that are credentials. They get three extra guarantees, all enforced through this one
# list so a newly added key can't silently miss them: settings.json is written owner-only, they are
# stripped from any API response to a non-local client (Settings opened from a phone over LAN
# access), and a blank field from such a client never overwrites the stored value. See
# `server/web/routes_settings.py`.
SECRET_KEYS = (
    "lichess_token",
    "local_llm_api_key",
    "anthropic_api_key",
)


# Owner read+write. The write bit matters on Windows: os.chmod there maps a mode without S_IWRITE
# onto the read-only attribute, which would make the *next* save fail.
FILE_MODE = 0o600


def redact(values: dict) -> list[str]:
    """Blank every non-empty secret in `values` (in place). Returns the keys that were hidden."""
    hidden = []
    for key in SECRET_KEYS:
        if values.get(key):
            values[key] = ""
            hidden.append(key)
    return hidden


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
    # This file can hold API keys (SECRET_KEYS), so make it owner-only rather than leaving it at
    # whatever the umask gives. Best-effort: on Windows os.chmod only toggles the read-only
    # attribute (FILE_MODE keeps the owner-write bit, so the file stays writable there), and some
    # filesystems refuse chmod outright. Neither may break saving settings.
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


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
    if "puzzle_animations" in settings:
        config.PUZZLE_ANIMATIONS = bool(settings["puzzle_animations"])
    if "puzzle_auto_advance" in settings:
        config.PUZZLE_AUTO_ADVANCE = bool(settings["puzzle_auto_advance"])
    if "puzzle_mistake_interleave" in settings:
        config.PUZZLE_MISTAKE_INTERLEAVE = bool(settings["puzzle_mistake_interleave"])
    if "local_llm_base_url" in settings:
        config.LOCAL_LLM_BASE_URL = (settings["local_llm_base_url"] or "").strip()
    if "local_llm_model" in settings:
        config.LOCAL_LLM_MODEL = (settings["local_llm_model"] or "").strip()
    if "local_llm_api_key" in settings:
        config.LOCAL_LLM_API_KEY = (settings["local_llm_api_key"] or "").strip()
    if "local_llm_api_key_header" in settings:
        config.LOCAL_LLM_API_KEY_HEADER = (settings["local_llm_api_key_header"] or "").strip()
    if "anthropic_api_key" in settings:
        config.ANTHROPIC_API_KEY = (settings["anthropic_api_key"] or "").strip()
    if "anthropic_model" in settings:
        config.ANTHROPIC_MODEL = (settings["anthropic_model"] or "").strip()
    if "web_host" in settings:
        # The server is already bound by the time a live save happens, so this only takes effect
        # on the next launch (apply_saved() reads it before uvicorn binds); still applied live so
        # effective() reflects it immediately for the Settings panel. An unusable value (only possible
        # by hand-editing settings.json; the API rejects it) falls back to loopback so the app starts.
        host = config.normalize_web_host(settings["web_host"])
        if host is None:
            print(
                f"[chess-settings] ignoring invalid web_host {settings['web_host']!r}; using 127.0.0.1",
                file=sys.stderr,
                flush=True,
            )
            host = "127.0.0.1"
        config.WEB_HOST = host


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
        "puzzle_animations": config.PUZZLE_ANIMATIONS,
        "puzzle_auto_advance": config.PUZZLE_AUTO_ADVANCE,
        "puzzle_mistake_interleave": config.PUZZLE_MISTAKE_INTERLEAVE,
        "local_llm_base_url": config.LOCAL_LLM_BASE_URL or "",
        "local_llm_model": config.LOCAL_LLM_MODEL or "",
        "local_llm_api_key": config.LOCAL_LLM_API_KEY or "",
        "local_llm_api_key_header": config.LOCAL_LLM_API_KEY_HEADER or "",
        "anthropic_api_key": config.ANTHROPIC_API_KEY or "",
        "anthropic_model": config.ANTHROPIC_MODEL or "",
        "web_host": config.WEB_HOST or "127.0.0.1",
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
