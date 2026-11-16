"""Tests for the ChatGPT-subscription backend (headless Codex CLI; subprocess mocked, never runs Codex).

Covers the argv lockdown, JSON event parsing, error mapping, chat threading, and `claude_bridge`
routing (with Codex on it never spawns `claude`, and backend precedence across all four options),
plus the Settings, doctor and puzzle-gate wiring.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from server import claude_bridge, config, doctor
from server.core import anthropic_api, codex_cli, local_llm
from server.core import settings as settings_mod

_FEN = "8/8/8/8/8/8/8/K6k w - - 0 1"


def _events(*events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _ok(text: str = "Nf3 keeps the pawn.", thread: str = "thread-1") -> str:
    return _events(
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )


def _failed(message: str) -> str:
    return _events(
        {"type": "thread.started", "thread_id": "thread-x"},
        {"type": "turn.started"},
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
    )


class _FakeCodex:
    """Stands in for `subprocess.run`: answers `codex login status` and `codex exec` separately."""

    def __init__(self):
        self.status = (0, "Logged in using ChatGPT")
        self.exec_result: tuple[int, str, str] | Exception = (0, _ok(), "")
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        if cmd[1:3] == ["login", "status"]:
            code, out = self.status
            return subprocess.CompletedProcess(cmd, code, out, "")
        self.calls.append({"cmd": cmd, **kwargs})
        if isinstance(self.exec_result, Exception):
            raise self.exec_result
        code, out, err = self.exec_result
        return subprocess.CompletedProcess(cmd, code, out, err)

    @property
    def last(self) -> dict:
        return self.calls[-1]


class _SubprocessShim:
    """A private `subprocess` for codex_cli whose `run` is the fake. Patching the real module's
    `run` would also catch `claude_bridge`'s calls, and the routing tests need those to fail."""

    TimeoutExpired = subprocess.TimeoutExpired
    SubprocessError = subprocess.SubprocessError
    CompletedProcess = subprocess.CompletedProcess

    def __init__(self, fake):
        self.run = fake


@pytest.fixture
def codex(monkeypatch, tmp_path):
    """Codex on, installed and signed in; `subprocess.run` faked. Returns the fake."""
    monkeypatch.setattr(config, "CODEX_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "CODEX_MODEL", "", raising=False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(codex_cli, "find_binary", lambda: "/usr/bin/codex")
    codex_cli._THREADS.clear()
    fake = _FakeCodex()
    monkeypatch.setattr(codex_cli, "subprocess", _SubprocessShim(fake))
    return fake


# --- enablement + login status -------------------------------------------------------------------


def test_is_enabled_follows_the_setting(monkeypatch):
    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)
    assert not codex_cli.is_enabled()
    monkeypatch.setattr(config, "CODEX_ENABLED", True, raising=False)
    assert codex_cli.is_enabled()


def test_login_status_not_installed(monkeypatch):
    monkeypatch.setattr(codex_cli, "find_binary", lambda: None)
    assert codex_cli.login_status() == {"installed": False, "logged_in": False}


# --- finding the binary --------------------------------------------------------------------------


def test_find_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/on/path/codex")
    assert codex_cli.find_binary() == "/on/path/codex"


def test_find_binary_falls_back_to_common_locations(monkeypatch, tmp_path):
    """A Dock/desktop launch has a short PATH; the usual install folders are still checked."""
    exe = tmp_path / "codex"
    exe.write_text("")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex_cli, "_common_locations", lambda: [str(tmp_path / "nope"), str(exe)])
    assert codex_cli.find_binary() == str(exe)
    monkeypatch.setattr(codex_cli, "_common_locations", lambda: [str(tmp_path / "nope")])
    assert codex_cli.find_binary() is None


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_common_locations_per_platform(monkeypatch, platform):
    monkeypatch.setattr(codex_cli.sys, "platform", platform)
    monkeypatch.setenv("APPDATA", r"C:\Users\me\AppData\Roaming")
    # Normalise separators: faking the platform doesn't change os.path, so on a Windows runner the
    # simulated macOS/Linux paths are joined with backslashes.
    paths = [p.replace("\\", "/") for p in codex_cli._common_locations()]
    assert paths
    if platform == "win32":
        assert any(p.endswith("codex.cmd") for p in paths)  # npm's Windows wrapper
        assert any(p.endswith("codex.exe") for p in paths)
    else:
        assert any(p.endswith(".local/bin/codex") for p in paths)


def test_login_status_signed_in(codex):
    assert codex_cli.login_status() == {"installed": True, "logged_in": True}


@pytest.mark.parametrize(
    "status",
    [(1, "Not logged in"), (0, "Logged in using an API key - sk-proj-***")],
    ids=["logged-out", "api-key-login"],
)
def test_login_status_requires_a_chatgpt_login(codex, status):
    """An API-key login doesn't count: the backend forces ChatGPT, so it would fail anyway."""
    codex.status = status
    assert codex_cli.login_status() == {"installed": True, "logged_in": False}


def test_login_status_never_raises(codex, monkeypatch):
    def broken(cmd, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(codex_cli, "subprocess", _SubprocessShim(broken))
    assert codex_cli.login_status() == {"installed": True, "logged_in": False}


# --- the command we run --------------------------------------------------------------------------


def test_complete_runs_locked_down_and_sends_prompt_on_stdin(codex, tmp_path):
    assert codex_cli.complete("PROMPT-TEXT") == "Nf3 keeps the pawn."
    call = codex.last
    cmd = call["cmd"]
    assert cmd[:2] == ["/usr/bin/codex", "exec"]
    for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--json",
                 "--ephemeral"):
        assert flag in cmd
    joined = " ".join(cmd)
    for setting in ("sandbox_mode=read-only", "web_search=disabled", "forced_login_method=chatgpt",
                    "--disable apps", "--disable plugins", "--disable shell_tool",
                    "--disable unified_exec", "--disable browser_use", "--disable computer_use"):
        assert setting in joined
    # Windows passes npm's codex.cmd arguments through cmd.exe: no quotes or metacharacters.
    for arg in cmd:
        assert not set(arg) & set('"&|<>^%!'), arg
    assert cmd[-1] == "-"  # prompt comes from stdin...
    assert "PROMPT-TEXT" not in joined  # ...never argv
    assert call["input"].endswith("PROMPT-TEXT")
    assert "Do not run commands" in call["input"]
    # Runs from an empty folder of its own under DATA_DIR, never the repo.
    assert call["cwd"] == str(tmp_path / "codex-workdir")
    assert (tmp_path / "codex-workdir").is_dir()
    assert "-m" not in cmd  # blank model = Codex's default


@pytest.mark.parametrize("model", ["gpt-test", "gpt-6.1-sol", "openai/gpt-5:latest", "o3_mini"])
def test_model_override_is_passed(codex, monkeypatch, model):
    monkeypatch.setattr(config, "CODEX_MODEL", f" {model} ", raising=False)
    codex_cli.complete("q")
    cmd = codex.last["cmd"]
    assert cmd[cmd.index("-m") + 1] == model


@pytest.mark.parametrize(
    "model", ["gpt & calc", 'gpt"x', "gpt|more", "%PATH%", "-c", "a b", "x" * 101, "gpt^x"]
)
def test_unsafe_model_name_is_refused_before_running(codex, monkeypatch, model):
    """On Windows the model reaches cmd.exe via codex.cmd, so metacharacters must never pass."""
    monkeypatch.setattr(config, "CODEX_MODEL", model, raising=False)
    with pytest.raises(codex_cli.CodexError, match="valid model name"):
        codex_cli.complete("q")
    assert codex.calls == []


def test_no_console_window_flag(codex, monkeypatch):
    """Windows gets CREATE_NO_WINDOW (no console flash per call); other platforms pass 0."""
    codex_cli.complete("q")
    assert codex.last["creationflags"] == codex_cli._NO_WINDOW
    if codex_cli.sys.platform != "win32":
        assert codex_cli._NO_WINDOW == 0


def test_output_is_decoded_as_utf8(codex):
    """Not the Windows locale codepage: chess prose has ×, …, curly quotes and figurines."""
    codex_cli.complete("q")
    assert codex.last["encoding"] == "utf-8"


def test_timeout_is_forwarded(codex):
    codex_cli.complete("q", timeout=42)
    assert codex.last["timeout"] == 42


# --- parsing the event stream --------------------------------------------------------------------


def test_last_agent_message_is_the_answer(codex):
    """Codex can narrate before its reply ("I'll check..."); only the final message is the answer."""
    codex.exec_result = (0, _events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Let me look.\n"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "  Final answer.  "}},
        {"type": "turn.completed"},
    ), "")
    assert codex_cli.complete("q") == "Final answer."


def test_parse_ignores_noise_lines():
    out = "not json\n[1, 2]\n" + _ok("Fine.", "t-9")
    assert codex_cli._parse(out) == ("Fine.", "t-9", None)


def test_warning_items_are_not_answers(codex):
    """Codex reports config warnings as `error` items; they must not become the reply."""
    codex.exec_result = (0, _events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "error", "message": "ignoring a setting"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Real reply."}},
        {"type": "turn.completed"},
    ), "")
    assert codex_cli.complete("q") == "Real reply."


def test_empty_reply_is_an_error(codex):
    codex.exec_result = (0, _events({"type": "thread.started", "thread_id": "t"},
                                    {"type": "turn.completed"}), "")
    with pytest.raises(codex_cli.CodexError, match="empty reply"):
        codex_cli.complete("q")


# --- errors --------------------------------------------------------------------------------------


def test_not_installed(codex, monkeypatch):
    monkeypatch.setattr(codex_cli, "find_binary", lambda: None)
    with pytest.raises(codex_cli.CodexError, match="isn't installed"):
        codex_cli.complete("q")


def test_logged_out_fails_fast_without_running_exec(codex):
    """Logged out, `codex exec` retries a 401 for ~20s; the status check skips that wait."""
    codex.status = (1, "Not logged in")
    with pytest.raises(codex_cli.CodexError, match="codex login"):
        codex_cli.complete("q")
    assert codex.calls == []


@pytest.mark.parametrize(
    "message, match",
    [
        ("unexpected status 401 Unauthorized: Missing bearer", "codex login"),
        ("You've hit your usage limit. Try again in 3 hours.", "usage limit is used up"),
        ("The 'gpt-x' model is not supported when using Codex with a ChatGPT account.",
         "can't use the model"),
        ("stream disconnected before completion", "Codex failed: stream disconnected"),
    ],
)
def test_turn_failed_is_mapped(codex, message, match):
    codex.exec_result = (1, _failed(message), "")
    with pytest.raises(codex_cli.CodexError, match=match):
        codex_cli.complete("q")


def test_nonzero_exit_without_events_uses_stderr(codex):
    codex.exec_result = (2, "", "error: unexpected argument '--frobnicate'\n")
    with pytest.raises(codex_cli.CodexError, match="unexpected argument"):
        codex_cli.complete("q")


def test_timeout(codex):
    codex.exec_result = subprocess.TimeoutExpired(cmd="codex", timeout=1)
    with pytest.raises(codex_cli.CodexError, match="too long"):
        codex_cli.complete("q")


def test_launch_failure(codex):
    codex.exec_result = PermissionError("permission denied")
    with pytest.raises(codex_cli.CodexError, match="Couldn't start"):
        codex_cli.complete("q")


# --- chat threading ------------------------------------------------------------------------------


def test_chat_starts_a_thread_then_resumes_it(codex):
    first = codex_cli.chat("first question")
    assert first == {"answer": "Nf3 keeps the pawn.", "session_id": "thread-1"}
    first_cmd = codex.last["cmd"]
    assert "resume" not in first_cmd
    assert "--ephemeral" not in first_cmd  # must stay resumable

    codex.exec_result = (0, _ok("Because of the fork.", "thread-1"), "")
    second = codex_cli.chat("why?", session_id="thread-1")
    assert second == {"answer": "Because of the fork.", "session_id": "thread-1"}
    cmd = codex.last["cmd"]
    assert cmd[2:4] == ["resume", "thread-1"]
    assert "sandbox_mode=read-only" in cmd  # resume has no --sandbox flag; -c carries it
    assert "shell_tool" in cmd  # the tool lockdown applies to resumed threads too


def test_chat_unknown_session_starts_fresh(codex):
    """A stale id (e.g. a `claude -p` session from before switching backends) isn't resumed."""
    res = codex_cli.chat("q", session_id="some-claude-session-id")
    assert "resume" not in codex.last["cmd"]
    assert res["session_id"] == "thread-1"


def test_failed_chat_does_not_register_its_thread(codex):
    codex.exec_result = (1, _failed("stream disconnected"), "")
    with pytest.raises(codex_cli.CodexError):
        codex_cli.chat("q")
    assert "thread-x" not in codex_cli._THREADS
    codex.exec_result = (0, _ok(), "")
    codex_cli.chat("q", session_id="thread-x")
    assert "resume" not in codex.last["cmd"]


# --- claude_bridge routing -----------------------------------------------------------------------


def test_backend_precedence(monkeypatch):
    """Custom URL > ChatGPT (Codex) > Anthropic key > the `claude` CLI."""
    for name, value in [("LOCAL_LLM_BASE_URL", ""), ("CODEX_ENABLED", False), ("ANTHROPIC_API_KEY", "")]:
        monkeypatch.setattr(config, name, value, raising=False)
    assert claude_bridge._byo_backend() is None

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x", raising=False)
    assert claude_bridge._byo_backend() is anthropic_api

    monkeypatch.setattr(config, "CODEX_ENABLED", True, raising=False)
    assert claude_bridge._byo_backend() is codex_cli

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://localhost:11434", raising=False)
    assert claude_bridge._byo_backend() is local_llm


@pytest.fixture
def no_claude(monkeypatch):
    """Fail the test if `claude_bridge` itself spawns anything (i.e. falls back to `claude -p`)."""

    def boom(*a, **k):
        raise AssertionError("spawned `claude` while ChatGPT (Codex) was configured")

    monkeypatch.setattr(claude_bridge.subprocess, "run", boom)
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    monkeypatch.setattr(claude_bridge, "_profile_facts", lambda: None)


def test_ask_routes_to_codex(codex, no_claude, monkeypatch):
    res = claude_bridge.ask("what now?", fen=_FEN)
    assert res == {"answer": "Nf3 keeps the pawn.", "session_id": "thread-1"}
    assert "what now?" in codex.last["input"]


def test_coach_routes_to_codex(codex, no_claude, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_game_facts", lambda sess: "FACTS")
    assert claude_bridge.coach_summary_ai(object()) == "Nf3 keeps the pawn."
    assert "--ephemeral" in codex.last["cmd"]
    # The bridge passes at least the backend's own timeout, since Codex is slower than an API.
    assert codex.last["timeout"] >= codex_cli.DEFAULT_TIMEOUT


def test_puzzle_coach_routes_to_codex(codex, no_claude, monkeypatch):
    answer, session_id = claude_bridge._run_puzzle_coach("explain this puzzle", 60)
    assert answer == "Nf3 keeps the pawn."
    assert session_id is None


def test_codex_error_becomes_chat_error(codex, no_claude, monkeypatch):
    codex.status = (1, "Not logged in")
    with pytest.raises(claude_bridge.ChatError, match="codex login"):
        claude_bridge.ask("q", fen=_FEN)


# --- settings, doctor, puzzles -------------------------------------------------------------------


def test_settings_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "CODEX_MODEL", "", raising=False)
    eff = settings_mod.update({"codex_enabled": True, "codex_model": " gpt-x "}, str(tmp_path))
    assert eff["codex_enabled"] is True and eff["codex_model"] == "gpt-x"
    assert config.CODEX_ENABLED is True and config.CODEX_MODEL == "gpt-x"
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["codex_enabled"] is True
    settings_mod.update({"codex_enabled": False}, str(tmp_path))
    assert config.CODEX_ENABLED is False


def test_codex_enabled_is_local_only_and_not_a_secret():
    assert "codex_enabled" in settings_mod.LOCAL_ONLY_KEYS
    assert "codex_enabled" not in settings_mod.SECRET_KEYS


def _client(monkeypatch, tmp_path, *, local: bool) -> TestClient:
    """This computer, or (a non-loopback peer) a phone on the LAN, as in test_settings_secrets."""
    from server.web.app import create_app

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    app = create_app()
    return TestClient(app) if local else TestClient(app, client=("192.168.1.50", 54321))


def test_get_settings_reports_codex_status(monkeypatch, tmp_path):
    from server.web import routes_settings

    monkeypatch.setattr(routes_settings.codex_cli, "login_status",
                        lambda: {"installed": True, "logged_in": False})
    data = _client(monkeypatch, tmp_path, local=True).get("/api/settings").json()
    assert data["codex"] == {"installed": True, "logged_in": False}
    assert "codex_enabled" in data["settings"] and "codex_model" in data["settings"]


def test_remote_client_cannot_turn_codex_on(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)
    resp = _client(monkeypatch, tmp_path, local=False).post(
        "/api/settings", json={"codex_enabled": True}
    )
    assert resp.status_code == 403
    assert config.CODEX_ENABLED is False


def test_remote_client_can_resend_codex_unchanged(monkeypatch, tmp_path):
    """The form posts every field, so an unchanged value from a phone must still save."""
    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "CODEX_MODEL", "", raising=False)  # restored after the test
    resp = _client(monkeypatch, tmp_path, local=False).post(
        "/api/settings", json={"codex_enabled": False, "codex_model": "gpt-y"}
    )
    assert resp.status_code == 200
    assert config.CODEX_MODEL == "gpt-y"


def test_doctor_counts_codex_as_an_ai_backend(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "CODEX_ENABLED", True, raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # no `claude` CLI
    monkeypatch.setattr(codex_cli, "find_binary", lambda: "/usr/bin/codex")
    check = doctor.status()["claude"]
    assert check["ok"] and "ChatGPT" in check["detail"]

    # Turned on but not installed: don't claim the AI works.
    monkeypatch.setattr(codex_cli, "find_binary", lambda: None)
    assert not doctor.status()["claude"]["ok"]


def test_puzzle_llm_gate_sees_every_backend(monkeypatch):
    from server.web import routes_puzzles

    monkeypatch.setattr(routes_puzzles.shutil, "which", lambda name: None)
    for name, value in [("LOCAL_LLM_BASE_URL", ""), ("CODEX_ENABLED", False), ("ANTHROPIC_API_KEY", "")]:
        monkeypatch.setattr(config, name, value, raising=False)
    assert not routes_puzzles._has_llm()
    monkeypatch.setattr(config, "CODEX_ENABLED", True, raising=False)
    assert routes_puzzles._has_llm()
    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-x", raising=False)
    assert routes_puzzles._has_llm()  # was missed before: the key alone now counts too


# --- switching backends mid-game -----------------------------------------------------------------


def _claude_ok(session: str):
    """A fake `subprocess.run` for the `claude -p` path that records its argv."""
    seen: dict = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        out = json.dumps({"result": "OK", "subtype": "success", "session_id": session})
        return subprocess.CompletedProcess(cmd, 0, out, "")

    return run, seen


def test_switching_from_codex_to_claude_starts_a_fresh_conversation(codex, monkeypatch):
    """Regression: the board's stored Codex thread id was handed to `claude --resume` and failed."""
    monkeypatch.setattr(claude_bridge, "_SESSION_OWNERS", {})
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    thread = claude_bridge.ask("q1", fen=_FEN)["session_id"]
    assert thread == "thread-1"

    monkeypatch.setattr(config, "CODEX_ENABLED", False, raising=False)  # user turns ChatGPT off
    run, seen = _claude_ok("claude-session")
    monkeypatch.setattr(claude_bridge.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_bridge.subprocess, "run", run)
    res = claude_bridge.ask("q2", fen=_FEN, session_id=thread)
    assert "--resume" not in seen["cmd"]
    assert res["session_id"] == "claude-session"

    # ...and claude's own id still resumes on the next question.
    claude_bridge.ask("q3", fen=_FEN, session_id="claude-session")
    assert seen["cmd"][seen["cmd"].index("--resume") + 1] == "claude-session"


def test_unknown_session_id_still_resumes_claude(monkeypatch):
    """An id the bridge never saw (e.g. from before a restart) is passed through as before."""
    monkeypatch.setattr(claude_bridge, "_SESSION_OWNERS", {})
    for name, value in [("LOCAL_LLM_BASE_URL", ""), ("CODEX_ENABLED", False), ("ANTHROPIC_API_KEY", "")]:
        monkeypatch.setattr(config, name, value, raising=False)
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    run, seen = _claude_ok("old-session")
    monkeypatch.setattr(claude_bridge.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_bridge.subprocess, "run", run)
    claude_bridge.ask("q", fen=_FEN, session_id="old-session")
    assert seen["cmd"][seen["cmd"].index("--resume") + 1] == "old-session"


def test_switching_from_claude_to_codex_starts_a_fresh_thread(codex, monkeypatch):
    monkeypatch.setattr(claude_bridge, "_SESSION_OWNERS", {"claude-session": "claude"})
    monkeypatch.setattr(claude_bridge, "_engine_facts", lambda *a, **k: None)
    monkeypatch.setattr(claude_bridge, "_speed_context", lambda: None)
    claude_bridge.ask("q", fen=_FEN, session_id="claude-session")
    assert "resume" not in codex.last["cmd"]
