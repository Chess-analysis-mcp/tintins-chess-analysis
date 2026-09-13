"""User settings store: persistence + applying over live config (precedence: settings > env)."""
from __future__ import annotations

import json

import pytest

from server import config
from server.core import settings


@pytest.fixture
def restore_config():
    """Snapshot + restore the config attributes settings mutates (so tests don't leak)."""
    saved = {
        k: getattr(config, k)
        for k in ("USERNAME", "USERNAME_ALIASES", "LICHESS_TOKEN", "PROFILE_RECENT_WINDOW",
                  "PROFILE_LIFETIME", "COACH_AI_AUTO", "PERSONALIZE_HISTORY", "WEB_HOST")
    }
    yield
    for k, v in saved.items():
        setattr(config, k, v)


def test_apply_overrides_live_config(restore_config):
    settings.apply(
        {"username": "Newbie", "aliases": "chesscom:foo, bar", "lichess_token": "tok",
         "profile_lifetime": "0"}
    )
    assert config.USERNAME == "Newbie"
    assert config.USERNAME_ALIASES == [("chesscom", "foo"), (None, "bar")]
    assert config.LICHESS_TOKEN == "tok"
    assert config.PROFILE_LIFETIME == 0  # "0" => lifetime view disabled


def test_update_persists_and_apply_saved_reloads(tmp_path, restore_config):
    d = str(tmp_path)
    eff = settings.update({"username": "Mary", "profile_lifetime": "all"}, data_dir=d)
    assert eff["username"] == "Mary"
    assert json.loads((tmp_path / "settings.json").read_text())["username"] == "Mary"

    # A fresh process: env defaults, then settings.json overrides them.
    config.USERNAME = "from_env"
    settings.apply_saved(data_dir=d)
    assert config.USERNAME == "Mary"


def test_effective_round_trips_lifetime(restore_config):
    settings.apply({"profile_lifetime": "all"})
    assert settings.effective()["profile_lifetime"] == "all"
    settings.apply({"profile_lifetime": "50"})
    assert settings.effective()["profile_lifetime"] == "50"


def test_coach_ai_auto_toggle_persists(tmp_path, restore_config):
    d = str(tmp_path)
    settings.update({"coach_ai_auto": True}, data_dir=d)  # opt-in (default is off)
    assert config.COACH_AI_AUTO is True
    assert json.loads((tmp_path / "settings.json").read_text())["coach_ai_auto"] is True
    # A fresh process picks the saved value back up over the env/default.
    config.COACH_AI_AUTO = False
    settings.apply_saved(data_dir=d)
    assert config.COACH_AI_AUTO is True
    assert settings.effective()["coach_ai_auto"] is True


def test_clean_path_strips_surrounding_quotes():
    # Windows "Copy as path" wraps in double-quotes; pasting verbatim must not break lookup.
    assert config.clean_path('"C:\\chess\\stockfish.exe"') == "C:\\chess\\stockfish.exe"
    assert config.clean_path("'/usr/local/bin/stockfish'") == "/usr/local/bin/stockfish"
    assert config.clean_path('  "/usr/local/bin/stockfish"  ') == "/usr/local/bin/stockfish"
    # Unquoted paths and blanks are untouched.
    assert config.clean_path("/usr/local/bin/stockfish") == "/usr/local/bin/stockfish"
    assert config.clean_path("") == ""
    assert config.clean_path(None) == ""


def test_update_strips_quotes_from_stockfish_path(tmp_path, restore_config):
    d = str(tmp_path)
    settings.update({"stockfish_path": '"/some/where/stockfish"'}, data_dir=d)
    # Persisted clean, not with the literal quote characters.
    saved = json.loads((tmp_path / "settings.json").read_text())["stockfish_path"]
    assert saved == "/some/where/stockfish"
    assert config.STOCKFISH_PATH == "/some/where/stockfish"


def test_web_host_toggle_persists(tmp_path, restore_config):
    d = str(tmp_path)
    settings.update({"web_host": "0.0.0.0"}, data_dir=d)  # opt-in to LAN access (default is loopback)
    assert config.WEB_HOST == "0.0.0.0"
    assert json.loads((tmp_path / "settings.json").read_text())["web_host"] == "0.0.0.0"
    # A fresh process picks the saved value back up over the env/default.
    config.WEB_HOST = "127.0.0.1"
    settings.apply_saved(data_dir=d)
    assert config.WEB_HOST == "0.0.0.0"
    assert settings.effective()["web_host"] == "0.0.0.0"


def test_web_host_blank_falls_back_to_loopback(restore_config):
    config.WEB_HOST = "0.0.0.0"
    settings.apply({"web_host": ""})
    assert config.WEB_HOST == "127.0.0.1"


@pytest.mark.parametrize("bad", ["not a host!", "0.0.0.0:8765", True, 123, ["0.0.0.0"]])
def test_invalid_saved_web_host_falls_back_to_loopback(bad, restore_config):
    # A hand-edited settings.json must never stop the app starting (a bool used to crash apply(),
    # a typo used to make uvicorn fail to bind): fall back to loopback instead.
    config.WEB_HOST = "0.0.0.0"
    settings.apply({"web_host": bad})
    assert config.WEB_HOST == "127.0.0.1"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "127.0.0.1"), ("", "127.0.0.1"), ("  0.0.0.0 ", "0.0.0.0"), ("[::]", "::"),
        ("LOCALHOST", "localhost"), ("192.168.1.20", "192.168.1.20"), ("::1", "::1"),
        ("my-laptop.local", None), ("0.0.0.0:8765", None), (7, None),
    ],
)
def test_normalize_web_host(raw, expected):
    assert config.normalize_web_host(raw) == expected


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1", "http://127.0.0.1:8765"), ("0.0.0.0", "http://127.0.0.1:8765"),
        ("::", "http://127.0.0.1:8765"), ("", "http://127.0.0.1:8765"),
        ("192.168.1.20", "http://192.168.1.20:8765"), ("::1", "http://[::1]:8765"),
    ],
)
def test_board_url_is_always_browsable(host, expected, monkeypatch):
    # http://0.0.0.0 can't be opened on Windows, so a wildcard bind must print/open loopback.
    monkeypatch.setattr(config, "WEB_HOST", host)
    monkeypatch.setattr(config, "WEB_PORT", 8765)
    assert config.board_url() == expected


def test_lan_board_url_only_when_reachable_from_other_devices(monkeypatch):
    monkeypatch.setattr(config, "WEB_PORT", 8765)
    monkeypatch.setattr(config, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    assert config.lan_board_url() is None
    monkeypatch.setattr(config, "WEB_HOST", "0.0.0.0")
    assert config.lan_board_url() == "http://192.168.1.20:8765"
    monkeypatch.setattr(config, "WEB_HOST", "10.0.0.5")  # bound to one specific interface
    assert config.lan_board_url() == "http://10.0.0.5:8765"
    monkeypatch.setattr(config, "WEB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "lan_ip", lambda: None)  # offline / no network interface
    assert config.lan_board_url() is None


def test_personalize_history_toggle_persists(tmp_path, restore_config):
    d = str(tmp_path)
    settings.update({"personalize_history": False}, data_dir=d)  # opt-out (default is on)
    assert config.PERSONALIZE_HISTORY is False
    assert json.loads((tmp_path / "settings.json").read_text())["personalize_history"] is False
    # A fresh process picks the saved value back up over the env/default.
    config.PERSONALIZE_HISTORY = True
    settings.apply_saved(data_dir=d)
    assert config.PERSONALIZE_HISTORY is False
    assert settings.effective()["personalize_history"] is False
