"""Network access ("Allow other devices on my network to connect"): the routes that behave differently
for requests from this computer vs. another device, the web_host validation, and the port check that
stops two boards silently sharing one port."""
from __future__ import annotations

import json
import socket

import pytest
from fastapi.testclient import TestClient

from server import config
from server.core import app_liveness
from server.web import app as app_module
from server.web import runner

PHONE = ("192.168.1.50", 50000)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Settings writes go to a temp data dir; config values the routes mutate are restored after."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    for key in ("WEB_HOST", "LICHESS_TOKEN"):
        monkeypatch.setattr(config, key, getattr(config, key))
    return tmp_path


def _client(client_addr=None):
    app = app_module.create_app()
    return TestClient(app, client=client_addr) if client_addr else TestClient(app)


def test_settings_rejects_unbindable_web_host(isolated):
    config.WEB_HOST = "127.0.0.1"
    r = _client().post("/api/settings", json={"web_host": "not a host!"})
    assert r.status_code == 400
    assert config.WEB_HOST == "127.0.0.1"
    assert not (isolated / "settings.json").exists()


def test_settings_stores_normalized_web_host(isolated):
    r = _client().post("/api/settings", json={"web_host": " [::] "})
    assert r.status_code == 200
    assert r.json()["settings"]["web_host"] == "::"
    assert json.loads((isolated / "settings.json").read_text())["web_host"] == "::"


def test_saving_other_settings_leaves_web_host_alone(isolated):
    # The Settings panel only sends web_host when the checkbox changed; a patch without it must not
    # touch a host that came from the environment (e.g. CHESS_WEB_HOST=192.168.1.20).
    config.WEB_HOST = "192.168.1.20"
    r = _client().post("/api/settings", json={"profile_recent": "50"})
    assert r.status_code == 200
    assert config.WEB_HOST == "192.168.1.20"
    assert "web_host" not in json.loads((isolated / "settings.json").read_text())


def test_token_hidden_from_other_devices_but_not_this_computer(isolated):
    config.LICHESS_TOKEN = "lip_secret"
    local = _client().get("/api/settings").json()
    assert local["settings"]["lichess_token"] == "lip_secret"
    assert local["lichess_token_hidden"] is False
    phone = _client(PHONE).get("/api/settings").json()
    assert phone["settings"]["lichess_token"] == ""
    assert phone["lichess_token_hidden"] is True


def test_blank_token_from_another_device_does_not_wipe_it(isolated):
    config.LICHESS_TOKEN = "lip_secret"
    r = _client(PHONE).post("/api/settings", json={"lichess_token": "", "profile_recent": "50"})
    assert r.status_code == 200
    assert config.LICHESS_TOKEN == "lip_secret"
    # This computer can still clear it on purpose.
    _client().post("/api/settings", json={"lichess_token": ""})
    assert config.LICHESS_TOKEN == ""


@pytest.mark.parametrize("path, fn", [("/api/closing", "closing"), ("/api/ping", "beat")])
def test_quit_signals_only_count_from_this_computer(path, fn, monkeypatch):
    # In app mode a phone closing its tab used to quit the app on the computer.
    calls = []
    monkeypatch.setattr(app_liveness, fn, lambda: calls.append(fn))
    assert _client(PHONE).post(path).status_code == 200
    assert calls == []
    assert _client().post(path).status_code == 200
    assert calls == [fn]


@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_client_forms_count_as_this_computer(client_host, monkeypatch):
    calls = []
    monkeypatch.setattr(app_liveness, "closing", lambda: calls.append(1))
    _client((client_host, 50000)).post("/api/closing")
    assert calls == [1]


def test_port_check_detects_an_existing_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)  # nothing accept()s here, so leave backlog room for each probe's connection
    port = srv.getsockname()[1]
    try:
        assert runner.board_port_in_use("0.0.0.0", port)
        assert runner.board_port_in_use("127.0.0.1", port)
        assert not runner.board_port_in_use("192.168.1.20", port)  # a specific LAN bind can't clash
    finally:
        srv.close()
    assert not runner.board_port_in_use("0.0.0.0", port)
