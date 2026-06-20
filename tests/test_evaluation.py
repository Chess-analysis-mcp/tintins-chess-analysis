"""Unit tests for the pure evaluation math (no engine needed)."""
from __future__ import annotations

import pytest

from server.core import evaluation as ev


def test_win_percent_zero_is_fifty():
    assert ev.win_percent(0) == pytest.approx(50.0)


def test_thresholds_scale_with_elo():
    assert ev.thresholds_for_elo(None) == ev.DEFAULT_THRESHOLDS
    # ~1500 Elo is the anchor: ×1.0 -> the default 5/10/15.
    assert ev.thresholds_for_elo(1500) == pytest.approx((5.0, 10.0, 15.0))
    casual = ev.thresholds_for_elo(1000)
    strong = ev.thresholds_for_elo(2000)
    master = ev.thresholds_for_elo(2400)
    # Stronger player -> smaller cutoffs at every severity.
    assert master[0] < strong[0] < ev.DEFAULT_THRESHOLDS[0] < casual[0]
    assert master[1] < strong[1] < master[2]  # mistake cutoff still below blunder cutoff


def test_classify_respects_custom_thresholds():
    # A 6% win drop: an inaccuracy at default sensitivity, a mistake at master sensitivity.
    assert ev.classify(60.0, 54.0) == "inaccuracy"
    assert ev.classify(60.0, 54.0, thresholds=(2.5, 6.0, 11.0)) == "mistake"


def test_win_percent_monotonic_in_cp():
    cps = [-1000, -500, -200, -50, 0, 50, 200, 500, 1000]
    wins = [ev.win_percent(c) for c in cps]
    assert wins == sorted(wins)
    assert all(0.0 <= w <= 100.0 for w in wins)


def test_win_percent_symmetry():
    for cp in (50, 137, 400, 999):
        assert ev.win_percent(cp) + ev.win_percent(-cp) == pytest.approx(100.0)


def test_win_percent_clamps():
    # Beyond the clamp the value should not change.
    assert ev.win_percent(5000) == pytest.approx(ev.win_percent(1000))
    assert ev.win_percent(-5000) == pytest.approx(ev.win_percent(-1000))


def test_win_percent_from_score_mate():
    assert ev.win_percent_from_score(None, 3) == pytest.approx(100.0)
    assert ev.win_percent_from_score(None, -2) == pytest.approx(0.0)
    assert ev.win_percent_from_score(150, None) == pytest.approx(ev.win_percent(150))


def test_classification_thresholds():
    # drop just below / at each boundary (Lichess 5/10/15 win%-point thresholds)
    assert ev.classify(50, 50) == "best"          # no drop
    assert ev.classify(60, 56) == "good"          # drop 4 (>BEST_EPS, <5)
    assert ev.classify(60, 55) == "inaccuracy"    # drop 5
    assert ev.classify(60, 50) == "mistake"       # drop 10
    assert ev.classify(60, 45) == "blunder"       # drop 15
    assert ev.classify(90, 30) == "blunder"       # drop 60


def test_classification_is_best_flag():
    # Tiny drop but flagged as engine's top move -> best.
    assert ev.classify(50, 49, is_best=True) == "best"


def test_move_accuracy_range_and_direction():
    perfect = ev.move_accuracy(50, 50)
    bad = ev.move_accuracy(60, 20)
    assert 0.0 <= bad < perfect <= 100.0
    assert perfect == pytest.approx(100.0, abs=0.5)


def test_aggregate_accuracy():
    assert ev.aggregate_accuracy([]) == 100.0
    assert ev.aggregate_accuracy([90.0, 80.0]) == pytest.approx(85.0)


def test_time_control_clock():
    assert ev.time_control_clock("600+0") == (600.0, 0.0)
    assert ev.time_control_clock("180+2") == (180.0, 2.0)
    assert ev.time_control_clock("300") == (300.0, 0.0)
    assert ev.time_control_clock("-") is None
    assert ev.time_control_clock("1/259200") is None  # correspondence
    assert ev.time_control_clock("") is None


def test_classify_speed_buckets():
    # Buckets from estimated duration = base + 40*increment.
    assert ev.classify_speed("60+0") == "bullet"
    assert ev.classify_speed("120+1") == "bullet"      # 120 + 40 = 160 < 180
    assert ev.classify_speed("180+0") == "blitz"       # exactly 180 -> blitz
    assert ev.classify_speed("300+0") == "blitz"
    assert ev.classify_speed("300+5") == "rapid"       # 300 + 200 = 500 >= 480
    assert ev.classify_speed("600+0") == "rapid"
    assert ev.classify_speed("1800+0") == "classical"
    assert ev.classify_speed("1/259200") == "correspondence"
    assert ev.classify_speed("-") == "correspondence"


def test_thresholds_for_speed_gradient():
    base = (5.0, 10.0, 15.0)
    # Blitz is the anchor — unchanged; faster is more forgiving, slower is stricter.
    assert ev.thresholds_for_speed(base, "blitz") == base
    assert ev.thresholds_for_speed(base, "unknown") == base
    assert ev.thresholds_for_speed(base, "rapid") == (4.5, 9.0, 13.5)        # slightly lower
    assert ev.thresholds_for_speed(base, "rapid")[0] < base[0]
    assert ev.thresholds_for_speed(base, "bullet")[0] > base[0]              # more forgiving
    # Monotonic: bullet >= blitz >= rapid >= classical >= correspondence (per inaccuracy cutoff).
    order = ["bullet", "blitz", "rapid", "classical", "correspondence"]
    firsts = [ev.thresholds_for_speed(base, s)[0] for s in order]
    assert firsts == sorted(firsts, reverse=True)


def test_classify_speed_falls_back_to_event_then_unknown():
    # No usable TimeControl -> read the lichess-style Event keyword.
    assert ev.classify_speed("", "Rated Blitz game") == "blitz"
    assert ev.classify_speed("?", "Rated Rapid tournament") == "rapid"
    # Nothing informative -> unknown (never crashes).
    assert ev.classify_speed(None, None) == "unknown"
    assert ev.classify_speed("", "Live Chess") == "unknown"
