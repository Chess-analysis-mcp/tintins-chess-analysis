"""Pairing key for network access: with the board listening beyond loopback, another device must
present the key (scanned from the 📱 Phone QR code) before it can use any route."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.web import app as app_module
from server.web import pairing

PHONE = ("192.168.1.50", 50000)
BOARD = "192.168.1.20:8765"


@pytest.fixture
def lan(monkeypatch):
    """Network access on (bound to every interface) with a known pairing key."""
    monkeypatch.setattr(config, "WEB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "WEB_BOUND_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "WEB_PORT", 8765)
    monkeypatch.setattr(config, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(pairing, "_token", "test-key")
    return monkeypatch


def _phone():
    return TestClient(
        app_module.create_app(), client=PHONE, base_url=f"http://{BOARD}", follow_redirects=False
    )


def _computer():
    return TestClient(app_module.create_app(), follow_redirects=False)


def _paired():
    c = _phone()
    assert c.get("/?pair=test-key").status_code == 303
    return c


def test_unpaired_device_is_refused(lan):
    c = _phone()
    r = c.get("/api/app-config")
    assert r.status_code == 401 and "Not paired" in r.json()["error"]
    page = c.get("/")
    assert page.status_code == 401 and "QR code" in page.text


def test_valid_key_sets_cookie_and_redirects_without_the_key(lan):
    r = _phone().get("/?pair=test-key")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "chess_pair=test-key" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_paired_device_can_use_the_board(lan):
    assert _paired().get("/api/app-config").status_code == 200


@pytest.mark.parametrize("bad", ["wrong", "", "test-key-extra", "tést"])
def test_wrong_key_is_refused(lan, bad):
    c = _phone()
    r = c.get("/api/app-config", params={"pair": bad})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers
    assert c.get("/api/app-config").status_code == 401


def test_forged_cookie_is_refused(lan):
    c = _phone()
    c.cookies.set("chess_pair", "guess")
    assert c.get("/api/app-config").status_code == 401


def test_key_in_query_does_not_authorize_posts(lan):
    assert _phone().post("/api/chat-reset?pair=test-key").status_code == 401


def test_cross_origin_post_from_paired_device_is_blocked(lan):
    c = _paired()
    assert c.post("/api/chat-reset", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/chat-reset", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/chat-reset", headers={"Origin": f"http://{BOARD}"}).status_code == 200


def test_this_computer_never_needs_the_key(lan):
    c = _computer()
    assert c.get("/api/app-config").status_code == 200
    assert c.post("/api/chat-reset").status_code == 200


def test_loopback_only_board_does_not_require_pairing(monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    r = TestClient(app_module.create_app(), client=PHONE).get("/api/app-config")
    # Unchanged behaviour: the Host/Origin guard handles loopback, no pairing involved.
    assert r.status_code == 200


def test_this_computer_gets_the_pairing_link(lan):
    body = _computer().get("/api/phone-access").json()
    assert body["url"] == f"http://{BOARD}/?pair=test-key"
    assert body["address"] == f"http://{BOARD}"


def test_key_is_never_sent_to_other_devices(lan):
    c = _paired()
    body = c.get("/api/phone-access")
    assert "test-key" not in body.text
    assert body.json()["url"] == f"http://{BOARD}"
    for path in ("/api/settings", "/api/app-config", "/api/doctor"):
        assert "test-key" not in c.get(path).text


def test_key_changes_each_launch():
    first = pairing.get_token()
    assert pairing.reset_token() != first
