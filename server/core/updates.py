"""Update checking + channel detection for the in-app "update available" notice.

Three distribution channels, distinguished by the install layout (NOT app-mode — both the
`.command` launcher and the `.app` set CHESS_APP_MODE=1):
  * ``git`` — a cloned checkout (has ``.git``). Self-updates via ``git pull``.
  * ``zip`` — the downloaded source zip (writable tree, no ``.git``). Self-updates by extracting the
              release source tarball over the folder (``scripts/apply_update.py``).
  * ``app`` — the macOS ``.app`` bundle (read-only at runtime). Manual download only.

Everything here is best-effort and NEVER raises to the caller: a network failure, a missing
release, or a malformed version just yields "no update". The GitHub lookup is throttled (cached
in-process AND on disk) so we don't hammer the API across restarts.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import httpx

from server import config

_SEMVER_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")

_LOCK = threading.Lock()
# Last GitHub result: {"tag": str, "url": str, "title": str, "checked_at": float}. None = not yet.
_CACHE: dict | None = None


# --- version comparison -------------------------------------------------------------------------

def parse_version(s: str) -> tuple[int, int, int]:
    """Lenient parse of "v0.2.0" / "0.2" / "0.2.0-beta" -> (major, minor, patch). Junk -> (0,0,0)."""
    m = _SEMVER_RE.search(s or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def severity(current: str, latest: str) -> str:
    """Bump size from current -> latest: "major" / "minor" / "patch" / "none" (latest not newer)."""
    cur, lat = parse_version(current), parse_version(latest)
    if lat <= cur:
        return "none"
    if lat[0] != cur[0]:
        return "major"
    if lat[1] != cur[1]:
        return "minor"
    return "patch"


# --- channel detection --------------------------------------------------------------------------

def update_channel() -> str:
    """"git" (has .git → git pull), "app" (read-only .app bundle), or "zip" (writable source)."""
    root = config.PROJECT_ROOT
    if os.path.isdir(os.path.join(root, ".git")):
        return "git"
    norm = root.replace(os.sep, "/")
    if os.environ.get("CHESS_APP_BUNDLE") == "1" or ".app/Contents/" in norm + "/":
        return "app"
    return "zip"


def can_self_update(channel: str | None = None) -> bool:
    """git + zip can apply in place; the read-only .app can only point the user at a download."""
    return (channel or update_channel()) in ("git", "zip")


# --- the GitHub release lookup (throttled, best-effort) -----------------------------------------

def _disk_cache_path() -> str:
    return os.path.join(config.DATA_DIR, "update-check.json")


def _load_disk_cache() -> dict | None:
    try:
        with open(_disk_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "tag" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_disk_cache(entry: dict) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_disk_cache_path(), "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
    except OSError:
        pass


def _fetch_latest_release() -> dict | None:
    """Hit GitHub's "latest release" endpoint. Returns {tag,url,title} or None (no release / error)."""
    url = f"https://api.github.com/repos/{config.UPDATE_REPO}/releases/latest"
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "chess-analysis-mcp", "Accept": "application/vnd.github+json"},
            timeout=config.UPDATE_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:  # 404 = repo has no releases yet; anything else = transient
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    tag = (body.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "tag": tag,
        "url": (body.get("html_url") or f"https://github.com/{config.UPDATE_REPO}/releases/latest").strip(),
        "title": (body.get("name") or tag).strip(),
    }


def _cached_release(force: bool) -> dict | None:
    """Return the latest-release info, refreshing from GitHub only when the throttle window elapsed."""
    global _CACHE
    now = time.time()
    with _LOCK:
        if _CACHE is None:
            _CACHE = _load_disk_cache()  # survive restarts
        fresh = _CACHE is not None and (now - _CACHE.get("checked_at", 0)) < config.UPDATE_CHECK_INTERVAL
        if fresh and not force:
            return _CACHE
        release = _fetch_latest_release()
        if release is None:
            # Keep a stale-but-usable cache if we have one; otherwise note we tried (avoid hammering).
            if _CACHE is not None:
                return _CACHE
            _CACHE = {"tag": "", "url": "", "title": "", "checked_at": now}
            _save_disk_cache(_CACHE)
            return None
        release["checked_at"] = now
        _CACHE = release
        _save_disk_cache(release)
        return release


def check_for_update(force: bool = False) -> dict:
    """The board's update status. Never raises; disabled / offline / no-release -> update_available False.

    {current, latest, update_available, severity, channel, can_self_update, release_url, title}.
    """
    channel = update_channel()
    base = {
        "current": config.APP_VERSION,
        "latest": "",
        "update_available": False,
        "severity": "none",
        "channel": channel,
        "can_self_update": can_self_update(channel),
        "release_url": f"https://github.com/{config.UPDATE_REPO}/releases/latest",
        "title": "",
    }
    if not config.UPDATE_CHECK_ENABLED:
        return base
    try:
        release = _cached_release(force)
    except Exception:  # noqa: BLE001 - a self-check must never break the page
        return base
    if not release or not release.get("tag"):
        return base
    sev = severity(config.APP_VERSION, release["tag"])
    base.update(
        latest=release["tag"].lstrip("vV"),
        update_available=sev != "none",
        severity=sev,
        release_url=release.get("url") or base["release_url"],
        title=release.get("title") or "",
    )
    return base


# --- one-click apply: the sentinel the launcher consumes on next start --------------------------

def sentinel_path() -> str:
    """Marker file the launcher checks at startup to apply a staged update (git pull / tarball).

    Lives in the project root (not DATA_DIR) so the launcher finds it in plain bash as
    ``./.update-requested`` — no per-OS data-dir resolution needed. The project root is writable on
    exactly the self-updatable channels (git / zip); gitignored so it isn't tracked."""
    return os.path.join(config.PROJECT_ROOT, ".update-requested")


def request_update(latest: str = "") -> dict:
    """Stage an update (write the sentinel). The launcher applies it on the NEXT start, then deletes
    it. Only meaningful for self-updatable channels; the caller gates on can_self_update()."""
    with open(sentinel_path(), "w", encoding="utf-8") as fh:
        json.dump({"latest": latest, "channel": update_channel(), "requested_at": time.time()}, fh)
    return {"ok": True, "restart_required": True}


# --- immediate apply: for installs started WITHOUT one of our launchers ------------------------
# The sentinel above is consumed by `Tintin's AI Chess Analysis.command` / `.bat` on their next
# start. Someone who cloned the repo and runs `uv run python scripts/run_web.py` (or reaches the
# board through the MCP server in Claude Code) has no launcher, so a sentinel would sit there
# forever and "Update now" would silently do nothing. For those sessions we update the checkout
# right here instead, and tell the user to restart. Deps take care of themselves: `uv run` syncs
# the environment on the next start.


def _git(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", config.PROJECT_ROOT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _last_line(*candidates: str) -> str:
    for text in candidates:
        lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
        if lines:
            return lines[-1][:300]
    return ""


def apply_now() -> dict:
    """Update this install in place, right now. Returns {ok, restart_required, detail} or
    {ok: False, error}. Never raises; the caller gates on can_self_update()."""
    channel = update_channel()
    if channel == "git":
        if not shutil.which("git"):
            return {
                "ok": False,
                "error": "Git isn't installed, so this checkout can't pull. Install Git from "
                "https://git-scm.com/downloads, or download the latest release instead.",
            }
        try:
            dirty = _git("status", "--porcelain")
            # Refuse rather than stash: on a developer's checkout those edits are the work in
            # progress, and a web button is the wrong place to move someone's uncommitted changes.
            if dirty.returncode == 0 and dirty.stdout.strip():
                return {
                    "ok": False,
                    "error": "This checkout has uncommitted changes, so nothing was touched. "
                    "Commit or stash them and try again (or just run `git pull` yourself).",
                }
            pull = _git("pull", "--ff-only")
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 - reported, not raised
            return {"ok": False, "error": f"Couldn't run git: {exc}"}
        if pull.returncode != 0:
            return {
                "ok": False,
                "error": "git pull --ff-only failed: "
                + (_last_line(pull.stderr, pull.stdout) or "unknown error"),
            }
        return {"ok": True, "restart_required": True, "detail": _last_line(pull.stdout)}

    if channel == "zip":
        script = os.path.join(config.PROJECT_ROOT, "scripts", "apply_update.py")
        try:
            proc = subprocess.run(
                [sys.executable, script], capture_output=True, text=True, timeout=900
            )
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Couldn't run the updater: {exc}"}
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": _last_line(proc.stderr, proc.stdout)
                or "The updater failed. Download the latest release instead.",
            }
        return {"ok": True, "restart_required": True, "detail": _last_line(proc.stdout)}

    return {
        "ok": False,
        "error": "This install can't update itself; download the latest from Releases.",
    }
