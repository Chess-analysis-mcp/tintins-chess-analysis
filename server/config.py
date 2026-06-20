"""Configuration for the chess review server.

All tunables live here so the engine, analysis, and MCP layers agree on defaults.
Values can be overridden via environment variables.
"""
from __future__ import annotations

import os
import shutil

# Repo root (this file is <repo>/server/config.py), used for repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Common locations a Stockfish binary lands in across the package managers we point
# users at. Searched (in order) only when STOCKFISH_PATH isn't set and `stockfish`
# isn't on PATH, so a normal `brew`/`apt` install needs zero configuration.
_COMMON_STOCKFISH_PATHS = [
    "/opt/homebrew/bin/stockfish",  # macOS, Apple Silicon Homebrew
    "/usr/local/bin/stockfish",     # macOS Intel Homebrew / manual installs
    "/usr/bin/stockfish",           # Debian/Ubuntu apt
    "/usr/games/stockfish",         # some Linux distros put it here
]


def _resolve_stockfish() -> str:
    """Best-effort path to the Stockfish binary.

    Priority: an explicit STOCKFISH_PATH (honoured as set, resolved via PATH if it's a
    bare command) -> `stockfish` on PATH -> the common install locations above. Falls
    back to the bare name "stockfish" so the engine still raises a clear, actionable
    error (see stockfish_install_hint) when nothing is found.
    """
    explicit = os.environ.get("STOCKFISH_PATH", "").strip()
    if explicit:
        return shutil.which(explicit) or explicit
    found = shutil.which("stockfish")
    if found:
        return found
    for path in _COMMON_STOCKFISH_PATHS:
        if os.path.isfile(path):
            return path
    return "stockfish"


def stockfish_install_hint(path: str | None = None) -> str:
    """One-line, copy-pasteable guidance shown when Stockfish can't be launched."""
    tried = path or STOCKFISH_PATH
    return (
        f"Stockfish engine not found (tried '{tried}'). Install it — macOS: "
        "`brew install stockfish`; Debian/Ubuntu: `sudo apt install stockfish` — or "
        "download it from https://stockfishchess.org/download/ and set STOCKFISH_PATH "
        "to the binary. See the README 'Installation' section."
    )


# Path to the Stockfish binary. Auto-detected (PATH + common locations) so a standard
# install needs no config; override with the STOCKFISH_PATH env var.
STOCKFISH_PATH: str = _resolve_stockfish()

# Depth used for on-demand single-position analysis (get_engine_line, REPL checks).
# Fixed depth keeps evals reproducible and cacheable.
DEFAULT_DEPTH: int = int(os.environ.get("CHESS_DEFAULT_DEPTH", "18"))

# Depth used when sweeping every ply of a full game. Lower than DEFAULT_DEPTH so a
# full-game review finishes in reasonable time; positions can be re-deepened on
# demand via get_engine_line.
SWEEP_DEPTH: int = int(os.environ.get("CHESS_SWEEP_DEPTH", "16"))

DEFAULT_MULTIPV: int = int(os.environ.get("CHESS_DEFAULT_MULTIPV", "1"))

# Engine process pool size. 1-2 is plenty for a single-user local tool. Default 2 so the
# web /evaluate route and a concurrent MCP call don't serialise behind one engine.
ENGINE_POOL_SIZE: int = int(os.environ.get("CHESS_ENGINE_POOL_SIZE", "2"))

# Per-engine UCI options.
ENGINE_THREADS: int = int(os.environ.get("CHESS_ENGINE_THREADS", "2"))
ENGINE_HASH_MB: int = int(os.environ.get("CHESS_ENGINE_HASH_MB", "128"))

# Centipawn magnitude treated as "mate-equivalent" when converting mate scores.
MATE_SCORE_CP: int = 10000

# Used by analyze_game(player="auto") to pick which side is "me" from PGN headers.
USERNAME: str = os.environ.get("CHESS_USERNAME", "thedarktintin")


def _parse_aliases(raw: str) -> list[tuple[str | None, str]]:
    """Parse CHESS_ALIASES into (platform|None, handle_lower) pairs.

    Just a comma-separated list of your other handles, e.g. "my_chesscom_name, my_other_name".
    Each item normally matches on any site; advanced users can pin one to a single platform with
    "platform:handle" ("chesscom:dpdemler"). All of them resolve to CHESS_USERNAME as the canonical
    player_id, so several accounts fold into one coaching profile (and into player="auto" detection).
    """
    pairs: list[tuple[str | None, str]] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            plat, name = tok.split(":", 1)
            pairs.append((plat.strip().lower() or None, name.strip().lower()))
        else:
            pairs.append((None, tok.lower()))
    return pairs


# Extra account handles that are also "me" (folded into CHESS_USERNAME's profile). Set in
# .mcp.json's env, e.g. CHESS_ALIASES="chesscom:dpdemler, my_other_lichess".
USERNAME_ALIASES: list[tuple[str | None, str]] = _parse_aliases(os.environ.get("CHESS_ALIASES", ""))

# Game history (personalised coaching). Each analysed game is appended as one line to
# <DATA_DIR>/history/games.jsonl, deduped by (game_id, reviewed_side). Identity aliases
# (one person, several lichess/chess.com accounts) live in <DATA_DIR>/identities.json, and
# a rebuildable per-player profile is cached in <DATA_DIR>/profiles/<player_id>.json.
# Defaults to <repo>/.chess-review (gitignored, so it stays local but out of version control).
# CHESS_DATA_DIR overrides the location; CHESS_HISTORY=0 disables recording entirely.
DATA_DIR: str = os.environ.get("CHESS_DATA_DIR", os.path.join(_REPO_ROOT, ".chess-review"))
HISTORY_ENABLED: bool = os.environ.get("CHESS_HISTORY", "1") != "0"

# Disk cache of fully-analysed games (<DATA_DIR>/analysis-cache/<game_id>_<side>.json), keyed by
# the same (game_id, reviewed_side) history dedupes on. Reopening a game already analysed on this
# machine — even in a previous app session — then loads from disk instead of re-running the
# ~20-45s Stockfish sweep. Best-effort; CHESS_ANALYSIS_CACHE=0 disables it. The entry cap bounds
# disk growth (least-recently-used pruned); CHESS_ANALYSIS_CACHE_MAX=0 means unbounded.
ANALYSIS_CACHE_ENABLED: bool = os.environ.get("CHESS_ANALYSIS_CACHE", "1") != "0"
ANALYSIS_CACHE_MAX: int = int(os.environ.get("CHESS_ANALYSIS_CACHE_MAX", "1000"))

# The engine-grounded templated coaching blurb (history.coach_summary) is always attached to a
# session summary — it's free (no engine/Claude work). The richer, Claude-WRITTEN summary is
# generated on demand via /api/coach (a button in the UI), so it only spends the user's Claude
# subscription when asked. This flag controls whether the UI presses that button AUTOMATICALLY for
# each game opened; off by default (CHESS_COACH_AI_AUTO=1 to default it on).
COACH_AI_AUTO: bool = os.environ.get("CHESS_COACH_AI_AUTO", "0") == "1"

# Self-terminate the server process after this many seconds of inactivity (no MCP tool call
# and no board request), so an abandoned session doesn't linger as a process forever. Activity
# resets the timer. Default 24h; CHESS_SESSION_TTL=0 disables the watchdog.
SESSION_TTL_SECONDS: int = int(os.environ.get("CHESS_SESSION_TTL", str(24 * 60 * 60)))


def _parse_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Coaching profile is a HYBRID of two views so it adapts as a player improves:
#   - "recent form" = the last CHESS_PROFILE_RECENT games (a sliding window; <=0 means all games).
#   - "lifetime"    = CHESS_PROFILE_LIFETIME: unset/"all" -> all history (default); a positive N ->
#                     the last N games; "0" -> DISABLED, leaving only the recent window (i.e. a pure
#                     sliding window). Both are recomputed from the full games.jsonl, so widening a
#                     window later loses nothing.
PROFILE_RECENT_WINDOW: int = _parse_int("CHESS_PROFILE_RECENT", 100)


def _parse_lifetime(raw: str | None) -> int | None:
    raw = (raw or "").strip().lower()
    if raw in ("", "all"):
        return None  # all history
    try:
        return max(int(raw), 0)  # 0 disables the lifetime view; positive caps it
    except ValueError:
        return None


PROFILE_LIFETIME: int | None = _parse_lifetime(os.environ.get("CHESS_PROFILE_LIFETIME"))

# Lichess game import (so users don't paste PGNs). The fetch_games/fetch_game tools call the
# public Lichess API. Auth is OPTIONAL: set LICHESS_TOKEN to a Personal Access Token
# (https://lichess.org/account/oauth/token, no scopes needed for public game export) and requests
# are throttled per-token instead of per-IP — the escape hatch for heavy users who hit rate limits.
# Anonymous (no token) works fine for public games. LICHESS_API_BASE is overridable for testing.
LICHESS_TOKEN: str = os.environ.get("LICHESS_TOKEN", "").strip()
LICHESS_API_BASE: str = os.environ.get("LICHESS_API_BASE", "https://lichess.org").rstrip("/")
# How many recent games fetch_games returns when a count isn't given.
LICHESS_DEFAULT_MAX: int = int(os.environ.get("CHESS_LICHESS_MAX", "3"))
# HTTP timeout (seconds) for Lichess requests.
LICHESS_TIMEOUT: float = float(os.environ.get("CHESS_LICHESS_TIMEOUT", "20"))

# Web board (Phase 4). The FastAPI server runs in the same process as the MCP server,
# sharing the one engine pool and ReviewSession. WEB_AUTOSTART=0 disables the autostart
# (e.g. when driving the web server standalone via scripts/run_web.py).
WEB_HOST: str = os.environ.get("CHESS_WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.environ.get("CHESS_WEB_PORT", "8765"))
WEB_AUTOSTART: bool = os.environ.get("CHESS_WEB_AUTOSTART", "1") != "0"
# Auto-open the board in the default browser the first time a game is analysed, so a
# first-time user never has to be told the URL. Set CHESS_WEB_OPEN=0 to disable.
WEB_OPEN: bool = os.environ.get("CHESS_WEB_OPEN", "1") != "0"
# "App mode": set by the double-click launcher (Tintins AI Chess Analysis.command / .bat) when serving the
# board standalone for users who never touch a terminal. The frontend reads it via
# /api/app-config and, when on, auto-loads the user's most recent Lichess game on open. Left off
# (0) for the MCP-driven board and dev `run_web.py <pgn>` runs, so neither gets a surprise autoload.
APP_MODE: bool = os.environ.get("CHESS_APP_MODE", "0") == "1"
