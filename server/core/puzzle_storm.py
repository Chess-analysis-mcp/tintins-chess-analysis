"""Puzzle storm - a timed "solve as many as you can" rush (P4).

A single in-process run (like `puzzle_session`, a process singleton) with a countdown clock. It
reuses the whole tactic-selection substrate (`puzzles.next_puzzle`) and the shared `puzzle_session`
so the board renders storm puzzles the same way, but it is deliberately **unrated**: it never
touches Glicko, only a personal `storm_high` / `storm_best_combo` in `state.json`.

Mechanics (Lichess-storm-inspired, tuned by the constants below):
  - A base clock (`config.PUZZLE_STORM_DURATION`, default 180s).
  - Each solved puzzle scores +1 and grows a combo; every `_COMBO_BONUS_EVERY` in a row adds
    `_COMBO_TIME_BONUS` seconds. A wrong move breaks the combo and costs `_WRONG_PENALTY` seconds.
  - Difficulty ramps up as you go (`_RAMP_PER_SOLVE`), starting a little below your rating.
  - The run ends when the clock hits zero; the highscore is persisted then.

Best-effort throughout: a storm bug must never break the board. `now` is injectable on every entry
point so the whole thing is unit-testable with a fake clock (no sleeping, no wall-time flakiness).
"""
from __future__ import annotations

import time
from typing import Optional

from .. import config
from . import puzzle_rating
from . import puzzle_session
from . import puzzles as puzzles_mod

# Scoring / clock tuning.
_COMBO_BONUS_EVERY = 5       # a bonus every N consecutive solves
_COMBO_TIME_BONUS = 5.0      # seconds added on a combo milestone
_WRONG_PENALTY = 8.0         # seconds lost on a wrong move
_RAMP_PER_SOLVE = 12.0       # target-rating rise per solve (harder as you go)
_START_OFFSET = -150.0       # start a little below the solver's rating (a gentle warm-up)
_RATING_FLOOR, _RATING_CEIL = 600.0, 2600.0


class StormRun:
    """One timed rush. Holds the clock, the running score/combo, and repeat-avoidance for the run."""

    def __init__(self, base_rating: float, seed: int, started_at: float, duration: int) -> None:
        self.base_rating = float(base_rating)
        self.seed = int(seed)
        self.started_at = started_at
        self.deadline = started_at + duration
        self.score = 0            # puzzles solved
        self.combo = 0            # current consecutive solves
        self.best_combo = 0
        self.misses = 0
        self.results: list[bool] = []   # per resolved puzzle (True solved / False missed)
        # One record per resolved puzzle so the run can be reviewed with the AI coach after time-up
        # (id/fen/themes/rating + whether it was solved + the move the solver played). Kept until the
        # run is cleared (leaving storm / starting a new run), so a post-run review is refresh-safe.
        self.log: list[dict] = []
        self.run_seen: set[str] = set()  # ids served THIS run (not the global seen set - storm is casual)
        self.ended = False

    def remaining(self, now: float) -> float:
        return max(0.0, round(self.deadline - now, 1))

    def _target_rating(self) -> float:
        target = self.base_rating + _START_OFFSET + self.score * _RAMP_PER_SOLVE
        return max(_RATING_FLOOR, min(_RATING_CEIL, target))


_RUN: Optional[StormRun] = None


def _now(now: Optional[float]) -> float:
    return time.time() if now is None else now


def _log_entry(puzzle: dict, *, solved: bool, your_move: Optional[str]) -> dict:
    """A compact, solution-free record of one resolved storm puzzle for the post-run AI review."""
    themes = [t for t in (puzzle.get("themes") or []) if t]
    return {
        "id": puzzle.get("id"),
        "fen": puzzle.get("solve_fen") or puzzle.get("fen"),  # the position the solver answered from
        "side_to_move": puzzle.get("side_to_move", "white"),
        "themes": themes,
        "rating": int(round(float(puzzle.get("rating", 1500) or 1500))),
        "solved": bool(solved),
        "your_move": your_move,  # the solver's move (the wrong one on a miss) — grounds the coach
    }


def get_run() -> Optional[StormRun]:
    return _RUN


def start(state: dict, *, now: Optional[float] = None) -> dict:
    """Begin a fresh storm run and serve the first puzzle. Counts as a day of practice."""
    global _RUN
    t = _now(now)
    seed = (int(state.get("user_seed", 0)) ^ int(t)) & 0x7FFFFFFF
    _RUN = StormRun(state.get("rating", 1500.0), seed, t, config.PUZZLE_STORM_DURATION)
    puzzle_rating.touch_daily_streak(state)
    puzzle_rating.save_state(state)
    first = _serve(_RUN, now=t)
    view = state_view(now=t)
    view["puzzle"] = first
    return view


def _serve(run: StormRun, *, now: float) -> Optional[dict]:
    """Pick the next puzzle for the run, set the shared session, and return the solver's view."""
    puzzle = puzzles_mod.next_puzzle(
        run._target_rating(),
        exclude=run.run_seen,
        seed=run.seed + run.score,  # vary order as the run progresses
        rd=None,
    )
    if not puzzle:
        return None
    run.run_seen.add(puzzle["id"])
    puzzle_session.set_current(puzzle)
    return {
        "id": puzzle["id"],
        "fen": puzzle.get("solve_fen") or puzzle["fen"],
        "side_to_move": puzzle.get("side_to_move", "white"),
        "themes": puzzle.get("themes", []),
        "rating": int(round(float(puzzle.get("rating", 1500)))),
    }


def next_puzzle(state: dict, *, now: Optional[float] = None) -> dict:
    """Serve the next puzzle after the current one resolved. Ends the run if the clock is up."""
    t = _now(now)
    run = _RUN
    if run is None:
        return {"ended": True, "active": False}
    if run.remaining(t) <= 0:
        return _finish(run, state, now=t)
    puzzle = _serve(run, now=t)
    view = state_view(now=t)
    view["puzzle"] = puzzle
    if puzzle is None:  # exhausted the pool (very unlikely) - end gracefully
        return _finish(run, state, now=t)
    return view


def submit_move(state: dict, uci: str, *, now: Optional[float] = None) -> dict:
    """Validate one solver move in the current storm puzzle and apply storm scoring.

    Returns per-move progress plus the live storm state. `puzzle_done` marks that the current puzzle
    resolved (solved or missed) so the caller should request the next one; a correct-but-not-final
    move returns the forced `opponent_reply_uci` and keeps the same puzzle.
    """
    t = _now(now)
    run = _RUN
    if run is None or run.ended:
        return {"error": "No active storm.", "ended": True}
    if run.remaining(t) <= 0:
        return _finish(run, state, now=t)

    prog = puzzle_session.get_current()
    if prog is None:
        return {"error": "No active puzzle."}

    result = puzzles_mod.validate_step(prog.puzzle, prog.ply_index, uci)

    if not result["correct"]:
        run.combo = 0
        run.misses += 1
        run.results.append(False)
        run.log.append(_log_entry(prog.puzzle, solved=False, your_move=uci))
        run.deadline -= _WRONG_PENALTY  # a wrong move costs time
        prog.finished = True
        out = {"correct": False, "puzzle_done": True, "solved": False}
        out.update(state_view(now=t))
        if run.remaining(t) <= 0:
            return _finish(run, state, now=t, extra=out)
        return out

    if result["is_complete"]:
        run.score += 1
        run.combo += 1
        run.best_combo = max(run.best_combo, run.combo)
        run.results.append(True)
        run.log.append(_log_entry(prog.puzzle, solved=True, your_move=uci))
        bonus = 0.0
        if run.combo % _COMBO_BONUS_EVERY == 0:
            bonus = _COMBO_TIME_BONUS
            run.deadline += bonus
        prog.finished = True
        out = {"correct": True, "puzzle_done": True, "solved": True, "time_bonus": bonus}
        out.update(state_view(now=t))
        return out

    # Correct but more to come: auto-play the forced reply and stay on this puzzle.
    prog.ply_index += 2
    out = {
        "correct": True,
        "puzzle_done": False,
        "solved": False,
        "opponent_reply_uci": result.get("opponent_reply_uci"),
    }
    out.update(state_view(now=t))
    return out


def state_view(*, now: Optional[float] = None) -> dict:
    """The live storm state (score, combo, remaining time), for the timer/scoreboard."""
    t = _now(now)
    run = _RUN
    if run is None:
        return {"active": False, "ended": True}
    return {
        "active": not run.ended,
        "ended": run.ended,
        "score": run.score,
        "combo": run.combo,
        "best_combo": run.best_combo,
        "misses": run.misses,
        "remaining": run.remaining(t),
        "duration": config.PUZZLE_STORM_DURATION,
        "results": run.results[-30:],
    }


def _finish(run: StormRun, state: dict, *, now: float, extra: Optional[dict] = None) -> dict:
    """End the run, persist the highscore + best combo, and return the final scoreboard."""
    run.ended = True
    run.deadline = min(run.deadline, now)
    puzzle_session.clear_current()
    new_high = run.score > int(state.get("storm_high", 0) or 0)
    if new_high:
        state["storm_high"] = run.score
    state["storm_best_combo"] = max(int(state.get("storm_best_combo", 0) or 0), run.best_combo)
    puzzle_rating.save_state(state)
    view = {
        "active": False,
        "ended": True,
        "score": run.score,
        "combo": run.combo,
        "best_combo": run.best_combo,
        "misses": run.misses,
        "remaining": 0.0,
        "high": int(state.get("storm_high", 0) or 0),
        "new_high": new_high,
        "results": run.results[-30:],
        "log": list(run.log),  # the per-puzzle review list for the post-run AI coach
    }
    if extra:
        merged = dict(extra)
        merged.update(view)
        return merged
    return view


def clear() -> None:
    """Drop any active run (e.g. on leaving storm mode)."""
    global _RUN
    _RUN = None
