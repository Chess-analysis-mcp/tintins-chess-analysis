"""Stockfish engine pool with reproducible, cached, fixed-depth analysis.

Design notes:
- Engine processes are *reused*, never spawned per call (a bounded pool guarded by a
  lock). Spawning Stockfish per request is the main performance trap.
- Analysis is at fixed depth so results are reproducible, and cached by
  (fen, depth, multipv) so repeat calls are free and deterministic.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from queue import Queue

import chess
import chess.engine

from server import config
from server.core.evaluation import win_percent_from_score


@dataclass
class EngineLine:
    """One principal variation from the engine, side-to-move relative."""

    cp: int | None  # centipawns (None if mate)
    mate: int | None  # mate-in-N (None if cp)
    pv_uci: list[str] = field(default_factory=list)

    @property
    def win_percent(self) -> float:
        return win_percent_from_score(self.cp, self.mate)


@dataclass
class AnalysisResult:
    """Result of analysing one FEN. lines[0] is the best line."""

    fen: str
    depth: int
    lines: list[EngineLine]

    @property
    def best(self) -> EngineLine:
        return self.lines[0]


class _EnginePool:
    """A tiny bounded pool of reusable SimpleEngine processes."""

    def __init__(self) -> None:
        self._pool: Queue[chess.engine.SimpleEngine] = Queue()
        self._lock = threading.Lock()
        self._started = False
        self._cache: dict[tuple[str, int, int], AnalysisResult] = {}

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            for _ in range(max(1, config.ENGINE_POOL_SIZE)):
                try:
                    eng = chess.engine.SimpleEngine.popen_uci(config.STOCKFISH_PATH)
                except FileNotFoundError as exc:
                    raise RuntimeError(config.stockfish_install_hint()) from exc
                eng.configure(
                    {"Threads": config.ENGINE_THREADS, "Hash": config.ENGINE_HASH_MB}
                )
                self._pool.put(eng)
            self._started = True

    def analyse(
        self,
        fen: str,
        *,
        depth: int = config.DEFAULT_DEPTH,
        multipv: int = 1,
    ) -> AnalysisResult:
        multipv = max(1, multipv)
        # Normalise FEN for stable cache keys (en passant / move counters matter to
        # the engine, so keep the full FEN as-is).
        key = (fen, depth, multipv)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        self._ensure_started()
        eng = self._pool.get()
        try:
            board = chess.Board(fen)
            infos = eng.analyse(
                board, chess.engine.Limit(depth=depth), multipv=multipv
            )
            if isinstance(infos, dict):  # multipv=1 may return a single dict
                infos = [infos]
            lines: list[EngineLine] = []
            for info in infos:
                score = info["score"].pov(board.turn)  # side-to-move relative
                lines.append(
                    EngineLine(
                        cp=score.score(),  # None if mate
                        mate=score.mate(),  # None if cp
                        pv_uci=[m.uci() for m in info.get("pv", [])],
                    )
                )
            result = AnalysisResult(fen=fen, depth=depth, lines=lines)
        finally:
            self._pool.put(eng)

        self._cache[key] = result
        return result

    def shutdown(self) -> None:
        with self._lock:
            while not self._pool.empty():
                eng = self._pool.get()
                try:
                    eng.quit()
                except chess.engine.EngineError:
                    pass
            self._started = False


# Process-wide singleton pool.
_POOL = _EnginePool()


def analyse(fen: str, *, depth: int = config.DEFAULT_DEPTH, multipv: int = 1) -> AnalysisResult:
    """Analyse a FEN at fixed depth. Cached and reproducible."""
    return _POOL.analyse(fen, depth=depth, multipv=multipv)


def shutdown() -> None:
    """Quit all engine processes. Call on server shutdown."""
    _POOL.shutdown()


def restart() -> None:
    """Quit engines and drop cached evals so the next analyse() respawns with the current
    config.STOCKFISH_PATH. Used when the engine path is changed at runtime via Settings."""
    with _POOL._lock:
        _POOL._cache.clear()
    _POOL.shutdown()
