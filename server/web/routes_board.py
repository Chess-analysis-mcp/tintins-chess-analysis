"""Board API routes.

Handlers are plain `def` (not `async def`) on purpose: the engine calls are blocking, so
Starlette runs these in its threadpool and they don't stall the event loop. They read/write
the same singleton ReviewSession + engine pool the MCP tools use.
"""
from __future__ import annotations

import chess
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server.core import app_liveness
from server.core import lines
from server.core import local_llm
from server.core import session as session_mod

router = APIRouter()

# Cached internet-reachability probe (see /connectivity). A miss means "offline", which gates the
# closable banner warning that the Lichess/tablebase/Claude network features won't work. Kept cheap:
# one short HEAD per TTL window, best-effort (any failure -> offline), never raised to the page.
_CONN_CACHE: dict[str, float | bool] = {}
_CONN_TTL = 30.0  # seconds


def _probe_online() -> bool:
    import time

    import httpx

    now = time.monotonic()
    cached_at = _CONN_CACHE.get("checked_at")
    if isinstance(cached_at, float) and (now - cached_at) < _CONN_TTL:
        return bool(_CONN_CACHE.get("online"))
    online = False
    try:
        # Probe the Lichess host the network features actually depend on; a quick HEAD is enough.
        resp = httpx.head(config.LICHESS_API_BASE, timeout=3.0, follow_redirects=True)
        online = resp.status_code < 500
    except Exception:  # noqa: BLE001 - any failure (DNS/timeout/refused) means "treat as offline"
        online = False
    _CONN_CACHE["checked_at"] = now
    _CONN_CACHE["online"] = online
    return online


@router.post("/ping")
def post_ping() -> dict:
    """App-mode heartbeat (backstop): the open tab calls this periodically. A long silence means the
    tab is gone; the app-liveness watchdog then shuts the standalone server down. No-op otherwise."""
    app_liveness.beat()
    return {"ok": True}


@router.post("/closing")
def post_closing() -> dict:
    """App-mode close beacon: the tab fires this on `pagehide` (close/refresh). After a short grace
    with no heartbeat (i.e. not a refresh) the server exits. Sent via navigator.sendBeacon."""
    app_liveness.closing()
    return {"ok": True}


class FenBody(BaseModel):
    fen: str


class EvaluateBody(BaseModel):
    fen: str
    move: str


class BestMovesBody(BaseModel):
    fen: str
    depth: int | None = None
    multipv: int = 3


@router.get("/doctor")
def get_doctor() -> dict:
    """Structured environment self-check for the UI setup banner (Python / Stockfish / claude CLI).
    Mirrors `python -m server.doctor`; best-effort, never raises."""
    from server import doctor

    try:
        return {"checks": doctor.status()}
    except Exception:  # noqa: BLE001 - a self-check must never break the page
        return {"checks": {}}


@router.get("/app-config")
def get_app_config() -> dict:
    """Frontend bootstrap: whether this is the standalone "app mode" launch (auto-load the user's
    most recent Lichess game on open) and the default username to use for that (CHESS_USERNAME)."""
    return {
        "app_mode": config.APP_MODE,
        "default_username": config.USERNAME or "",  # canonical "me" (Lichess if set, else chess.com)
        "lichess_username": config.LICHESS_USERNAME or "",  # autoloadable handle (drives first-run)
        "chesscom_username": config.CHESSCOM_USERNAME or "",  # configured chess.com handle (if any)
        "chesscom_sync": config.CHESSCOM_SYNC_ENABLED,  # auto-analyze new chess.com games on launch?
        "chesscom_sync_max": config.CHESSCOM_SYNC_MAX,  # how many recent games auto-sync checks
        "coach_ai_auto": config.COACH_AI_AUTO,  # auto-press the AI-summary button on each game?
        "personalize_history": config.PERSONALIZE_HISTORY,  # inject coaching profile into chat?
        "current_version": config.APP_VERSION,  # for the update notice (cheap, local)
        # Is the in-browser AI served by a local LLM (works offline) vs. Claude over the network?
        # Drives the offline banner's wording (AI still works offline only with a local LLM).
        "local_llm": local_llm.is_enabled(),
    }


@router.get("/connectivity")
def get_connectivity() -> dict:
    """Is the machine online? Drives a closable banner warning that the network-only features
    (Lichess game fetch + endgame tablebase, and Claude-backed AI when no local LLM is configured)
    won't work offline. Best-effort + cached; never raised to the page."""
    return {"online": _probe_online(), "local_llm": local_llm.is_enabled()}


@router.get("/session")
def get_session() -> dict:
    """The current review summary, or {empty: true} if nothing has been analysed yet."""
    sess = session_mod.get_session()
    if sess is None:
        return {"empty": True}
    summary = session_mod.summarize_session(sess)
    summary["explore_fen"] = sess.explore_fen
    # The raw PGN, so the board can re-analyse this same game from the other side without a refetch.
    # Web-only (kept off summarize_session so the MCP tool output stays compact).
    summary["pgn"] = sess.pgn
    # An already-generated AI coach summary (from this session or restored from cache), so reopening
    # a game shows it immediately instead of making the user press "Generate". Web-only.
    summary["coach_ai_text"] = sess.coach_ai_text
    return summary


@router.get("/timeline")
def get_timeline() -> dict:
    """The full per-node game timeline (eval/fen/move/best move) for graph + navigation."""
    sess = session_mod.get_session()
    if sess is None:
        return {"empty": True}
    return {"player": sess.player, "result": sess.result, "nodes": sess.timeline}


@router.post("/best-move")
def best_move(body: FenBody) -> JSONResponse:
    """Engine's top move for a position (used by the board's best-move arrow toggle)."""
    try:
        chess.Board(body.fen)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid FEN: {exc}"}, status_code=400)
    res = lines.engine_line(body.fen)
    return JSONResponse(
        {
            "uci": res["line_uci"][0] if res["line_uci"] else None,
            "san": res["best_san"],
            "win_percent": res["win_percent"],
            "side_to_move": res["side_to_move"],
        }
    )


@router.post("/best-moves")
def best_moves(body: BestMovesBody) -> JSONResponse:
    """Top-N engine moves (multipv) for a position, for the live best-move arrows.

    Called repeatedly at increasing depth by the board so the arrows refine over time.
    """
    try:
        chess.Board(body.fen)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid FEN: {exc}"}, status_code=400)

    depth = body.depth or config.DEFAULT_DEPTH
    info = lines.engine_line(body.fen, depth=depth, multipv=max(1, body.multipv))
    src = info.get("lines") or [
        {
            "line_uci": info["line_uci"],
            "line_san": info["line_san"],
            "win_percent": info["win_percent"],
            "eval": info["eval"],
        }
    ]
    moves = [
        {
            "uci": ln["line_uci"][0],
            "san": ln["line_san"][0] if ln.get("line_san") else None,
            "win_percent": ln["win_percent"],
            "eval": ln["eval"],
        }
        for ln in src
        if ln.get("line_uci")
    ]
    return JSONResponse({"side_to_move": info["side_to_move"], "depth": depth, "moves": moves})


@router.post("/threats")
def threats(body: BestMovesBody) -> JSONResponse:
    """Top-N threats in a position: the moves the side NOT to move is threatening to play.

    Implemented as a null-move search (the standard "threat" analysis): pass the turn to the
    side that just moved and ask the engine for its best moves. In check there is no legal
    null move (the "threat" is the check itself), so we return no threats.
    """
    try:
        board = chess.Board(body.fen)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid FEN: {exc}"}, status_code=400)
    if board.is_check() or board.is_game_over():
        return JSONResponse({"moves": []})
    board.push(chess.Move.null())

    depth = body.depth or config.DEFAULT_DEPTH
    info = lines.engine_line(board.fen(), depth=depth, multipv=max(1, body.multipv))
    src = info.get("lines") or [
        {
            "line_uci": info["line_uci"],
            "line_san": info["line_san"],
            "win_percent": info["win_percent"],
            "eval": info["eval"],
        }
    ]
    moves = [
        {
            "uci": ln["line_uci"][0],
            "san": ln["line_san"][0] if ln.get("line_san") else None,
            "win_percent": ln["win_percent"],
            "eval": ln["eval"],
        }
        for ln in src
        if ln.get("line_uci")
    ]
    return JSONResponse({"side_to_move": info["side_to_move"], "depth": depth, "moves": moves})


@router.get("/position/{index}")
def get_position(index: int) -> JSONResponse:
    """FEN one move before mistake `index`, plus metadata. Moves the review cursor."""
    res = session_mod.goto_core(index)
    status = 404 if "error" in res else 200
    return JSONResponse(res, status_code=status)


@router.post("/legal-moves")
def legal_moves(body: FenBody) -> JSONResponse:
    """Legal destination map for a FEN (parity/validation against client-side chess.js)."""
    try:
        board = chess.Board(body.fen)
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid FEN: {exc}"}, status_code=400)

    dests: dict[str, list[str]] = {}
    for mv in board.legal_moves:
        dests.setdefault(chess.square_name(mv.from_square), []).append(
            chess.square_name(mv.to_square)
        )
    return JSONResponse(
        {
            "dests": dests,
            "turn": "white" if board.turn == chess.WHITE else "black",
            "check": board.is_check(),
        }
    )


@router.post("/evaluate")
def evaluate(body: EvaluateBody) -> JSONResponse:
    """Evaluate a candidate move — the SAME path the terminal's get_engine_line uses."""
    try:
        chess.Board(body.fen)  # validate before handing to the engine
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid FEN: {exc}"}, status_code=400)
    return JSONResponse(lines.engine_line(body.fen, move=body.move))
