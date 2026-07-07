"""Puzzle loading, selection, and move validation for the tactical trainer.

Engine-free and best-effort, like `openings.py`: the vendored baseline shard
(`server/data/puzzles/baseline.jsonl.gz`, gzip JSONL) is loaded once and filtered in memory
(a few thousand dicts is trivial; no SQLite). Per-user variety comes from a seeded shuffle of the
candidate pool plus a served-`seen_ids` exclusion, so same-rating users get different,
non-repeating streams from identical static files.

Each puzzle dict (one JSONL line):
    {id, fen, moves:[uci...], rating, rd, themes:[...], popularity, nbplays, game_url}
`moves[0]` is the auto-played setup move (it creates the puzzle); the solver finds `moves[1:]`,
with opponent replies forced at the even indices.
"""

from __future__ import annotations

import functools
import gzip
import json
import random
from pathlib import Path
from typing import Iterable, Optional

import chess

_BASELINE = Path(__file__).resolve().parent.parent / "data" / "puzzles" / "baseline.jsonl.gz"

# Selection: start with a tight band around the user's rating and widen until we have enough.
_BAND = 100
_MIN_POOL = 12


@functools.lru_cache(maxsize=1)
def _baseline() -> list[dict]:
    """All vendored baseline puzzles. Built once, lazily. Missing/corrupt -> [] (degrade)."""
    puzzles: list[dict] = []
    if not _BASELINE.is_file():
        return puzzles
    try:
        with gzip.open(_BASELINE, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    puzzles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return puzzles


def available_themes() -> list[str]:
    """Sorted distinct theme tags present in the loaded pool (for the frontend theme picker)."""
    seen: set[str] = set()
    for p in _baseline():
        for t in p.get("themes", []):
            seen.add(t)
    return sorted(seen)


def _candidates(rating: float, themes: Optional[Iterable[str]]) -> list[dict]:
    """Puzzles within a rating band around `rating`, widened until the pool is usable."""
    pool = _baseline()
    if not pool:
        return []
    want = set(themes) if themes else None
    if want:
        pool = [p for p in pool if want & set(p.get("themes", []))]
    if not pool:
        return []
    band = _BAND
    while band <= 1200:
        near = [p for p in pool if abs(float(p.get("rating", 1500)) - rating) <= band]
        if len(near) >= _MIN_POOL:
            return near
        band += _BAND
    return pool  # whole (theme-filtered) pool as a last resort


def next_puzzle(
    rating: float,
    *,
    themes: Optional[Iterable[str]] = None,
    exclude: Optional[set[str]] = None,
    seed: Optional[int] = None,
    difficulty: Optional[str] = None,
) -> Optional[dict]:
    """Pick a puzzle near `rating`, theme-filtered + not in `exclude`, via a per-user seeded shuffle.

    `difficulty` of "easier"/"harder" shifts the target rating by one band. Returns the parsed
    puzzle (with a derived `side_to_move`) or None when nothing is available.
    """
    target = float(rating)
    if difficulty == "easier":
        target -= _BAND
    elif difficulty == "harder":
        target += _BAND

    candidates = _candidates(target, themes)
    exclude = exclude or set()
    fresh = [p for p in candidates if p.get("id") not in exclude]
    pool = fresh or candidates  # everything seen? fall back to the full band rather than nothing
    if not pool:
        return None

    rng = random.Random(seed if seed is not None else 0)
    order = pool[:]
    rng.shuffle(order)
    chosen = order[0]
    return _with_side(chosen)


def get_puzzle(puzzle_id: str) -> Optional[dict]:
    """Look up a loaded puzzle by id (for /move and /explain after selection)."""
    for p in _baseline():
        if p.get("id") == puzzle_id:
            return _with_side(p)
    return None


def _with_side(puzzle: dict) -> dict:
    """Attach `side_to_move` = the colour the solver plays (the position AFTER the setup move)."""
    out = dict(puzzle)
    try:
        board = chess.Board(puzzle["fen"])
        moves = puzzle.get("moves", [])
        if moves:
            board.push_uci(moves[0])
        out["side_to_move"] = "white" if board.turn == chess.WHITE else "black"
        out["solve_fen"] = board.fen()  # the position the solver actually sees
    except (ValueError, KeyError):
        out["side_to_move"] = "white"
        out["solve_fen"] = puzzle.get("fen", "")
    return out


def validate_step(puzzle: dict, ply_index: int, uci: str) -> dict:
    """Is `uci` the expected solution move at `ply_index` (into `puzzle['moves']`)?

    Replays the forced line up to `ply_index` from the puzzle FEN, so it's stateless. At the
    final solver move, any legal move that delivers checkmate is accepted (mate-leaf tolerance).
    Returns `{correct, is_complete, expected_uci, opponent_reply_uci}`.
    """
    moves = puzzle.get("moves", [])
    if ply_index < 0 or ply_index >= len(moves):
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    board = chess.Board(puzzle["fen"])
    try:
        for m in moves[:ply_index]:
            board.push_uci(m)
    except ValueError:
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    expected = moves[ply_index]
    is_last = ply_index == len(moves) - 1
    correct = uci == expected

    if not correct and is_last:
        # Branch tolerance: at a mate leaf any legal mating move counts.
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                test = board.copy(stack=False)
                test.push(mv)
                if test.is_checkmate():
                    correct = True
        except ValueError:
            pass

    if not correct:
        return {"correct": False, "is_complete": False, "expected_uci": expected, "opponent_reply_uci": None}

    next_index = ply_index + 1
    if next_index >= len(moves):
        return {"correct": True, "is_complete": True, "expected_uci": expected, "opponent_reply_uci": None}
    return {
        "correct": True,
        "is_complete": False,
        "expected_uci": expected,
        "opponent_reply_uci": moves[next_index],
    }


def position_fen(puzzle: dict, ply_index: int) -> str:
    """The FEN the solver moves from at `ply_index` (after replaying the forced prefix).

    Used to engine-ground the moves the user actually tried, so the coach can say concretely why
    each one works or fails. Best-effort -> the puzzle FEN on any error.
    """
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", [])[:ply_index]:
            board.push_uci(m)
        return board.fen()
    except (ValueError, KeyError):
        return puzzle.get("fen", "")


def solution_san(puzzle: dict) -> list[str]:
    """The full solution line (including the setup move) in SAN, for the coach facts."""
    out: list[str] = []
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", []):
            mv = chess.Move.from_uci(m)
            out.append(board.san(mv))
            board.push(mv)
    except (ValueError, KeyError):
        return out
    return out
