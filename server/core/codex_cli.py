"""ChatGPT-subscription backend for the in-browser chat + AI coach, via the headless Codex CLI.

The fourth AI backend, alongside the default headless `claude -p` (Claude subscription),
`core/local_llm.py` (any OpenAI-compatible URL) and `core/anthropic_api.py` (Anthropic API key).
This one shells out to OpenAI's `codex exec`, which signs in with the user's **ChatGPT plan**, so
it costs nothing beyond the subscription. Codex owns the login, token refresh and API rules; we
only send a prompt on stdin and read the JSON event stream it prints back.

Codex is a coding agent, not a plain model, so every call is locked down:
- Its shell, browser and computer-use tools are switched off, so it can't run anything at all.
  This doesn't depend on the OS sandbox, which is less mature on native Windows.
- `sandbox_mode=read-only` on top, plus an empty working folder (`_workdir`).
- `--ignore-user-config --ignore-rules --disable apps --disable plugins` and web search off, so
  none of the user's own Codex MCP servers, connectors or plugins load.
- `forced_login_method=chatgpt`, so an exported OPENAI_API_KEY can never switch the user to
  per-token API billing.

Cross-platform notes: on Windows an npm install is a `codex.cmd` batch wrapper, and `cmd.exe`
re-parses its arguments, so no argument may carry quotes or shell metacharacters. That's why the
`-c` values are bare words (Codex takes a value that isn't valid TOML as a literal string), why
the user-typed model name is validated (`_MODEL_RE`), and why the prompt goes on stdin.

Like the other optional backends, no tools are needed: `claude_bridge` pre-computes every engine
fact into the prompt text.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from server import config

# Each call starts a full agent (about 6s for a short answer), and the coach summary is long.
DEFAULT_TIMEOUT = 300

# `codex login status` is a local file check (~30ms), so it's cheap to run before every call.
_STATUS_TIMEOUT = 15

_INSTALL_HINT = (
    "The Codex CLI isn't installed, so the ChatGPT option can't run. Install it "
    "(`npm install -g @openai/codex`, or see developers.openai.com/codex/cli), run `codex login` "
    "once and choose Sign in with ChatGPT, then try again."
)
_LOGIN_HINT = (
    "The Codex CLI isn't signed in with ChatGPT. Open a terminal, run `codex login`, choose "
    "Sign in with ChatGPT, then try again."
)

# Flags shared by a new thread and a resumed one. `resume` takes no `--sandbox`, so the sandbox
# is set through `-c` for both. Values are bare words on purpose (see the module docstring).
_LOCKDOWN = (
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ignore-rules",
    "--disable", "apps",
    "--disable", "plugins",
    "--disable", "shell_tool",
    "--disable", "unified_exec",
    "--disable", "browser_use",
    "--disable", "computer_use",
    "-c", "sandbox_mode=read-only",
    "-c", "web_search=disabled",
    "-c", "forced_login_method=chatgpt",
)

# Model ids look like `gpt-6.1-sol` or `provider/model:tag`. Anything else is refused, because on
# Windows the argument passes through cmd.exe, where `&`, `|`, `%` and quotes are live.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")

# No console window flashing up for each call on Windows (a no-op flag elsewhere).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Put ahead of every prompt. Its tools are off anyway; this stops it narrating that it would check.
_PREAMBLE = (
    "Answer from the text below only. Do not run commands, read files, or use any tools; "
    "everything you need is already included.\n\n"
)

# Thread ids this process created. Only these are resumed, so a stale id (from before a restart, or
# a `claude -p` session id from before the user switched backends) starts a fresh thread instead
# of failing. Same contract as `local_llm._CONVOS`.
_THREADS: set[str] = set()


class CodexError(Exception):
    """Raised with a user-facing message when the Codex call can't complete."""


def is_enabled() -> bool:
    """True when the user turned on "Use my ChatGPT subscription" in Settings."""
    return bool(config.CODEX_ENABLED)


def _common_locations() -> list[str]:
    """Where Codex usually lands when it isn't on PATH (an app opened from the Dock, Finder or a
    desktop launcher gets a much shorter PATH than a terminal)."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return [
            os.path.join(appdata, "npm", "codex.cmd"),
            os.path.join(home, ".codex", "packages", "standalone", "current", "bin", "codex.exe"),
        ]
    return [
        os.path.join(home, ".local", "bin", "codex"),  # the standalone installer
        os.path.join(home, ".codex", "packages", "standalone", "current", "bin", "codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        os.path.join(home, ".npm-global", "bin", "codex"),
    ]


def find_binary() -> str | None:
    """The Codex CLI to run: on PATH, else a common install location, else None."""
    found = shutil.which("codex")
    if found:
        return found
    return next((p for p in _common_locations() if os.path.isfile(p)), None)


def login_status() -> dict:
    """`{installed, logged_in}` for the Settings panel and the pre-flight check. Never raises."""
    codex = find_binary()
    if not codex:
        return {"installed": False, "logged_in": False}
    try:
        proc = subprocess.run(
            [codex, "login", "status"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STATUS_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return {"installed": True, "logged_in": False}
    # Prints e.g. "Logged in using ChatGPT". An API-key login doesn't count: we force ChatGPT.
    text = f"{proc.stdout}\n{proc.stderr}".lower()
    return {"installed": True, "logged_in": proc.returncode == 0 and "chatgpt" in text}


def _workdir() -> str:
    """An empty folder to run Codex from, so its read-only sandbox has nothing nearby to read."""
    path = os.path.join(config.DATA_DIR, "codex-workdir")
    os.makedirs(path, exist_ok=True)
    return path


def _command(codex: str, resume_id: str | None, ephemeral: bool) -> list[str]:
    cmd = [codex, "exec"]
    if resume_id:
        cmd += ["resume", resume_id]
    cmd += [*_LOCKDOWN, "--json"]
    if ephemeral:
        cmd.append("--ephemeral")  # one-off call: save no session, since nothing will resume it
    model = (config.CODEX_MODEL or "").strip()
    if model:
        if not _MODEL_RE.fullmatch(model):
            raise CodexError(
                f"'{model[:60]}' isn't a valid model name. Use letters, digits, '.', '-', '_', ':' "
                "or '/' only, or clear the Model box in Settings to use Codex's default."
            )
        cmd += ["-m", model]
    cmd.append("-")  # read the prompt from stdin (no argv length limit, nothing in `ps`)
    return cmd


def _parse(stdout: str) -> tuple[str, str | None, str | None]:
    """`(answer, thread_id, error)` from Codex's JSON-lines event stream.

    The answer is the LAST `agent_message`: Codex may narrate ("I'll check…") before its reply.
    `error` is set when the turn failed.
    """
    answer, thread_id, error = "", None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and (item.get("text") or "").strip():
                answer = item["text"].strip()
        elif kind == "turn.failed":
            error = (event.get("error") or {}).get("message") or "the request failed"
    return answer, thread_id, error


def _friendly(detail: str) -> str:
    low = (detail or "").lower()
    if "401" in low or "unauthorized" in low or "not logged in" in low:
        return _LOGIN_HINT
    if "usage limit" in low or "rate limit" in low or "429" in low:
        return (
            "Your ChatGPT plan's Codex usage limit is used up for now. It resets on its own; "
            "check `/status` in Codex, or wait and ask again."
        )
    if "model is not supported" in low or "not supported when using codex" in low:
        return (
            f"Your ChatGPT plan can't use the model '{(config.CODEX_MODEL or '').strip()}'. "
            "Clear the Model box in Settings to use Codex's default."
        )
    first = (detail or "").strip().splitlines()[0] if (detail or "").strip() else "unknown error"
    return f"Codex failed: {first[:300]}"


def _run(prompt: str, *, resume_id: str | None, ephemeral: bool, timeout: int) -> tuple[str, str | None]:
    """Run one Codex turn and return `(answer, thread_id)`. Raises CodexError."""
    codex = find_binary()
    if not codex:
        raise CodexError(_INSTALL_HINT)
    # Fail fast with a clear message: logged out, Codex retries a 401 for ~20s before giving up.
    if not login_status()["logged_in"]:
        raise CodexError(_LOGIN_HINT)
    cmd = _command(codex, resume_id, ephemeral)
    try:
        proc = subprocess.run(
            cmd,
            input=_PREAMBLE + prompt,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=_workdir(),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise CodexError("ChatGPT (Codex) took too long to respond. Try asking again.")
    except OSError as exc:
        raise CodexError(f"Couldn't start the Codex CLI: {exc}")
    answer, thread_id, error = _parse(proc.stdout or "")
    if error or proc.returncode != 0:
        raise CodexError(_friendly(error or proc.stderr or proc.stdout))
    if not answer:
        raise CodexError("ChatGPT (Codex) returned an empty reply. Try again.")
    return answer, thread_id


def complete(prompt: str, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Single-shot completion (no threading), for the AI coach summary and puzzle coach."""
    answer, _ = _run(prompt, resume_id=None, ephemeral=True, timeout=timeout)
    return answer


def chat(prompt: str, *, session_id: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Threaded chat turn. Returns ``{answer, session_id}`` (the Codex thread id).

    A ``session_id`` this process created resumes that thread; a missing or unknown one starts a
    fresh thread.
    """
    resume_id = session_id if session_id in _THREADS else None
    answer, thread_id = _run(prompt, resume_id=resume_id, ephemeral=False, timeout=timeout)
    thread_id = thread_id or resume_id
    if thread_id:
        # Only registered once the turn succeeded, so a failed call never becomes resumable.
        _THREADS.add(thread_id)
    return {"answer": answer, "session_id": thread_id}
