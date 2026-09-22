"""Anthropic Messages API client for the in-browser chat + AI coach ("bring your own key").

The third AI backend, alongside the default headless `claude -p` (the user's Claude subscription,
no per-token billing) and `core/local_llm.py` (any OpenAI-compatible local server or hosted
provider). This one talks to the Anthropic API directly with the user's own API key, which is
**billed per token** rather than covered by a subscription, so it is opt-in and never a fallback:
nothing here runs unless the user pastes a key into Settings.

Why a separate module instead of pointing `local_llm` at api.anthropic.com: the Messages API is not
the OpenAI chat-completions shape (different endpoint, headers, request and response bodies), so it
gets the official `anthropic` SDK rather than a hand-rolled compatibility shim.

Like the local path, this needs no tool calling: `claude_bridge` has already pre-computed every
engine fact into the prompt text.
"""
from __future__ import annotations

import uuid

from server import config

# Local models are slow and cloud ones aren't, but the caller's timeout covers both paths.
DEFAULT_TIMEOUT = 600
# A ceiling, not a target: we're billed for what the model actually writes. Kept at the SDK's
# recommended non-streaming default so a long coach summary is never truncated mid-sentence.
MAX_TOKENS = 16000
# Claude Opus 5 unless the user picks another model in Settings. Deliberately not the cheapest
# option: which model to pay for is the user's call, and the Settings dropdown shows the prices.
DEFAULT_MODEL = "claude-opus-5"


class AnthropicError(Exception):
    """Raised with a user-facing message when the API call can't complete."""


# In-process conversation store, keyed by a generated session id: the Messages API is stateless, so
# a follow-up resends the whole thread. Same contract as `local_llm._CONVOS` (wiped on restart; an
# unknown id just starts a fresh conversation).
_CONVOS: dict[str, list[dict]] = {}


def is_enabled() -> bool:
    """True when the user has saved an Anthropic API key (so this backend should be used)."""
    return bool((config.ANTHROPIC_API_KEY or "").strip())


def model() -> str:
    """The model id to request (Settings override, else the default)."""
    return (config.ANTHROPIC_MODEL or "").strip() or DEFAULT_MODEL


def _client(timeout: int):
    """Return `(anthropic_module, client)`. Raises AnthropicError with an actionable message."""
    try:
        import anthropic
    except ImportError:
        raise AnthropicError(
            "The `anthropic` package isn't installed, so the Claude API option can't run. "
            "Re-run the installer (or `uv sync`) to pick it up."
        )
    key = (config.ANTHROPIC_API_KEY or "").strip()
    if not key:
        raise AnthropicError("No Anthropic API key is configured. Add one in Settings.")
    # max_retries is the SDK default (2): it already backs off on 429s and 5xx for us.
    return anthropic, anthropic.Anthropic(api_key=key, timeout=float(timeout))


def _text_of(response) -> str:
    """The assistant's prose from a Messages response.

    Only `text` blocks: with thinking on (adaptive is the default on current models) the content
    list also carries `thinking` blocks, whose text is empty under the default display setting.
    """
    parts = [b.text for b in (response.content or []) if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip()


def _post(messages: list[dict], *, timeout: int) -> str:
    """POST the thread to the Messages API and return the assistant's text."""
    anthropic, client = _client(timeout)
    chosen = model()
    try:
        response = client.messages.create(
            model=chosen,
            max_tokens=MAX_TOKENS,
            messages=messages,
        )
    except anthropic.AuthenticationError:
        raise AnthropicError(
            "Anthropic rejected that API key. Check it in Settings (it should start with "
            "`sk-ant-`), or create a new one at console.anthropic.com."
        )
    except anthropic.PermissionDeniedError:
        raise AnthropicError(
            "That API key isn't allowed to use the Messages API. Check its permissions at "
            "console.anthropic.com."
        )
    except anthropic.NotFoundError:
        raise AnthropicError(
            f"Anthropic doesn't recognise the model '{chosen}'. Pick a different model in Settings."
        )
    except anthropic.RateLimitError:
        raise AnthropicError(
            "Anthropic is rate-limiting this key. Wait a moment and ask again."
        )
    except anthropic.APITimeoutError:
        raise AnthropicError(f"Claude ({chosen}) took too long to respond. Try asking again.")
    except anthropic.APIConnectionError:
        raise AnthropicError(
            "Can't reach the Anthropic API. Check your internet connection."
        )
    except anthropic.APIStatusError as exc:
        # The most common one here is a 400 for an exhausted credit balance, whose message is
        # already user-facing, so pass it through rather than inventing wording.
        detail = getattr(exc, "message", "") or str(exc)
        raise AnthropicError(f"The Anthropic API returned an error: {detail}")
    if getattr(response, "stop_reason", "") == "refusal":
        raise AnthropicError(
            "Claude declined to answer that one. Try rephrasing the question."
        )
    text = _text_of(response)
    if not text:
        raise AnthropicError(f"Claude ({chosen}) returned an empty reply. Try again.")
    return text


def complete(prompt: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Single-shot completion (no threading), for the AI coach summary."""
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
    answer = _post(history + [{"role": "user", "content": prompt}], timeout=timeout)
    # Only commit the exchange once it succeeded, so a failed call doesn't poison the thread.
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": answer})
    return {"answer": answer, "session_id": session_id}
