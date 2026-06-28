"""Endgame tablebase probe via the public Lichess tablebase API (Syzygy, <=7 men).

In the endgame a depth-limited eval is fuzzy — "+1.2" tells the coach almost nothing about
whether a position is actually *won*. A tablebase gives the EXACT theoretical result
(win/draw/loss) plus distance-to-zeroing/mate for any position with <=7 pieces, so coaching in
those positions is precise instead of a guess. We hit the free public Lichess tablebase
(https://tablebase.lichess.ovh) — same dependency (`httpx`) and best-effort posture as
`lichess.py`: any failure (offline, rate limit, parse) returns None and the caller simply omits
the fact. Only the chat / coach-summary path probes (it passes `probe_tablebase=True` to
`engine_line`); the interactive board never does, so the hot path stays network-free.

`CHESS_TABLEBASE=0` disables it entirely.
"""
from __future__ import annotations

import chess
import httpx

from server import config

# Syzygy tablebases cover up to 7 pieces (kings included).
PIECE_LIMIT = 7

# Categories the Lichess API returns, all from the side-to-move's perspective.
_VALID = {
    "win",
    "cursed-win",
    "maybe-win",
    "draw",
    "blessed-loss",
    "maybe-loss",
    "loss",
    "unknown",
}

# Mirror a result to the opposite side's perspective: a win for one side is a loss for the other,
# a 50-move-rule "cursed win" becomes a "blessed loss", a draw stays a draw. DTZ/DTM flip sign.
_FLIP = {
    "win": "loss",
    "loss": "win",
    "cursed-win": "blessed-loss",
    "blessed-loss": "cursed-win",
    "maybe-win": "maybe-loss",
    "maybe-loss": "maybe-win",
    "draw": "draw",
    "unknown": "unknown",
}

# Successful probes only (a transient failure must NOT be cached as a permanent "no data").
_cache: dict[str, dict] = {}


def count_men(board: chess.Board) -> int:
    """Total pieces on the board (kings included) — the tablebase coverage check."""
    return chess.popcount(board.occupied)


def _normalize(data: dict, men: int) -> dict | None:
    cat = (data.get("category") or "unknown").strip()
    if cat not in _VALID:
        cat = "unknown"
    return {
        "category": cat,
        "dtz": data.get("dtz"),
        "dtm": data.get("dtm"),
        "men": men,
        "checkmate": bool(data.get("checkmate")),
        "stalemate": bool(data.get("stalemate")),
    }


def probe(fen: str) -> dict | None:
    """Exact tablebase result for `fen` (side-to-move perspective), or None.

    None on: tablebase disabled, an unparseable FEN, more than `PIECE_LIMIT` pieces, or any
    network/parse failure. Successful lookups are memoised for the process lifetime.
    """
    if not config.TABLEBASE_ENABLED or not fen:
        return None
    if fen in _cache:
        return _cache[fen]
    try:
        board = chess.Board(fen)
    except ValueError:
        return None
    men = count_men(board)
    if men > PIECE_LIMIT:
        return None
    try:
        resp = httpx.get(
            f"{config.TABLEBASE_API_BASE}/standard",
            params={"fen": fen},
            headers={"User-Agent": "chess-analysis-mcp"},
            timeout=config.TABLEBASE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    result = _normalize(data, men)
    if result is not None:
        _cache[fen] = result
    return result


def flip(result: dict | None) -> dict | None:
    """Re-express a probe result from the opposite side's perspective (None passes through)."""
    if not result:
        return None
    out = dict(result)
    out["category"] = _FLIP.get(result.get("category", "unknown"), "unknown")
    out["dtz"] = -result["dtz"] if result.get("dtz") is not None else None
    out["dtm"] = -result["dtm"] if result.get("dtm") is not None else None
    return out
