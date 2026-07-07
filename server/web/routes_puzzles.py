"""Puzzle-trainer API routes (all under /api/puzzle).

Sync handlers, like the board routes: selection/validation is pure-Python (engine-free) and the
optional coach spawns `claude -p` in the threadpool. Everything is wrapped so a puzzle bug can
never break the analysis board — a failure returns `{error}` rather than raising. The request
middleware already calls `lifecycle.touch()`, so handlers don't.
"""
from __future__ import annotations

import os
import shutil

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server import claude_bridge
from server.core import local_llm
from server.core import puzzle_rating
from server.core import puzzle_session
from server.core import puzzles as puzzles_mod

router = APIRouter()


def _has_engine() -> bool:
    path = config.STOCKFISH_PATH
    return bool(shutil.which(path) or ("/" in path and os.path.exists(path)))


def _has_llm() -> bool:
    return bool(local_llm.is_enabled() or shutil.which("claude"))


def _disabled() -> JSONResponse:
    return JSONResponse({"error": "Puzzle mode is disabled."}, status_code=404)


@router.get("/puzzle/config")
def puzzle_config() -> dict:
    """Gate the frontend: whether puzzles exist + the user's rating/streak + engine/LLM presence."""
    if not config.PUZZLES_ENABLED:
        return {"enabled": False}
    state = puzzle_rating.load_state()
    return {
        "enabled": bool(puzzles_mod._baseline()),
        "your_rating": int(round(state["rating"])),
        "rd": int(round(state["rd"])),
        "streak": state.get("streak", 0),
        "best_streak": state.get("best_streak", 0),
        "themes_available": puzzles_mod.available_themes(),
        "has_engine": _has_engine(),
        "has_llm": _has_llm(),
    }


@router.get("/puzzle/next")
def puzzle_next(theme: str | None = None, difficulty: str | None = None) -> JSONResponse:
    """Select a puzzle near the user's rating; auto-play the setup move; never reveal the solution."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    state = puzzle_rating.load_state()
    themes = [t.strip() for t in theme.split(",") if t.strip()] if theme else None
    puzzle = puzzles_mod.next_puzzle(
        state["rating"],
        themes=themes,
        exclude=set(state.get("seen_ids", [])),
        seed=state.get("user_seed"),
        difficulty=difficulty,
    )
    if not puzzle:
        return JSONResponse({"error": "No puzzles available."}, status_code=404)

    puzzle_rating.mark_seen(state, puzzle["id"])
    puzzle_rating.save_state(state)
    puzzle_session.set_current(puzzle)

    return JSONResponse({
        "id": puzzle["id"],
        "fen": puzzle["fen"],
        "setup_move": puzzle["moves"][0] if puzzle.get("moves") else None,
        "solve_fen": puzzle.get("solve_fen"),
        "side_to_move": puzzle.get("side_to_move", "white"),
        "themes": puzzle.get("themes", []),
        "rating": int(round(float(puzzle.get("rating", 1500)))),
        "your_rating": int(round(state["rating"])),
        "game_url": puzzle.get("game_url"),
    })


class MoveBody(BaseModel):
    id: str
    uci: str


def _score_attempt(prog, *, score: float, hinted_or_given: bool) -> dict:
    """Apply one terminal outcome to the rating state at most once. Returns a rating summary."""
    puzzle = prog.puzzle
    state = puzzle_rating.load_state()
    rd_ok = float(puzzle.get("rd", 999)) < config.PUZZLE_MAX_RD
    rated = rd_ok and prog.hints_used == 0 and not hinted_or_given
    summary = puzzle_rating.record_result(
        state,
        puzzle_id=puzzle.get("id", ""),
        puzzle_rating=float(puzzle.get("rating", 1500)),
        puzzle_rd=float(puzzle.get("rd", 60)),
        themes=puzzle.get("themes", []),
        score=score,
        rated=rated,
    )
    puzzle_rating.save_state(state)
    prog.scored = True
    return summary


@router.post("/puzzle/move")
def puzzle_move(body: MoveBody) -> JSONResponse:
    """Validate the solver's move at the current step; advance, fail, or complete the puzzle."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    prog = puzzle_session.get_current()
    if prog is None or prog.id != body.id:
        return JSONResponse({"error": "No active puzzle (load one first)."}, status_code=409)

    result = puzzles_mod.validate_step(prog.puzzle, prog.ply_index, body.uci)
    moves = prog.puzzle.get("moves", [])
    # Remember what was tried (and from where) so the coach can analyse it, engine-grounded.
    prog.tried.append({
        "uci": body.uci,
        "fen_before": puzzles_mod.position_fen(prog.puzzle, prog.ply_index),
        "correct": result["correct"],
        "ply_index": prog.ply_index,
    })

    if not result["correct"]:
        prog.attempts += 1
        summary = None
        # The first wrong move costs the rating (Lichess-style), but we do NOT reveal the solution:
        # the user can keep trying, or press "Show solution". So no expected/solution in the response.
        if not prog.scored:
            prog.failed = True
            summary = _score_attempt(prog, score=0.0, hinted_or_given=False)
        return JSONResponse({"correct": False, "is_complete": False, "can_retry": True, "rating": summary})

    if result["is_complete"]:
        prog.finished = True
        summary = None
        if not prog.scored:
            solved_clean = not prog.failed and prog.hints_used == 0
            summary = _score_attempt(
                prog,
                score=1.0 if solved_clean else 0.0,
                hinted_or_given=prog.hints_used > 0,
            )
        return JSONResponse({"correct": True, "is_complete": True, "rating": summary})

    # Correct but more to come: skip past the solver move + the forced opponent reply.
    prog.ply_index += 2
    return JSONResponse({
        "correct": True,
        "is_complete": False,
        "opponent_reply_uci": result.get("opponent_reply_uci"),
    })


class PuzzleIdBody(BaseModel):
    id: str


@router.post("/puzzle/hint")
def puzzle_hint(body: PuzzleIdBody) -> JSONResponse:
    """Reveal the piece to move at the current step (and make the attempt unrated)."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    prog = puzzle_session.get_current()
    if prog is None or prog.id != body.id:
        return JSONResponse({"error": "No active puzzle."}, status_code=409)
    moves = prog.puzzle.get("moves", [])
    if prog.ply_index >= len(moves):
        return JSONResponse({"error": "Nothing to hint."}, status_code=400)
    prog.hints_used += 1
    expected = moves[prog.ply_index]
    return JSONResponse({"from_square": expected[:2], "rated": False})


@router.post("/puzzle/giveup")
def puzzle_giveup(body: PuzzleIdBody) -> JSONResponse:
    """Reveal the full remaining solution and score the puzzle 0 (unrated)."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    prog = puzzle_session.get_current()
    if prog is None or prog.id != body.id:
        return JSONResponse({"error": "No active puzzle."}, status_code=409)
    if not prog.scored:
        prog.failed = True
        _score_attempt(prog, score=0.0, hinted_or_given=True)
    prog.finished = True
    moves = prog.puzzle.get("moves", [])
    return JSONResponse({
        "solution_uci": moves[prog.ply_index:],
        "solution_san": puzzles_mod.solution_san(prog.puzzle),
    })


@router.get("/puzzle/state")
def puzzle_state() -> dict:
    """Rating curve + per-theme stats for a stats card."""
    state = puzzle_rating.load_state()
    return {
        "rating": int(round(state["rating"])),
        "rd": int(round(state["rd"])),
        "streak": state.get("streak", 0),
        "best_streak": state.get("best_streak", 0),
        "by_theme": state.get("by_theme", {}),
        "history": state.get("history", [])[-50:],
    }


@router.get("/puzzle/current")
def puzzle_current() -> JSONResponse:
    """The in-progress puzzle, so a browser reload can resume the same puzzle at the same spot.

    Returns the position the solver is currently facing (the forced line replayed up to the current
    ply) plus the solving colour, never the solution. `{active: false}` if nothing is in progress
    (e.g. the server restarted) -> the frontend just loads a fresh puzzle.
    """
    if not config.PUZZLES_ENABLED:
        return JSONResponse({"active": False})
    prog = puzzle_session.get_current()
    if prog is None:
        return JSONResponse({"active": False})
    p = prog.puzzle
    state = puzzle_rating.load_state()
    enriched = puzzles_mod._with_side(p)  # gives the solver's colour
    return JSONResponse({
        "active": True,
        "finished": prog.finished,
        "id": p.get("id"),
        "fen": puzzles_mod.position_fen(p, prog.ply_index),  # the spot to resume at
        "side_to_move": enriched.get("side_to_move", "white"),
        "themes": p.get("themes", []),
        "rating": int(round(float(p.get("rating", 1500)))),
        "your_rating": int(round(state["rating"])),
        "failed": prog.failed,
        "hinted": prog.hints_used > 0,
        "game_url": p.get("game_url"),
    })


class ExplainBody(BaseModel):
    id: str
    outcome: str  # solved_first_try | solved_with_hints | failed
    your_move: str | None = None


@router.post("/puzzle/explain")
def puzzle_explain(body: ExplainBody) -> JSONResponse:
    """The puzzle coach: name the motif (solved) or refute the user's move then teach (failed)."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    prog = puzzle_session.get_current()
    in_session = bool(prog and prog.id == body.id)
    puzzle = prog.puzzle if in_session else puzzles_mod.get_puzzle(body.id)
    if not puzzle:
        return JSONResponse({"error": "Unknown puzzle."}, status_code=404)
    tried = prog.tried if in_session else None
    try:
        answer = claude_bridge.explain_puzzle(
            puzzle, body.outcome, your_move=body.your_move, tried=tried
        )
    except claude_bridge.ChatError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"answer": answer})
