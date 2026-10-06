"""Who "me" is across history: case-insensitive ids, handles learned from uploads, the legacy "me"
bucket, and the user's profile spanning every id their games were stored under. All on a temp
DATA_DIR with hand-built records: no engine, no network."""
from __future__ import annotations

import pytest

from server import config
from server.core import history
from server.core.game_analysis import resolve_player


@pytest.fixture(autouse=True)
def _tmp_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "HISTORY_ENABLED", True)
    config._compose_identity("", "", "")
    yield
    config._compose_identity("", "", "")


def _set_user(lichess="", chesscom=""):
    config._compose_identity(lichess, chesscom, "")


def _add(handle, side="white", site="https://www.chess.com/game/1", game_id=None, player_id=None):
    """Record a game the way record_game does (resolve_identity at write time)."""
    headers = {
        "White": handle if side == "white" else "opp",
        "Black": handle if side == "black" else "opp",
        "Site": site,
    }
    pid, platform, name = history.resolve_identity(headers, side)
    rec = {
        "game_id": game_id or f"g{len(history.load_records())}",
        "reviewed_side": side,
        "player_id": player_id or pid,
        "platform": platform,
        "player_name": name,
        "analyzed_at": "2026-01-01T00:00:00",
        "mistakes": [],
        "counts": {},
        "accuracy": 80,
    }
    history.append_record(rec)
    return rec["player_id"]


def test_capitalised_username_is_stored_lowercase_and_profiled():
    _set_user(chesscom="DJH_24")
    assert _add("DJH_24") == "djh_24" == history.my_player_id()
    assert history.get_my_profile()["games_analyzed"] == 1
    assert history.get_my_profile()["display_name"] == "DJH_24"  # real capitalisation shown


def test_old_records_with_original_capitalisation_still_count():
    _set_user(chesscom="DJH_24")
    _add("DJH_24", player_id="DJH_24")  # written by an older version
    assert len(history.load_records(player_id="djh_24")) == 1
    assert len(history.my_records()) == 1
    assert history.get_my_profile()["games_analyzed"] == 1


def test_uploaded_second_account_shares_one_player_id():
    _set_user(chesscom="DJH_24")
    history.ensure_self_alias("AltAcct", "chesscom")
    _add("DJH_24")
    _add("AltAcct")
    assert history.list_players() == ["djh_24"]
    assert history.get_my_profile()["games_analyzed"] == 2


def test_legacy_me_games_stay_mine_after_setting_a_username():
    # Paste-only user uploads an export (handle filed under "me"), then sets a different username.
    history.ensure_self_alias("PasteGuy", "chesscom")
    _add("PasteGuy")
    _set_user(lichess="LiHandle")
    _add("PasteGuy")  # a new game from the uploaded account
    _add("LiHandle", site="https://lichess.org/abc")
    assert [history.is_my_record(r) for r in history.load_records()] == [True, True, True]
    assert history.get_my_profile()["games_analyzed"] == 3


def test_opponent_games_are_not_mine_once_a_username_is_set():
    _set_user(chesscom="DJH_24")
    _add("DJH_24")
    _add("SomeoneElse")
    assert [r["player_name"] for r in history.my_records()] == ["DJH_24"]
    assert history.get_profile("someoneelse")["games_analyzed"] == 1


def test_no_username_means_whole_history_is_mine():
    _add("DJH_24")
    _add("Other")
    assert len(history.my_records()) == 2
    assert history.insights()["games"] == 2


def test_auto_side_uses_handles_learned_from_uploads():
    history.ensure_self_alias("PasteGuy", "chesscom")
    assert resolve_player({"White": "x", "Black": "PasteGuy"}, "auto") == "black"
    _set_user(lichess="LiHandle")  # still recognised after a username is set
    assert resolve_player({"White": "x", "Black": "PasteGuy"}, "auto") == "black"


def test_my_profile_is_independent_of_the_open_game(monkeypatch):
    from server import claude_bridge
    from server.core import puzzles

    calls = []
    monkeypatch.setattr(history, "get_profile", lambda *a, **k: calls.append("session") or {})
    monkeypatch.setattr(history, "get_my_profile", lambda *a, **k: calls.append("mine") or {})
    puzzles.weakness_themes({})
    claude_bridge._profile_facts()
    assert calls == ["mine", "mine"]
