"""Mistake puzzles from the user's own games (P3.5) — the "From your games" source.

Resurface positions where the player themselves went wrong in a past analysed game as engine-
validated practice. Unlike a curated tactic there's no single forced line: acceptance is eval-based
(any move whose win% drop is under the game's inaccuracy threshold passes), so a positional spot has
several right answers while a real missed tactic has one. These are **always unrated** (they never
touch the Glicko rating) and spaced-repetition gated.

Engine-free here: candidates come straight from the stored history records (each flagged mistake
already carries `fen_before`, the played + best move, the win% drop, and motif tags). Only the move
the user *submits* is evaluated live, by `routes_puzzles` via `lines.engine_line`. Best-effort: any
failure just yields no puzzle, never an exception into the board.
"""
from __future__ import annotations

import io
import time
from typing import Optional

import chess
import chess.pgn

from . import history
from . import puzzle_rating

# A position retires from the queue after this many clean solves; a failure resets it (resurface).
_RETIRE_SUCCESSES = 2
# How many recently-served mistake keys to remember and skip, so pressing "Skip" (or interleave)
# gives a genuinely different position instead of re-serving the single top-ranked one.
_RECENT_MAX = 15
# Default inaccuracy cutoff (win%-drop) when a record predates skill-scaled thresholds.
_DEFAULT_ACCEPT_SWING = 5.0
# Skill -> target-swing percentile: clamp rating into this window, then map linearly onto the
# percentile band below. Beginners drill big blunders (high percentile of their own swings);
# stronger players get the subtle, small-swing misses.
#
# Calibration (data-backed): the clamp window is ~the 2nd-98th percentile of the Lichess Rapid
# rating distribution (which fits N(1500, sigma~=335): p2~=812, p98~=2188), so it spans the real
# active population without wasting range on empty tails. The percentile band was raised from the
# original 0.90/0.25 to 0.95/0.35 after checking it against real user mistake data: 0.90/0.25 left
# a mid-rated (~1400) player targeting only ~p62 of their own swings (just above their median),
# which is too subtle - a player at that level still benefits from drilling their meatier blunders.
# 0.95/0.35 re-centres a ~1400 onto ~p70. (This stays a percentile-linear map; it deliberately does
# not try to fix high-end step-compression, which would need a swing-value-space interpolation.)
_SKILL_LO, _SKILL_HI = 800.0, 2200.0
_PCT_HI, _PCT_LO = 0.95, 0.35  # low skill -> 0.95 (biggest swings); high skill -> 0.35


def practice_key(game_id: str, side: str, ply: int) -> str:
    return f"{game_id}:{side}:{ply}"


def _target_percentile(skill: float) -> float:
    t = (max(_SKILL_LO, min(_SKILL_HI, float(skill))) - _SKILL_LO) / (_SKILL_HI - _SKILL_LO)
    return _PCT_HI - (_PCT_HI - _PCT_LO) * t


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list. Empty -> 0.0."""
    if not sorted_vals:
        return 0.0
    idx = int(round(pct * (len(sorted_vals) - 1)))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def _candidate_mistakes(data_dir: Optional[str] = None) -> list[dict]:
    """Every flagged mistake across the user's analysed games, as puzzle candidates.

    Pulled from `history.load_records` (records embed their `mistakes` list + skill-scaled
    `thresholds`), filtered to those with the data a puzzle needs (a position + a played move).
    """
    try:
        records = history.load_records(player_id=history.my_player_id(data_dir), data_dir=data_dir)
    except Exception:  # noqa: BLE001 - a bad history file must not break puzzle mode
        return []

    out: list[dict] = []
    for rec in records:
        game_id = rec.get("game_id")
        side = rec.get("reviewed_side")
        if not game_id or not side:
            continue
        thresholds = rec.get("thresholds") or []
        accept_swing = float(thresholds[0]) if thresholds else _DEFAULT_ACCEPT_SWING
        for m in rec.get("mistakes", []) or []:
            fen = m.get("fen_before")
            if not fen or not m.get("uci"):
                continue
            ply = m.get("ply")
            out.append({
                "key": practice_key(game_id, side, ply),
                "game_id": game_id,
                "reviewed_side": side,
                "ply": ply,
                "fen": fen,
                "side_to_move": m.get("color") or side,
                "win_drop": float(m.get("win_drop", 0.0) or 0.0),
                "accept_swing": accept_swing,
                "played_uci": m.get("uci"),
                "played_san": m.get("san"),
                # Raw PGN of the game — so the chosen puzzle can reconstruct the opponent's
                # preceding move (see `_prev_move`) without the engine. Dropped before serving.
                "pgn": rec.get("pgn"),
                "best_uci": m.get("best_uci"),
                "best_san": m.get("best_san"),
                "classification": m.get("classification"),
                "motifs": m.get("motifs", []) or [],
                # Badge metadata ("From your game · vs X · blitz · Mar 3").
                "white": rec.get("white"),
                "black": rec.get("black"),
                "speed": rec.get("speed") or "unknown",
                "date": rec.get("date"),
                "game_url": rec.get("game_url"),
            })
    return out


def _prev_move(pgn: Optional[str], ply: Optional[int]) -> Optional[dict]:
    """The opponent's move that led *into* the mistake position, reconstructed engine-free from the
    stored PGN so the puzzle can play it in as a setup animation (context: "they just did X").

    `ply` is 1-indexed at the player's mistake move; the opponent's preceding move is one ply
    earlier. Returns `{prev_fen, setup_uci, setup_san}` (the board before that move + the move
    itself), or None when there's no prior move (a mistake on ply 1) or the PGN can't be replayed.
    """
    if not pgn or not ply or ply < 2:
        return None
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
        if game is None:
            return None
        board = game.board()
        moves = list(game.mainline_moves())
        idx = ply - 2  # 0-indexed opponent move preceding the player's mistake (moves[ply-1])
        if idx < 0 or idx >= len(moves):
            return None
        for mv in moves[:idx]:
            board.push(mv)
        opp = moves[idx]
        return {"prev_fen": board.fen(), "setup_uci": opp.uci(), "setup_san": board.san(opp)}
    except Exception:  # noqa: BLE001 - a bad/foreign PGN just means no setup move, never a crash
        return None


def _is_retired(state: dict, key: str) -> bool:
    entry = (state.get("practiced") or {}).get(key)
    return bool(entry and entry.get("successes", 0) >= _RETIRE_SUCCESSES)


def _dedup(candidates: list[dict]) -> list[dict]:
    """Collapse near-identical mistakes so the same tactic served over consecutive plies (the engine
    flags every ply the same shot was available) doesn't resurface as "essentially the same puzzle".

    Group by (game_id, best move), keeping the single most instructive position (biggest win% drop).
    """
    best_by_key: dict[tuple, dict] = {}
    for c in candidates:
        k = (c.get("game_id"), c.get("best_uci") or c.get("fen"))
        cur = best_by_key.get(k)
        if cur is None or c["win_drop"] > cur["win_drop"]:
            best_by_key[k] = c
    return list(best_by_key.values())


def mark_mistake_served(state: dict, key: str) -> None:
    """Record a mistake puzzle as just served (caller persists), so the next pick skips it."""
    recent = state.setdefault("mistake_recent", [])
    if key in recent:
        recent.remove(key)
    recent.append(key)
    del recent[:-_RECENT_MAX]


def next_mistake_puzzle(state: dict, data_dir: Optional[str] = None) -> Optional[dict]:
    """Pick the most instructive un-retired own-game mistake for the player's skill.

    Skill proxy = the puzzle Glicko rating (always present). Order by closeness to a skill-relative
    percentile of the *player's own* win%-swing distribution (beginners -> big blunders, strong
    players -> subtle misses), breaking ties toward recurring motifs. Near-duplicate positions are
    collapsed and recently-served ones skipped, so consecutive picks stay varied. Returns a puzzle
    dict with `source="your_games"` (no forced line — the solve position IS `fen`), or None when
    there's nothing eligible.
    """
    candidates = _dedup([c for c in _candidate_mistakes(data_dir) if not _is_retired(state, c["key"])])
    if not candidates:
        return None
    # Skip positions served in the last few picks; if that leaves nothing, allow them again.
    recent = set(state.get("mistake_recent", []))
    candidates = [c for c in candidates if c["key"] not in recent] or candidates

    skill = float(state.get("rating", 1500.0) or 1500.0)
    swings = sorted(c["win_drop"] for c in candidates)
    target = _percentile(swings, _target_percentile(skill))

    def sort_key(c: dict) -> tuple:
        recurring = _recurring(c["motifs"], data_dir)
        return (round(abs(c["win_drop"] - target), 1), 0 if recurring else 1, -c["win_drop"])

    candidates.sort(key=sort_key)
    chosen = dict(candidates[0])
    chosen["source"] = "your_games"
    chosen["id"] = chosen["key"]  # the stable key doubles as the puzzle id for /move + /explain
    # Attach the opponent's preceding move so the board can play it in before the solver answers.
    prev = _prev_move(chosen.get("pgn"), chosen.get("ply"))
    if prev:
        chosen.update(prev)
    chosen.pop("pgn", None)  # don't carry the whole PGN into the session/response
    return chosen


def _recurring(motifs: list[str], data_dir: Optional[str]) -> bool:
    """Does this mistake carry a motif the player repeats (per the coaching profile)?"""
    try:
        return any(history._is_recurring(mo, data_dir) for mo in (motifs or []))
    except Exception:  # noqa: BLE001
        return False


def record_practice_result(state: dict, key: str, solved: bool) -> None:
    """Update spaced-repetition state after an attempt (caller persists). A clean solve advances
    toward retirement; a failure resurfaces the position (successes reset to 0)."""
    practiced = state.setdefault("practiced", {})
    entry = practiced.setdefault(key, {"successes": 0})
    entry["successes"] = entry.get("successes", 0) + 1 if solved else 0
    entry["last"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Mistake puzzles are unrated, but a completed one still counts as a day of practice.
    puzzle_rating.touch_daily_streak(state)
