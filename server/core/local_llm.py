"""Direct-HTTP client for a local / self-hosted LLM (no `claude` CLI needed).

When `config.LOCAL_LLM_BASE_URL` is set, the in-browser chat + AI coach summary talk to a local
model server **directly over HTTP** instead of shelling out to `claude -p`. We target the
**OpenAI-compatible** `POST /v1/chat/completions` endpoint, which is the common denominator across
Ollama (`/v1/...`), LM Studio, llama.cpp's `server`, and a LiteLLM proxy — so a user running any of
those needs no `claude` install and no login.

This is viable because every engine fact the model needs is already pre-computed into the prompt
text by `claude_bridge` (so no tool/function-calling is required), exactly as the old
`ANTHROPIC_BASE_URL`-via-CLI mode relied on.
"""
from __future__ import annotations

import uuid

import httpx

from server import config

# Local models are much slower than the cloud, so default to a generous timeout. Callers may
# override (e.g. the coach summary, which produces more text).
DEFAULT_TIMEOUT = 600


class LocalLLMError(Exception):
    """Raised with a user-facing message when the local LLM call can't complete."""


# In-process conversation store, keyed by a generated session id — the direct-HTTP analogue of
# `claude --resume`. Local servers are stateless, so we resend the full message list each call.
# Wiped on process restart (a missed id just starts a fresh conversation, like a failed --resume).
_CONVOS: dict[str, list[dict]] = {}


def is_enabled() -> bool:
    """True when a local-LLM base URL is configured (so the direct-HTTP path should be used)."""
    return bool((config.LOCAL_LLM_BASE_URL or "").strip())


def _completions_url(base: str) -> str:
    """Normalise a configured base URL to the OpenAI-compatible chat-completions endpoint.

    Accepts the forms users actually paste: a bare host (Ollama: ``http://localhost:11434``), a
    ``/v1`` base (LM Studio: ``http://localhost:1234/v1``), or the full endpoint already.
    """
    base = (base or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _post(messages: list[dict], *, timeout: int) -> str:
    """POST the messages to the local server and return the assistant's text. Raises LocalLLMError."""
    base = (config.LOCAL_LLM_BASE_URL or "").strip()
    if not base:
        raise LocalLLMError("No local AI URL is configured. Set one in Settings.")
    model = (config.LOCAL_LLM_MODEL or "").strip()
    if not model:
        raise LocalLLMError(
            "No local AI model is set. Pick a model in Settings (e.g. click “Detect Ollama”)."
        )
    url = _completions_url(base)
    payload = {"model": model, "messages": messages, "stream": False}
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except httpx.TimeoutException:
        raise LocalLLMError(
            f"The local AI ({model}) took too long to respond. Local models can be slow — try a "
            "smaller model, or ask again."
        )
    except httpx.HTTPError:
        raise LocalLLMError(
            f"Can't reach the local AI at {base} — is the server (Ollama / LM Studio) running and "
            "is the URL correct?"
        )
    if resp.status_code != 200:
        snippet = (resp.text or "").strip().replace("\n", " ")[:200]
        raise LocalLLMError(
            f"The local AI returned HTTP {resp.status_code}"
            + (f": {snippet}" if snippet else ".")
        )
    try:
        data = resp.json()
        answer = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError):
        raise LocalLLMError(
            "The local AI replied in an unexpected format (expected an OpenAI-compatible "
            "/v1/chat/completions response)."
        )
    if not answer:
        raise LocalLLMError(f"The local AI ({model}) returned an empty reply. Try again.")
    return answer


def complete(prompt: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Single-shot completion (no threading) — used by the AI coach summary."""
    return _post([{"role": "user", "content": prompt}], timeout=timeout)


def chat(prompt: str, *, session_id: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Threaded chat turn. Returns ``{answer, session_id}``.

    A known ``session_id`` resumes that conversation (resending prior turns); a missing/unknown one
    starts a fresh conversation under a new id.
    """
    history = _CONVOS.get(session_id) if session_id else None
    if history is None:
        session_id = uuid.uuid4().hex
        history = []
        _CONVOS[session_id] = history
    messages = history + [{"role": "user", "content": prompt}]
    answer = _post(messages, timeout=timeout)
    # Only commit the exchange once it succeeded, so a failed call doesn't poison the thread.
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": answer})
    return {"answer": answer, "session_id": session_id}
