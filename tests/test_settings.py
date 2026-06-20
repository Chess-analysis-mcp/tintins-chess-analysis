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
        for k in ("USERNAME", "USERNAME_ALIASES", "LICHESS_TOKEN", "PROFILE_RECENT_WINDOW", "PROFILE_LIFETIME")
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
