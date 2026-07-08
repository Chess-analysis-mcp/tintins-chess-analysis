"""MCP-tool parity for the puzzle trainer (next_puzzle / solve_puzzle).

Drives the same shared `puzzle_session` the board uses, on the vendored baseline shard — no network,
no Stockfish, no `claude` CLI. State is redirected to a tmp DATA_DIR so the suite never touches the
user's store. Mirrors `test_web_puzzles.py` but through the terminal tool surface.
"""
from __future__ import annotations

import pytest

from server import config
from server import mcp_server
from server.core import lines
from server.core import puzzle_session
from server.core import puzzles as puzzles_mod


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    # Every solved baseline puzzle should count so the Glicko path is exercised deterministically
    # (a small slice of the baseline has rd >= PUZZLE_MAX_RD and would otherwise play unrated).
    monkeypatch.setattr(config, "PUZZLE_MAX_RD", 9999)
    # Keep the suite Stockfish-free: the explain-facts builder degrades to themes + the ground-truth
    # solution line when the engine returns nothing, so stub it out (no real engine launch).
    monkeypatch.setattr(lines, "engine_line", lambda *a, **k: {})
    puzzle_session.clear_current()
    yield
    puzzle_session.clear_current()


def _solver_moves(puzzle_id: str) -> str:
    """The solver's moves (odd plies) for a curated puzzle, as one space-separated string."""
    puzzle = puzzles_mod.get_puzzle(puzzle_id)
    moves = puzzle["moves"]
    return " ".join(moves[i] for i in range(1, len(moves), 2))


def test_next_puzzle_serves_a_position_without_the_solution():
    nx = mcp_server.next_puzzle()
    assert nx["id"] and nx["fen"] and nx["side_to_move"] in ("white", "black")
    assert nx["source"] == "lichess"
    assert "moves" not in nx and "solution" not in nx  # never leak the answer
    # It set the shared session the board reads.
    prog = puzzle_session.get_current()
    assert prog is not None and prog.id == nx["id"]


def test_solve_puzzle_full_line_updates_rating():
    nx = mcp_server.next_puzzle()
    res = mcp_server.solve_puzzle(_solver_moves(nx["id"]))
    assert res["correct"] and res["is_complete"]
    assert res["outcome"] == "solved_first_try"
    assert res["rating"]["rating_after"] != res["rating"]["rating_before"]
    # Explain facts are grounded in the puzzle (themes lead) even with no engine/LLM.
    assert "coach_facts" in res and res["coach_facts"]


def test_solve_puzzle_accepts_san():
    nx = mcp_server.next_puzzle()
    puzzle = puzzles_mod.get_puzzle(nx["id"])
    solution_san = puzzles_mod.solution_san(puzzle)  # includes the setup move at index 0
    solver_san = " ".join(solution_san[i] for i in range(1, len(solution_san), 2))
    res = mcp_server.solve_puzzle(solver_san)
    assert res["correct"] and res["is_complete"]


def test_wrong_move_fails_without_revealing_solution():
    nx = mcp_server.next_puzzle()
    res = mcp_server.solve_puzzle("a1a1")  # a deliberately illegal/wrong move
    assert res["correct"] is False
    assert res["outcome"] == "failed"
    assert "expected" not in res and "solution" not in res


def test_solve_without_active_puzzle_errors():
    puzzle_session.clear_current()
    res = mcp_server.solve_puzzle("e2e4")
    assert "error" in res


def test_multi_move_puzzle_can_be_solved_one_move_at_a_time():
    """A partial (correct but non-final) submission advances the shared session; the next call
    continues it — the terminal equivalent of the board's per-ply loop."""
    # Find a baseline puzzle whose solver has >1 move.
    nx = None
    for _ in range(30):
        cand = mcp_server.next_puzzle()
        if "id" not in cand:
            break
        puzzle = puzzles_mod.get_puzzle(cand["id"])
        if len(puzzle["moves"]) >= 4:  # setup + solver + reply + solver
            nx = cand
            break
    if nx is None:
        pytest.skip("no multi-move puzzle in the sampled baseline slice")
    moves = puzzles_mod.get_puzzle(nx["id"])["moves"]
    first = mcp_server.solve_puzzle(moves[1])
    assert first["correct"] and not first["is_complete"]
    # Remaining solver moves finish it.
    rest = " ".join(moves[i] for i in range(3, len(moves), 2))
    done = mcp_server.solve_puzzle(rest)
    assert done["correct"] and done["is_complete"]
