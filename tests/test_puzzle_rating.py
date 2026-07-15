"""Glicko-2 math + puzzle state persistence.

The math is checked against the published worked example in Glickman's "Example of the Glicko-2
system" (rating 1500 / RD 200 / vol 0.06, tau 0.5, vs three opponents), whose answer is
r' ~= 1464.06, RD' ~= 151.52, vol' ~= 0.05999. No network, no engine.
"""
from __future__ import annotations

from server.core import puzzle_rating as pr


def test_glicko2_matches_published_worked_example():
    rating, rd, vol = pr.glicko2_update(
        1500.0, 200.0, 0.06,
        [(1400.0, 30.0, 1.0), (1550.0, 100.0, 0.0), (1700.0, 300.0, 0.0)],
        tau=0.5,
    )
    assert abs(rating - 1464.06) < 0.1, rating
    assert abs(rd - 151.52) < 0.1, rd
    assert abs(vol - 0.05999) < 0.0005, vol


def test_solving_a_harder_puzzle_raises_rating_failing_lowers_it():
    up_r, _, _ = pr.glicko2_update(1500.0, 200.0, 0.06, [(1600.0, 50.0, 1.0)])
    down_r, _, _ = pr.glicko2_update(1500.0, 200.0, 0.06, [(1600.0, 50.0, 0.0)])
    assert up_r > 1500.0 > down_r


def test_empty_period_only_inflates_rd():
    r, rd, vol = pr.glicko2_update(1500.0, 80.0, 0.06, [])
    assert r == 1500.0 and vol == 0.06
    assert rd >= 80.0  # RD grows toward the default when you don't play


def test_state_roundtrip_and_seed_is_stable(tmp_path):
    s1 = pr.load_state(data_dir=str(tmp_path))
    seed = s1["user_seed"]
    assert seed  # generated + persisted on first load
    s2 = pr.load_state(data_dir=str(tmp_path))
    assert s2["user_seed"] == seed  # stable across loads


def test_record_result_rd_gate(tmp_path):
    # A puzzle with high RD (>= PUZZLE_MAX_RD) plays UNRATED: rating must not move.
    state = pr.load_state(data_dir=str(tmp_path))
    before = state["rating"]
    out = pr.record_result(
        state, puzzle_id="x", puzzle_rating=1500, puzzle_rd=300, themes=["fork"],
        score=1.0, rated=False,
    )
    assert out["rated"] is False
    assert state["rating"] == before  # unrated -> unchanged
    assert state["streak"] == 1  # streak + by_theme still update
    assert state["by_theme"]["fork"] == {"seen": 1, "solved": 1}

    # A well-established puzzle (rated) does move the rating.
    out2 = pr.record_result(
        state, puzzle_id="y", puzzle_rating=1600, puzzle_rd=40, themes=["pin"],
        score=1.0, rated=True,
    )
    assert out2["rated"] is True
    assert state["rating"] != before
    assert state["streak"] == 2


def test_failure_resets_streak(tmp_path):
    state = pr.load_state(data_dir=str(tmp_path))
    pr.record_result(state, puzzle_id="a", puzzle_rating=1200, puzzle_rd=40, themes=[], score=1.0, rated=True)
    pr.record_result(state, puzzle_id="b", puzzle_rating=1200, puzzle_rd=40, themes=[], score=1.0, rated=True)
    assert state["streak"] == 2 and state["best_streak"] == 2
    pr.record_result(state, puzzle_id="c", puzzle_rating=1200, puzzle_rd=40, themes=[], score=0.0, rated=True)
    assert state["streak"] == 0
    assert state["best_streak"] == 2  # best is remembered


def test_daily_streak_counts_once_per_day_and_extends_on_consecutive_days():
    state = pr._default_state()
    # First completed puzzle of "day 1" starts the streak.
    state["last_active_date"] = ""
    pr.touch_daily_streak(state)
    day1 = state["last_active_date"]
    assert state["daily_streak"] == 1 and state["best_daily_streak"] == 1

    # A second puzzle the same day is a no-op (idempotent within a day).
    pr.touch_daily_streak(state)
    assert state["daily_streak"] == 1

    # Pretend the last active day was yesterday -> a consecutive day extends the streak.
    from datetime import datetime, timedelta
    d1 = datetime.strptime(day1, "%Y-%m-%d").date()
    state["last_active_date"] = (d1 - timedelta(days=1)).strftime("%Y-%m-%d")
    state["daily_streak"] = 4
    pr.touch_daily_streak(state)
    assert state["daily_streak"] == 5 and state["best_daily_streak"] == 5

    # A gap of more than one day resets the streak to 1 (best is remembered).
    state["last_active_date"] = (d1 - timedelta(days=3)).strftime("%Y-%m-%d")
    state["daily_streak"] = 5
    pr.touch_daily_streak(state)
    assert state["daily_streak"] == 1 and state["best_daily_streak"] == 5


def test_record_result_advances_daily_streak(tmp_path):
    state = pr.load_state(data_dir=str(tmp_path))
    assert state["daily_streak"] == 0
    # Even a failed attempt counts as a day of practice.
    pr.record_result(state, puzzle_id="a", puzzle_rating=1200, puzzle_rd=40, themes=[], score=0.0, rated=True)
    assert state["daily_streak"] == 1 and state["last_active_date"]
