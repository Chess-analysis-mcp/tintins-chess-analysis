"""Fetch games from the public Lichess API so users don't have to paste PGNs.

Two entry points, both returning data that flows straight into `analyze_game` (the `pgn`
field is exactly the format `game_analysis.analyze_game` consumes — Elo headers, a `Site`
containing "lichess" so platform normalisation works, and `[%clk]` comments so time-trouble
motifs work):

  - fetch_user_games(username, max=3, ...) -> list[GameSummary]  (newest first)
  - fetch_game(game_id_or_url)             -> GameSummary

Auth is OPTIONAL: set `LICHESS_TOKEN` (a Personal Access Token) to be throttled per-token
instead of per-IP — the escape hatch for heavy users. Public games need no token.
"""
from __future__ import annotations

import datetime
import json
import time
from dataclasses import asdict, dataclass

import httpx

from server import config


class LichessError(RuntimeError):
    """A user-facing problem talking to Lichess (network, bad id, rate limit, ...)."""


@dataclass
class GameSummary:
    """One game's metadata plus its full PGN (ready to hand to analyze_game)."""

    game_id: str
    url: str
    white: str
    black: str
    white_elo: int | None
    black_elo: int | None
    result: str
    speed: str
    opening: str | None
    date: str | None
    pgn: str

    def to_dict(self) -> dict:
        return asdict(self)


def _headers(accept: str) -> dict[str, str]:
    h = {"User-Agent": "chess-analysis-mcp", "Accept": accept}
    if config.LICHESS_TOKEN:
        h["Authorization"] = f"Bearer {config.LICHESS_TOKEN}"
    return h


def _get(url: str, params: dict, *, accept: str) -> str:
    """GET with friendly, user-facing errors mapped from Lichess status codes."""
    try:
        resp = httpx.get(
            url,
            params=params,
            headers=_headers(accept),
            timeout=config.LICHESS_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:  # network / timeout / DNS
        raise LichessError(f"Could not reach Lichess: {exc}") from exc

    if resp.status_code == 404:
        raise LichessError("Lichess returned 404 — no such username or game id.")
    if resp.status_code == 401:
        raise LichessError("Lichess rejected the token (HTTP 401) — check LICHESS_TOKEN.")
    if resp.status_code == 429:
        raise LichessError(
            "Lichess rate limit hit (HTTP 429). Wait about a minute and try again. Heavy users "
            "can set a LICHESS_TOKEN (a free Personal Access Token from "
            "https://lichess.org/account/oauth/token) to be throttled per-token instead of per-IP."
        )
    if resp.status_code >= 400:
        raise LichessError(f"Lichess error (HTTP {resp.status_code}): {resp.text[:200]}")
    return resp.text


def _date_from(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y.%m.%d")


def _result_from(winner: str | None, status: str | None) -> str:
    if winner == "white":
        return "1-0"
    if winner == "black":
        return "0-1"
    if status in ("draw", "stalemate"):
        return "1/2-1/2"
    return "*"  # ongoing / aborted / unknown


def _name(side: dict) -> str:
    user = side.get("user") or {}
    if user.get("name"):
        return user["name"]
    if side.get("aiLevel"):
        return f"Stockfish level {side['aiLevel']}"
    return "Anonymous"


def _summary_from_json(g: dict) -> GameSummary:
    players = g.get("players", {}) or {}
    white = players.get("white", {}) or {}
    black = players.get("black", {}) or {}
    opening = g.get("opening") or {}
    gid = g.get("id", "")
    return GameSummary(
        game_id=gid,
        url=f"{config.LICHESS_API_BASE}/{gid}",
        white=_name(white),
        black=_name(black),
        white_elo=white.get("rating"),
        black_elo=black.get("rating"),
        result=_result_from(g.get("winner"), g.get("status")),
        speed=g.get("speed", "unknown"),
        opening=opening.get("name"),
        date=_date_from(g.get("createdAt")),
        pgn=g.get("pgn", ""),
    )


def _resolve_username(username: str | None) -> str:
    """Empty or "me" resolves to the configured CHESS_USERNAME, so "analyze my recent games" works."""
    name = (username or "").strip()
    if not name or name.lower() == "me":
        name = (config.USERNAME or "").strip()
    if not name:
        raise LichessError("A Lichess username is required (or set CHESS_USERNAME).")
    return name


def fetch_user_games(
    username: str,
    max: int | None = None,
    *,
    rated: bool | None = None,
    perf: str | None = None,
    color: str | None = None,
    since_days: int | None = None,
) -> list[GameSummary]:
    """Fetch a user's most recent games (newest first) as GameSummary objects.

    `perf` is a comma-separated Lichess speed filter ("blitz,rapid"); `color` filters to games
    the user played as white/black; `since_days` limits to the last N days.
    """
    name = _resolve_username(username)
    n = max if (max and max > 0) else config.LICHESS_DEFAULT_MAX
    params: dict = {
        "max": n,
        "pgnInJson": "true",  # PGN included on each ndjson record
        "clocks": "true",     # keep [%clk] so time-trouble motifs work
        "opening": "true",
        "sort": "dateDesc",
    }
    if rated is not None:
        params["rated"] = "true" if rated else "false"
    if perf:
        params["perfType"] = perf
    if color in ("white", "black"):
        params["color"] = color
    if since_days and since_days > 0:
        params["since"] = int((time.time() - since_days * 86400) * 1000)

    url = f"{config.LICHESS_API_BASE}/api/games/user/{name}"
    text = _get(url, params, accept="application/x-ndjson")
    return [_summary_from_json(json.loads(line)) for line in text.splitlines() if line.strip()]


def _extract_game_id(raw: str) -> str:
    """Accept a bare id or a full Lichess URL (with optional /white, /black, #move suffixes)."""
    gid = (raw or "").strip().split("?")[0].split("#")[0].rstrip("/")
    if "/" in gid:
        # Drop the optional /white|/black orientation suffix, then take the id segment.
        parts = [p for p in gid.split("/") if p and p.lower() not in ("white", "black")]
        gid = parts[-1] if parts else ""
    # A game id is 8 chars; a full (12-char) id still starts with the public id -> keep the first 8.
    return gid[:8]


def fetch_game(game_id_or_url: str) -> GameSummary:
    """Fetch a single game by its Lichess id or URL."""
    gid = _extract_game_id(game_id_or_url)
    if not gid:
        raise LichessError("A Lichess game id or URL is required.")
    url = f"{config.LICHESS_API_BASE}/game/export/{gid}"
    params = {"pgnInJson": "true", "clocks": "true", "opening": "true"}
    text = _get(url, params, accept="application/json")
    return _summary_from_json(json.loads(text))
