"""Puzzle selection + move validation (engine-free).

`validate_step` is pinned with hand-built FENs including the mate-in-1 leaf branch tolerance;
selection is checked against a controlled in-memory pool (the real baseline is monkeypatched out)
for rating-band, theme, seen-exclusion and the per-user seeded-shuffle behaviour.
"""
from __future__ import annotations

import pytest

from server import config
from server.core import puzzles


@pytest.fixture(autouse=True)
def _no_network_downloads(monkeypatch):
    """next_puzzle now triggers a background shard warm-up; keep these unit tests network-free and
    baseline-only (no downloaded pool) so selection behaviour is deterministic."""
    monkeypatch.setattr(puzzles.puzzle_shards, "ensure_bands_around", lambda *a, **k: None)
    monkeypatch.setattr(puzzles, "_downloaded_pool", lambda: [])


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


# --- merged (baseline + downloaded) pool (P3) ------------------------------------------------

def test_merged_pool_includes_downloaded_and_dedupes_by_id(monkeypatch):
    monkeypatch.setattr(puzzles, "_baseline", lambda: _pool())
    extra = {"id": "dl_new", "fen": "8/8/8/8/8/8/8/8 w - - 0 1", "moves": ["a1a2"],
             "rating": 1000, "rd": 50, "themes": ["fork"]}
    dupe = dict(_pool()[0])  # same id as a baseline puzzle
    monkeypatch.setattr(puzzles, "_downloaded_pool", lambda: [extra, dupe])
    ids = [p["id"] for p in puzzles._merged_pool()]
    assert "dl_new" in ids                 # downloaded puzzle surfaces
    assert ids.count(dupe["id"]) == 1      # the duplicate id appears once


# --- weakness themes (P3) --------------------------------------------------------------------

def test_weakness_themes_from_puzzle_stats():
    state = {"by_theme": {"fork": {"seen": 10, "solved": 3},   # 30% -> weak
                          "pin": {"seen": 10, "solved": 9},    # 90% -> strong
                          "skewer": {"seen": 2, "solved": 0}}}  # too few samples
    themes = puzzles.weakness_themes(state)
    assert "fork" in themes
    assert "pin" not in themes and "skewer" not in themes


def test_weakness_themes_maps_game_motifs(monkeypatch):
    from server.core import history
    monkeypatch.setattr(history, "get_profile", lambda *a, **k: {
        "recent": {"games": 20,  # enough analysed games that motif weaknesses are trusted
                   "top_motifs": [{"motif": "missed_fork", "count": 3},
                                  {"motif": "back_rank", "count": 2},
                                  {"motif": "pawn_grab", "count": 5}]}})  # pawn_grab -> None (dropped)
    themes = puzzles.weakness_themes({})
    assert "fork" in themes and "backRankMate" in themes
    assert None not in themes


def test_weakness_themes_held_back_until_enough_games(monkeypatch):
    """A single early game must not skew the whole stream (the user asked not to be biased so soon)."""
    from server.core import history
    monkeypatch.setattr(history, "get_profile", lambda *a, **k: {
        "recent": {"games": 1,  # below _MIN_HISTORY_GAMES -> no motif bias yet
                   "top_motifs": [{"motif": "missed_fork", "count": 3}]}})
    assert puzzles.weakness_themes({}) == []


def test_weakness_themes_held_back_until_enough_puzzle_attempts(monkeypatch):
    from server.core import history
    monkeypatch.setattr(history, "get_profile", lambda *a, **k: {"recent": {"games": 0}})
    # One weak theme, but too few total attempts for the puzzle-stat signal to kick in.
    state = {"by_theme": {"fork": {"seen": 4, "solved": 0}}}
    assert puzzles.weakness_themes(state) == []


# --- "Work on" card: only trainable motifs, no metadata tags ---------------------------------

def test_weak_theme_stats_excludes_metadata_tags():
    by_theme = {
        "master": {"seen": 10, "solved": 0},     # metadata (origin) -> excluded
        "oneMove": {"seen": 10, "solved": 0},     # metadata (length) -> excluded
        "middlegame": {"seen": 10, "solved": 0},  # metadata (phase) -> excluded
        "fork": {"seen": 8, "solved": 2},         # real motif, weak (25%) -> kept
        "pin": {"seen": 8, "solved": 7},          # real motif, strong -> dropped by rate
        "skewer": {"seen": 2, "solved": 0},       # too few attempts -> dropped
    }
    weak = puzzles.weak_theme_stats(by_theme)
    names = [w["theme"] for w in weak]
    assert names == ["fork"]  # only the genuinely-weak trainable motif


def test_is_trainable_theme():
    assert puzzles.is_trainable_theme("fork")
    assert puzzles.is_trainable_theme("backRankMate")
    assert not puzzles.is_trainable_theme("master")
    assert not puzzles.is_trainable_theme("oneMove")


def test_weakness_bias_ignores_metadata_tags(monkeypatch):
    from server.core import history
    monkeypatch.setattr(history, "get_profile", lambda *a, **k: {"recent": {"games": 0}})
    # Plenty of attempts, but the only weak "theme" is a metadata tag -> no bias.
    state = {"by_theme": {"master": {"seen": 20, "solved": 0}}}
    assert puzzles.weakness_themes(state) == []


# --- high-RD (unrated) puzzles are kept out of selection -------------------------------------

def test_candidates_exclude_high_rd_puzzles(monkeypatch):
    fen = "8/8/8/8/8/8/8/8 w - - 0 1"
    pool = []
    for i in range(20):
        pool.append({"id": f"lo{i}", "fen": fen, "rating": 1500, "rd": 60, "themes": []})
        pool.append({"id": f"hi{i}", "fen": fen, "rating": 1500, "rd": 500, "themes": []})
    monkeypatch.setattr(puzzles, "_merged_pool", lambda: pool)
    monkeypatch.setattr(config, "PUZZLE_MAX_RD", 130)
    cands = puzzles._candidates(1500, None)
    assert cands and all(float(c["rd"]) < 130 for c in cands)  # no unrated (high-RD) puzzles served


def test_candidates_fall_back_when_all_high_rd(monkeypatch):
    fen = "8/8/8/8/8/8/8/8 w - - 0 1"
    pool = [{"id": f"hi{i}", "fen": fen, "rating": 1500, "rd": 500, "themes": []} for i in range(20)]
    monkeypatch.setattr(puzzles, "_merged_pool", lambda: pool)
    monkeypatch.setattr(config, "PUZZLE_MAX_RD", 130)
    # Better an unrated puzzle than none: the filter falls back when it would empty the pool.
    assert puzzles._candidates(1500, None)
