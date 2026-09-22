"""Credentials must never reach another device.

The board can be opened from a phone on the same Wi-Fi (Settings -> "Allow other devices on my
network to connect"), and that phone can open the Settings panel too. These tests pin the rule that
every key in `settings.SECRET_KEYS` is withheld from a non-local client, on the way out AND on the
way back from a save, and that such a client's blank field never wipes the stored value.
"""
from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from server import config
from server.core import settings as settings_mod
from server.web.app import create_app

# One representative value per secret, so a leak names the field that leaked.
_SECRETS = {
    "lichess_token": "lip_tok_secret",
    "local_llm_api_key": "sk-or-v1-secret",
    "anthropic_api_key": "sk-ant-secret",
}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir and start from no saved settings."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    for key, value in _SECRETS.items():
        monkeypatch.setattr(config, key.upper() if key != "lichess_token" else "LICHESS_TOKEN",
                            "", raising=False)
    return tmp_path


def _client(local: bool) -> TestClient:
    """A TestClient that looks like this computer, or like a phone on the LAN."""
    app = create_app()
    if local:
        return TestClient(app)
    # A non-loopback peer: `is_local_client` returns False, as for a real device on the network.
    return TestClient(app, client=("192.168.1.50", 54321))


def test_secret_keys_covers_every_credential_setting():
    """A new credential must be added to SECRET_KEYS, or it silently skips all of this."""
    assert set(_SECRETS) == set(settings_mod.SECRET_KEYS)
    # And every one of them is actually a saveable setting.
    for key in settings_mod.SECRET_KEYS:
        assert key in settings_mod.KEYS


def test_local_client_can_read_and_write_secrets(app_env):
    local = _client(True)
    res = local.post("/api/settings", json=_SECRETS).json()
    assert res["secrets_hidden"] == []
    for key, value in _SECRETS.items():
        assert res["settings"][key] == value
    got = local.get("/api/settings").json()
    assert got["secrets_hidden"] == []
    for key, value in _SECRETS.items():
        assert got["settings"][key] == value


def test_get_withholds_every_secret_from_a_remote_client(app_env):
    _client(True).post("/api/settings", json=_SECRETS)
    body = _client(False).get("/api/settings")
    assert body.status_code == 200
    data = body.json()
    assert sorted(data["secrets_hidden"]) == sorted(_SECRETS)
    for key in _SECRETS:
        assert data["settings"][key] == ""
    # Nothing anywhere else in the payload leaks them either (flags, hints, data_dir, ...).
    raw = json.dumps(data)
    for value in _SECRETS.values():
        assert value not in raw


def test_post_response_withholds_every_secret_from_a_remote_client(app_env):
    """The echoed-back settings need the same redaction as GET: saving an unrelated field from a
    phone must not hand that phone the stored keys."""
    _client(True).post("/api/settings", json=_SECRETS)
    body = _client(False).post("/api/settings", json={"player_elo": "1400"})
    assert body.status_code == 200
    data = body.json()
    assert sorted(data["secrets_hidden"]) == sorted(_SECRETS)
    raw = json.dumps(data)
    for value in _SECRETS.values():
        assert value not in raw


def test_remote_blank_does_not_wipe_a_stored_secret(app_env):
    """A phone's Settings form shows blanks (it never got the values), so saving must keep them."""
    _client(True).post("/api/settings", json=_SECRETS)
    _client(False).post("/api/settings", json={key: "" for key in _SECRETS})
    kept = _client(True).get("/api/settings").json()["settings"]
    for key, value in _SECRETS.items():
        assert kept[key] == value


def test_remote_can_still_change_a_secret_it_types(app_env):
    """Withholding is about not leaking; a deliberate new value still saves."""
    _client(True).post("/api/settings", json=_SECRETS)
    _client(False).post("/api/settings", json={"anthropic_api_key": "sk-ant-typed-on-phone"})
    kept = _client(True).get("/api/settings").json()["settings"]
    assert kept["anthropic_api_key"] == "sk-ant-typed-on-phone"
    assert kept["lichess_token"] == _SECRETS["lichess_token"]  # untouched


def test_local_client_can_clear_a_secret(app_env):
    _client(True).post("/api/settings", json=_SECRETS)
    _client(True).post("/api/settings", json={"anthropic_api_key": ""})
    assert _client(True).get("/api/settings").json()["settings"]["anthropic_api_key"] == ""


def test_settings_file_is_owner_only(app_env):
    _client(True).post("/api/settings", json=_SECRETS)
    path = os.path.join(str(app_env), "settings.json")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if os.name == "posix":
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0, oct(mode)


def test_redact_only_touches_non_empty_values():
    values = {"lichess_token": "", "anthropic_api_key": "sk-ant-x", "username": "tintin"}
    hidden = settings_mod.redact(values)
    assert hidden == ["anthropic_api_key"]
    assert values == {"lichess_token": "", "anthropic_api_key": "", "username": "tintin"}


# --- platform behaviour --------------------------------------------------------------------------


def test_file_mode_keeps_the_file_writable():
    """On Windows os.chmod only honours the read-only attribute: a mode without the owner-write bit
    would mark settings.json read-only and break the next save."""
    assert settings_mod.FILE_MODE & stat.S_IWRITE


def test_save_survives_a_filesystem_that_refuses_chmod(app_env, monkeypatch):
    """Tightening permissions is best-effort (Windows, network shares, exotic filesystems): if
    chmod fails, the settings must still be written."""

    def refuse(*args, **kwargs):
        raise OSError("chmod is not supported here")

    monkeypatch.setattr(settings_mod.os, "chmod", refuse)
    settings_mod.save({"username": "tintin", "anthropic_api_key": "sk-ant-x"}, str(app_env))
    assert settings_mod.load(str(app_env))["username"] == "tintin"
