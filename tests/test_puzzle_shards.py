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
    puzzle_shards._ALL_INFLIGHT = False
    puzzle_shards._ALL_COMPLETE = False
    puzzles._downloaded_pool.cache_clear()
    yield
    puzzle_shards._MANIFEST_CACHE = None
    puzzle_shards._ALL_INFLIGHT = False
    puzzle_shards._ALL_COMPLETE = False
    puzzles._downloaded_pool.cache_clear()


# --- band geometry + RD-aware width -------------------------------------------------------------

def test_band_bounds_clamps_to_range():
    assert puzzle_shards.band_bounds(1234) == (1200, 1300)
    assert puzzle_shards.band_bounds(100) == (600, 700)     # below the floor
    assert puzzle_shards.band_bounds(9000) == (2800, 2900)  # above the ceiling


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


# --- full-set warm-up ---------------------------------------------------------------------------

def _multi_manifest(sha: str) -> dict:
    """A 3-band manifest (all pointing at the same test shard bytes) for the warm-up path."""
    bands = [[1000, 1100], [1100, 1200], [1200, 1300]]
    return {
        "version": 1,
        "band_width": 100,
        "shards": [
            {"band": b, "file": f"band_{b[0]}_{b[1]}.jsonl.gz",
             "count": 1, "themes": {"fork": 1}, "bytes": len(BAND_BYTES), "sha256": sha}
            for b in bands
        ],
    }


def _run_threads_synchronously(monkeypatch):
    """Make threading.Thread run its target inline, so the warm-up is deterministic in a test."""
    monkeypatch.setattr(puzzle_shards.threading, "Thread",
                        lambda target, daemon=None: type("T", (), {"start": lambda self: target()})())


def test_ensure_all_bands_downloads_every_band(monkeypatch):
    monkeypatch.setattr(puzzle_shards.httpx, "get",
                        lambda *a, **k: _FakeResp(200, json_data=_multi_manifest(GOOD_SHA)))
    monkeypatch.setattr(puzzle_shards.httpx, "stream",
                        lambda *a, **k: _FakeResp(200, content=BAND_BYTES))
    _run_threads_synchronously(monkeypatch)
    puzzle_shards.ensure_all_bands()
    on_disk = {n for n in os.listdir(config._puzzle_dir()) if n.startswith("band_")}
    assert on_disk == {"band_1000_1100.jsonl.gz", "band_1100_1200.jsonl.gz", "band_1200_1300.jsonl.gz"}
    # Marked complete -> a second call is a no-op (guarded), even if the network would now fail.
    assert puzzle_shards._ALL_COMPLETE is True
    monkeypatch.setattr(puzzle_shards.httpx, "get", lambda *a, **k: _FakeResp(404))
    puzzle_shards.ensure_all_bands()  # no crash, no re-fetch


def test_ensure_all_bands_noop_when_downloads_disabled(monkeypatch):
    monkeypatch.setattr(config, "PUZZLE_DOWNLOAD", False)
    called = {"n": 0}
    monkeypatch.setattr(puzzle_shards.threading, "Thread",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or type(
                            "T", (), {"start": lambda self: None})())
    puzzle_shards.ensure_all_bands()
    assert called["n"] == 0  # never even spawns the warm-up thread


def test_ensure_all_bands_retries_after_a_failed_attempt(monkeypatch):
    """An offline first attempt doesn't mark complete, so a later (online) call still fills the set."""
    _run_threads_synchronously(monkeypatch)
    monkeypatch.setattr(puzzle_shards.httpx, "get", lambda *a, **k: _FakeResp(404))  # offline
    puzzle_shards.ensure_all_bands()
    assert puzzle_shards._ALL_COMPLETE is False
    d = config._puzzle_dir()
    assert not (os.path.isdir(d) and any(n.startswith("band_") for n in os.listdir(d)))
    # Now "online": the retry completes the set.
    monkeypatch.setattr(puzzle_shards.httpx, "get",
                        lambda *a, **k: _FakeResp(200, json_data=_multi_manifest(GOOD_SHA)))
    monkeypatch.setattr(puzzle_shards.httpx, "stream",
                        lambda *a, **k: _FakeResp(200, content=BAND_BYTES))
    puzzle_shards.ensure_all_bands()
    assert puzzle_shards._ALL_COMPLETE is True
