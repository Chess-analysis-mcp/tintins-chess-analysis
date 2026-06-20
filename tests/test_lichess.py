"""Tests for the Lichess fetch layer (network mocked — never hits lichess.org)."""
from __future__ import annotations

import json

import httpx
import pytest

from server.core import lichess

# A minimal but realistic PGN, as Lichess embeds it in the `pgn` field with pgnInJson=true.
_PGN = (
    '[Event "Rated blitz game"]\n[Site "https://lichess.org/abcd1234"]\n'
    '[White "alice"]\n[Black "bob"]\n[WhiteElo "1600"]\n[BlackElo "1550"]\n'
    '[TimeControl "300+0"]\n[Result "1-0"]\n\n1. e4 e5 2. Nf3 1-0\n'
)


def _record(gid: str = "abcd1234", winner: str = "white") -> dict:
    return {
        "id": gid,
        "speed": "blitz",
        "status": "mate",
        "winner": winner,
        "createdAt": 1_700_000_000_000,
        "players": {
            "white": {"user": {"name": "alice"}, "rating": 1600},
            "black": {"user": {"name": "bob"}, "rating": 1550},
        },
        "opening": {"eco": "C42", "name": "Petrov Defense"},
        "pgn": _PGN,
    }


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def captured(monkeypatch):
    """Capture the outgoing request and return a programmable fake response."""
    box: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        box["url"] = url
        box["params"] = params or {}
        box["headers"] = headers or {}
        return box["response"]

    monkeypatch.setattr(lichess.httpx, "get", fake_get)
    return box


def test_fetch_user_games_parses_ndjson(captured, monkeypatch):
    monkeypatch.setattr(lichess.config, "LICHESS_TOKEN", "")
    body = "\n".join(json.dumps(_record(g)) for g in ("aaaa1111", "bbbb2222"))
    captured["response"] = _FakeResponse(200, body)

    games = lichess.fetch_user_games("alice", max=2)

    assert [g.game_id for g in games] == ["aaaa1111", "bbbb2222"]
    g = games[0]
    assert g.white == "alice" and g.black == "bob"
    assert g.white_elo == 1600 and g.black_elo == 1550
    assert g.result == "1-0" and g.speed == "blitz"
    assert g.opening == "Petrov Defense"
    assert g.date == "2023.11.14"
    assert g.pgn == _PGN  # full PGN preserved for analyze_game
    # Request shape: clocks + pgn embedded, newest first.
    assert captured["url"].endswith("/api/games/user/alice")
    assert captured["params"]["max"] == 2
    assert captured["params"]["pgnInJson"] == "true"
    assert captured["params"]["clocks"] == "true"
    assert captured["params"]["sort"] == "dateDesc"
    assert "Authorization" not in captured["headers"]


def test_token_sets_bearer_header(captured, monkeypatch):
    monkeypatch.setattr(lichess.config, "LICHESS_TOKEN", "tok_secret")
    captured["response"] = _FakeResponse(200, json.dumps(_record()))
    lichess.fetch_user_games("alice", max=1)
    assert captured["headers"]["Authorization"] == "Bearer tok_secret"


def test_username_me_resolves_to_config(captured, monkeypatch):
    monkeypatch.setattr(lichess.config, "USERNAME", "myhandle")
    captured["response"] = _FakeResponse(200, "")
    lichess.fetch_user_games("me")
    assert captured["url"].endswith("/api/games/user/myhandle")


def test_result_mapping(captured):
    captured["response"] = _FakeResponse(200, json.dumps(_record(winner="black")))
    assert lichess.fetch_game("abcd1234").result == "0-1"


def test_draw_result(monkeypatch):
    rec = _record()
    rec.pop("winner")
    rec["status"] = "draw"
    assert lichess._summary_from_json(rec).result == "1/2-1/2"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("abcd1234", "abcd1234"),
        ("https://lichess.org/abcd1234", "abcd1234"),
        ("https://lichess.org/abcd1234/black", "abcd1234"),
        ("https://lichess.org/abcd1234#32", "abcd1234"),
        ("lichess.org/abcd1234/white?foo=1", "abcd1234"),
    ],
)
def test_extract_game_id(raw, expected):
    assert lichess._extract_game_id(raw) == expected


def test_fetch_game_uses_export_endpoint(captured):
    captured["response"] = _FakeResponse(200, json.dumps(_record()))
    g = lichess.fetch_game("https://lichess.org/abcd1234/black")
    assert captured["url"].endswith("/game/export/abcd1234")
    assert g.pgn == _PGN


def test_429_is_friendly(captured):
    captured["response"] = _FakeResponse(429, "")
    with pytest.raises(lichess.LichessError) as exc:
        lichess.fetch_user_games("alice")
    assert "429" in str(exc.value) and "LICHESS_TOKEN" in str(exc.value)


def test_404_is_friendly(captured):
    captured["response"] = _FakeResponse(404, "")
    with pytest.raises(lichess.LichessError, match="404"):
        lichess.fetch_game("nope")


def test_network_error_wrapped(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(lichess.httpx, "get", boom)
    with pytest.raises(lichess.LichessError, match="Could not reach Lichess"):
        lichess.fetch_user_games("alice")


def test_empty_username_errors(monkeypatch):
    monkeypatch.setattr(lichess.config, "USERNAME", "")
    with pytest.raises(lichess.LichessError, match="username is required"):
        lichess.fetch_user_games("")
