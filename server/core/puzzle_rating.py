"""Glicko-2 rating for the puzzle trainer + per-user state persistence.

Pure math (no engine, no network), so it's fully unit-testable against the published Glicko-2
worked example (Glickman, "Example of the Glicko-2 system"). Each puzzle is treated as one
opponent at its stored `Rating`/`RatingDeviation`, and we update after every solved/failed puzzle
(a rating period of one game), exactly as Lichess effectively does.

State lives in `<DATA_DIR>/puzzles/state.json`, written with the same atomic `.tmp` -> os.replace
+ version-header + best-effort idiom as `analysis_cache.py`, so a corrupt/old file just resets to
defaults rather than breaking the board.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Optional

from .. import config

STATE_VERSION = 1

# Glicko-2 constants / new-player seed (the published defaults).
_SCALE = 173.7178
_DEFAULT_RATING = 1500.0
_DEFAULT_RD = 350.0
_DEFAULT_VOL = 0.06
_TAU = 0.5
_EPSILON = 1e-6


# --- Glicko-2 math ------------------------------------------------------------------------------

def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def glicko2_update(
    rating: float,
    rd: float,
    vol: float,
    results: list[tuple[float, float, float]],
    tau: float = _TAU,
) -> tuple[float, float, float]:
    """Return the updated (rating, rd, vol) after a rating period.

    `results` is a list of (opponent_rating, opponent_rd, score) where score is 1 (win/solved),
    0 (loss/failed) or 0.5 (draw). An empty period only inflates RD toward the unrated default
    (the "did not play" step), leaving rating + volatility unchanged.
    """
    # Step 2: to the Glicko-2 scale.
    mu = (rating - _DEFAULT_RATING) / _SCALE
    phi = rd / _SCALE

    if not results:
        # Step 6 only: no games this period -> RD grows by the volatility.
        phi_star = math.sqrt(phi * phi + vol * vol)
        return rating, min(phi_star * _SCALE, _DEFAULT_RD), vol

    gs: list[float] = []
    es: list[float] = []
    scores: list[float] = []
    for opp_rating, opp_rd, score in results:
        mu_j = (opp_rating - _DEFAULT_RATING) / _SCALE
        phi_j = opp_rd / _SCALE
        gs.append(_g(phi_j))
        es.append(_expected(mu, mu_j, phi_j))
        scores.append(score)

    # Step 3: estimated variance of the rating based only on game outcomes.
    v_inv = sum(g * g * e * (1.0 - e) for g, e in zip(gs, es))
    v = 1.0 / v_inv

    # Step 4: estimated improvement in rating.
    delta_sum = sum(g * (s - e) for g, e, s in zip(gs, es, scores))
    delta = v * delta_sum

    # Step 5: new volatility via the Illinois algorithm.
    a = math.log(vol * vol)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau

    fA = f(A)
    fB = f(B)
    while abs(B - A) > _EPSILON:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA = fA / 2.0
        B, fB = C, fC

    new_vol = math.exp(A / 2.0)

    # Step 6: pre-rating-period RD.
    phi_star = math.sqrt(phi * phi + new_vol * new_vol)

    # Step 7: new RD and rating.
    new_phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    new_mu = mu + new_phi * new_phi * delta_sum

    new_rating = new_mu * _SCALE + _DEFAULT_RATING
    new_rd = new_phi * _SCALE
    return new_rating, new_rd, new_vol


# --- State persistence --------------------------------------------------------------------------

def _state_path(data_dir: Optional[str] = None) -> str:
    base = os.path.join(data_dir, "puzzles") if data_dir else config._puzzle_dir()
    return os.path.join(base, "state.json")


def _default_state() -> dict:
    return {
        "version": STATE_VERSION,
        "rating": _DEFAULT_RATING,
        "rd": _DEFAULT_RD,
        "vol": _DEFAULT_VOL,
        "user_seed": random.randint(1, 2_000_000_000),
        "seen_ids": [],
        "solved_ids": [],
        "streak": 0,
        "best_streak": 0,
        "history": [],
        "by_theme": {},
    }


def load_state(data_dir: Optional[str] = None) -> dict:
    """Load the puzzle state, generating + persisting a fresh one (with a user_seed) on first run.

    Best-effort: a missing/corrupt/old-version file yields a fresh default state.
    """
    path = _state_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") != STATE_VERSION:
            raise ValueError("stale state version")
        # Fill any keys a partial/older file is missing.
        base = _default_state()
        base.update(data)
        if not base.get("user_seed"):
            base["user_seed"] = random.randint(1, 2_000_000_000)
        return base
    except (OSError, ValueError, json.JSONDecodeError):
        state = _default_state()
        save_state(state, data_dir)
        return state


def save_state(state: dict, data_dir: Optional[str] = None) -> None:
    """Atomically persist the state. Best-effort: never raises (puzzles must not break the board)."""
    path = _state_path(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - persistence must never break a solve
        pass


def mark_seen(state: dict, puzzle_id: str) -> None:
    """Record a puzzle as served (so a failed one isn't re-served either). Caller persists."""
    if puzzle_id and puzzle_id not in state["seen_ids"]:
        state["seen_ids"].append(puzzle_id)


def record_result(
    state: dict,
    *,
    puzzle_id: str,
    puzzle_rating: float,
    puzzle_rd: float,
    themes: list[str],
    score: float,
    rated: bool,
) -> dict:
    """Apply one puzzle outcome to the state in place and return a result summary.

    Streak + per-theme stats always update; the Glicko rating only moves when `rated` (the
    RD-gate + first-try rule is decided by the caller). Returns
    `{rating_before, rating_after, delta, rated, streak}`.
    """
    rating_before = state["rating"]
    solved = score >= 1.0

    if rated:
        new_rating, new_rd, new_vol = glicko2_update(
            state["rating"], state["rd"], state["vol"],
            [(puzzle_rating, puzzle_rd, score)],
        )
        state["rating"], state["rd"], state["vol"] = new_rating, new_rd, new_vol

    # Streak: a solve extends it, a fail resets it.
    if solved:
        state["streak"] = state.get("streak", 0) + 1
        state["best_streak"] = max(state.get("best_streak", 0), state["streak"])
        if puzzle_id and puzzle_id not in state["solved_ids"]:
            state["solved_ids"].append(puzzle_id)
    else:
        state["streak"] = 0

    # Per-theme tally (a puzzle-specific weakness view, kept separate from the game profile).
    for theme in themes or []:
        bucket = state["by_theme"].setdefault(theme, {"seen": 0, "solved": 0})
        bucket["seen"] += 1
        if solved:
            bucket["solved"] += 1

    rating_after = state["rating"]
    state["history"].append({
        "id": puzzle_id,
        "puzzle_rating": puzzle_rating,
        "result": 1 if solved else 0,
        "rated": rated,
        "rating_after": round(rating_after, 1),
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    return {
        "rating_before": round(rating_before, 1),
        "rating_after": round(rating_after, 1),
        "delta": round(rating_after - rating_before, 1),
        "rated": rated,
        "streak": state["streak"],
    }
