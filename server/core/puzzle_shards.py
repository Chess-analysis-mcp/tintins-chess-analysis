"""Drift-aware dense-shard downloading for the puzzle trainer (P3).

The vendored baseline (`server/data/puzzles/baseline.jsonl.gz`, ~150/band) always works offline, but
it's thin. This module layers the dense per-band shards (~10k/band) on top, fetched **on demand**
from the SEPARATE puzzle-data repo's release (`config.PUZZLE_SHARD_REPO` / `PUZZLE_SHARD_TAG`, i.e.
`Chess-analysis-mcp/tintins-chess-puzzles` / `puzzles-v1`) and cached under `<DATA_DIR>/puzzles/`.

Everything is **best-effort and never raises**: a missing manifest, a network error, or a checksum
mismatch just leaves the baseline in place. All writes land in `config._puzzle_dir()` (external,
writable), NOT inside the read-only `.app` bundle — so App-mode users get downloads too.

Mirrors two existing patterns: `updates.py` (throttled GitHub fetch + in-process/disk cache) and
`analysis_cache._prune` (LRU-by-mtime cap + atomic `.tmp` -> os.replace write).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from typing import Optional

import httpx

from .. import config

# Band geometry — must match scripts/build_puzzle_shards.py (BAND_WIDTH/BAND_LO/BAND_HI).
BAND_WIDTH = 100
BAND_LO = 600
BAND_HI = 2900  # exclusive upper edge; the last band is 2800-2900

_LOCK = threading.Lock()
# Cached manifest: {"version", "band_width", "shards": [...], "checked_at": float}. None = not loaded.
_MANIFEST_CACHE: Optional[dict] = None
# Bands (lo, hi) whose download is currently in flight, so we don't spawn duplicate workers.
_INFLIGHT: set[tuple[int, int]] = set()


# --- paths ---------------------------------------------------------------------------------------

def _manifest_path() -> str:
    return os.path.join(config._puzzle_dir(), "manifest.json")


def _band_filename(lo: int, hi: int) -> str:
    return f"band_{lo}_{hi}.jsonl.gz"


def _band_path(lo: int, hi: int) -> str:
    return os.path.join(config._puzzle_dir(), _band_filename(lo, hi))


def band_bounds(rating: float) -> tuple[int, int]:
    """The [lo, hi) band a rating falls in — clamped to the published range, like the build script."""
    r = int(rating)
    lo = max(BAND_LO, min(BAND_HI - BAND_WIDTH, (r // BAND_WIDTH) * BAND_WIDTH))
    return lo, lo + BAND_WIDTH


def _asset_url(filename: str) -> str:
    """GitHub release-asset URL: .../releases/download/<tag>/<filename> (data repo, not the app repo)."""
    return (
        f"https://github.com/{config.PUZZLE_SHARD_REPO}"
        f"/releases/download/{config.PUZZLE_SHARD_TAG}/{filename}"
    )


# --- manifest (throttled, best-effort) -----------------------------------------------------------

def _load_disk_manifest() -> Optional[dict]:
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("shards"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_disk_manifest(entry: dict) -> None:
    try:
        os.makedirs(config._puzzle_dir(), exist_ok=True)
        tmp = f"{_manifest_path()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, _manifest_path())
    except OSError:
        pass


def _fetch_manifest() -> Optional[dict]:
    """GET manifest.json from the data-repo release. Returns the parsed dict or None (error)."""
    try:
        resp = httpx.get(
            _asset_url("manifest.json"),
            headers={"User-Agent": "chess-analysis-mcp"},
            timeout=config.UPDATE_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("shards"), list):
        return None
    return data


def ensure_manifest(force: bool = False) -> Optional[dict]:
    """Return the shard manifest, refreshing from GitHub only past the throttle window.

    Best-effort: on a fetch failure keep the stale disk copy if we have one. Downloads are gated on
    `config.PUZZLE_DOWNLOAD`; when off we still serve a previously-downloaded disk manifest (so the
    cached shards remain usable) but never hit the network.
    """
    global _MANIFEST_CACHE
    now = time.time()
    with _LOCK:
        if _MANIFEST_CACHE is None:
            _MANIFEST_CACHE = _load_disk_manifest()
        fresh = (
            _MANIFEST_CACHE is not None
            and (now - _MANIFEST_CACHE.get("checked_at", 0)) < config.PUZZLE_MANIFEST_INTERVAL
        )
        if (fresh or not config.PUZZLE_DOWNLOAD) and not force:
            return _MANIFEST_CACHE
        fetched = _fetch_manifest()
        if fetched is None:
            return _MANIFEST_CACHE  # stale-but-usable, or None
        fetched["checked_at"] = now
        _MANIFEST_CACHE = fetched
        _save_disk_manifest(fetched)
        return fetched


def _shard_entry(manifest: Optional[dict], lo: int, hi: int) -> Optional[dict]:
    if not manifest:
        return None
    fname = _band_filename(lo, hi)
    for shard in manifest.get("shards", []):
        if shard.get("file") == fname or shard.get("band") == [lo, hi]:
            return shard
    return None


# --- downloading + verification + LRU prune ------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prune() -> None:
    """LRU-cap downloaded band shards by mtime (copy of analysis_cache._prune). Best-effort."""
    cap = config.PUZZLE_CACHE_MAX
    if cap <= 0:
        return
    try:
        d = config._puzzle_dir()
        entries = [
            os.path.join(d, n)
            for n in os.listdir(d)
            if n.startswith("band_") and n.endswith(".jsonl.gz")
        ]
        if len(entries) <= cap:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        for p in entries[: len(entries) - cap]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def ensure_band(lo: int, hi: int) -> Optional[str]:
    """Ensure the dense shard for band [lo, hi) is on disk; return its path or None.

    A cache hit just marks the file hot (mtime) so the LRU keeps it. A miss downloads the release
    asset, verifies sha256 against the manifest, atomically moves it into place, prunes, and
    invalidates the in-memory downloaded-pool cache. Every failure mode -> None (baseline stays).
    """
    path = _band_path(lo, hi)
    if os.path.exists(path):
        try:
            os.utime(path, None)  # mark recently-used for the LRU
        except OSError:
            pass
        return path
    if not config.PUZZLE_DOWNLOAD:
        return None

    manifest = ensure_manifest()
    shard = _shard_entry(manifest, lo, hi)
    if not shard:
        return None

    tmp = f"{path}.tmp"
    try:
        os.makedirs(config._puzzle_dir(), exist_ok=True)
        with httpx.stream(
            "GET",
            _asset_url(shard["file"]),
            headers={"User-Agent": "chess-analysis-mcp"},
            timeout=config.UPDATE_TIMEOUT,
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                return None
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
    except (httpx.HTTPError, OSError):
        _safe_remove(tmp)
        return None

    want = shard.get("sha256")
    if want:
        try:
            if _sha256(tmp) != want:
                _safe_remove(tmp)
                return None
        except OSError:
            _safe_remove(tmp)
            return None

    try:
        os.replace(tmp, path)
    except OSError:
        _safe_remove(tmp)
        return None

    _prune()
    _invalidate_pool()
    return path


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _invalidate_pool() -> None:
    """Clear puzzles._downloaded_pool's cache after a new shard lands (lazy import avoids a cycle)."""
    try:
        from . import puzzles
        puzzles._downloaded_pool.cache_clear()
    except Exception:  # noqa: BLE001 - cache invalidation must never break a download
        pass


# --- RD-aware warm-up ----------------------------------------------------------------------------

def bands_for(rating: float, rd: float) -> list[tuple[int, int]]:
    """The band set to keep warm around `rating`, widened while RD is high (calibrating).

    n = clamp(ceil(rd / PUZZLE_RD_PER_BAND), 2, 6): a fresh rd~350 user warms a broad neighbouring
    spread; a settled rd~60 user only the tight +/-2. Bands are clamped to the published range.
    """
    try:
        per = max(1, config.PUZZLE_RD_PER_BAND)
        n = max(2, min(6, math.ceil(max(0.0, rd) / per)))
    except (TypeError, ValueError):
        n = 2
    lo, hi = band_bounds(rating)
    out: list[tuple[int, int]] = []
    for k in range(-n, n + 1):
        blo = lo + k * BAND_WIDTH
        if BAND_LO <= blo <= BAND_HI - BAND_WIDTH:
            out.append((blo, blo + BAND_WIDTH))
    return out


def _warm(bands: list[tuple[int, int]]) -> None:
    for lo, hi in bands:
        with _LOCK:
            if (lo, hi) in _INFLIGHT:
                continue
            _INFLIGHT.add((lo, hi))
        try:
            ensure_band(lo, hi)
        finally:
            with _LOCK:
                _INFLIGHT.discard((lo, hi))


def ensure_bands_around(rating: float, rd: float) -> None:
    """Fire-and-forget background warm-up of the RD-scaled band window. Best-effort, non-blocking.

    Serves the CURRENT call from whatever is already cached; the fetch benefits the next call. A
    no-op when downloads are disabled or every needed band is already present.
    """
    if not config.PUZZLE_DOWNLOAD:
        return
    bands = [b for b in bands_for(rating, rd) if not os.path.exists(_band_path(*b))]
    if not bands:
        return
    threading.Thread(target=_warm, args=(bands,), daemon=True).start()
