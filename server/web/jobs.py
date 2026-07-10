"""Background full-game analysis for the web board.

The board lets a user reopen a past game (local history) or a fetched Lichess game. Analysis is
the slow part (~20-45s engine sweep), so we run it in a daemon thread and expose a tiny status
machine the frontend polls — meanwhile the board navigates the moves from a client-side PGN replay
(see frontend `openGame`). When the job finishes it populates the SAME singleton `ReviewSession`
the MCP tools use, so `/api/session` and `/api/timeline` then serve the real analysis.

Single-user, single-engine-pool tool ⇒ at most one analysis at a time. A monotonically increasing
`token` gives superseding semantics: if a newer open arrives, the older thread's result is dropped
rather than clobbering the session. Mirrors `mcp_server.analyze_game` (set_session + record_game),
minus the browser-open (the board is already open).
"""
from __future__ import annotations

import threading
import time

from server import config
from server.core import analysis_cache
from server.core import history
from server.core import session as session_mod
from server.core.game_analysis import analyze_game as _analyze_game

_lock = threading.Lock()
_state: dict = {
    "status": "idle",
    "error": None,
    "game_id": None,
    "token": 0,
    # Progress of the in-flight sweep (one fixed-depth engine call per ply ⇒ roughly linear):
    "done": 0,  # positions evaluated so far
    "total": 0,  # positions to evaluate (plies + 1); 0 until the first report
    "eta_seconds": None,  # projected time remaining, once we have a stable per-ply rate
    # Batch (multi-game upload) progress; total_games == 1 for a normal single-game open.
    "total_games": 1,  # games in the current job
    "done_games": 0,  # games fully analysed + recorded so far
    "current_game": 1,  # 1-based index of the game being analysed now
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    """Current job status: one of idle | pending | ready | error (+ error/game_id)."""
    with _lock:
        return dict(_state)


def _run(pgn: str, player: str, token: int) -> None:
    started = time.monotonic()

    def _progress(done: int, total: int) -> None:
        # Project ETA from the average per-position time so far. We wait for a couple of positions
        # before trusting the rate (the engine pool warms up on the first call), leaving eta=None
        # until then so the frontend can show an indeterminate bar.
        elapsed = time.monotonic() - started
        eta = None
        if done >= 2 and total > done:
            eta = (elapsed / done) * (total - done)
        with _lock:
            if token == _state["token"]:  # ignore reports from a superseded job
                _state.update(done=done, total=total, eta_seconds=eta)

    try:
        sess = _analyze_game(pgn, player=player, on_progress=_progress)
    except Exception as exc:  # analysis failed — surface a friendly message, never crash the server
        with _lock:
            if token == _state["token"]:  # ignore if a newer job superseded us
                _state.update(status="error", error=str(exc))
        return
    with _lock:
        if token != _state["token"]:
            return  # superseded; drop this result
        session_mod.set_session(sess)
    analysis_cache.store(sess)  # so the next reopen is instant; best-effort, never raises
    # Persist for coaching — best-effort, exactly like the MCP path; never fail the job over it.
    if config.HISTORY_ENABLED:
        try:
            history.record_game(sess)
        except Exception:  # pragma: no cover - defensive
            pass
    with _lock:
        if token == _state["token"]:
            _state.update(status="ready", error=None)


def start(pgn: str, player: str = "auto") -> dict:
    """Kick off a background analysis, superseding any in-flight one. Returns the new status.

    If this exact game+side was analysed before (disk cache hit), the stored session is loaded
    synchronously and the job lands on ``ready`` immediately — no engine sweep, no polling wait.
    """
    cached = analysis_cache.load(pgn, player)
    with _lock:
        _state["token"] += 1
        token = _state["token"]
        _state.update(
            status="pending", error=None, game_id=None, done=0, total=0, eta_seconds=None,
            total_games=1, done_games=0, current_game=1,
        )
        if cached is not None:
            # Already analysed (and so already in history) — show it right away.
            session_mod.set_session(cached)
            _state.update(status="ready", done_games=1)
            return dict(_state)
    threading.Thread(
        target=_run, args=(pgn, player, token), name="chess-analyze", daemon=True
    ).start()
    return status()


def _run_batch(
    games: list[str], sides: list[str], self_handle: str | None,
    platform: str | None, token: int,
) -> None:
    """Analyse a list of games sequentially (one engine pool), recording each to history so the
    whole upload lands in "My games". The board is switched to the first game that analyses, so the
    user has something live to step through while the rest run."""
    if self_handle and config.HISTORY_ENABLED:
        try:
            history.ensure_self_alias(self_handle, platform)  # fold the uploader's handle into "me"
        except Exception:  # pragma: no cover - identity write must never break the batch
            pass

    first_set = False
    for i, (pgn, side) in enumerate(zip(games, sides)):
        with _lock:
            if token != _state["token"]:
                return  # superseded by a newer open
            _state.update(current_game=i + 1, done=0, total=0, eta_seconds=None)
        started = time.monotonic()

        def _progress(done: int, total: int, _started=started) -> None:
            elapsed = time.monotonic() - _started
            eta = (elapsed / done) * (total - done) if done >= 2 and total > done else None
            with _lock:
                if token == _state["token"]:
                    _state.update(done=done, total=total, eta_seconds=eta)

        sess = analysis_cache.load(pgn, side)  # re-uploaded games skip the sweep entirely
        was_cached = sess is not None
        if sess is None:
            try:
                sess = _analyze_game(pgn, player=side, on_progress=_progress)
            except Exception as exc:  # skip a bad game, keep going, remember the last error
                with _lock:
                    if token == _state["token"]:
                        _state.update(error=str(exc))
                continue
            analysis_cache.store(sess)  # cache the fresh sweep for next time
        with _lock:
            if token != _state["token"]:
                return
            if not first_set:
                session_mod.set_session(sess)  # show the first analysed game on the board
                first_set = True
        # A cache hit means the game is already in history (it was recorded on its first
        # analysis); re-recording it here would append a duplicate line to games.jsonl on every
        # re-sync/re-upload. Readers dedupe, so counts stay correct either way — this just keeps
        # the append-only log from bloating. Matches the single-game `start()` path, which also
        # skips recording on a cache hit.
        if config.HISTORY_ENABLED and not was_cached:
            try:
                history.record_game(sess)
            except Exception:  # pragma: no cover - history is best-effort
                pass
        with _lock:
            if token == _state["token"]:
                _state.update(done_games=i + 1)

    with _lock:
        if token == _state["token"]:
            _state.update(status="ready")


def start_batch(
    games: list[str], sides: list[str], *, self_handle: str | None = None,
    platform: str | None = None,
) -> dict:
    """Kick off a sequential background analysis of several games, superseding any in-flight job."""
    with _lock:
        _state["token"] += 1
        token = _state["token"]
        _state.update(
            status="pending", error=None, game_id=None, done=0, total=0, eta_seconds=None,
            total_games=len(games), done_games=0, current_game=1,
        )
    threading.Thread(
        target=_run_batch, args=(games, sides, self_handle, platform, token),
        name="chess-analyze-batch", daemon=True,
    ).start()
    return status()
