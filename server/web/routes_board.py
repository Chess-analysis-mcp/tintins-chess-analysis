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
from server.core import lines
from server.core import session as session_mod

router = APIRouter()


class FenBody(BaseModel):
    fen: str


class EvaluateBody(BaseModel):
    fen: str
    move: str


class BestMovesBody(BaseModel):
    fen: str
    depth: int | None = None
    multipv: int = 3


@router.get("/session")
def get_session() -> dict:
    """The current review summary, or {empty: true} if nothing has been analysed yet."""
    sess = session_mod.get_session()
    if sess is None:
        return {"empty": True}
    summary = session_mod.summarize_session(sess)
    summary["explore_fen"] = sess.explore_fen
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
