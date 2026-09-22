"""Direct-HTTP client for a local / self-hosted / BYO-provider LLM (no `claude` CLI needed).

When `config.LOCAL_LLM_BASE_URL` is set, the in-browser chat + AI coach summary talk to that model
server **directly over HTTP** instead of shelling out to `claude -p`. We target the
**OpenAI-compatible** `POST /v1/chat/completions` endpoint, which is the common denominator across
Ollama (`/v1/...`), LM Studio, llama.cpp's `server`, and a LiteLLM proxy — so a user running any of
those needs no `claude` install and no login.

The same path also reaches a **hosted** OpenAI-compatible provider, which differs only in wanting
an auth token: set `config.LOCAL_LLM_API_KEY` and we attach it (`Authorization: Bearer` by default,
Azure OpenAI's `api-key` header when the URL looks like Azure, or any header name you name in
`config.LOCAL_LLM_API_KEY_HEADER`). That way nobody has to run a gateway like LiteLLM in front of
their provider just to inject the header.

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
    ``/v1`` base (LM Studio: ``http://localhost:1234/v1``), or the full endpoint already. A query
    string is split off first and re-attached, so Azure OpenAI's mandatory
    ``/chat/completions?api-version=...`` is recognised as a complete endpoint rather than having
    another ``/v1/chat/completions`` glued onto it.
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        return base
    path, sep, query = base.partition("?")
    path = path.rstrip("/")
    if path.endswith("/chat/completions"):
        pass
    elif path.endswith("/v1"):
        path = f"{path}/chat/completions"
    else:
        path = f"{path}/v1/chat/completions"
    return f"{path}{sep}{query}"


def _is_local_host(base: str) -> bool:
    """True for a URL pointing at this machine / LAN, so errors can name the right fix."""
    host = (base or "").split("//", 1)[-1].split("/", 1)[0].split("@")[-1].lower()
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # strip :port, keep bare IPv6
    return (
        host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal")
        or host.startswith(("192.168.", "10.", "127."))
        or host.endswith(".local")
    )


def _is_azure(base: str) -> bool:
    """True for an Azure OpenAI endpoint, which authenticates with its own `api-key` header."""
    return "azure.com" in (base or "").lower()


def _auth_headers() -> dict[str, str]:
    """The auth header(s) for the configured provider ({} when no key is set, i.e. a local server).

    `config.LOCAL_LLM_API_KEY_HEADER` chooses the scheme; see the config comment for the accepted
    values. An unrecognised value is treated as a literal header name carrying the raw key, which
    covers the providers that use neither convention.
    """
    key = (config.LOCAL_LLM_API_KEY or "").strip()
    if not key:
        return {}
    style = (config.LOCAL_LLM_API_KEY_HEADER or "").strip()
    if not style or style.lower() == "auto":
        style = "api-key" if _is_azure(config.LOCAL_LLM_BASE_URL) else "bearer"
    low = style.lower()
    if low in ("bearer", "authorization"):
        # Don't double up a scheme the user already typed into the key field.
        value = key if key.lower().startswith(("bearer ", "basic ")) else f"Bearer {key}"
        return {"Authorization": value}
    if low == "api-key":
        return {"api-key": key}
    return {style: key}


def _post(messages: list[dict], *, timeout: int) -> str:
    """POST the messages to the local server and return the assistant's text. Raises LocalLLMError."""
    base = (config.LOCAL_LLM_BASE_URL or "").strip()
    if not base:
        raise LocalLLMError("No local AI URL is configured. Set one in Settings.")
    model = (config.LOCAL_LLM_MODEL or "").strip()
    if not model:
        raise LocalLLMError(
            "No local AI model is set. Pick a model in Settings (e.g. click “Detect models”)."
        )
    url = _completions_url(base)
    payload = {"model": model, "messages": messages, "stream": False}
    headers = _auth_headers()
    try:
        resp = httpx.post(url, json=payload, timeout=timeout, headers=headers or None)
    except httpx.TimeoutException:
        raise LocalLLMError(
            f"The local AI ({model}) took too long to respond. Local models can be slow — try a "
            "smaller model, or ask again."
        )
    except httpx.HTTPError:
        # Same failure, two very different fixes: start your local server, vs. check the URL and
        # your connection to a remote provider. Guess from the host which advice to give.
        if _is_local_host(base):
            raise LocalLLMError(
                f"Can't reach the local AI at {base} — is the server (Ollama / LM Studio) running "
                "and is the URL correct?"
            )
        raise LocalLLMError(
            f"Can't reach the AI provider at {base} — check the URL and your internet connection."
        )
    if resp.status_code != 200:
        snippet = (resp.text or "").strip().replace("\n", " ")[:200]
        if resp.status_code in (401, 403):
            # Nearly always the key (missing, wrong, or sent under the header this provider
            # doesn't read), so name that instead of the generic HTTP line.
            hint = (
                "Check the API key in Settings"
                if headers
                else "This provider wants an API key: add one in Settings"
            )
            raise LocalLLMError(
                f"The AI provider rejected the request (HTTP {resp.status_code}). {hint}"
                + (f". It said: {snippet}" if snippet else ".")
            )
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
