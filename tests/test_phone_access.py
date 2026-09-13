"""Phone access: the shared "can a phone open the board, and where" state behind the 📱 Phone popover
and the AI chat, plus the chat picking it up on phone questions."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import claude_bridge
from server import config
from server.web import app as app_module


@pytest.fixture
def net(monkeypatch):
    """A computer on Wi-Fi at 192.168.1.20 serving on port 8765; tests set the saved/bound hosts."""
    monkeypatch.setattr(config, "WEB_PORT", 8765)
    monkeypatch.setattr(config, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(config, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "WEB_BOUND_HOST", "127.0.0.1")
    return monkeypatch


def test_off_reports_the_address_it_would_have(net):
    st = config.phone_access()
    assert st == {"active": False, "enabled": False, "restart_needed": False,
                  "url": "http://192.168.1.20:8765", "port": 8765}


def test_saved_but_not_restarted_needs_a_restart(net):
    net.setattr(config, "WEB_HOST", "0.0.0.0")  # just saved in Settings; still bound to loopback
    st = config.phone_access()
    assert st["enabled"] and not st["active"] and st["restart_needed"]
    assert st["url"] == "http://192.168.1.20:8765"
    # What works *right now* is still loopback-only.
    assert config.board_url() == "http://127.0.0.1:8765"
    assert config.lan_board_url() is None


def test_active_after_restart(net):
    net.setattr(config, "WEB_HOST", "0.0.0.0")
    net.setattr(config, "WEB_BOUND_HOST", "0.0.0.0")
    st = config.phone_access()
    assert st["active"] and st["enabled"] and not st["restart_needed"]
    assert st["url"] == "http://192.168.1.20:8765"
    assert config.board_url() == "http://127.0.0.1:8765"


def test_no_network_address(net):
    net.setattr(config, "lan_ip", lambda: None)
    net.setattr(config, "WEB_HOST", "0.0.0.0")
    net.setattr(config, "WEB_BOUND_HOST", "0.0.0.0")
    st = config.phone_access()
    assert st["active"] and st["url"] is None


def test_phone_access_endpoint(net):
    net.setattr(config, "WEB_HOST", "0.0.0.0")
    net.setattr(config, "WEB_BOUND_HOST", "0.0.0.0")
    body = TestClient(app_module.create_app()).get("/api/phone-access").json()
    assert body["active"] is True and body["url"] == "http://192.168.1.20:8765"


@pytest.mark.parametrize("q", [
    "how do I use this on my phone?",
    "can I open it on my iPad",
    "what IP address do I type?",
    "does it work on another device?",
    "how do I see this on mobile",
    "my phone won't load it, could it be the firewall or my VPN?",
])
def test_phone_questions_count_as_app_questions(q):
    assert claude_bridge._looks_like_app_question(q)


@pytest.mark.parametrize("q", [
    "why is this move bad?",
    "what should I play here?",
    "is the knight better on e5 than d4?",
])
def test_chess_questions_do_not(q):
    assert not claude_bridge._looks_like_app_question(q)


def _prompt(question):
    return claude_bridge._compose_prompt(question, None, None, None, None, None)


def test_chat_gives_the_exact_address_when_on(net):
    net.setattr(config, "WEB_HOST", "0.0.0.0")
    net.setattr(config, "WEB_BOUND_HOST", "0.0.0.0")
    p = _prompt("how do I use this on my phone?")
    assert "PHONE ACCESS RIGHT NOW: ON" in p
    assert "http://192.168.1.20:8765" in p
    assert "stay on with the app running" in p


def test_chat_explains_how_to_turn_it_on_when_off(net):
    p = _prompt("how do I use this on my phone?")
    assert "PHONE ACCESS RIGHT NOW: OFF" in p
    assert "Allow other devices on my network to connect" in p
    assert "http://192.168.1.20:8765" in p


def test_chat_mentions_restart_when_pending(net):
    net.setattr(config, "WEB_HOST", "0.0.0.0")
    assert "restarted" in _prompt("how do I use this on my phone?")


def test_chat_knows_the_connection_troubleshooting(net):
    # Same list as the popover's "Problems connecting?" section.
    p = _prompt("my phone can't connect, why?")
    for hint in ("same Wi-Fi", "guest", "firewall", "router", "VPN", "Not secure"):
        assert hint in p, hint


def test_pure_chess_question_gets_no_phone_facts(net):
    assert "PHONE ACCESS" not in _prompt("why is this move bad?")


def test_phone_facts_never_break_the_prompt(net):
    def boom():
        raise RuntimeError("no network")
    net.setattr(config, "phone_access", boom)
    assert "PHONE ACCESS" not in _prompt("how do I use this on my phone?")
