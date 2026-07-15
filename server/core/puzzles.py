"""Puzzle loading, selection, and move validation for the tactical trainer.

Engine-free and best-effort, like `openings.py`: the vendored baseline shard
(`server/data/puzzles/baseline.jsonl.gz`, gzip JSONL) is loaded once and filtered in memory
(a few thousand dicts is trivial; no SQLite). Per-user variety comes from a seeded shuffle of the
candidate pool plus a served-`seen_ids` exclusion, so same-rating users get different,
non-repeating streams from identical static files.

Each puzzle dict (one JSONL line):
    {id, fen, moves:[uci...], rating, rd, themes:[...], popularity, nbplays, game_url}
`moves[0]` is the auto-played setup move (it creates the puzzle); the solver finds `moves[1:]`,
with opponent replies forced at the even indices.
"""

from __future__ import annotations

import functools
import glob
import gzip
import json
import os
import random
from pathlib import Path
from typing import Iterable, Optional

import chess

from .. import config
from . import puzzle_shards

_BASELINE = Path(__file__).resolve().parent.parent / "data" / "puzzles" / "baseline.jsonl.gz"

# Selection: start with a tight band around the user's rating and widen until we have enough.
_BAND = 100
_MIN_POOL = 12
# "Train my weaknesses" only kicks in once there's enough signal to trust — otherwise the very first
# game or puzzle skews the whole stream (the user explicitly asked not to be biased that early).
_MIN_HISTORY_GAMES = 5  # analysed games before game-motif weaknesses drive selection
_MIN_PUZZLE_ATTEMPTS = 10  # total puzzle attempts before in-app theme stats drive selection

# Map a game-history motif (history.tag_motifs) onto a puzzle theme tag, so "train my weaknesses"
# can bias selection toward the tactics the player actually misses in real games. None = no clean
# puzzle-theme equivalent (e.g. a pawn grab isn't a curated tactic motif).
# Lichess tags every puzzle with THEME tags, but many are metadata, not trainable skills: the
# puzzle's length ("oneMove"), where it came from ("master"), the game phase, or the resulting eval
# ("crushing"). Surfacing "master 0%" or "oneMove 0%" in the "Work on" card is noise — only real
# tactical/mate motifs are actionable. Everything here is excluded from the weakness card + bias.
_NON_MOTIF_THEMES: frozenset[str] = frozenset({
    "oneMove", "short", "long", "veryLong",          # puzzle length
    "master", "masterVsMaster", "superGM",           # puzzle origin
    "opening", "middlegame", "endgame",              # game phase
    "crushing", "advantage", "equality", "mate",     # resulting eval / generic goal
})


def is_trainable_theme(theme: str) -> bool:
    """Is this a real tactical/mate motif a player can drill, vs. a Lichess metadata tag?"""
    return bool(theme) and theme not in _NON_MOTIF_THEMES


def weak_theme_stats(by_theme: dict, *, min_seen: int = 4, max_rate: float = 0.7,
                     limit: int = 3) -> list[dict]:
    """The "Work on" list: trainable themes with enough attempts and a poor solve rate, worst first.

    Returns `[{theme, seen, solved, rate}]` (rate 0..1). Meta tags are excluded so the card only
    ever suggests genuine motifs (fork, pin, backRankMate, ...), never "master"/"oneMove".
    """
    out = []
    for theme, stat in (by_theme or {}).items():
        if not is_trainable_theme(theme):
            continue
        seen = int((stat or {}).get("seen", 0) or 0)
        solved = int((stat or {}).get("solved", 0) or 0)
        if seen >= min_seen and (solved / seen) < max_rate:
            out.append({"theme": theme, "seen": seen, "solved": solved, "rate": solved / seen})
    out.sort(key=lambda x: x["rate"])  # worst solve rate first
    return out[:limit]


_MOTIF_TO_THEME: dict[str, Optional[str]] = {
    "missed_fork": "fork",
    "allowed_fork": "fork",
    "back_rank": "backRankMate",
    "hung_piece": "hangingPiece",
    "missed_capture": "hangingPiece",
    "missed_mate": "mateIn2",
    "allowed_mate": "mateIn2",
    "pawn_grab": None,
}


def _load_jsonl_gz(path: str) -> list[dict]:
    """Parse a gzip-JSONL puzzle shard. Missing/corrupt -> [] (degrade); bad lines skipped."""
    out: list[dict] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


@functools.lru_cache(maxsize=1)
def _baseline() -> list[dict]:
    """All vendored baseline puzzles. Built once, lazily. Missing/corrupt -> [] (degrade)."""
    if not _BASELINE.is_file():
        return []
    return _load_jsonl_gz(str(_BASELINE))


@functools.lru_cache(maxsize=1)
def _downloaded_pool() -> list[dict]:
    """All puzzles from downloaded dense band shards under <DATA_DIR>/puzzles (P3).

    Cached; `puzzle_shards.ensure_band` clears this cache (via `_invalidate_pool`) when a new shard
    lands, so the next selection sees it. Empty when nothing has been downloaded yet."""
    out: list[dict] = []
    try:
        pattern = os.path.join(config._puzzle_dir(), "band_*.jsonl.gz")
        for path in glob.glob(pattern):
            out.extend(_load_jsonl_gz(path))
    except OSError:
        return []
    return out


def _merged_pool() -> list[dict]:
    """Baseline + downloaded shards, de-duplicated by id (a downloaded copy wins). The baseline is
    always the guaranteed floor; downloaded bands just deepen it around the user's rating."""
    dl = _downloaded_pool()
    base = _baseline()
    if not dl:
        return base
    seen: set[str] = set()
    merged: list[dict] = []
    for p in (*dl, *base):
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        merged.append(p)
    return merged


def available_themes() -> list[str]:
    """Sorted distinct theme tags present in the loaded pool (for the frontend theme picker)."""
    seen: set[str] = set()
    for p in _merged_pool():
        for t in p.get("themes", []):
            seen.add(t)
    return sorted(seen)


def weakness_themes(state: dict) -> list[str]:
    """Puzzle themes the player is weak on, blending their game history and in-app puzzle stats.

    (a) Game history: recurring motifs from the coaching profile (history.get_profile recent
        top_motifs, count>=2) mapped through `_MOTIF_TO_THEME`.
    (b) Puzzle self-stats: themes with a poor solve rate in `state['by_theme']` (already puzzle
        theme tags, no mapping needed).
    Union, capped, best-effort — either half degrading to empty is fine. Returns [] when there's no
    signal (caller then falls back to normal rating-band selection)."""
    themes: list[str] = []

    # (a) game-history motifs -> themes. Only once we've analysed enough games that the recurring
    # motifs mean something (not just whatever the single most-recent game happened to contain).
    try:
        from . import history
        profile = history.get_profile() or {}
        recent = profile.get("recent") or {}
        if int(recent.get("games", 0) or 0) >= _MIN_HISTORY_GAMES:
            for entry in recent.get("top_motifs") or []:
                if entry.get("count", 0) < 2:
                    continue
                mapped = _MOTIF_TO_THEME.get(entry.get("motif", ""))
                if mapped and mapped not in themes:
                    themes.append(mapped)
    except Exception:  # noqa: BLE001 - weakness bias must never break selection
        pass

    # (b) in-app puzzle per-theme weakness (worst solve rate first, min sample size). Held back until
    # the player has attempted enough puzzles overall, so the first puzzle's theme can't dominate.
    try:
        by_theme = state.get("by_theme", {}) or {}
        total_attempts = sum(int((s or {}).get("seen", 0) or 0) for s in by_theme.values())
        if total_attempts >= _MIN_PUZZLE_ATTEMPTS:
            scored = []
            for theme, stat in by_theme.items():
                if not is_trainable_theme(theme):  # skip metadata tags (master/oneMove/phase/…)
                    continue
                seen = int(stat.get("seen", 0) or 0)
                solved = int(stat.get("solved", 0) or 0)
                if seen >= 4:
                    scored.append((solved / seen, theme))
            scored.sort()  # worst rate first
            for rate, theme in scored:
                if rate < 0.6 and theme not in themes:
                    themes.append(theme)
    except Exception:  # noqa: BLE001
        pass

    return themes[:4]


def _candidates(rating: float, themes: Optional[Iterable[str]]) -> list[dict]:
    """Puzzles within a rating band around `rating`, widened until the pool is usable."""
    pool = _merged_pool()
    if not pool:
        return []
    want = set(themes) if themes else None
    if want:
        pool = [p for p in pool if want & set(p.get("themes", []))]
    if not pool:
        return []
    # Only serve well-established puzzles that will actually move the Glicko rating. The ~5% with a
    # high RatingDeviation play "unrated", which confuses users ("why didn't my rating change?"), so
    # drop them from selection — falling back to the unfiltered pool only if that empties it.
    rated_pool = [p for p in pool if float(p.get("rd", 999)) < config.PUZZLE_MAX_RD]
    pool = rated_pool or pool
    band = _BAND
    while band <= 1200:
        near = [p for p in pool if abs(float(p.get("rating", 1500)) - rating) <= band]
        if len(near) >= _MIN_POOL:
            return near
        band += _BAND
    return pool  # whole (theme-filtered) pool as a last resort


def next_puzzle(
    rating: float,
    *,
    themes: Optional[Iterable[str]] = None,
    exclude: Optional[set[str]] = None,
    seed: Optional[int] = None,
    difficulty: Optional[str] = None,
    rd: Optional[float] = None,
) -> Optional[dict]:
    """Pick a puzzle near `rating`, theme-filtered + not in `exclude`, via a per-user seeded shuffle.

    `difficulty` of "easier"/"harder" shifts the target rating by one band. `rd` is accepted for
    signature stability but no longer affects selection (the full shard set is downloaded in the
    background regardless). Returns the parsed puzzle (with a derived `side_to_move`) or None when
    nothing is available.
    """
    target = float(rating)
    if difficulty == "easier":
        target -= _BAND
    elif difficulty == "harder":
        target += _BAND

    # Fire-and-forget: pull the whole shard set (~16 MB) in the background on first use so the pool
    # deepens over time. This call serves from whatever is cached now. Best-effort + non-blocking.
    puzzle_shards.ensure_all_bands()

    candidates = _candidates(target, themes)
    exclude = exclude or set()
    fresh = [p for p in candidates if p.get("id") not in exclude]
    pool = fresh or candidates  # everything seen? fall back to the full band rather than nothing
    if not pool:
        return None

    rng = random.Random(seed if seed is not None else 0)
    order = pool[:]
    rng.shuffle(order)
    chosen = order[0]
    return _with_side(chosen)


def get_puzzle(puzzle_id: str) -> Optional[dict]:
    """Look up a loaded puzzle by id (for /move and /explain after selection)."""
    for p in _merged_pool():
        if p.get("id") == puzzle_id:
            return _with_side(p)
    return None


def _with_side(puzzle: dict) -> dict:
    """Attach `side_to_move` = the colour the solver plays (the position AFTER the setup move)."""
    out = dict(puzzle)
    try:
        board = chess.Board(puzzle["fen"])
        moves = puzzle.get("moves", [])
        if moves:
            board.push_uci(moves[0])
        out["side_to_move"] = "white" if board.turn == chess.WHITE else "black"
        out["solve_fen"] = board.fen()  # the position the solver actually sees
    except (ValueError, KeyError):
        out["side_to_move"] = "white"
        out["solve_fen"] = puzzle.get("fen", "")
    return out


def validate_step(puzzle: dict, ply_index: int, uci: str) -> dict:
    """Is `uci` the expected solution move at `ply_index` (into `puzzle['moves']`)?

    Replays the forced line up to `ply_index` from the puzzle FEN, so it's stateless. At the
    final solver move, any legal move that delivers checkmate is accepted (mate-leaf tolerance).
    Returns `{correct, is_complete, expected_uci, opponent_reply_uci}`.
    """
    moves = puzzle.get("moves", [])
    if ply_index < 0 or ply_index >= len(moves):
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    board = chess.Board(puzzle["fen"])
    try:
        for m in moves[:ply_index]:
            board.push_uci(m)
    except ValueError:
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    expected = moves[ply_index]
    is_last = ply_index == len(moves) - 1
    correct = uci == expected

    if not correct and is_last:
        # Branch tolerance: at a mate leaf any legal mating move counts.
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                test = board.copy(stack=False)
                test.push(mv)
                if test.is_checkmate():
                    correct = True
        except ValueError:
            pass

    if not correct:
        return {"correct": False, "is_complete": False, "expected_uci": expected, "opponent_reply_uci": None}

    next_index = ply_index + 1
    if next_index >= len(moves):
        return {"correct": True, "is_complete": True, "expected_uci": expected, "opponent_reply_uci": None}
    return {
        "correct": True,
        "is_complete": False,
        "expected_uci": expected,
        "opponent_reply_uci": moves[next_index],
    }


def position_fen(puzzle: dict, ply_index: int) -> str:
    """The FEN the solver moves from at `ply_index` (after replaying the forced prefix).

    Used to engine-ground the moves the user actually tried, so the coach can say concretely why
    each one works or fails. Best-effort -> the puzzle FEN on any error.
    """
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", [])[:ply_index]:
            board.push_uci(m)
        return board.fen()
    except (ValueError, KeyError):
        return puzzle.get("fen", "")


def solution_san(puzzle: dict) -> list[str]:
    """The full solution line (including the setup move) in SAN, for the coach facts."""
    out: list[str] = []
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", []):
            mv = chess.Move.from_uci(m)
            out.append(board.san(mv))
            board.push(mv)
    except (ValueError, KeyError):
        return out
    return out
