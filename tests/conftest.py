"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from server.core import engine


@pytest.fixture(scope="session", autouse=True)
def _shutdown_engine_pool():
    """Quit any Stockfish processes the shared pool started, so the run leaves none behind."""
    yield
    engine.shutdown()
