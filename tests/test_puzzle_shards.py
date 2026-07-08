"""Drift-aware shard downloading (P3): manifest throttle, sha256 verification, RD-aware band width,
and the LRU prune. All network is mocked — these never hit GitHub."""
from __future__ import annotations

import gzip
import hashlib
import json
import os

import pytest

from server import config
from server.core import puzzle_shards
from server.core import puzzles


BAND_BYTES = gzip.compress(b'{"id":"z1","fen":"8/8/8/8/8/8/8/8 w - - 0 1","moves":["a1a2"],'
                           b'"rating":1250,"rd":50,"themes":["fork"]}\n')
GOOD_SHA = hashlib.sha256(BAND_BYTES).hexdigest()


def _manifest(sha: str) -> dict:
    return {
        "version": 1,
        "band_width": 100,
        "shards": [
            {"band": [1200, 1300], "file": "band_1200_1300.jsonl.gz",
             "count": 1, "themes": {"fork": 1}, "bytes": len(BAND_BYTES), "sha256": sha},
        ],
    }


class _FakeResp:
    """Doubles as an httpx.get response and an httpx.stream context manager."""
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data
        self._content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self, n):
        yield self._content


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point DATA_DIR at a tmp dir, enable downloads, and reset the module + pool caches."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PUZZLE_DOWNLOAD", True)
    puzzle_shards._MANIFEST_CACHE = None
    puzzles._downloaded_pool.cache_clear()
    yield
    puzzle_shards._MANIFEST_CACHE = None
    puzzles._downloaded_pool.cache_clear()


# --- band geometry + RD-aware width -------------------------------------------------------------

def test_band_bounds_clamps_to_range():
    assert puzzle_shards.band_bounds(1234) == (1200, 1300)
    assert puzzle_shards.band_bounds(100) == (600, 700)     # below the floor
    assert puzzle_shards.band_bounds(9000) == (2800, 2900)  # above the ceiling


def test_bands_for_widens_with_rd():
    fresh = puzzle_shards.bands_for(1500, 350)   # calibrating -> wide
    settled = puzzle_shards.bands_for(1500, 60)  # established -> tight
    assert len(fresh) > len(settled)
    # A fresh, high-RD player warms a strictly wider neighbouring-Elo set.
    assert set(settled).issubset(set(fresh))
    # Never narrower than +/-2 bands (5 total) nor wider than +/-6 (13 total).
    assert 5 <= len(settled)
    assert len(fresh) <= 13


# --- manifest throttle --------------------------------------------------------------------------

def test_manifest_throttled_and_disk_cached(monkeypatch):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeResp(200, json_data=_manifest(GOOD_SHA))

    monkeypatch.setattr(puzzle_shards.httpx, "get", fake_get)
    m1 = puzzle_shards.ensure_manifest()
    m2 = puzzle_shards.ensure_manifest()  # within the throttle window -> served from cache
    assert m1 and m2 and calls["n"] == 1
    # Persisted to disk under <DATA_DIR>/puzzles/manifest.json.
    assert os.path.exists(os.path.join(config._puzzle_dir(), "manifest.json"))


def test_manifest_fetch_failure_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise puzzle_shards.httpx.HTTPError("network down")

    monkeypatch.setattr(puzzle_shards.httpx, "get", boom)
    assert puzzle_shards.ensure_manifest() is None  # no crash, just no manifest


# --- shard download + sha256 verification -------------------------------------------------------

def test_ensure_band_rejects_checksum_mismatch(monkeypatch):
    monkeypatch.setattr(puzzle_shards.httpx, "get",
                        lambda *a, **k: _FakeResp(200, json_data=_manifest("deadbeef")))
    monkeypatch.setattr(puzzle_shards.httpx, "stream",
                        lambda *a, **k: _FakeResp(200, content=BAND_BYTES))
    path = puzzle_shards.ensure_band(1200, 1300)
    assert path is None
    assert not os.path.exists(puzzle_shards._band_path(1200, 1300))


def test_ensure_band_downloads_verifies_and_invalidates_pool(monkeypatch):
    monkeypatch.setattr(puzzle_shards.httpx, "get",
                        lambda *a, **k: _FakeResp(200, json_data=_manifest(GOOD_SHA)))
    monkeypatch.setattr(puzzle_shards.httpx, "stream",
                        lambda *a, **k: _FakeResp(200, content=BAND_BYTES))
    assert puzzles._downloaded_pool() == []  # nothing cached yet (also seeds the lru cache)
    path = puzzle_shards.ensure_band(1200, 1300)
    assert path and os.path.exists(path)
    # The pool cache was invalidated, so the newly-downloaded puzzle is now visible.
    pool = puzzles._downloaded_pool()
    assert any(p["id"] == "z1" for p in pool)


# --- LRU prune ----------------------------------------------------------------------------------

def test_prune_caps_to_max(monkeypatch):
    monkeypatch.setattr(config, "PUZZLE_CACHE_MAX", 1)
    d = config._puzzle_dir()
    os.makedirs(d, exist_ok=True)
    old = os.path.join(d, "band_1000_1100.jsonl.gz")
    new = os.path.join(d, "band_1200_1300.jsonl.gz")
    for p in (old, new):
        with open(p, "wb") as fh:
            fh.write(BAND_BYTES)
    os.utime(old, (1, 1))  # make `old` the least-recently-used
    puzzle_shards._prune()
    assert os.path.exists(new) and not os.path.exists(old)


def test_prune_unbounded_when_zero(monkeypatch):
    monkeypatch.setattr(config, "PUZZLE_CACHE_MAX", 0)
    d = config._puzzle_dir()
    os.makedirs(d, exist_ok=True)
    for lo in (1000, 1100, 1200):
        with open(os.path.join(d, f"band_{lo}_{lo + 100}.jsonl.gz"), "wb") as fh:
            fh.write(BAND_BYTES)
    puzzle_shards._prune()
    assert len([n for n in os.listdir(d) if n.startswith("band_")]) == 3
