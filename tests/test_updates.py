"""Tests for the update-check layer (version compare, channel detection, sentinel; network mocked)."""
from __future__ import annotations

import os

import pytest

from server import config
from server.core import updates


# --- version parsing + severity -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v0.2.0", (0, 2, 0)),
        ("0.2", (0, 2, 0)),
        ("1.10.3", (1, 10, 3)),
        ("v2.0.0-beta", (2, 0, 0)),
        ("garbage", (0, 0, 0)),
        ("", (0, 0, 0)),
    ],
)
def test_parse_version(raw, expected):
    assert updates.parse_version(raw) == expected


@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("0.1.1", "0.2.0", "minor"),
        ("0.1.1", "1.0.0", "major"),
        ("0.1.1", "0.1.2", "patch"),
        ("0.2.0", "0.1.1", "none"),   # latest older
        ("0.2.0", "0.2.0", "none"),   # equal
        ("1.10", "1.11", "minor"),    # the user's own example
        ("1.10", "2.0", "major"),     # the user's own example
    ],
)
def test_severity(current, latest, expected):
    assert updates.severity(current, latest) == expected


# --- channel detection --------------------------------------------------------------------------

def test_channel_git(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("CHESS_APP_BUNDLE", raising=False)
    assert updates.update_channel() == "git"
    assert updates.can_self_update() is True


def test_channel_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))  # no .git
    monkeypatch.delenv("CHESS_APP_BUNDLE", raising=False)
    assert updates.update_channel() == "zip"
    assert updates.can_self_update() is True


def test_channel_app_via_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CHESS_APP_BUNDLE", "1")
    assert updates.update_channel() == "app"
    assert updates.can_self_update() is False


def test_channel_app_via_path(monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", "/Applications/Foo.app/Contents/Resources/repo")
    monkeypatch.delenv("CHESS_APP_BUNDLE", raising=False)
    assert updates.update_channel() == "app"


# --- check_for_update (network mocked) ----------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    """Each test starts with a clean in-process cache and an isolated on-disk cache dir."""
    monkeypatch.setattr(updates, "_CACHE", None)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", True)


def test_check_reports_update(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "0.1.1")
    monkeypatch.setattr(updates, "_fetch_latest_release",
                        lambda: {"tag": "v0.2.0", "url": "https://example/r", "title": "0.2.0"})
    out = updates.check_for_update(force=True)
    assert out["update_available"] is True
    assert out["severity"] == "minor"
    assert out["latest"] == "0.2.0"        # leading v stripped
    assert out["release_url"] == "https://example/r"


def test_check_no_update_when_current_is_latest(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "0.2.0")
    monkeypatch.setattr(updates, "_fetch_latest_release",
                        lambda: {"tag": "v0.2.0", "url": "u", "title": "t"})
    assert updates.check_for_update(force=True)["update_available"] is False


def test_check_offline_is_safe(monkeypatch):
    monkeypatch.setattr(config, "APP_VERSION", "0.1.1")
    monkeypatch.setattr(updates, "_fetch_latest_release", lambda: None)  # offline / no release
    out = updates.check_for_update(force=True)
    assert out["update_available"] is False
    assert out["current"] == "0.1.1"


def test_check_disabled(monkeypatch):
    monkeypatch.setattr(config, "UPDATE_CHECK_ENABLED", False)

    def _boom():  # must not be called when disabled
        raise AssertionError("network hit while disabled")

    monkeypatch.setattr(updates, "_fetch_latest_release", _boom)
    assert updates.check_for_update(force=True)["update_available"] is False


# --- sentinel -----------------------------------------------------------------------------------

def test_request_update_writes_sentinel(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
    res = updates.request_update("0.2.0")
    assert res["ok"] is True and res["restart_required"] is True
    assert os.path.exists(updates.sentinel_path())
    assert updates.sentinel_path() == str(tmp_path / ".update-requested")


# --- apply_now(): the immediate path for installs started without a launcher --------------------


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def git_checkout(tmp_path, monkeypatch):
    """Make update_channel() report "git" and give `git` a stub. Returns the recorded calls."""
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
    os.makedirs(os.path.join(tmp_path, ".git"), exist_ok=True)
    monkeypatch.setattr(updates.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []
    replies: dict[str, _Proc] = {"status": _Proc(0, ""), "pull": _Proc(0, "Updating abc..def\n")}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        for key, proc in replies.items():
            if key in cmd:
                return proc
        return _Proc(0, "")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    return {"calls": calls, "replies": replies}


def test_apply_now_pulls_a_clean_checkout(git_checkout):
    res = updates.apply_now()
    assert res["ok"] and res["restart_required"]
    # Checked for local edits first, then fast-forwarded (never a merge, never a rebase).
    assert ["status", "--porcelain"] == git_checkout["calls"][0][-2:]
    assert git_checkout["calls"][1][-2:] == ["pull", "--ff-only"]


def test_apply_now_refuses_a_dirty_checkout(git_checkout):
    """A developer's uncommitted work must not be stashed or clobbered by a web button."""
    git_checkout["replies"]["status"] = _Proc(0, " M server/config.py\n")
    res = updates.apply_now()
    assert not res["ok"]
    assert "uncommitted changes" in res["error"]
    assert not any("pull" in c for c in git_checkout["calls"])  # nothing was pulled


def test_apply_now_reports_a_failed_pull(git_checkout):
    git_checkout["replies"]["pull"] = _Proc(1, "", "fatal: Not possible to fast-forward, aborting.")
    res = updates.apply_now()
    assert not res["ok"]
    assert "fast-forward" in res["error"]


def test_apply_now_without_git_installed(git_checkout, monkeypatch):
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)
    res = updates.apply_now()
    assert not res["ok"] and "Git isn't installed" in res["error"]


def test_apply_now_zip_channel_runs_the_updater(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)  # no .git -> zip
    monkeypatch.delenv("CHESS_APP_BUNDLE", raising=False)
    assert updates.update_channel() == "zip"
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc(0, "Updated to v9.9.9\n")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    res = updates.apply_now()
    assert res["ok"] and res["restart_required"]
    assert seen["cmd"][0] == updates.sys.executable
    assert seen["cmd"][1].endswith(os.path.join("scripts", "apply_update.py"))


def test_apply_now_zip_channel_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
    monkeypatch.delenv("CHESS_APP_BUNDLE", raising=False)
    monkeypatch.setattr(
        updates.subprocess, "run", lambda cmd, **kw: _Proc(1, "", "could not download the tarball")
    )
    res = updates.apply_now()
    assert not res["ok"] and "tarball" in res["error"]


def test_apply_now_refuses_the_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
    monkeypatch.setenv("CHESS_APP_BUNDLE", "1")
    res = updates.apply_now()
    assert not res["ok"] and "can't update itself" in res["error"]
