"""Evaluation math: centipawns -> win%, move classification, per-move accuracy.

Formulas follow Lichess's published approach (spec §6). Everything here is pure and
deterministic so it is trivially testable and reproducible.
"""
from __future__ import annotations

import math
from typing import Literal

Classification = Literal["best", "good", "inaccuracy", "mistake", "blunder"]

# Lichess sigmoid constant for cp -> win%.
_WIN_K = 0.00368208

# Clamp cp magnitude before the sigmoid; beyond this the win% is already ~saturated.
_CP_CLAMP = 1000

# Classification thresholds on the win% drop (win_before - win_after), in win% points
# (0-100 scale). These mirror Lichess, which thresholds its winningChances scale [-1,1]
# at 0.1/0.2/0.3; multiplied by 50 that is 5/10/15 win% points. Verified to reproduce
# Lichess's own labels on example_pgns/game1.pgn.
BLUNDER_DROP = 15.0
MISTAKE_DROP = 10.0
INACCURACY_DROP = 5.0

# A move within this win% of the engine's best is considered "best".
BEST_EPS = 2.0


def win_percent(cp: int | float) -> float:
    """Convert a centipawn score (side-to-move relative) to a win% in [0, 100].

    cp == 0 -> 50. Positive favours the side to move. Mate scores should be passed
    in as large +/- centipawn values (see win_percent_from_score)."""
    c = max(-_CP_CLAMP, min(_CP_CLAMP, cp))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-_WIN_K * c)) - 1.0)


def win_percent_from_score(cp: int | None, mate: int | None) -> float:
    """Win% from either a centipawn value or a mate-in-N.

    Exactly one of cp / mate is expected to be meaningful (python-chess gives mate
    when a forced mate is found). Mate for the side to move -> ~100, mate against -> ~0.
    """
    if mate is not None:
        return 100.0 if mate > 0 else 0.0
    if cp is None:
        return 50.0
    return win_percent(cp)


DEFAULT_THRESHOLDS: tuple[float, float, float] = (INACCURACY_DROP, MISTAKE_DROP, BLUNDER_DROP)


def classify(
    win_before: float,
    win_after: float,
    *,
    is_best: bool = False,
    thresholds: tuple[float, float, float] | None = None,
) -> Classification:
    """Classify a move by the drop in the mover's win% (win_before - win_after).

    win_before = best win% available before the move (from the mover's perspective).
    win_after  = win% after the move actually played (from the mover's perspective).
    Set is_best=True when the move played equals the engine's top choice.
    `thresholds` = (inaccuracy, mistake, blunder) win%-drop cutoffs; defaults to 5/10/15.
    """
    inacc, mist, blund = thresholds or DEFAULT_THRESHOLDS
    drop = win_before - win_after
    if drop >= blund:
        return "blunder"
    if drop >= mist:
        return "mistake"
    if drop >= inacc:
        return "inaccuracy"
    if is_best or drop <= BEST_EPS:
        return "best"
    return "good"


def thresholds_for_elo(elo: float | None) -> tuple[float, float, float]:
    """Scale the (inaccuracy, mistake, blunder) cutoffs to a player's skill.

    Stronger players make subtler errors, so their cutoffs shrink (smaller win% drops get
    flagged). `elo` is on a normalized scale (~chess.com / FIDE); pass None for the default
    5/10/15. Anchored so ~1500 -> ×1.0, with a clamped linear factor either side.
    """
    if elo is None:
        return DEFAULT_THRESHOLDS
    factor = max(0.5, min(1.4, 1.75 - 0.0005 * elo))
    return tuple(round(t * factor, 1) for t in DEFAULT_THRESHOLDS)  # type: ignore[return-value]


# Per-mode multiplier applied on top of the Elo-scaled cutoffs. Blitz is the anchor (1.0):
# the default 5/10/15 cutoffs were tuned against blitz play and felt right there. Slower modes
# get LOWER thresholds (more sensitive — with time on the clock, smaller errors are real and
# worth flagging); faster modes get HIGHER thresholds (more forgiving). "unknown" leaves the
# cutoffs unchanged so games without a known time control behave exactly as before.
SPEED_THRESHOLD_FACTORS: dict[str, float] = {
    "bullet": 1.15,
    "blitz": 1.0,
    "rapid": 0.9,
    "classical": 0.8,
    "correspondence": 0.75,
    "unknown": 1.0,
}


def thresholds_for_speed(
    thresholds: tuple[float, float, float], speed: str | None
) -> tuple[float, float, float]:
    """Scale already-computed (inaccuracy, mistake, blunder) cutoffs by the game's mode.

    Multiplies on top of `thresholds_for_elo`, so the final cutoffs reflect both skill and mode.
    See SPEED_THRESHOLD_FACTORS for the gradient (blitz = unchanged anchor).
    """
    factor = SPEED_THRESHOLD_FACTORS.get(speed or "unknown", 1.0)
    if factor == 1.0:
        return thresholds
    return tuple(round(t * factor, 1) for t in thresholds)  # type: ignore[return-value]


def move_accuracy(win_before: float, win_after: float) -> float:
    """Per-move accuracy% in [0, 100] from the win% drop (Lichess-style)."""
    drop = max(0.0, win_before - win_after)
    acc = 103.1668 * math.exp(-0.04354 * drop) - 3.1669
    return max(0.0, min(100.0, acc))


def aggregate_accuracy(accuracies: list[float]) -> float:
    """Aggregate per-move accuracies into a single per-side accuracy%.

    Simple arithmetic mean to start (the plan notes this can be upgraded to
    Lichess's volatility-weighted mean later). Empty -> 100 (no moves to fault).
    """
    if not accuracies:
        return 100.0
    return sum(accuracies) / len(accuracies)


# --------------------------------------------------------------------------------------
# Game speed (time-format) classification
# --------------------------------------------------------------------------------------
# Bucket a game into Lichess-style speed categories from its estimated duration,
# base + 40*increment seconds (40 = Lichess's assumed game length). Tagging each game's
# mode lets coaching apply mode-appropriate expectations — a blunder in bullet is far more
# forgivable than in a classical game — and surface how a player's patterns differ by mode.
Speed = Literal["bullet", "blitz", "rapid", "classical", "correspondence", "unknown"]

_SPEED_KEYWORDS = ("bullet", "blitz", "rapid", "classical", "correspondence")


def time_control_clock(time_control: str | None) -> tuple[float, float] | None:
    """(base_seconds, increment_seconds) from a PGN TimeControl, or None when it isn't a
    sudden-death clock ("-", "?", empty, or a correspondence "days" spec like "1/259200")."""
    tc = (time_control or "").strip()
    if not tc or tc in ("-", "?"):
        return None
    head, _, inc = tc.partition("+")
    if "/" in head:  # "1/259200" = correspondence (days), not a sudden-death clock
        return None
    try:
        base = float(head)
        increment = float(inc) if inc else 0.0
    except ValueError:
        return None
    return (base, increment) if base > 0 else None


def classify_speed(time_control: str | None, event: str | None = None) -> Speed:
    """Bucket a game into bullet/blitz/rapid/classical/correspondence from its TimeControl.

    Uses the estimated duration base + 40*increment (matching Lichess's buckets). Falls back to
    a keyword in the Event header ("Rated Blitz game") when the TimeControl is absent/unclear, and
    returns "unknown" when neither is informative.
    """
    tc = (time_control or "").strip()
    if tc and "/" in tc.split("+", 1)[0]:
        return "correspondence"
    clock = time_control_clock(tc)
    if clock is not None:
        base, increment = clock
        estimated = base + 40.0 * increment
        if estimated < 180:
            return "bullet"
        if estimated < 480:
            return "blitz"
        if estimated < 1500:
            return "rapid"
        return "classical"
    if tc == "-":  # lichess uses "-" for correspondence / unlimited
        return "correspondence"
    blob = (event or "").lower()
    for kw in _SPEED_KEYWORDS:
        if kw in blob:
            return kw  # type: ignore[return-value]
    return "unknown"
