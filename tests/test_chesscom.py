"""Tests for the Chess.com fetch layer (network mocked — never hits chess.com)."""
from __future__ import annotations

import httpx
import pytest

from server.core import chesscom

_PGN = (
    '[Event "Live Chess"]\n[Site "Chess.com"]\n'
    '[White "alice"]\n[Black "bob"]\n[WhiteElo "1600"]\n[BlackElo "1550"]\n'
    '[TimeControl "300"]\n[Result "1-0"]\n'
    '[ECOUrl "https://www.chess.com/openings/Petrovs-Defense-3.Nxe5"]\n'
    '[Link "https://www.chess.com/game/live/123456789"]\n\n1. e4 e5 2. Nf3 1-0\n'
)


def _game(gid: str = "123456789", end_time: int = 1_700_000_000, white_result: str = "win") -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{gid}",
        "pgn": _PGN,
        "time_class": "blitz",
        "end_time": end_time,
        "rules": "chess",
        "white": {"username": "alice", "rating": 1600, "result": white_result},
        "black": {"username": "bob", "rating": 1550, "result": "checkmated" if white_result == "win" else "win"},
    }


@pytest.fixture
def fake_api(monkeypatch):
    """Programmable fake: maps URL substrings to JSON payloads."""
    box: dict = {"routes": {}, "urls": []}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = ""

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        box["urls"].append(url)
        for frag, payload in box["routes"].items():
            if frag in url:
                return _Resp(payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(chesscom.httpx, "get", fake_get)
    return box


def test_fetch_user_games_newest_first_across_archives(fake_api):
    fake_api["routes"] = {
        "/games/archives": {
            "archives": [
                "https://api.chess.com/pub/player/alice/games/2023/10",
                "https://api.chess.com/pub/player/alice/games/2023/11",
            ]
        },
        "/2023/11": {"games": [_game("new1", 1_700_000_000), _game("new2", 1_700_100_000)]},
        "/2023/10": {"games": [_game("old1", 1_690_000_000)]},
    }
    games = chesscom.fetch_user_games("alice", max=3)
    assert [g.game_id for g in games] == ["new2", "new1", "old1"]  # newest month + newest first
    g = games[0]
    assert g.white == "alice" and g.black == "bob"
    assert g.result == "1-0" and g.speed == "blitz"
    assert g.opening == "Petrovs Defense"  # ECOUrl tail, move-suffix trimmed
    assert g.pgn == _PGN
    assert g.to_dict()["url"].endswith("/new2")
    assert "end_time" not in g.to_dict()


def test_max_caps_and_skips_older_archives(fake_api):
    fake_api["routes"] = {
        "/games/archives": {
            "archives": [
                "https://api.chess.com/pub/player/alice/games/2023/10",
                "https://api.chess.com/pub/player/alice/games/2023/11",
            ]
        },
        "/2023/11": {"games": [_game("a", 2), _game("b", 3)]},
        "/2023/10": {"games": [_game("c", 1)]},
    }
    games = chesscom.fetch_user_games("alice", max=2)
    assert len(games) == 2
    # The older month should never have been fetched.
    assert not any("/2023/10" in u for u in fake_api["urls"])


def test_variants_and_pgnless_games_skipped(fake_api):
    odd = _game("odd")
    odd["rules"] = "chess960"
    empty = _game("empty")
    empty["pgn"] = ""
    fake_api["routes"] = {
        "/games/archives": {"archives": ["https://api.chess.com/pub/player/alice/games/2023/11"]},
        "/2023/11": {"games": [odd, empty, _game("ok")]},
    }
    games = chesscom.fetch_user_games("alice", max=10)
    assert [g.game_id for g in games] == ["ok"]


_PGN_FALLBACK = (
    '[Event "Live Chess"]\n[Site "Chess.com"]\n[Date "2026.06.26"]\n'
    '[White "alice"]\n[Black "carol"]\n[WhiteElo "1700"]\n[BlackElo "1680"]\n'
    '[TimeControl "600"]\n[Result "1-0"]\n'
    '[ECOUrl "https://www.chess.com/openings/Scandinavian-Defense-Mieses-Kotrc-Variation"]\n'
    '[EndDate "2026.06.26"]\n[EndTime "23:36:58"]\n'
    '[Link "https://www.chess.com/game/live/999888777"]\n\n1. e4 d5 2. exd5 1-0\n'
)


class _StatusResp:
    """A fake httpx response with a settable status_code/text/json."""

    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            import json as _json

            raise _json.JSONDecodeError("no json", "", 0)
        return self._payload


def test_broken_archive_404_falls_back_to_pgn_export(monkeypatch):
    """A real month whose JSON archive 404s with an internal-error body is served via /pgn."""
    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        if url.endswith("/games/archives"):
            return _StatusResp(payload={"archives": ["https://api.chess.com/pub/player/alice/games/2026/06"]})
        if url.endswith("/2026/06/pgn"):
            return _StatusResp(text=_PGN_FALLBACK)
        if url.endswith("/2026/06"):
            return _StatusResp(status_code=404, text='{"code":0,"message":"An internal error has occurred."}')
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(chesscom.httpx, "get", fake_get)
    games = chesscom.fetch_user_games("alice", max=5)
    assert [g.game_id for g in games] == ["999888777"]
    g = games[0]
    assert g.white == "alice" and g.black == "carol"
    assert g.white_elo == 1700 and g.result == "1-0"
    assert g.speed == "rapid"  # 600s -> rapid, derived from TimeControl
    assert g.opening == "Scandinavian Defense Mieses Kotrc Variation"
    assert g.date == "2026.06.26"


def test_phantom_future_month_404_is_skipped_not_fetched_via_pgn(monkeypatch):
    """A 'Date cannot be set in the future' 404 is a real phantom month — no /pgn fallback."""
    urls: list[str] = []

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        urls.append(url)
        if url.endswith("/games/archives"):
            return _StatusResp(payload={
                "archives": [
                    "https://api.chess.com/pub/player/alice/games/2023/11",
                    "https://api.chess.com/pub/player/alice/games/2027/01",
                ]
            })
        if url.endswith("/2027/01"):
            return _StatusResp(status_code=404, text='{"code":0,"message":"Date cannot be set in the future"}')
        if url.endswith("/2023/11"):
            return _StatusResp(payload={"games": [_game("real", 1_700_000_000)]})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(chesscom.httpx, "get", fake_get)
    games = chesscom.fetch_user_games("alice", max=5)
    assert [g.game_id for g in games] == ["real"]
    assert not any(u.endswith("/2027/01/pgn") for u in urls)  # no wasted /pgn probe on a future month


def test_result_mapping():
    draw = _game(white_result="agreed")
    draw["black"]["result"] = "agreed"
    assert chesscom._summary_from_json(draw).result == "1/2-1/2"
    black_win = _game(white_result="checkmated")
    black_win["black"]["result"] = "win"
    assert chesscom._summary_from_json(black_win).result == "0-1"


def test_username_me_resolves_to_config(fake_api, monkeypatch):
    monkeypatch.setattr(chesscom.config, "CHESSCOM_USERNAME", "myhandle")
    fake_api["routes"] = {"/games/archives": {"archives": []}}
    chesscom.fetch_user_games("me")
    assert any("/pub/player/myhandle/" in u for u in fake_api["urls"])


def test_empty_username_errors(monkeypatch):
    monkeypatch.setattr(chesscom.config, "CHESSCOM_USERNAME", "")
    with pytest.raises(chesscom.ChesscomError, match="username is required"):
        chesscom.fetch_user_games("")


def test_network_error_wrapped(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(chesscom.httpx, "get", boom)
    with pytest.raises(chesscom.ChesscomError, match="Could not reach Chess.com"):
        chesscom.fetch_user_games("alice")


def test_404_is_friendly(monkeypatch):
    class _Resp:
        status_code = 404
        text = ""

    monkeypatch.setattr(chesscom.httpx, "get", lambda *a, **k: _Resp())
    with pytest.raises(chesscom.ChesscomError, match="404"):
        chesscom.fetch_user_games("nobody")
