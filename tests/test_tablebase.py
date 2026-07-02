"""Tests for the endgame tablebase probe + its chat-fact formatting (network mocked)."""
from __future__ import annotations

import httpx
import pytest

from server import claude_bridge
from server.core import tablebase

# KQ vs K, White to move — a textbook tablebase win.
_FEN_KQK = "8/8/8/4k3/8/8/3Q4/4K3 w - - 0 1"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    tablebase._cache.clear()
    monkeypatch.setattr(tablebase.config, "TABLEBASE_ENABLED", True)
    yield
    tablebase._cache.clear()


def _mock_response(monkeypatch, payload: dict, box: dict | None = None):
    def fake_get(url, params=None, headers=None, timeout=None):
        if box is not None:
            box["url"] = url
            box["params"] = params or {}
        return _FakeResponse(payload)

    monkeypatch.setattr(tablebase.httpx, "get", fake_get)


def test_probe_normalises_win(monkeypatch):
    box: dict = {}
    _mock_response(monkeypatch, {"category": "win", "dtz": 21, "dtm": 17}, box)
    res = tablebase.probe(_FEN_KQK)
    assert res["category"] == "win"
    assert res["dtz"] == 21 and res["dtm"] == 17
    assert res["men"] == 3
    # Hits the configured tablebase endpoint with the FEN.
    assert box["url"].endswith("/standard")
    assert box["params"]["fen"] == _FEN_KQK


def test_probe_skips_when_too_many_men(monkeypatch):
    called = {"n": 0}

    def fake_get(*a, **k):
        called["n"] += 1
        return _FakeResponse({"category": "draw"})

    monkeypatch.setattr(tablebase.httpx, "get", fake_get)
    # Standard start position has 32 pieces — never probed.
    assert tablebase.probe("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1") is None
    assert called["n"] == 0


def test_probe_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(tablebase.config, "TABLEBASE_ENABLED", False)
    res = tablebase.probe(_FEN_KQK)
    assert res is None


def test_network_failure_is_none_and_not_cached(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(tablebase.httpx, "get", boom)
    assert tablebase.probe(_FEN_KQK) is None
    assert _FEN_KQK not in tablebase._cache  # a transient failure must not poison the cache


def test_flip_inverts_perspective():
    res = {"category": "win", "dtz": 10, "dtm": 8, "men": 4}
    flipped = tablebase.flip(res)
    assert flipped["category"] == "loss"
    assert flipped["dtz"] == -10 and flipped["dtm"] == -8
    # Draw and cursed/blessed map symmetrically.
    assert tablebase.flip({"category": "draw"})["category"] == "draw"
    assert tablebase.flip({"category": "cursed-win"})["category"] == "blessed-loss"


# --- formatting (claude_bridge) ---------------------------------------------------------------


def test_current_fact_mentions_exact_and_outcome():
    fact = claude_bridge._tablebase_current_fact({"category": "draw", "men": 5})
    assert "EXACT" in fact and "DRAW" in fact


def test_move_fact_flags_thrown_away_result():
    before = {"category": "win", "men": 5, "dtm": 12, "dtz": 12}
    after = {"category": "draw", "men": 5}
    fact = claude_bridge._tablebase_move_fact(before, after, "Kf6")
    assert "threw away" in fact and "Kf6" in fact


def test_move_fact_holds_result():
    before = {"category": "win", "men": 5, "dtm": 12, "dtz": 12}
    after = {"category": "win", "men": 5, "dtm": 11, "dtz": 11}
    fact = claude_bridge._tablebase_move_fact(before, after, "Qd5")
    assert "holds the result" in fact


def test_criticality_only_move():
    info = {
        "win_percent": 95.0,
        "lines": [
            {"win_percent": 95.0, "line_san": ["Qd5"]},
            {"win_percent": 60.0, "line_san": ["Qa1"]},
        ],
    }
    out = claude_bridge._criticality(info)
    assert out and "ONLY good move" in out


def test_criticality_silent_when_alternatives_close():
    info = {
        "win_percent": 70.0,
        "lines": [
            {"win_percent": 70.0, "line_san": ["Nf3"]},
            {"win_percent": 68.0, "line_san": ["Nc3"]},
        ],
    }
    assert claude_bridge._criticality(info) is None


# --- positive coaching: strengths + sound sacrifices ------------------------------------------

from server.core.session import MoveReview, ReviewSession  # noqa: E402


def _move(**kw) -> MoveReview:
    base = dict(
        ply=1,
        move_number=10,
        color="white",
        move_san="Ng5",
        move_uci="f3g5",
        fen_before="",
        fen_after="",
        eval_before=0.0,
        eval_after=0.0,
        win_before=55.0,
        win_after=50.0,
        win_swing=5.0,
        classification="good",
        best_move_san="Ng5",
        accuracy=90.0,
    )
    base.update(kw)
    return MoveReview(**base)


def test_sacrifice_fires_on_quiet_piece_offer():
    # White's knight steps to g5 where the h6 pawn wins it for nothing, yet the move is sound.
    m = _move(
        fen_before="7k/8/7p/8/8/5N2/8/4K3 w - - 0 1",
        fen_after="7k/8/7p/6N1/8/8/8/4K3 b - - 1 1",
        move_san="Ng5",
        move_uci="f3g5",
    )
    d = claude_bridge._sac_detail(m)
    assert d is not None
    assert d["invested"] == 3 and d["is_capture"] is False and d["quiet"] == 1


def test_sacrifice_fires_on_capturing_sac_with_check():
    # Bxh7+ gives a bishop (3) for a pawn (1): a net 2-point sacrifice, with check.
    m = _move(
        fen_before="6k1/5ppp/8/8/8/8/8/2B1K3 w - - 0 1",
        fen_after="6k1/5ppB/8/8/8/8/8/4K3 b - - 0 1",
        move_san="Bxh7+",
        move_uci="c1h7",
    )
    d = claude_bridge._sac_detail(m)
    assert d is not None
    assert d["invested"] == 2 and d["captured"] == 1 and d["gives_check"] is True


def test_sacrifice_ignores_even_recapture_trap():
    # Nxd5 captures a knight and is recaptured by the e6 pawn — an even trade, not a sacrifice.
    m = _move(
        fen_before="7k/8/4p3/3n4/5N2/8/8/4K3 w - - 0 1",
        fen_after="7k/8/4p3/3N4/8/8/8/4K3 b - - 0 1",
        move_san="Nxd5",
        move_uci="f4d5",
    )
    assert claude_bridge._sac_detail(m) is None


def test_sacrifices_skips_already_winning_and_unsound():
    sac = dict(
        fen_before="7k/8/7p/8/8/5N2/8/4K3 w - - 0 1",
        fen_after="7k/8/7p/6N1/8/8/8/4K3 b - - 1 1",
        move_san="Ng5",
        move_uci="f3g5",
    )
    winning = _move(win_before=92.0, win_after=90.0, **sac)  # already crushing — not a feat
    unsound = _move(win_before=55.0, win_after=20.0, **sac)  # engine says it loses — not sound
    blunder = _move(classification="blunder", **sac)  # not a "good"/"best" move
    sess = ReviewSession(pgn="", player="white", all_moves=[winning, unsound, blunder])
    assert claude_bridge._sacrifices(sess) == []


def test_strengths_clean_conversion():
    moves = [
        _move(ply=1, move_number=20, win_before=85.0, classification="best"),
        _move(ply=3, move_number=21, win_before=88.0, classification="good"),
        _move(ply=5, move_number=22, win_before=90.0, classification="best"),
        _move(ply=7, move_number=23, win_before=95.0, classification="good"),
    ]
    sess = ReviewSession(pgn="", player="white", result="1-0", all_moves=moves)
    out = claude_bridge._strengths(sess)
    assert any("Clean conversion" in s for s in out)


def test_strengths_silent_when_winning_then_blundered():
    moves = [
        _move(ply=1, move_number=20, win_before=85.0, classification="best"),
        _move(ply=3, move_number=21, win_before=88.0, classification="blunder"),
        _move(ply=5, move_number=22, win_before=40.0, classification="good"),
    ]
    # Winning but threw it away (and lost) — no clean-conversion praise, low accuracy.
    sess = ReviewSession(
        pgn="", player="white", result="0-1", accuracy_white=60.0, accuracy_black=85.0, all_moves=moves
    )
    out = claude_bridge._strengths(sess)
    assert not any("Clean conversion" in s for s in out)


def test_strengths_high_accuracy():
    sess = ReviewSession(
        pgn="",
        player="black",
        result="0-1",
        accuracy_white=70.0,
        accuracy_black=93.0,
        all_moves=[_move(color="black")],
    )
    out = claude_bridge._strengths(sess)
    assert any("High accuracy" in s and "93" in s for s in out)
