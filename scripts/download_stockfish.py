#!/usr/bin/env python3
"""Download the official static Stockfish from GitHub into the app's managed engine path.

The cross-platform fallback used by ``install.sh`` / ``install.ps1`` (and re-usable anywhere) when
there's no system Stockfish and no working package manager — the same idea as the macOS ``.app``
launcher's ``download_stockfish`` (``scripts/build_app.sh``), but for Windows and Linux too. It
needs **no package manager, no sudo, and no PATH changes**: the binary lands at
``config._managed_stockfish_path()`` (``<DATA_DIR>/engine/stockfish[.exe]``), which the app
auto-detects via ``config._resolve_stockfish``.

Stdlib-only (urllib / tarfile / zipfile), so it can run with any Python. Picks the build for this
OS + CPU, downloads, extracts just the engine binary, verifies it actually speaks UCI (an
over-aggressive CPU variant would crash), and falls back to a more compatible build if not. On
success it prints the resolved engine path to stdout and exits 0 (also a no-op success when a system
engine already exists); on failure it prints a hint to stderr and exits 1.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

# Make `server` importable when run straight from a checkout (this file is <repo>/scripts/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import config  # noqa: E402

# Pin the Stockfish release this app is tested against (kept in sync with build_app.sh's `sf_18`).
TAG = "sf_18"
_BASE = f"https://github.com/official-stockfish/Stockfish/releases/download/{TAG}"


def _candidates() -> list[str]:
    """Asset base-names (no extension) to try, best-performing first → most-compatible last.

    Empty when there's no prebuilt static binary for this platform/CPU (e.g. Linux on ARM), so the
    caller falls back to a manual-install hint.
    """
    machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    import platform

    machine = (platform.machine() or machine).lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return ["stockfish-macos-m1-apple-silicon"]
        return [
            "stockfish-macos-x86-64-avx2",
            "stockfish-macos-x86-64-sse41-popcnt",
            "stockfish-macos-x86-64",
        ]
    if os.name == "nt":
        if "arm" in machine or machine == "aarch64":
            return ["stockfish-windows-armv8"]
        return [
            "stockfish-windows-x86-64-avx2",
            "stockfish-windows-x86-64-sse41-popcnt",
            "stockfish-windows-x86-64",
        ]
    # Linux / other POSIX. The Stockfish release ships only x86-64 Linux statics (ARM Linux has no
    # generic build), so non-x86 returns empty and the installer points the user at a manual install.
    if machine in ("x86_64", "amd64", "x64", "i386", "i686"):
        return [
            "stockfish-ubuntu-x86-64-avx2",
            "stockfish-ubuntu-x86-64-sse41-popcnt",
            "stockfish-ubuntu-x86-64",
        ]
    return []


def _progress_band() -> tuple[str, int, int] | None:
    """Where to report download progress: (file, start_pct, end_pct), or None if not requested.

    The installer sets CHESS_INSTALL_PROGRESS (the splash's progress.js) and, optionally,
    CHESS_INSTALL_PROGRESS_BAND="start,end" — the percentage slice this download owns on the bar.
    """
    path = os.environ.get("CHESS_INSTALL_PROGRESS")
    if not path:
        return None
    band = os.environ.get("CHESS_INSTALL_PROGRESS_BAND", "72,92")
    try:
        start_s, end_s = band.split(",")
        return path, int(start_s), int(end_s)
    except Exception:  # noqa: BLE001 - a malformed band just disables progress reporting
        return None


def _write_progress(path: str, pct: int, step: str) -> None:
    """Atomically write the splash's progress.js (tmp+rename) so it never reads a partial file."""
    step = step.replace("\\", "\\\\").replace('"', '\\"')
    line = (
        "window.__setInstallProgress && window.__setInstallProgress("
        f'{{ pct: {pct}, step: "{step}" }});\n'
    )
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(line)
        os.replace(tmp, path)
    except OSError:
        pass


def _download(url: str, dest_file: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "tintins-chess-analysis"})
    band = _progress_band()
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest_file, "wb") as out:
        if band is None:
            shutil.copyfileobj(resp, out)
            return
        # Stream in chunks so we can map bytes-downloaded onto the splash's progress bar. The band
        # is the slice of the overall bar this engine download owns; we only rewrite progress.js when
        # the whole-number percent changes, to avoid hammering the disk.
        path, lo, hi = band
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        got = 0
        last = -1
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if total > 0:
                pct = lo + int((hi - lo) * got / total)
                if pct != last:
                    last = pct
                    _write_progress(path, pct, "Downloading the chess engine (Stockfish)…")


def _pick_member(names: list[str], asset: str, want_exe: bool) -> str | None:
    """The archive member that is the engine binary (not the bundled docs/scripts)."""
    target = asset + (".exe" if want_exe else "")
    for name in names:  # exact basename match (e.g. stockfish/stockfish-ubuntu-x86-64-avx2)
        if os.path.basename(name) == target:
            return name
    for name in names:  # heuristic fallback: a stockfish-* binary
        base = os.path.basename(name)
        if not base or name.endswith("/"):
            continue
        if not base.lower().startswith("stockfish"):
            continue
        if want_exe and base.lower().endswith(".exe"):
            return name
        if not want_exe and "." not in base:  # the POSIX binary has no extension
            return name
    return None


def _extract_binary(archive: str, asset: str, dest: str) -> bool:
    """Write just the engine binary from `archive` to `dest`. True on success."""
    want_exe = os.name == "nt"
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = _pick_member(zf.namelist(), asset, want_exe)
            if not member:
                return False
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        return True
    with tarfile.open(archive) as tf:
        member = _pick_member(tf.getnames(), asset, want_exe)
        if not member:
            return False
        src = tf.extractfile(member)
        if src is None:
            return False
        with src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    return True


def _runs_uci(path: str) -> bool:
    """Confirm the binary launches and speaks UCI (catches an unsupported-CPU build that crashes)."""
    try:
        proc = subprocess.run(
            [path], input="uci\nquit\n", capture_output=True, text=True, timeout=20
        )
    except Exception:  # noqa: BLE001 - any launch failure means this build is unusable here
        return False
    return "uciok" in (proc.stdout or "")


def main() -> int:
    # A system engine on PATH already satisfies the app — nothing to download.
    existing = shutil.which("stockfish")
    if existing:
        print(existing)
        return 0

    dest = config._managed_stockfish_path()
    if os.path.exists(dest) and _runs_uci(dest):
        print(dest)  # a previous download is still good
        return 0

    candidates = _candidates()
    if not candidates:
        sys.stderr.write(
            "No prebuilt Stockfish is published for this platform/CPU. Install it from "
            "https://stockfishchess.org/download/ and set the Stockfish path in Settings.\n"
        )
        return 1

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    ext = ".zip" if os.name == "nt" else ".tar"
    for asset in candidates:
        url = f"{_BASE}/{asset}{ext}"
        tmp = tempfile.mkdtemp()
        try:
            archive = os.path.join(tmp, "sf" + ext)
            sys.stderr.write(f"Downloading {asset}{ext} ({TAG})...\n")
            _download(url, archive)
            if not _extract_binary(archive, asset, dest):
                sys.stderr.write(f"  {asset}: engine binary not found in the archive — skipping.\n")
                continue
            if os.name != "nt":
                os.chmod(dest, 0o755)
            if _runs_uci(dest):
                print(dest)
                return 0
            sys.stderr.write(f"  {asset} didn't run on this CPU — trying a more compatible build...\n")
            try:
                os.remove(dest)
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - try the next candidate, then report failure
            sys.stderr.write(f"  {asset}: {exc}\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    sys.stderr.write(
        "Could not download Stockfish (check your internet connection). Install it from "
        "https://stockfishchess.org/download/ and set the Stockfish path in Settings.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
