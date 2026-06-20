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
    "aliases",
    "lichess_token",
    "profile_recent",
    "profile_lifetime",
    "stockfish_path",
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
    if "username" in settings:
        config.USERNAME = (settings["username"] or "").strip()
    if "aliases" in settings:
        config.USERNAME_ALIASES = config._parse_aliases(settings["aliases"] or "")
    if "lichess_token" in settings:
        config.LICHESS_TOKEN = (settings["lichess_token"] or "").strip()
    if "profile_recent" in settings:
        try:
            config.PROFILE_RECENT_WINDOW = int(settings["profile_recent"])
        except (ValueError, TypeError):
            pass
    if "profile_lifetime" in settings:
        config.PROFILE_LIFETIME = config._parse_lifetime(str(settings["profile_lifetime"]))
    if "stockfish_path" in settings:
        sp = (settings["stockfish_path"] or "").strip()
        if sp:
            config.STOCKFISH_PATH = shutil.which(sp) or sp


def apply_saved(data_dir: Optional[str] = None) -> dict:
    """Load + apply settings.json at startup. Returns the loaded settings (possibly empty)."""
    settings = load(data_dir)
    apply(settings)
    return settings


def _aliases_to_str(pairs: list[tuple[Optional[str], str]]) -> str:
    return ", ".join((f"{plat}:{name}" if plat else name) for plat, name in pairs)


def effective() -> dict:
    """The current effective values (as raw strings) for the Settings form."""
    return {
        "username": config.USERNAME or "",
        "aliases": _aliases_to_str(config.USERNAME_ALIASES),
        "lichess_token": config.LICHESS_TOKEN or "",
        "profile_recent": str(config.PROFILE_RECENT_WINDOW),
        "profile_lifetime": "all" if config.PROFILE_LIFETIME is None else str(config.PROFILE_LIFETIME),
        "stockfish_path": config.STOCKFISH_PATH or "",
    }


def update(patch: dict, data_dir: Optional[str] = None) -> dict:
    """Merge a partial settings patch into the store, persist it, apply it live, return effective."""
    settings = load(data_dir)
    for key in KEYS:
        if key in patch:
            settings[key] = patch[key]
    save(settings, data_dir)
    apply(settings)
    return effective()
