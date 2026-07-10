"""Dense-shard downloading for the puzzle trainer (P3).

The vendored baseline (`server/data/puzzles/baseline.jsonl.gz`, ~150/band) always works offline, but
it's thin. This module layers the dense per-band shards (~10k/band) on top, fetched from the SEPARATE
puzzle-data repo's release (`config.PUZZLE_SHARD_REPO` / `PUZZLE_SHARD_TAG`, i.e.
`Chess-analysis-mcp/tintins-chess-puzzles` / `puzzles-v1`) and cached under `<DATA_DIR>/puzzles/`.

The whole set is small (~16 MB / 23 bands), so `ensure_all_bands()` just pulls **every** band in the
background on first puzzle use and keeps it — no per-rating windowing, no LRU eviction, no opt-in.

Everything is **best-effort and never raises**: a missing manifest, a network error, or a checksum
mismatch just leaves the baseline in place. All writes land in `config._puzzle_dir()` (external,
writable), NOT inside the read-only `.app` bundle — so App-mode users get downloads too. Mirrors
`updates.py`'s throttled GitHub fetch + in-process/disk cache + atomic `.tmp` -> os.replace write.
"""
from __future__ import annotations

import hashlib
import json
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


# --- downloading + verification ------------------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_band(lo: int, hi: int) -> Optional[str]:
    """Ensure the dense shard for band [lo, hi) is on disk; return its path or None.

    A cache hit returns immediately. A miss downloads the release asset, verifies sha256 against the
    manifest, atomically moves it into place, and invalidates the in-memory downloaded-pool cache.
    Every failure mode -> None (the vendored baseline stays in place). Downloaded bands persist —
    the whole set is small (~16 MB), so there's no LRU eviction.
    """
    path = _band_path(lo, hi)
    if os.path.exists(path):
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


# --- full-set warm-up ----------------------------------------------------------------------------
# The whole shard set is small (~16 MB), so rather than windowing by rating we just pull EVERY band
# in the background on first puzzle use and keep it — puzzle mode then works fully offline at any
# rating with no per-user tuning. Best-effort: any failure leaves the vendored baseline in place.

_ALL_INFLIGHT = False  # a warm-up thread is currently running (don't spawn a duplicate)
_ALL_COMPLETE = False  # every manifest band is on disk (nothing left to fetch this process)


def _manifest_bands(manifest: Optional[dict]) -> list[tuple[int, int]]:
    """Every (lo, hi) band listed in the manifest, in ascending order."""
    out: list[tuple[int, int]] = []
    for shard in (manifest or {}).get("shards", []):
        band = shard.get("band")
        if isinstance(band, list) and len(band) == 2:
            out.append((int(band[0]), int(band[1])))
    out.sort()
    return out


def _download_all_worker() -> None:
    global _ALL_INFLIGHT, _ALL_COMPLETE
    try:
        bands = _manifest_bands(ensure_manifest())
        for lo, hi in bands:
            ensure_band(lo, hi)  # best-effort; a miss just leaves that band un-downloaded
        if bands and all(os.path.exists(_band_path(lo, hi)) for lo, hi in bands):
            with _LOCK:
                _ALL_COMPLETE = True
    finally:
        with _LOCK:
            _ALL_INFLIGHT = False


def ensure_all_bands() -> None:
    """Fire-and-forget background download of the ENTIRE shard set. Best-effort, non-blocking.

    A no-op when downloads are disabled or the full set is already on disk. Runs at most one thread
    at a time and retries on a later call if a previous attempt didn't complete (e.g. was offline),
    so a user who comes online mid-session still fills in the set without a restart.
    """
    global _ALL_INFLIGHT
    if not config.PUZZLE_DOWNLOAD:
        return
    with _LOCK:
        if _ALL_INFLIGHT or _ALL_COMPLETE:
            return
        _ALL_INFLIGHT = True
    threading.Thread(target=_download_all_worker, daemon=True).start()
