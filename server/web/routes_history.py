"""History + game-import + background-analysis routes for the web board.

Powers the board's third column: list previously-analysed local games (`/api/history`), browse a
Lichess user's recent games (`/api/lichess/games`), and reopen any of them by kicking off a
background analysis (`/api/analyze` + `/api/analysis-status`). Sync `def` handlers like
routes_board — they're light (history is a file read; lichess is a short HTTP call; analyze just
spawns a thread).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.core import game_analysis
from server.core import history
from server.core import lichess
from server.core import multipgn
from server.web import jobs

router = APIRouter()


class AnalyzeBody(BaseModel):
    pgn: str
    player: str = "auto"


class AnalyzeBatchBody(BaseModel):
    pgn: str
    player: str = "auto"
    username: str = ""  # the uploader's handle; blank -> auto-detect (handle common to all games)


def _side_for(headers: dict, self_handle: str | None, player: str) -> str:
    """Which side to review for one game: the uploader's handle if present, else the chosen
    player, else auto-detect from configured handles."""
    if self_handle:
        sh = self_handle.strip().lower()
        if (headers.get("White") or "").strip().lower() == sh:
            return "white"
        if (headers.get("Black") or "").strip().lower() == sh:
            return "black"
    if (player or "").lower() in ("white", "black"):
        return player.lower()
    return game_analysis.resolve_player(headers, "auto")


@router.get("/history")
def get_history() -> dict:
    """Newest-first list of the configured user's previously-analysed games (for the panel)."""
    try:
        rows = history.history_rows(player_id=history.my_player_id())
    except Exception as exc:  # pragma: no cover - history must never break the board
        return {"games": [], "error": str(exc)}
    return {"player_id": history.my_player_id(), "games": rows}


@router.get("/lichess/games")
def get_lichess_games(username: str = "", max: int = config.LICHESS_DEFAULT_MAX, perf: str = "") -> JSONResponse:
    """Recent Lichess games (newest first) for `username` (blank -> configured CHESS_USERNAME)."""
    try:
        games = lichess.fetch_user_games(username, max=max, perf=perf or None)
    except lichess.LichessError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"count": len(games), "games": [g.to_dict() for g in games]})


@router.post("/analyze")
def post_analyze(body: AnalyzeBody) -> JSONResponse:
    """Start a background analysis of `pgn` (reviewing `player`); returns immediately as pending."""
    if not (body.pgn or "").strip():
        return JSONResponse({"error": "No PGN provided."}, status_code=400)
    return JSONResponse(jobs.start(body.pgn, player=body.player or "auto"))


@router.post("/analyze-batch")
def post_analyze_batch(body: AnalyzeBatchBody) -> JSONResponse:
    """Analyse a multi-game PGN (e.g. a Chess.com export) in the background, recording each game so
    the whole upload appears in "My games". Returns immediately with the game count + the first
    game (so the board can show it while the rest run)."""
    games = multipgn.split_pgn(body.pgn or "")
    if not games:
        return JSONResponse({"error": "No valid games found in that PGN."}, status_code=400)

    prefer = [config.USERNAME] + [a for _, a in config.USERNAME_ALIASES]
    self_handle = (body.username or "").strip() or multipgn.detect_self_handle(games, prefer=prefer)
    first_headers = multipgn.headers_of(games[0])
    platform = history._platform_from_headers(first_headers)
    sides = [_side_for(multipgn.headers_of(g), self_handle, body.player) for g in games]

    jobs.start_batch(games, sides, self_handle=self_handle, platform=platform)
    return JSONResponse(
        {
            "status": "pending",
            "total_games": len(games),
            "first_pgn": games[0],
            "first_side": sides[0],
            "self_handle": self_handle,
        }
    )


@router.get("/analysis-status")
def get_analysis_status() -> dict:
    """Poll target while a background analysis runs: idle | pending | ready | error."""
    return jobs.status()
