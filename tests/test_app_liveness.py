"""App-mode liveness: explicit close beacon (fast) + heartbeat backstop, no false-quit on blur."""
from __future__ import annotations

import pytest

from server import config
from server.core import app_liveness


@pytest.fixture(autouse=True)
def _reset():
    app_liveness._armed = False
    app_liveness._last_beat = 0.0
    app_liveness._closing_at = None
    saved = (app_liveness.BEAT_TIMEOUT, app_liveness.CLOSE_GRACE)
    yield
    app_liveness.BEAT_TIMEOUT, app_liveness.CLOSE_GRACE = saved
    app_liveness._armed = False
    app_liveness._closing_at = None


def test_not_armed_until_first_beat():
    assert app_liveness._expired() is False  # browser never connected -> never exits


def test_heartbeat_keeps_alive_within_backstop():
    app_liveness.BEAT_TIMEOUT = 1000
    app_liveness.beat()
    assert app_liveness._armed is True
    assert app_liveness._expired() is False


def test_heartbeat_backstop_expires():
    app_liveness.BEAT_TIMEOUT = 0  # any silence counts (backstop for a close that skipped pagehide)
    app_liveness.beat()
    assert app_liveness._expired() is True


def test_close_beacon_expires_after_grace():
    app_liveness.BEAT_TIMEOUT = 1000  # backstop won't fire; only the close path should
    app_liveness.CLOSE_GRACE = 0
    app_liveness.beat()
    app_liveness.closing()
    assert app_liveness._expired() is True


def test_refresh_cancels_pending_close():
    app_liveness.BEAT_TIMEOUT = 1000
    app_liveness.CLOSE_GRACE = 0
    app_liveness.beat()
    app_liveness.closing()      # pagehide on refresh
    app_liveness.beat()         # the reloaded page heartbeats -> cancels the close
    assert app_liveness._expired() is False


def test_start_is_noop_outside_app_mode(monkeypatch):
    monkeypatch.setattr(config, "APP_MODE", False)
    app_liveness._started = False
    app_liveness.start()
    assert app_liveness._started is False
