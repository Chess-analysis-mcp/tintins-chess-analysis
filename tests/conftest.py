"""Shared test fixtures.

The puzzle trainer fires a fire-and-forget background thread (`puzzle_shards.ensure_all_bands`,
reached from `puzzles.select_puzzle`) that downloads the full shard set from GitHub. In the suite
that thread would hit the real network and, worse, *leak past the test that spawned it* — a lingering
worker mutates shared module state and races the mocked download in `test_puzzle_shards` on the same
temp path, so a puzzle test that drives `next_puzzle` can flake an unrelated shard test (seen as an
intermittent CI-only failure). Disabling downloads by default makes every test that doesn't opt in
network-free and thread-free; `select_puzzle` still serves from the vendored baseline + cached bands,
and `ensure_band` early-returns when the flag is off, so any already-running worker goes quiet too.
`test_puzzle_shards` re-enables downloads in its own module fixture to exercise the real path."""
from __future__ import annotations

import pytest

from server import config


@pytest.fixture(autouse=True)
def _no_background_puzzle_download(monkeypatch):
    monkeypatch.setattr(config, "PUZZLE_DOWNLOAD", False)
