"""Split a multi-game PGN into individual games and figure out which player is "you".

Chess.com / Lichess let you export many games as a single PGN file (games concatenated, each
starting with an `[Event ...]` header). `analyze_game` takes one game at a time, so the web
board's "Paste PGN / upload" flow uses this to fan a file out into per-game PGNs and to detect
the uploader's handle (the one appearing in every game) so each game is reviewed from *their*
side and folded into *their* "My games" history.

Splitting is text-based (lossless: original headers + `[%clk]` comments are preserved), with
python-chess only used to validate that a chunk actually contains a game.
"""
from __future__ import annotations

import io
import re

import chess.pgn

# Split immediately before each line that starts a new game's tag pair section.
_EVENT_BOUNDARY = re.compile(r"(?m)^(?=\[Event\b)")


def split_pgn(text: str) -> list[str]:
    """Split a (possibly multi-game) PGN into individual game PGN strings, in file order.

    Lossless: each returned string is the original text for that game (clocks/headers intact).
    Chunks that don't parse into a game with at least one move are dropped, so a stray header
    block or trailing whitespace never becomes a bogus game.
    """
    if not text or not text.strip():
        return []
    games: list[str] = []
    for chunk in _EVENT_BOUNDARY.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            game = chess.pgn.read_game(io.StringIO(chunk))
        except Exception:
            game = None
        if game is None or game.next() is None:  # unparseable or no moves
            continue
        games.append(chunk + "\n")
    return games


def headers_of(game_pgn: str) -> dict:
    """The tag-pair headers of a single game PGN (empty dict if unreadable)."""
    try:
        headers = chess.pgn.read_headers(io.StringIO(game_pgn))
    except Exception:
        headers = None
    return dict(headers) if headers else {}


def detect_self_handle(games: list[str], prefer: list[str] | None = None) -> str | None:
    """The handle that appears (as White or Black) in *every* game — i.e. the uploader.

    In a personal export you are in all of your games, so the intersection of the players across
    games is (usually) just you. Returns the original-case handle. If a `prefer` handle (e.g. the
    configured CHESS_USERNAME / aliases) is among the common set, it wins; otherwise a single
    unambiguous common handle is returned, else None (caller falls back to per-game auto-detect).
    """
    if not games:
        return None
    per_game: list[dict[str, str]] = []  # lowercased handle -> original case, per game
    for g in games:
        h = headers_of(g)
        names: dict[str, str] = {}
        for key in ("White", "Black"):
            raw = (h.get(key) or "").strip()
            if raw:
                names[raw.lower()] = raw
        per_game.append(names)

    common: set[str] | None = None
    for names in per_game:
        common = set(names) if common is None else (common & set(names))
    common = common or set()
    if not common:
        return None

    def original_case(handle_lc: str) -> str:
        for names in per_game:
            if handle_lc in names:
                return names[handle_lc]
        return handle_lc

    prefer_lc = {p.strip().lower() for p in (prefer or []) if p and p.strip()}
    for c in common:
        if c in prefer_lc:
            return original_case(c)
    if len(common) == 1:
        return original_case(next(iter(common)))
    return None  # ambiguous (e.g. every game vs the same opponent)
