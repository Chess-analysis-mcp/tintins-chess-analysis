"""Fetch games from the public Chess.com API so users don't have to paste PGNs.

Mirrors `server.core.lichess`: one entry point returning data that flows straight into
`analyze_game` (each game's `pgn` is exactly what Chess.com serves — full headers including a
`Link`/`Site` for platform normalisation, plus `[%clk]` comments so time-trouble motifs work):

  - fetch_user_games(username, max=5, since_days=None) -> list[GameSummary]  (newest first)

The published-data API (https://api.chess.com/pub/...) is public and needs no auth. Games are
organised into monthly archives, so we walk the archive list newest-first until we have enough.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass

import httpx

from server import config


class ChesscomError(RuntimeError):
    """A user-facing problem talking to Chess.com (network, bad username, rate limit, ...)."""


@dataclass
class GameSummary:
    """One game's metadata plus its full PGN (same shape as lichess.GameSummary)."""

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
    end_time: int = 0  # epoch seconds; used to sort newest-first across archives

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("end_time", None)
        return d


def _headers() -> dict[str, str]:
    # Chess.com blocks requests without a real User-Agent; identify ourselves politely.
    return {"User-Agent": "chess-analysis-mcp (github.com/Chess-analysis-mcp)"}


def _get_json(url: str) -> dict:
    """GET with friendly, user-facing errors mapped from Chess.com status codes."""
    try:
        resp = httpx.get(url, headers=_headers(), timeout=config.CHESSCOM_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:  # network / timeout / DNS
        raise ChesscomError(f"Could not reach Chess.com: {exc}") from exc

    if resp.status_code == 404:
        raise ChesscomError("Chess.com returned 404 — no such username.")
    if resp.status_code == 429:
        raise ChesscomError("Chess.com rate limit hit (HTTP 429). Wait a minute and try again.")
    if resp.status_code >= 400:
        raise ChesscomError(f"Chess.com error (HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise ChesscomError("Chess.com returned an unreadable response.") from exc


def _date_from(epoch: int | None) -> str | None:
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y.%m.%d")


_RESULT_WORDS_WIN = ("win",)


def _result_from(white_result: str, black_result: str) -> str:
    if white_result in _RESULT_WORDS_WIN:
        return "1-0"
    if black_result in _RESULT_WORDS_WIN:
        return "0-1"
    if white_result or black_result:  # both non-win codes (agreed, repetition, stalemate, ...)
        return "1/2-1/2"
    return "*"


def _opening_from_pgn(pgn: str) -> str | None:
    """A readable opening name from the PGN's ECOUrl (bulk exports omit an Opening header)."""
    for line in pgn.splitlines():
        if line.startswith('[ECOUrl "'):
            tail = line.split('"')[1].rstrip("/").rsplit("/", 1)[-1]
            name = tail.replace("-", " ").strip()
            # Trim the move-list suffix some ECOUrls carry ("...Defense 3.Nc3-a6" -> "...Defense").
            words = []
            for w in name.split():
                if w[:1].isdigit() and "." in w:
                    break
                words.append(w)
            return " ".join(words) or None
        if not line.startswith("["):
            break
    return None


def _summary_from_json(g: dict) -> GameSummary:
    white = g.get("white", {}) or {}
    black = g.get("black", {}) or {}
    url = g.get("url", "") or ""
    pgn = g.get("pgn", "") or ""
    return GameSummary(
        game_id=url.rstrip("/").rsplit("/", 1)[-1] or url,
        url=url,
        white=white.get("username", "?"),
        black=black.get("username", "?"),
        white_elo=white.get("rating"),
        black_elo=black.get("rating"),
        result=_result_from(white.get("result", ""), black.get("result", "")),
        speed=g.get("time_class", "unknown"),
        opening=_opening_from_pgn(pgn),
        date=_date_from(g.get("end_time")),
        pgn=pgn,
        end_time=int(g.get("end_time") or 0),
    )


def _resolve_username(username: str | None) -> str:
    name = (username or "").strip()
    if not name or name.lower() == "me":
        name = (config.CHESSCOM_USERNAME or "").strip()
    if not name:
        raise ChesscomError("A Chess.com username is required (set it in ⚙ Settings).")
    return name


def fetch_user_games(
    username: str,
    max: int | None = None,
    *,
    since_days: int | None = None,
) -> list[GameSummary]:
    """Fetch a user's most recent games (newest first) as GameSummary objects.

    Walks the monthly archives newest-first until `max` games are collected (or, with
    `since_days`, until games get older than the window). Skips variants/odd games with no PGN.
    """
    name = _resolve_username(username)
    n = max if (max and max > 0) else config.LICHESS_DEFAULT_MAX
    cutoff = None
    if since_days and since_days > 0:
        cutoff = datetime.datetime.now(tz=datetime.timezone.utc).timestamp() - since_days * 86400

    base = config.CHESSCOM_API_BASE
    archives = _get_json(f"{base}/pub/player/{name}/games/archives").get("archives", [])
    games: list[GameSummary] = []
    for archive_url in reversed(archives):  # newest month first
        month = [
            _summary_from_json(g)
            for g in _get_json(archive_url).get("games", [])
            if g.get("pgn") and g.get("rules", "chess") == "chess"
        ]
        month.sort(key=lambda g: g.end_time, reverse=True)
        for g in month:
            if cutoff is not None and g.end_time and g.end_time < cutoff:
                return games  # rest of history is older than the window
            games.append(g)
            if len(games) >= n:
                return games
    return games
