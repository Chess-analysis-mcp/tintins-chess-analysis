"""Tests for the Anthropic Messages API backend (SDK mocked - never calls the real API).

Also covers `claude_bridge` routing: with a key set it uses this client and never spawns `claude`,
and an explicit local/custom model URL still wins over the key.
"""
from __future__ import annotations

import sys

import pytest

from server import claude_bridge, config
from server.core import anthropic_api


class _Block:
    def __init__(self, type_: str, text: str = ""):
        self.type = type_
        self.text = text


class _Response:
    def __init__(self, blocks, stop_reason: str = "end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


class _FakeMessages:
    """Stands in for `client.messages`, recording the request and returning a canned response."""

    def __init__(self, box):
        self._box = box

    def create(self, **kwargs):
        self._box["request"] = kwargs
        out = self._box["response"]
        if isinstance(out, Exception):
            raise out
        return out


class _FakeClient:
    def __init__(self, box, **kwargs):
        box["client_kwargs"] = kwargs
        self.messages = _FakeMessages(box)


class _FakeErrors:
    """The SDK's exception classes, in the same shape `anthropic_api` catches them."""

    class APIError(Exception):
        pass

    class AuthenticationError(APIError):
        pass

    class PermissionDeniedError(APIError):
        pass

    class NotFoundError(APIError):
        pass

    class RateLimitError(APIError):
        pass

    class APITimeoutError(APIError):
        pass

    class APIConnectionError(APIError):
        pass

    class APIStatusError(APIError):
        def __init__(self, message=""):
            super().__init__(message)
            self.message = message


@pytest.fixture
def sdk(monkeypatch):
    """Configure a key and swap `_client` for a fake SDK. Returns the recording box."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "", raising=False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    anthropic_api._CONVOS.clear()
    box: dict = {"response": _Response([_Block("text", "Nf3 keeps the pawn.")])}

    def fake_client(timeout):
        box["timeout"] = timeout
        return _FakeErrors, _FakeClient(box, timeout=timeout)

    monkeypatch.setattr(anthropic_api, "_client", fake_client)
    return box


# --- enablement + model choice -------------------------------------------------------------------


def test_is_enabled(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    assert not anthropic_api.is_enabled()
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x", raising=False)
    assert anthropic_api.is_enabled()


def test_model_defaults_to_opus(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "", raising=False)
    assert anthropic_api.model() == "claude-opus-5"
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-haiku-4-5", raising=False)
    assert anthropic_api.model() == "claude-haiku-4-5"


def test_missing_package_is_a_friendly_error(monkeypatch):
    """A stale install without the SDK must say so, not raise ImportError at the user."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", None)  # -> ImportError on import
    with pytest.raises(anthropic_api.AnthropicError, match="isn't installed"):
        anthropic_api.complete("q")


def test_no_key_is_a_friendly_error(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    with pytest.raises(anthropic_api.AnthropicError, match="No Anthropic API key"):
        anthropic_api.complete("q")


# --- complete() ----------------------------------------------------------------------------------


def test_complete_sends_the_prompt_and_returns_text(sdk):
    out = anthropic_api.complete("why is Bxh7 bad?")
    assert out == "Nf3 keeps the pawn."
    req = sdk["request"]
    assert req["model"] == "claude-opus-5"
    assert req["max_tokens"] == anthropic_api.MAX_TOKENS
    assert req["messages"] == [{"role": "user", "content": "why is Bxh7 bad?"}]
    # No `thinking` / `output_config`: the defaults work on every model we offer (effort errors on
    # Haiku 4.5), so we don't send a per-model capability matrix.
    assert "thinking" not in req and "output_config" not in req


def test_thinking_blocks_are_skipped(sdk):
    """Adaptive thinking is on by default, and its blocks carry no text under the default display."""
    sdk["response"] = _Response([_Block("thinking", ""), _Block("text", "The knight is loose.")])
    assert anthropic_api.complete("q") == "The knight is loose."


def test_multiple_text_blocks_are_joined(sdk):
    sdk["response"] = _Response([_Block("text", "One."), _Block("text", "Two.")])
    assert anthropic_api.complete("q") == "One.\nTwo."


def test_model_override_is_used(sdk, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-sonnet-5", raising=False)
    anthropic_api.complete("q")
    assert sdk["request"]["model"] == "claude-sonnet-5"


# --- chat() threading ----------------------------------------------------------------------------


def test_chat_threads_on_session_id(sdk):
    first = anthropic_api.chat("first question")
    sid = first["session_id"]
    assert first["answer"] == "Nf3 keeps the pawn." and sid

    sdk["response"] = _Response([_Block("text", "second answer")])
    second = anthropic_api.chat("second question", session_id=sid)
    assert second["session_id"] == sid
    assert [m["content"] for m in sdk["request"]["messages"]] == [
        "first question",
        "Nf3 keeps the pawn.",
        "second question",
    ]


def test_chat_unknown_session_starts_fresh(sdk):
    res = anthropic_api.chat("q", session_id="nope")
    assert res["session_id"] != "nope"
    assert sdk["request"]["messages"] == [{"role": "user", "content": "q"}]


def test_chat_failure_does_not_poison_thread(sdk):
    sid = anthropic_api.chat("q1")["session_id"]
    sdk["response"] = _FakeErrors.RateLimitError()
    with pytest.raises(anthropic_api.AnthropicError):
        anthropic_api.chat("q2", session_id=sid)
    sdk["response"] = _Response([_Block("text", "recovered")])
    anthropic_api.chat("q3", session_id=sid)
    assert [m["content"] for m in sdk["request"]["messages"]] == [
        "q1",
        "Nf3 keeps the pawn.",
        "q3",
    ]


# --- error mapping -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,match",
    [
        (_FakeErrors.AuthenticationError(), "rejected that API key"),
        (_FakeErrors.PermissionDeniedError(), "isn't allowed"),
        (_FakeErrors.NotFoundError(), "doesn't recognise the model"),
        (_FakeErrors.RateLimitError(), "rate-limiting"),
        (_FakeErrors.APITimeoutError(), "took too long"),
        (_FakeErrors.APIConnectionError(), "Can't reach the Anthropic API"),
        (_FakeErrors.APIStatusError("credit balance is too low"), "credit balance is too low"),
    ],
)
def test_error_mapping(sdk, exc, match):
    sdk["response"] = exc
    with pytest.raises(anthropic_api.AnthropicError, match=match):
        anthropic_api.complete("q")


def test_refusal_is_reported(sdk):
    sdk["response"] = _Response([_Block("text", "")], stop_reason="refusal")
    with pytest.raises(anthropic_api.AnthropicError, match="declined"):
        anthropic_api.complete("q")


def test_empty_reply(sdk):
    sdk["response"] = _Response([_Block("text", "   ")])
    with pytest.raises(anthropic_api.AnthropicError, match="empty"):
        anthropic_api.complete("q")


# --- claude_bridge routing -----------------------------------------------------------------------


def test_backend_selection_precedence(monkeypatch):
    """Custom URL beats an API key; a key beats the CLI; neither means the CLI."""
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    assert claude_bridge._byo_backend() is None

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x", raising=False)
    assert claude_bridge._byo_backend() is anthropic_api

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://localhost:11434", raising=False)
    assert claude_bridge._byo_backend() is claude_bridge.local_llm


def test_ask_routes_to_anthropic_and_never_spawns_claude(sdk, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess while an Anthropic key was configured")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    res = claude_bridge.ask("what now?", fen="8/8/8/8/8/8/8/K6k w - - 0 1")
    assert res["answer"] == "Nf3 keeps the pawn."
    assert res["session_id"]


def test_coach_routes_to_anthropic(sdk, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)
    monkeypatch.setattr(claude_bridge, "_game_facts", lambda sess: "FACTS")

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess while an Anthropic key was configured")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    assert claude_bridge.coach_summary_ai(object()) == "Nf3 keeps the pawn."


def test_anthropic_error_becomes_chat_error(sdk, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)
    sdk["response"] = _FakeErrors.AuthenticationError()
    with pytest.raises(claude_bridge.ChatError, match="rejected that API key"):
        claude_bridge.ask("q", fen="8/8/8/8/8/8/8/K6k w - - 0 1")


def test_puzzle_coach_routes_to_anthropic(sdk, monkeypatch):
    """The third refactored call site: the puzzle coach shares the same backend selector."""

    def boom(*a, **k):
        raise AssertionError("spawned a subprocess while an Anthropic key was configured")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    answer, session_id = claude_bridge._run_puzzle_coach("explain this puzzle", 60)
    assert answer == "Nf3 keeps the pawn."
    # `claude -p` returns a resumable conversation id; the direct-HTTP backends don't.
    assert session_id is None


def test_puzzle_coach_surfaces_backend_errors(sdk):
    sdk["response"] = _FakeErrors.RateLimitError()
    with pytest.raises(claude_bridge.ChatError, match="rate-limiting"):
        claude_bridge._run_puzzle_coach("explain this puzzle", 60)
