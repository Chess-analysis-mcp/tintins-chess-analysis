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
