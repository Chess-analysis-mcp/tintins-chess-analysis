"""Puzzle selection + move validation (engine-free).

`validate_step` is pinned with hand-built FENs including the mate-in-1 leaf branch tolerance;
selection is checked against a controlled in-memory pool (the real baseline is monkeypatched out)
for rating-band, theme, seen-exclusion and the per-user seeded-shuffle behaviour.
"""
from __future__ import annotations

from server.core import puzzles


# --- validate_step --------------------------------------------------------------------------

# Back-rank position: after the setup move ...Kg8, BOTH Ra8# and Rb8# are legal mates (the
# f7/g7/h7 pawns box the king in), so the mate-leaf branch must accept either.
MATE_PUZZLE = {
    "id": "m1",
    "fen": "7k/5ppp/8/8/8/8/8/RR4K1 b - - 0 1",
    "moves": ["h8g8", "a1a8"],
    "rating": 1000,
    "rd": 50,
    "themes": ["backRankMate", "mate", "mateIn1"],
}

# A multi-move line (move equality only; later plies need not be legal from the replayed prefix).
MULTI_PUZZLE = {
    "id": "x1",
    "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "moves": ["e2e4", "e7e5", "g1f3", "b8c6"],
    "rating": 1200,
    "rd": 50,
    "themes": ["opening"],
}


def test_validate_step_correct_completes():
    r = puzzles.validate_step(MATE_PUZZLE, 1, "a1a8")
    assert r["correct"] and r["is_complete"]


def test_validate_step_mate_leaf_accepts_alternative_mate():
    # Not the stored move, but a legal checkmate at the final ply -> accepted.
    r = puzzles.validate_step(MATE_PUZZLE, 1, "b1b8")
    assert r["correct"] and r["is_complete"]


def test_validate_step_rejects_wrong_move():
    r = puzzles.validate_step(MATE_PUZZLE, 1, "g1f1")
    assert not r["correct"]
    assert r["expected_uci"] == "a1a8"


def test_validate_step_intermediate_returns_forced_reply():
    r = puzzles.validate_step(MULTI_PUZZLE, 1, "e7e5")
    assert r["correct"] and not r["is_complete"]
    assert r["opponent_reply_uci"] == "g1f3"


def test_validate_step_final_move_of_multi_line():
    r = puzzles.validate_step(MULTI_PUZZLE, 3, "b8c6")
    assert r["correct"] and r["is_complete"]


# --- selection ------------------------------------------------------------------------------

def _pool():
    out = []
    for i in range(20):
        out.append({
            "id": f"p{i}",
            "fen": "8/8/8/8/8/8/8/8 w - - 0 1",
            "moves": ["a1a2", "a8a7"],
            "rating": 1000,
            "rd": 50,
            "themes": ["fork"] if i % 2 == 0 else ["pin"],
        })
    # An out-of-band puzzle that must never be chosen for a 1000 target.
    out.append({"id": "far", "fen": "8/8/8/8/8/8/8/8 w - - 0 1", "moves": ["a1a2"],
                "rating": 2600, "rd": 50, "themes": ["fork"]})
    return out


def test_selection_respects_rating_band(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: _pool())
    for seed in range(8):
        p = puzzles.next_puzzle(1000, seed=seed)
        assert p["id"] != "far"  # the 2600 puzzle is far outside the 1000 band


def test_selection_theme_filter(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: _pool())
    p = puzzles.next_puzzle(1000, themes=["pin"], seed=3)
    assert "pin" in p["themes"]


def test_selection_excludes_seen(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: _pool())
    first = puzzles.next_puzzle(1000, seed=5)["id"]
    second = puzzles.next_puzzle(1000, seed=5, exclude={first})["id"]
    assert second != first


def test_seeded_shuffle_is_deterministic_per_seed_and_varies_across_seeds(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: _pool())
    # Deterministic: same seed -> same pick.
    assert puzzles.next_puzzle(1000, seed=7)["id"] == puzzles.next_puzzle(1000, seed=7)["id"]
    # Varies: across many seeds we see more than one distinct first pick.
    picks = {puzzles.next_puzzle(1000, seed=s)["id"] for s in range(12)}
    assert len(picks) > 1


def test_side_to_move_is_after_the_setup_move(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: [MATE_PUZZLE])
    p = puzzles.next_puzzle(1000, seed=0)
    # FEN is black-to-move; after ...Kg8 the solver (White) is on move.
    assert p["side_to_move"] == "white"
