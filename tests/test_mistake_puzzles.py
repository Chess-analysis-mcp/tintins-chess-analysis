"""Mistake puzzles from the user's own games (P3.5): skill-relative ordering, recurring-motif
tie-break, spaced-repetition, multi-solution acceptance, and the guarantee they never touch Glicko.
History + engine are mocked — no network, no Stockfish."""
from __future__ import annotations

import json

import chess
import pytest

from server.core import puzzle_mistakes as pm

FEN = "8/8/8/8/8/8/8/8 w - - 0 1"


def _m(ply, uci, win_drop, best_uci=None, cls="mistake", color="white", motifs=None):
    # Distinct default best move per mistake, so the (game_id, best_uci) dedup treats them as the
    # separate positions they're meant to be (real distinct mistakes have distinct best moves).
    best_uci = best_uci or ("b1" + uci[2:])
    return {"ply": ply, "fen_before": FEN, "uci": uci, "san": uci, "best_uci": best_uci,
            "best_san": best_uci, "win_drop": win_drop, "classification": cls,
            "color": color, "motifs": motifs or []}


def _rec(game_id, side, mistakes, thresholds=(5, 10, 15)):
    return {"game_id": game_id, "reviewed_side": side, "thresholds": list(thresholds),
            "white": "me", "black": "opp", "speed": "blitz", "date": "2026.01.01",
            "game_url": "https://lichess.org/x", "mistakes": mistakes}


@pytest.fixture(autouse=True)
def _stub_identity(monkeypatch):
    monkeypatch.setattr(pm.history, "my_player_id", lambda *a, **k: "me")
    monkeypatch.setattr(pm.history, "_is_recurring", lambda *a, **k: False)


# --- skill-relative ordering --------------------------------------------------------------------

def test_ordering_is_skill_relative(monkeypatch):
    # Two clear clusters: big blunders vs subtle slips. A beginner should be fed the big cluster,
    # a strong player the subtle one (skill-relative severity), independent of the exact percentile
    # constants (_PCT_HI/_PCT_LO).
    recs = [_rec("g1", "white", [
        _m(3, "a2a3", 42),    # big-blunder cluster
        _m(9, "d2d4", 38),
        _m(11, "e2e4", 34),
        _m(5, "b2b3", 6),     # subtle cluster
        _m(7, "c2c3", 8),
        _m(13, "f2f4", 5),
    ])]
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    low = pm.next_mistake_puzzle({"rating": 800})    # beginner -> big cluster
    high = pm.next_mistake_puzzle({"rating": 2200})  # strong  -> subtle cluster
    assert low["win_drop"] >= 34 and high["win_drop"] <= 8
    assert high["win_drop"] < low["win_drop"]        # strong players get more subtle mistakes
    # The single biggest blunder is served first to the beginner.
    assert low["source"] == "your_games" and low["id"] == "g1:white:3"


def test_recurring_motif_breaks_ties(monkeypatch):
    recs = [_rec("g1", "white", [
        _m(3, "a2a3", 20, motifs=["missed_fork"]),
        _m(5, "b2b3", 20, motifs=["pawn_grab"]),
    ])]
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    monkeypatch.setattr(pm.history, "_is_recurring", lambda mo, dd: mo == "missed_fork")
    chosen = pm.next_mistake_puzzle({"rating": 1500})
    assert chosen["ply"] == 3  # equal swings -> the recurring-motif position wins


# --- spaced repetition --------------------------------------------------------------------------

def test_retired_positions_are_skipped(monkeypatch):
    recs = [_rec("g1", "white", [_m(3, "a2a3", 40)])]
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    key = pm.practice_key("g1", "white", 3)
    state = {"rating": 1500, "practiced": {key: {"successes": 2}}}
    assert pm.next_mistake_puzzle(state) is None  # the only candidate is retired


def test_record_practice_result_advances_then_resets():
    state = {}
    pm.record_practice_result(state, "k", solved=True)
    assert state["practiced"]["k"]["successes"] == 1
    pm.record_practice_result(state, "k", solved=True)
    assert state["practiced"]["k"]["successes"] == 2  # now retired
    pm.record_practice_result(state, "k", solved=False)
    assert state["practiced"]["k"]["successes"] == 0  # a failure resurfaces it


def test_no_candidates_returns_none(monkeypatch):
    monkeypatch.setattr(pm.history, "load_records", lambda **k: [])
    assert pm.next_mistake_puzzle({"rating": 1500}) is None


# --- dedup + recently-served (no "essentially the same puzzle" repeats) --------------------------

def test_same_best_move_over_consecutive_plies_collapses_to_one(monkeypatch):
    # The same missed move flagged over 3 plies in a row must not resurface as 3 near-identical
    # puzzles (the user's complaint). Collapse to one, keeping the most instructive (biggest swing).
    recs = [_rec("g1", "white", [
        _m(3, "a2a3", 15, best_uci="c1f4"),
        _m(5, "b2b3", 20, best_uci="c1f4"),
        _m(7, "c2c3", 10, best_uci="c1f4"),
    ])]
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    cands = pm._dedup(pm._candidate_mistakes())
    assert len(cands) == 1
    assert cands[0]["win_drop"] == 20


def test_recently_served_position_is_skipped(monkeypatch):
    recs = [_rec("g1", "white", [
        _m(3, "a2a3", 20, best_uci="c1f4"),
        _m(5, "b2b3", 20, best_uci="d1e2"),
    ])]
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    state = {"rating": 1500}
    first = pm.next_mistake_puzzle(state)
    pm.mark_mistake_served(state, first["key"])
    second = pm.next_mistake_puzzle(state)
    assert second["key"] != first["key"]  # a genuinely different position after serving the first


def test_mark_mistake_served_caps_and_dedupes():
    state = {}
    for i in range(30):
        pm.mark_mistake_served(state, f"k{i}")
    assert len(state["mistake_recent"]) == pm._RECENT_MAX
    assert state["mistake_recent"][-1] == "k29"  # newest kept
    pm.mark_mistake_served(state, "k29")  # re-serving moves it to the end, no dup
    assert state["mistake_recent"].count("k29") == 1


def test_hint_on_mistake_puzzle_reveals_best_from_square(monkeypatch):
    from server.web import routes_puzzles as rp
    from server.core import puzzle_session

    puzzle = {"id": "g1:white:3", "key": "g1:white:3", "source": "your_games",
              "fen": FEN, "side_to_move": "white", "best_uci": "c1f4"}
    puzzle_session.set_current(dict(puzzle))
    resp = rp.puzzle_hint(rp.PuzzleIdBody(id="g1:white:3"))
    body = json.loads(resp.body)
    assert body["from_square"] == "c1"  # the best move's origin, NOT a 400 (was the bug)
    assert body["rated"] is False


# --- opponent's preceding move (setup animation) ------------------------------------------------

# 1.e4 e5 2.Bc4 Nc6 3.Qh5 -> Black to move on ply 6; the preceding move is White's 3.Qh5.
_PGN = "1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 *"


def _fen_after(pgn: str, n_plies: int) -> str:
    board = chess.pgn.read_game(pm.io.StringIO(pgn)).board()
    for mv in list(chess.pgn.read_game(pm.io.StringIO(pgn)).mainline_moves())[:n_plies]:
        board.push(mv)
    return board.fen()


def test_prev_move_reconstructs_opponents_move_into_the_position():
    # Player's mistake is Black's 3...Nf6 (ply 6). The setup move is White's 3.Qh5 (ply 5), and
    # playing it from prev_fen must land exactly on the mistake position (fen after 5 plies).
    prev = pm._prev_move(_PGN, 6)
    assert prev is not None
    board = chess.Board(prev["prev_fen"])
    assert board.san(chess.Move.from_uci(prev["setup_uci"])) == prev["setup_san"] == "Qh5"
    board.push_uci(prev["setup_uci"])
    assert board.fen() == _fen_after(_PGN, 5)  # setup lands on the mistake position


def test_prev_move_degrades_gracefully():
    assert pm._prev_move(None, 6) is None          # no PGN stored (older record)
    assert pm._prev_move(_PGN, 1) is None          # mistake on ply 1 -> no preceding move
    assert pm._prev_move("not a pgn {{{", 6) is None  # unparseable PGN never raises


def test_next_mistake_puzzle_attaches_setup_and_drops_pgn(monkeypatch):
    recs = [_rec("g1", "black", [_m(6, "f6g4", 30, color="black")])]
    for r in recs:
        r["pgn"] = _PGN
    monkeypatch.setattr(pm.history, "load_records", lambda **k: recs)
    chosen = pm.next_mistake_puzzle({"rating": 1500})
    assert chosen["setup_san"] == "Qh5" and chosen["setup_uci"] == "d1h5"
    assert "pgn" not in chosen  # the raw PGN is not carried into the session/response


# --- multi-solution acceptance + Glicko is never touched (route helper) --------------------------

def _engine(swing, **extra):
    m = {"win_swing": swing, "better_move_san": "Nf3", "is_engine_best": True,
         "refutation_line_san": [], "refutation_line_uci": []}
    m.update(extra)
    return lambda fen, move=None, **k: {"move": m}


def test_mistake_move_accepts_below_threshold_and_never_rates(monkeypatch):
    from server.web import routes_puzzles as rp
    from server.core import puzzle_session

    puzzle = {"id": "g1:white:3", "key": "g1:white:3", "source": "your_games",
              "fen": FEN, "accept_swing": 5.0, "side_to_move": "white"}
    prog = puzzle_session.set_current(dict(puzzle))
    monkeypatch.setattr(rp.puzzle_rating, "load_state", lambda *a, **k: {"practiced": {}})
    monkeypatch.setattr(rp.puzzle_rating, "save_state", lambda *a, **k: None)
    # The Glicko update must NEVER run for a mistake puzzle.
    monkeypatch.setattr(rp.puzzle_rating, "record_result",
                        lambda *a, **k: pytest.fail("mistake puzzle touched Glicko"))

    # A small drop (2 < 5) is accepted and completes the puzzle.
    monkeypatch.setattr(rp.lines, "engine_line", _engine(2.0))
    body = json.loads(rp._mistake_move(prog, "b1c3").body)
    assert body["correct"] and body["is_complete"] and body["source"] == "your_games"


def test_mistake_move_rejects_a_slip_with_refutation(monkeypatch):
    from server.web import routes_puzzles as rp
    from server.core import puzzle_session

    puzzle = {"id": "g1:white:5", "key": "g1:white:5", "source": "your_games",
              "fen": FEN, "accept_swing": 5.0, "side_to_move": "white"}
    prog = puzzle_session.set_current(dict(puzzle))
    monkeypatch.setattr(rp.puzzle_rating, "load_state", lambda *a, **k: {"practiced": {}})
    monkeypatch.setattr(rp.puzzle_rating, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(rp.lines, "engine_line",
                        _engine(30.0, refutation_line_uci=["h1h7"], refutation_line_san=["Qxh7"]))
    body = json.loads(rp._mistake_move(prog, "g2g4").body)
    assert not body["correct"] and body["can_retry"]
    assert body["refutation_uci"] == ["h1h7"]
