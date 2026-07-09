"""Puzzle-trainer API routes (all under /api/puzzle).

Sync handlers, like the board routes: selection/validation is pure-Python (engine-free) and the
optional coach spawns `claude -p` in the threadpool. Everything is wrapped so a puzzle bug can
never break the analysis board — a failure returns `{error}` rather than raising. The request
middleware already calls `lifecycle.touch()`, so handlers don't.
"""
from __future__ import annotations

import os
import random
import shutil

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config
from server import claude_bridge
from server.core import lines
from server.core import local_llm
from server.core import puzzle_flow
from server.core import puzzle_mistakes
from server.core import puzzle_rating
from server.core import puzzle_session
from server.core import puzzle_storm
from server.core import puzzles as puzzles_mod

router = APIRouter()


def _has_engine() -> bool:
    path = config.STOCKFISH_PATH
    if shutil.which(path):
        return True
    # A full/relative path that exists but which() didn't resolve (e.g. an extension PATHEXT missed,
    # or no exec bit) still counts — the engine pool launches it by path anyway. Detect a path-like
    # string with the OS separators, not a literal "/" (Windows uses "\\", which the old check missed
    # so a managed C:\...\stockfish.exe read as "no engine" and disabled From-your-games/interleave).
    looks_like_path = os.sep in path or bool(os.altsep and os.altsep in path)
    return bool(looks_like_path and os.path.exists(path))


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
        "daily_streak": state.get("daily_streak", 0),
        "best_daily_streak": state.get("best_daily_streak", 0),
        "storm_high": state.get("storm_high", 0),
        "storm_best_combo": state.get("storm_best_combo", 0),
        "storm_duration": config.PUZZLE_STORM_DURATION,
        "themes_available": puzzles_mod.available_themes(),
        "has_engine": _has_engine(),
        "has_llm": _has_llm(),
    }


def _mistake_puzzle_response(state: dict) -> JSONResponse:
    """Serve a 'from your games' mistake puzzle (P3.5). Gated on the engine (it validates moves)."""
    if not _has_engine():
        return JSONResponse(
            {"error": "Mistake puzzles need the chess engine (Stockfish) to check your moves."},
            status_code=409,
        )
    puzzle = puzzle_mistakes.next_mistake_puzzle(state)
    if not puzzle:
        return JSONResponse(
            {"error": "No mistake puzzles yet - analyse some of your games first."},
            status_code=404,
        )
    puzzle_session.set_current(puzzle)
    # Remember it so the next pick/skip serves a different position, then persist.
    puzzle_mistakes.mark_mistake_served(state, puzzle["key"])
    puzzle_rating.save_state(state)
    # Play in the opponent's preceding move (when we could reconstruct it) so the solver sees the
    # move that created the position, exactly like a curated puzzle's setup move. `solve_fen` stays
    # the mistake position (the engine validates from `puzzle["fen"]`, which is unchanged).
    setup_uci = puzzle.get("setup_uci")
    prev_fen = puzzle.get("prev_fen")
    start_fen = prev_fen if (setup_uci and prev_fen) else puzzle["fen"]
    return JSONResponse({
        "id": puzzle["id"],
        "source": "your_games",
        "fen": start_fen,
        "solve_fen": puzzle["fen"],  # the mistake position the solver actually answers from
        "setup_move": setup_uci if (setup_uci and prev_fen) else None,
        "setup_san": puzzle.get("setup_san") if (setup_uci and prev_fen) else None,
        "side_to_move": puzzle.get("side_to_move", "white"),
        "themes": puzzle.get("motifs", []),
        "your_rating": int(round(state["rating"])),
        "game_url": puzzle.get("game_url"),
        # The move the player actually made in that game — shown as a grey "you played" arrow.
        "played_uci": puzzle.get("played_uci"),
        "played_san": puzzle.get("played_san"),
        # Replay-link + badge metadata.
        "game_id": puzzle.get("game_id"),
        "reviewed_side": puzzle.get("reviewed_side"),
        "ply": puzzle.get("ply"),
        # How much win% the original mistake cost — shown small in the badge for context.
        "win_drop": round(float(puzzle.get("win_drop", 0.0) or 0.0), 1),
        "badge": {
            "white": puzzle.get("white"),
            "black": puzzle.get("black"),
            "speed": puzzle.get("speed"),
            "date": puzzle.get("date"),
        },
    })


@router.get("/puzzle/next")
def puzzle_next(
    theme: str | None = None,
    difficulty: str | None = None,
    weakness: bool = False,
    source: str = "lichess",
) -> JSONResponse:
    """Select a puzzle near the user's rating; auto-play the setup move; never reveal the solution.

    `weakness=1` biases selection toward the player's weak themes (game history + puzzle stats),
    overriding an explicit `theme`. `source=your_games` serves an engine-validated position from one
    of the player's own past games instead (unrated). The resolved themes are echoed as
    `trained_themes`.
    """
    if not config.PUZZLES_ENABLED:
        return _disabled()
    state = puzzle_rating.load_state()
    if source == "your_games":
        return _mistake_puzzle_response(state)
    # Occasionally swap a curated tactic for an own-game mistake puzzle (Settings-toggleable), so the
    # trainer surfaces the player's real weaknesses without them switching source. Only when the
    # engine is present (mistake puzzles validate live) and a mistake puzzle is actually available;
    # a difficulty override means the user is steering the curated stream, so leave it alone.
    if (
        config.PUZZLE_MISTAKE_INTERLEAVE
        and difficulty is None
        and _has_engine()
        and random.random() < config.PUZZLE_MISTAKE_INTERLEAVE_PROB
        and puzzle_mistakes.next_mistake_puzzle(state) is not None
    ):
        return _mistake_puzzle_response(state)
    if weakness:
        themes = puzzles_mod.weakness_themes(state) or None
    else:
        themes = [t.strip() for t in theme.split(",") if t.strip()] if theme else None
    puzzle = puzzles_mod.next_puzzle(
        state["rating"],
        themes=themes,
        exclude=set(state.get("seen_ids", [])),
        seed=state.get("user_seed"),
        difficulty=difficulty,
        rd=state.get("rd"),
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
        "trained_themes": themes or [],
    })


class MoveBody(BaseModel):
    id: str
    uci: str


def _score_attempt(prog, *, score: float, hinted_or_given: bool) -> dict:
    """Apply one terminal outcome to the rating state at most once. Returns a rating summary.

    Thin alias for the shared `puzzle_flow.score_attempt` so the board and the MCP tools rate an
    attempt by exactly the same rule.
    """
    return puzzle_flow.score_attempt(prog, score=score, hinted_or_given=hinted_or_given)


def _mistake_move(prog, uci: str) -> JSONResponse:
    """Validate a move for a 'from your games' mistake puzzle: accept any move whose win% drop is
    under the game's inaccuracy threshold (multi-solution). UNRATED - never touches Glicko."""
    puzzle = prog.puzzle
    result = lines.engine_line(puzzle["fen"], move=uci)
    move = result.get("move")
    if not move:  # illegal / unparseable move
        return JSONResponse({"correct": False, "is_complete": False, "can_retry": True,
                             "error": "Illegal move."})
    swing = float(move.get("win_swing", 99.0))
    accept = float(puzzle.get("accept_swing", 5.0))
    solved = swing < accept
    prog.tried.append({"uci": uci, "fen_before": puzzle["fen"], "correct": solved, "ply_index": 1})

    if solved:
        prog.finished = True
        if not prog.scored:
            state = puzzle_rating.load_state()
            puzzle_mistakes.record_practice_result(state, puzzle["key"], solved=True)
            puzzle_rating.save_state(state)
            prog.scored = True
        return JSONResponse({
            "correct": True, "is_complete": True, "source": "your_games",
            "win_swing": round(swing, 1), "better_move_san": move.get("better_move_san"),
            "is_engine_best": move.get("is_engine_best"),
        })

    prog.attempts += 1
    prog.failed = True
    return JSONResponse({
        "correct": False, "is_complete": False, "can_retry": True, "source": "your_games",
        "win_swing": round(swing, 1),
        "refutation_san": move.get("refutation_line_san", []),
        "refutation_uci": move.get("refutation_line_uci", []),
    })


@router.post("/puzzle/move")
def puzzle_move(body: MoveBody) -> JSONResponse:
    """Validate the solver's move at the current step; advance, fail, or complete the puzzle."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    prog = puzzle_session.get_current()
    if prog is None or prog.id != body.id:
        return JSONResponse({"error": "No active puzzle (load one first)."}, status_code=409)

    if prog.puzzle.get("source") == "your_games":
        return _mistake_move(prog, body.uci)

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
    # "From your games" mistake puzzles have no forced `moves` line — hint the engine's best move
    # (from the original analysis) instead. They're already unrated, so no rating consequence.
    if prog.puzzle.get("source") == "your_games":
        best = prog.puzzle.get("best_uci")
        if not best:
            return JSONResponse({"error": "No hint available for this position."}, status_code=400)
        prog.hints_used += 1
        return JSONResponse({"from_square": best[:2], "rated": False})
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

    if prog.puzzle.get("source") == "your_games":
        # Reveal the engine's best move (from the original analysis) + record a spaced-rep miss.
        # Never touches Glicko.
        if not prog.scored:
            prog.failed = True
            state = puzzle_rating.load_state()
            puzzle_mistakes.record_practice_result(state, prog.puzzle["key"], solved=False)
            puzzle_rating.save_state(state)
            prog.scored = True
        prog.finished = True
        best_uci = prog.puzzle.get("best_uci")
        best_san = prog.puzzle.get("best_san")
        return JSONResponse({
            "solution_uci": [best_uci] if best_uci else [],
            "solution_san": [best_san] if best_san else [],
        })

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
        "daily_streak": state.get("daily_streak", 0),
        "best_daily_streak": state.get("best_daily_streak", 0),
        "by_theme": state.get("by_theme", {}),
        # The "Work on" list: only trainable motifs (metadata tags like master/oneMove filtered out).
        "weak_themes": puzzles_mod.weak_theme_stats(state.get("by_theme", {})),
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

    if p.get("source") == "your_games":
        return JSONResponse({
            "active": True,
            "finished": prog.finished,
            "source": "your_games",
            "id": p.get("id"),
            "fen": p.get("fen"),  # the mistake position is the solve position (no forced line)
            "side_to_move": p.get("side_to_move", "white"),
            "themes": p.get("motifs", []),
            "your_rating": int(round(state["rating"])),
            "failed": prog.failed,
            "hinted": prog.hints_used > 0,
            "played_uci": p.get("played_uci"),
            "played_san": p.get("played_san"),
            "game_url": p.get("game_url"),
            "game_id": p.get("game_id"),
            "reviewed_side": p.get("reviewed_side"),
            "ply": p.get("ply"),
            "win_drop": round(float(p.get("win_drop", 0.0) or 0.0), 1),
            "badge": {
                "white": p.get("white"), "black": p.get("black"),
                "speed": p.get("speed"), "date": p.get("date"),
            },
        })

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
        result = claude_bridge.explain_puzzle(
            puzzle, body.outcome, your_move=body.your_move, tried=tried
        )
    except claude_bridge.ChatError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    # The position the follow-up chat should ground on: the solve position (mistake puzzles have
    # no forced line, so `fen` is it). `session_id` lets the chat thread onto this explanation.
    chat_fen = puzzle.get("solve_fen") or puzzle.get("fen")
    return JSONResponse({
        "answer": result.get("answer", ""),
        "session_id": result.get("session_id"),
        "chat_fen": chat_fen,
    })


# --- Puzzle storm (timed rush, P4) --------------------------------------------------------------
# Unrated: reuses the tactic selection + shared session but never touches Glicko. All best-effort.


@router.post("/puzzle/storm/start")
def storm_start() -> JSONResponse:
    """Begin a timed storm run and serve the first puzzle."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    if not puzzles_mod._baseline():
        return JSONResponse({"error": "No puzzles available."}, status_code=404)
    state = puzzle_rating.load_state()
    return JSONResponse(puzzle_storm.start(state))


class StormMoveBody(BaseModel):
    uci: str


@router.post("/puzzle/storm/move")
def storm_move(body: StormMoveBody) -> JSONResponse:
    """Submit one move in the current storm puzzle (storm scoring; never rated)."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    state = puzzle_rating.load_state()
    return JSONResponse(puzzle_storm.submit_move(state, body.uci))


@router.get("/puzzle/storm/next")
def storm_next() -> JSONResponse:
    """Serve the next storm puzzle once the current one has resolved."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    state = puzzle_rating.load_state()
    return JSONResponse(puzzle_storm.next_puzzle(state))


@router.get("/puzzle/storm/state")
def storm_state() -> JSONResponse:
    """The live storm scoreboard + personal bests (for the timer/scoreboard + a between-runs card)."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    state = puzzle_rating.load_state()
    view = puzzle_storm.state_view()
    view["high"] = state.get("storm_high", 0)
    view["best_combo_ever"] = state.get("storm_best_combo", 0)
    return JSONResponse(view)


@router.get("/puzzle/storm/review")
def storm_review() -> JSONResponse:
    """The finished run's per-puzzle log (id/fen/themes/result), for the post-run review list.

    Refresh-safe: the run object lingers (ended) until the user starts a new run or leaves storm, so
    the game-over review survives a page reload. Empty when there's no run to review.
    """
    if not config.PUZZLES_ENABLED:
        return _disabled()
    run = puzzle_storm.get_run()
    return JSONResponse({"log": list(run.log) if run is not None else []})


@router.get("/puzzle/solution")
def puzzle_solution(id: str) -> JSONResponse:
    """A curated puzzle's solver line, so a FINISHED puzzle can be stepped through on the board.

    Reveal-on-demand: only fetched once a puzzle is resolved (a solve/Show-solution in the Solve
    trainer, or a post-run Storm review), so a live puzzle never carries its solution.
    `solution_uci`/`solution_san` start at the solver's first move (the setup move that reaches
    `solve_fen` is omitted). Unknown id (e.g. a "from your games" mistake puzzle) -> 404.
    """
    if not config.PUZZLES_ENABLED:
        return _disabled()
    puzzle = puzzles_mod.get_puzzle(id)
    if not puzzle:
        return JSONResponse({"error": "Unknown puzzle."}, status_code=404)
    moves = puzzle.get("moves", []) or []
    return JSONResponse({
        "solve_fen": puzzle.get("solve_fen") or puzzles_mod.position_fen(puzzle, 1),
        "solution_uci": moves[1:],
        "solution_san": puzzles_mod.solution_san(puzzle)[1:],
    })


@router.post("/puzzle/storm/summary")
def storm_summary() -> JSONResponse:
    """One Claude-written recap of the just-finished run: the recurring weak themes across misses."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    if not _has_llm():
        return JSONResponse(
            {"error": "The AI coach is unavailable (no `claude` CLI or local model)."},
            status_code=503,
        )
    run = puzzle_storm.get_run()
    if run is None or not run.log:
        return JSONResponse({"error": "No finished run to summarize."}, status_code=404)
    try:
        result = claude_bridge.summarize_storm_run(run.log)
    except claude_bridge.ChatError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"answer": result.get("answer", ""), "session_id": result.get("session_id")})


@router.post("/puzzle/storm/end")
def storm_end() -> JSONResponse:
    """Abandon the current run (leaving storm mode). Persists the highscore reached so far."""
    if not config.PUZZLES_ENABLED:
        return _disabled()
    run = puzzle_storm.get_run()
    if run is not None and not run.ended:
        state = puzzle_rating.load_state()
        # Fold the reached score into the highscore before dropping the run.
        if run.score > int(state.get("storm_high", 0) or 0):
            state["storm_high"] = run.score
        state["storm_best_combo"] = max(int(state.get("storm_best_combo", 0) or 0), run.best_combo)
        puzzle_rating.save_state(state)
    puzzle_storm.clear()
    return JSONResponse({"ended": True})
