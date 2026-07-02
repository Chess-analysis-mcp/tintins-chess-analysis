"""Tests for the direct-HTTP local-LLM client (network mocked — never hits a real server).

Also covers that `claude_bridge` routes to the local client (and never spawns `claude`) when a
local-LLM URL is configured.
"""
from __future__ import annotations

import httpx
import pytest

from server import claude_bridge, config
from server.core import local_llm


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok_payload(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture
def local_on(monkeypatch):
    """Configure a local-LLM URL + model for the duration of a test."""
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://localhost:11434", raising=False)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "qwen2.5-coder", raising=False)
    # Each test starts with a clean conversation store.
    local_llm._CONVOS.clear()


@pytest.fixture
def captured(monkeypatch):
    """Capture the outgoing POST and return a programmable fake response."""
    box: dict = {}

    def fake_post(url, json=None, timeout=None):
        box["url"] = url
        box["json"] = json or {}
        box["timeout"] = timeout
        resp = box["response"]
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(local_llm.httpx, "post", fake_post)
    return box


# --- URL normalisation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base,expected",
    [
        ("http://localhost:11434", "http://localhost:11434/v1/chat/completions"),
        ("http://localhost:11434/", "http://localhost:11434/v1/chat/completions"),
        ("http://localhost:1234/v1", "http://localhost:1234/v1/chat/completions"),
        ("http://localhost:1234/v1/", "http://localhost:1234/v1/chat/completions"),
        (
            "http://host:8000/v1/chat/completions",
            "http://host:8000/v1/chat/completions",
        ),
    ],
)
def test_completions_url(base, expected):
    assert local_llm._completions_url(base) == expected


def test_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    assert not local_llm.is_enabled()
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://x", raising=False)
    assert local_llm.is_enabled()


# --- complete() ----------------------------------------------------------------------------------


def test_complete_parses_and_sends_model(local_on, captured):
    captured["response"] = _FakeResponse(200, _ok_payload("Bishop takes h7 is unsound."))
    out = local_llm.complete("why?")
    assert out == "Bishop takes h7 is unsound."
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["json"]["model"] == "qwen2.5-coder"
    assert captured["json"]["stream"] is False
    assert captured["json"]["messages"] == [{"role": "user", "content": "why?"}]


def test_missing_model_raises(local_on, captured, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "", raising=False)
    captured["response"] = _FakeResponse(200, _ok_payload("x"))
    with pytest.raises(local_llm.LocalLLMError, match="model"):
        local_llm.complete("why?")


# --- chat() threading ----------------------------------------------------------------------------


def test_chat_threads_on_session_id(local_on, captured):
    captured["response"] = _FakeResponse(200, _ok_payload("answer one"))
    first = local_llm.chat("first question")
    sid = first["session_id"]
    assert first["answer"] == "answer one"
    assert sid

    captured["response"] = _FakeResponse(200, _ok_payload("answer two"))
    second = local_llm.chat("second question", session_id=sid)
    assert second["session_id"] == sid
    # The follow-up resends the prior user+assistant turns plus the new question.
    sent = captured["json"]["messages"]
    assert [m["content"] for m in sent] == [
        "first question",
        "answer one",
        "second question",
    ]


def test_chat_unknown_session_starts_fresh(local_on, captured):
    captured["response"] = _FakeResponse(200, _ok_payload("hi"))
    res = local_llm.chat("q", session_id="does-not-exist")
    assert res["session_id"] != "does-not-exist"
    assert captured["json"]["messages"] == [{"role": "user", "content": "q"}]


def test_chat_failure_does_not_poison_thread(local_on, captured):
    captured["response"] = _FakeResponse(200, _ok_payload("ok"))
    first = local_llm.chat("q1")
    sid = first["session_id"]

    captured["response"] = _FakeResponse(500, text="boom")
    with pytest.raises(local_llm.LocalLLMError):
        local_llm.chat("q2", session_id=sid)

    # The failed turn was not committed: the next call resends only q1/ok + the new question.
    captured["response"] = _FakeResponse(200, _ok_payload("recovered"))
    local_llm.chat("q3", session_id=sid)
    assert [m["content"] for m in captured["json"]["messages"]] == ["q1", "ok", "q3"]


# --- error mapping -------------------------------------------------------------------------------


def test_connection_error(local_on, captured):
    captured["response"] = httpx.ConnectError("refused")
    with pytest.raises(local_llm.LocalLLMError, match="reach the local AI"):
        local_llm.complete("q")


def test_timeout(local_on, captured):
    captured["response"] = httpx.TimeoutException("slow")
    with pytest.raises(local_llm.LocalLLMError, match="too long"):
        local_llm.complete("q")


def test_non_200(local_on, captured):
    captured["response"] = _FakeResponse(404, text="not found")
    with pytest.raises(local_llm.LocalLLMError, match="HTTP 404"):
        local_llm.complete("q")


def test_empty_content(local_on, captured):
    captured["response"] = _FakeResponse(200, _ok_payload("   "))
    with pytest.raises(local_llm.LocalLLMError, match="empty"):
        local_llm.complete("q")


def test_bad_shape(local_on, captured):
    captured["response"] = _FakeResponse(200, {"unexpected": True})
    with pytest.raises(local_llm.LocalLLMError, match="unexpected format"):
        local_llm.complete("q")


# --- claude_bridge routing -----------------------------------------------------------------------


def test_ask_routes_to_local_and_never_spawns_claude(local_on, monkeypatch):
    """With a local URL set, ask() uses the local client and does NOT shell out to `claude`."""
    # Keep the engine/session/profile out of it — we're only testing routing.
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)

    def boom(*a, **k):  # subprocess must never be called on the local path
        raise AssertionError("claude_bridge.ask spawned a subprocess in local mode")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    monkeypatch.setattr(
        claude_bridge.local_llm,
        "chat",
        lambda prompt, session_id=None, timeout=600: {"answer": "local says hi", "session_id": "s1"},
    )

    res = claude_bridge.ask("what now?", fen="8/8/8/8/8/8/8/K6k w - - 0 1")
    assert res == {"answer": "local says hi", "session_id": "s1"}


def test_coach_routes_to_local(local_on, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)
    monkeypatch.setattr(claude_bridge, "_game_facts", lambda sess: "FACTS")

    def boom(*a, **k):
        raise AssertionError("coach_summary_ai spawned a subprocess in local mode")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    monkeypatch.setattr(claude_bridge.local_llm, "complete", lambda prompt, timeout=600: "SUMMARY")

    assert claude_bridge.coach_summary_ai(object()) == "SUMMARY"


def test_local_error_becomes_chat_error(local_on, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)

    def raise_local(*a, **k):
        raise claude_bridge.local_llm.LocalLLMError("server down")

    monkeypatch.setattr(claude_bridge.local_llm, "chat", raise_local)
    with pytest.raises(claude_bridge.ChatError, match="server down"):
        claude_bridge.ask("q", fen="8/8/8/8/8/8/8/K6k w - - 0 1")
