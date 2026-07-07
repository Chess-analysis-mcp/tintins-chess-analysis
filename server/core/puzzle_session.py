"""In-memory state for the puzzle currently being solved.

A process singleton, mirroring `session.py`'s ReviewSession, so the board and a future MCP tool
share one "current puzzle". Holds the puzzle plus solve progress (which ply the solver is on,
attempts, whether hints were used, and whether it has already been failed/scored).
"""

from __future__ import annotations

import time
from typing import Optional


class PuzzleProgress:
    """The puzzle in front of the user right now + how the attempt is going."""

    def __init__(self, puzzle: dict) -> None:
        self.puzzle: dict = puzzle
        self.ply_index: int = 1  # moves[0] is the auto-played setup; the solver starts at 1
        self.attempts: int = 0
        # Every move the solver submitted, in order: {uci, fen_before, correct, ply_index}. Lets the
        # coach discuss what the player actually tried (engine-grounded), not just the failing move.
        self.tried: list[dict] = []
        self.hints_used: int = 0
        self.failed: bool = False  # the solver played at least one wrong move (rating already lost)
        self.scored: bool = False  # guard so one puzzle moves the rating at most once
        self.finished: bool = False  # solving is over (solved, or the solution was revealed)
        self.started_at: float = time.time()

    @property
    def id(self) -> str:
        return self.puzzle.get("id", "")

    @property
    def first_try(self) -> bool:
        return self.attempts == 0 and self.hints_used == 0


_CURRENT: Optional[PuzzleProgress] = None


def set_current(puzzle: dict) -> PuzzleProgress:
    global _CURRENT
    _CURRENT = PuzzleProgress(puzzle)
    return _CURRENT


def get_current() -> Optional[PuzzleProgress]:
    return _CURRENT


def clear_current() -> None:
    global _CURRENT
    _CURRENT = None
