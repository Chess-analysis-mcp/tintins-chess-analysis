"""Puzzle storm (timed rush) core logic, on the vendored baseline with a fake clock.

No network, no Stockfish, no wall-time sleeps: every entry point takes an injectable `now`, so the
countdown, combo bonuses, and time penalties are asserted deterministically. State is redirected to a
tmp DATA_DIR. Confirms storm never touches the Glicko rating (it's unrated by design).
"""
from __future__ import annotations

import pytest

from server import config
from server.core import puzzle_rating
from server.core import puzzle_session
from server.core import puzzle_storm
from server.core import puzzles as puzzles_mod


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PUZZLE_STORM_DURATION", 180)
    puzzle_storm.clear()
    puzzle_session.clear_current()
    yield
    puzzle_storm.clear()
    puzzle_session.clear_current()


def _solve_current(state, *, now):
    """Play the full solution for the puzzle currently in the session; return the final move dict."""
    prog = puzzle_session.get_current()
    moves = puzzles_mod.get_puzzle(prog.id)["moves"]
    last = None
    for i in range(1, len(moves), 2):
        last = puzzle_storm.submit_move(state, moves[i], now=now)
        if last.get("puzzle_done"):
            break
    return last


def test_start_serves_a_puzzle_and_starts_the_clock():
    state = puzzle_rating.load_state()
    view = puzzle_storm.start(state, now=1000.0)
    assert view["active"] is True and view["ended"] is False
    assert view["score"] == 0 and view["remaining"] == 180
    assert view["puzzle"]["id"] and view["puzzle"]["side_to_move"] in ("white", "black")
    # The shared session now holds the storm puzzle (board parity).
    assert puzzle_session.get_current().id == view["puzzle"]["id"]


def test_solving_scores_and_leaves_glicko_untouched():
    state = puzzle_rating.load_state()
    rating_before = state["rating"]
    puzzle_storm.start(state, now=1000.0)
    res = _solve_current(state, now=1001.0)
    assert res["solved"] is True and res["puzzle_done"] is True
    assert res["score"] == 1
    # Storm is unrated: the persisted Glicko rating must be unchanged.
    assert puzzle_rating.load_state()["rating"] == rating_before


def test_wrong_move_breaks_combo_and_costs_time():
    state = puzzle_rating.load_state()
    puzzle_storm.start(state, now=1000.0)
    res = puzzle_storm.submit_move(state, "a1a1", now=1000.0)  # illegal/wrong
    assert res["correct"] is False and res["puzzle_done"] is True
    assert res["combo"] == 0 and res["misses"] == 1
    assert res["remaining"] == pytest.approx(180 - 8.0)  # _WRONG_PENALTY


def test_combo_milestone_grants_time_bonus():
    state = puzzle_rating.load_state()
    puzzle_storm.start(state, now=1000.0)
    last = None
    for _ in range(puzzle_storm._COMBO_BONUS_EVERY):
        last = _solve_current(state, now=1000.0)
        if not last.get("solved"):
            pytest.skip("hit an unsolvable/duplicate baseline pick before the milestone")
        puzzle_storm.next_puzzle(state, now=1000.0)
    assert last["combo"] == puzzle_storm._COMBO_BONUS_EVERY
    assert last["time_bonus"] == puzzle_storm._COMBO_TIME_BONUS
    # The bonus extended the clock past the base duration (no time elapsed in the fake clock).
    assert last["remaining"] > 180


def test_clock_expiry_ends_run_and_persists_highscore():
    state = puzzle_rating.load_state()
    puzzle_storm.start(state, now=1000.0)
    _solve_current(state, now=1000.0)  # score 1
    # Ask for the next puzzle after the clock has run out.
    view = puzzle_storm.next_puzzle(state, now=1000.0 + 999)
    assert view["ended"] is True
    assert view["score"] == 1 and view["new_high"] is True
    assert puzzle_rating.load_state()["storm_high"] == 1


def test_move_after_expiry_finishes_gracefully():
    state = puzzle_rating.load_state()
    puzzle_storm.start(state, now=1000.0)
    res = puzzle_storm.submit_move(state, "e2e4", now=1000.0 + 999)
    assert res["ended"] is True
