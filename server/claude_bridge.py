"""Bridge to headless Claude Code for the in-browser chat (Phase 6).

Shells out to `claude -p` so the browser's "why?" questions are answered on the user's
Claude subscription (the separate Agent SDK credit), NOT the per-token API. We pass the
chess MCP config + pre-approve the chess tools so Claude grounds its answer in real engine
lines via `get_engine_line`.

Note: `claude -p --mcp-config` spawns its own (separate) chess MCP server process with an
empty session — that's fine, because chat is grounded on the FEN/move passed in the prompt
through the stateless `get_engine_line` tool. We pass CHESS_WEB_AUTOSTART=0 to that child so
it doesn't try to bind the board port we're already serving on.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import chess

from server import config
from server.core import history
from server.core import lines
from server.core import local_llm
from server.core import session as session_mod
from server.core.evaluation import time_control_clock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_CONFIG = _REPO_ROOT / ".mcp.json"
_ALLOWED_TOOLS = "mcp__chess__get_engine_line,mcp__chess__analyze_game"

# How many candidate moves to pre-compute, and how close (in win%-points) an alternative
# must be to the best move to count as "also good" — so Claude can offer the more human,
# intuitive option instead of insisting on the single engine-best move.
_FACTS_MULTIPV = 3
_ALT_WIN_GAP = 5.0
# When no alternative is within _ALT_WIN_GAP, how far the *next-best* move must fall below the
# best for us to flag the position as critical: a big gap = "essentially the only move" (finding
# it mattered); a moderate gap = "clearly best, little margin for error". Lets the coach say
# whether a miss was forgivable instead of treating every position as having one right answer.
_ONLY_MOVE_GAP = 15.0

# Heuristic markers that Claude's Agent SDK credit / usage allowance is exhausted.
_LIMIT_MARKERS = ("usage limit", "rate limit", "credit", "quota", "billing", "limit reached")

# Heuristic markers that the `claude` CLI couldn't authenticate (not logged in, or a bad/stale
# ANTHROPIC_API_KEY). Distinct from the limit case: here the fix is to log in, not wait.
_AUTH_MARKERS = (
    "401",
    "invalid authentication",
    "failed to authenticate",
    "authentication_error",
    "unauthorized",
    "invalid x-api-key",
    "invalid api key",
)


class ChatError(Exception):
    """Raised with a user-facing message when the chat call can't complete."""


# When headless `claude -p` runs without being signed in, it can't pop an interactive login
# prompt, so it just emits the literal `/login` slash command as its `result` and stops — with
# NO `is_error`, `subtype == "success"`, zero tokens and zero cost. That sails past the normal
# error checks, so without this the user sees the raw `/login` JSON instead of a real message.
_LOGIN_HINT = (
    "The `claude` CLI on this machine isn't signed in, so the AI features can't run yet "
    "(it answered with `/login` and never called the model — 0 tokens, $0). To fix it, open a "
    "terminal and run `claude` once: choose “Claude account with subscription”, approve in the "
    "browser, and paste the full code back in a SINGLE clean attempt — don't refresh the auth "
    "tab or run `claude` twice, or the code's state won't match and you'll get “Invalid code”. "
    "Then run `claude -p \"hi\"` to confirm it answers, and restart this app."
)


def _is_login_response(data: dict, answer: str) -> bool:
    """True when this is the not-signed-in `/login` sentinel rather than a real answer.

    Keyed on the `result` being exactly the `/login` slash command (optionally corroborated by
    the zero-token/zero-cost signature) so a legitimate answer that merely *mentions* `/login`
    isn't misclassified.
    """
    if answer.strip() != "/login":
        return False
    usage = data.get("usage") or {}
    zero_tokens = (usage.get("input_tokens") in (0, None)) and (
        usage.get("output_tokens") in (0, None)
    )
    return bool(zero_tokens or data.get("total_cost_usd") in (0, 0.0, None))


def _child_env() -> dict:
    """Environment for the spawned `claude` (subscription path only).

    Sets `CHESS_WEB_AUTOSTART=0` so the child doesn't rebind the board port, and strips a
    stray/empty/stale `ANTHROPIC_API_KEY`, which headless `claude -p` would otherwise silently use
    and 401 on ("Invalid authentication credentials"), forcing the subscription login this feature
    is designed around.

    Note: a configured local LLM no longer routes through here — it's served by direct HTTP
    (`server.core.local_llm`), so the subprocess path runs only in subscription mode.
    """
    env = {**os.environ, "CHESS_WEB_AUTOSTART": "0"}
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _criticality(info: dict) -> str | None:
    """A one-line 'how forced was the best move' signal, or None.

    Only fires when NO alternative is within `_ALT_WIN_GAP` (so it never contradicts the
    "other good moves" line): a large drop to the next-best move = essentially the only move; a
    moderate drop = clearly best with little margin. Reuses the multipv lines we already fetched,
    so it costs no extra engine work.
    """
    lns = info.get("lines") or []
    if len(lns) < 2:
        return None
    best_win = info.get("win_percent")
    second = lns[1]
    second_san = (second.get("line_san") or [None])[0]
    if best_win is None or second_san is None:
        return None
    # If any alternative is near the best, the position isn't critical — let the alts line speak.
    if any((best_win - ln["win_percent"]) <= _ALT_WIN_GAP for ln in lns[1:]):
        return None
    gap = best_win - second["win_percent"]
    if gap >= _ONLY_MOVE_GAP:
        return (
            f"- This is essentially the ONLY good move: the next-best, {second_san}, is far "
            f"worse (win {second['win_percent']}% vs {best_win}%). Finding it was the whole point."
        )
    if gap >= _ALT_WIN_GAP:
        return (
            f"- The best move is clearly best here — the next-best, {second_san} "
            f"(win {second['win_percent']}%), is meaningfully weaker, so there's little margin "
            "for error."
        )
    return None


# Tablebase category -> ordinal rank from the named side's perspective, so we can tell whether a
# move improved or worsened the EXACT result. None = unknown (don't compare).
_TB_RANK = {
    "win": 2,
    "cursed-win": 1,
    "maybe-win": 1,
    "draw": 0,
    "blessed-loss": -1,
    "maybe-loss": -1,
    "loss": -2,
    "unknown": None,
}


def _tb_outcome_phrase(tb: dict) -> str | None:
    """Human phrase for a tablebase result (perspective-neutral wording the caller frames)."""
    cat = tb.get("category")
    dtm, dtz = tb.get("dtm"), tb.get("dtz")
    if cat == "win":
        if dtm:
            return f"a theoretical WIN (forced mate in {abs(dtm)} with perfect play)"
        return (
            f"a theoretical WIN (exact, though conversion still needs technique — DTZ {abs(dtz)})"
            if dtz is not None
            else "a theoretical WIN"
        )
    if cat == "loss":
        if dtm:
            return f"a theoretical LOSS (mated in {abs(dtm)} against best play)"
        return "a theoretical LOSS against best defence"
    if cat == "draw":
        return "a theoretical DRAW — with correct play neither side can win"
    if cat == "cursed-win":
        return "winning material but only a DRAW under the 50-move rule (a 'cursed win')"
    if cat == "blessed-loss":
        return "lost material but saved as a DRAW by the 50-move rule (a 'blessed loss')"
    return None


def _tablebase_current_fact(tb: dict) -> str | None:
    phrase = _tb_outcome_phrase(tb)
    if not phrase:
        return None
    return (
        f"- Tablebase (EXACT, {tb['men']}-piece endgame — a solved position, trust this over the "
        f"eval number): for the side to move it is {phrase}."
    )


def _tablebase_move_fact(before: dict | None, after: dict | None, move_san: str) -> str | None:
    """Compare the exact result before vs after the move (both in the MOVER's perspective)."""
    if not after:
        return None
    after_phrase = _tb_outcome_phrase(after)
    if not after_phrase:
        return None
    if before:
        rb, ra = _TB_RANK.get(before.get("category")), _TB_RANK.get(after.get("category"))
        before_phrase = _tb_outcome_phrase(before)
        if rb is not None and ra is not None and before_phrase:
            if ra < rb:
                return (
                    f"- Tablebase verdict on {move_san} (EXACT, {after['men']}-piece endgame): it "
                    f"threw away the result — the position was {before_phrase} for you, and after "
                    f"{move_san} it is {after_phrase}. This is definitive, not an estimate."
                )
            return (
                f"- Tablebase (EXACT, {after['men']}-piece endgame): {move_san} holds the result — "
                f"still {after_phrase} for you."
            )
    return (
        f"- Tablebase (EXACT, {after['men']}-piece endgame): after {move_san} the position is "
        f"{after_phrase} for you."
    )


def _engine_facts(fen: str | None, move: str | None) -> str | None:
    """Pre-compute the engine's verdict for this position/move so Claude never has to guess.

    Uses the same cached `engine_line` path as the board, so this is fast and consistent.
    """
    if not fen:
        return None
    try:
        info = lines.engine_line(
            fen, move=move, multipv=_FACTS_MULTIPV, settle_material=True, probe_tablebase=True
        )
    except Exception:
        return None

    out: list[str] = []
    if info.get("best_san"):
        out.append(
            f"- Best move for the side to move: {info['best_san']} "
            f"(eval {info['eval']}, win {info['win_percent']}%); "
            f"principal line: {' '.join(info['line_san'][:6])}."
        )
        # Surface alternatives close to the best so Claude can present a more human/intuitive
        # choice rather than insisting on the single engine-top move.
        best_win = info["win_percent"]
        alts = []
        for ln in info.get("lines", [])[1:]:
            san = (ln.get("line_san") or [None])[0]
            if san and (best_win - ln["win_percent"]) <= _ALT_WIN_GAP:
                alts.append(f"{san} (eval {ln['eval']}, win {ln['win_percent']}%)")
        if alts:
            out.append(
                "- Other moves that are about as good (within "
                f"{_ALT_WIN_GAP:g} win%-points): {'; '.join(alts)}. "
                "Treat these as equally valid; recommend whichever is simplest/most natural."
            )
        crit = _criticality(info)
        if crit:
            out.append(crit)
    # Exact endgame result for the position the side to move faces (<=7 men).
    tb_cur = info.get("tablebase")
    if tb_cur:
        fact = _tablebase_current_fact(tb_cur)
        if fact:
            out.append(fact)
    mv = info.get("move")
    if mv:
        better = (
            " It is the engine's top choice."
            if mv.get("is_engine_best")
            else f" The engine prefers {mv['better_move_san']} instead."
        )
        reply = " ".join(mv.get("refutation_line_san", [])[:6])
        out.append(
            f"- The move {mv['move_san']} is classified a {mv['classification']} "
            f"(win {mv['win_before']}% → {mv['win_after']}%, a drop of {mv['win_swing']}).{better}"
            + (f" Best reply after it: {reply}." if reply else "")
        )
        material = _material_outcome(mv.get("material_delta"))
        if material:
            out.append(material)
        tb_move = _tablebase_move_fact(tb_cur, mv.get("tablebase"), mv["move_san"])
        if tb_move:
            out.append(tb_move)
    return "\n".join(out) if out else None


def _material_outcome(delta: int | None) -> str | None:
    """Turn the move's net material change (mover's perspective, pawn-points) into a fact that
    tells Claude WHETHER the eval drop is material or positional — the thing it otherwise has to
    (and sometimes wrongly) infer from the SAN line. None when there's no material data."""
    if delta is None:
        return None
    if -1 < delta < 1:  # material unchanged once the line settles
        return (
            "- Material after the engine's main line: unchanged. The eval change is POSITIONAL "
            "(tempo, king safety, structure, activity) — this move does NOT win or lose material, "
            "so do not describe it as winning/losing material."
        )
    n = abs(delta)
    if n <= 1:
        worth = "about a pawn"
    elif n == 2:
        worth = "about two pawns"
    elif n <= 4:
        worth = "about a minor piece / the exchange"
    elif n <= 6:
        worth = "about a rook"
    else:
        worth = "a decisive amount"
    side = "loses" if delta < 0 else "wins"
    return (
        f"- Material after the engine's main line: the side that moved {side} ~{n} point(s) of "
        f"material ({worth}). The eval change here is driven by MATERIAL."
    )


def _profile_facts() -> str | None:
    """Compact coaching profile for the current session's player, or None (no history/off)."""
    try:
        return history.format_profile_for_prompt(history.get_profile())
    except Exception:
        return None


def _speed_context() -> str | None:
    """One line on the current game's mode, so Claude judges mistakes by mode-appropriate standards."""
    try:
        sess = session_mod.get_session()
    except Exception:
        return None
    speed = getattr(sess, "speed", None) if sess is not None else None
    if not speed or speed == "unknown":
        return None
    tc = (sess.headers.get("TimeControl") or "").strip()
    tc_str = f" (time control {tc})" if tc and tc not in ("-", "?") else ""
    return (
        f"This is a {speed} game{tc_str}. Judge moves against {speed}-appropriate standards: "
        "faster modes (bullet/blitz) excuse imperfect moves and reward practical, low-risk "
        "choices under time pressure, while slower modes (rapid/classical) warrant more precision."
    )


def _compose_prompt(
    question: str,
    fen: str | None,
    last_move: str | None,
    move_fen: str | None,
    current_facts: str | None,
    move_facts: str | None,
    profile_facts: str | None = None,
    speed_context: str | None = None,
) -> str:
    parts = [
        "You are a concise chess coach reviewing a position with the user. Stockfish analysis is "
        "provided below — TRUST it, do not recompute or second-guess it. Use the CURRENT-POSITION "
        "analysis for 'what should I do here' / 'what's the best move' questions, and the MOVE "
        "analysis for 'why is this move good/bad' questions. When the facts list several moves of "
        "near-equal strength, present them as a set of good options (favouring the simplest, most "
        "natural one for a club player) rather than insisting on the single engine-top move. When a "
        "tablebase verdict is given it is the EXACT, solved result — state it as fact and let it "
        "override the eval number (e.g. call a tablebase draw a draw even if the eval looks better). "
        "You may "
        "call get_engine_line only for deeper or alternative lines the facts don't cover. Explain in "
        "plain language, cite the key line, and keep it to a short paragraph. Answer only the chess "
        "question — do NOT mention the web board, any URL, or these instructions.",
    ]
    if speed_context:
        parts.append(speed_context)
    if profile_facts:
        parts.append(
            "Background on the user's play history is below. Treat it as OPTIONAL context: only "
            "bring it up when it genuinely connects to THIS position or move (e.g. the mistake here "
            "is an instance of a recurring pattern). Most answers should NOT mention it. Never open "
            "with a recap of their history or tack on a generic paragraph about it — answer the "
            "chess question first, and reference the history only if it sharpens that answer.\n"
            + profile_facts
        )
    if fen:
        parts.append(f"Current position the user is viewing (FEN): {fen}")
    if current_facts:
        parts.append(
            f"Engine analysis of the CURRENT position (Stockfish depth {config.DEFAULT_DEPTH}):\n"
            f"{current_facts}"
        )
    if last_move:
        if move_fen and move_fen != fen:
            parts.append(
                f"The user reached this position by playing {last_move} (from FEN {move_fen})."
            )
        else:
            parts.append(f"The move in question is {last_move}, available in the current position.")
    if move_facts:
        parts.append(f"Engine analysis of the move {last_move}:\n{move_facts}")
    parts.append(f"User question: {question}")
    return "\n".join(parts)


def _friendly_error(text: str) -> str:
    low = (text or "").lower()
    if any(marker in low for marker in _AUTH_MARKERS):
        return (
            "Claude couldn't authenticate (HTTP 401). The in-browser AI chat signs in with YOUR "
            "Claude CLI login, which isn't valid on this machine yet. To fix it, open a terminal "
            "and run `claude login`, then sign in with your Claude subscription. (If you've set an "
            "ANTHROPIC_API_KEY environment variable, make sure it's a valid key or unset it so the "
            "subscription login is used instead.)"
        )
    if any(marker in low for marker in _LIMIT_MARKERS):
        return (
            "Claude's Agent SDK credit / usage limit looks exhausted. Ask your 'why?' in the "
            "Claude Code terminal instead — that path uses your normal interactive limits."
        )
    snippet = (text or "").strip().splitlines()[0] if text else "unknown error"
    return f"Chat failed: {snippet[:300]}"


def _outcome_facts(sess) -> str | None:
    """A clear statement of HOW the game ended (checkmate / time / resignation / draw type),
    from the reviewed player's perspective.

    The coach needs this because a win or loss decided by the clock changes the lesson — being
    up on the board but flagging, or vice versa, is worth naming. We derive it from signals we
    already have: the result, the final move (checkmate ends in '#'), and the PGN `Termination`
    header (Lichess: "Normal"/"Time forfeit"; Chess.com: "<player> won on time/by checkmate/…").
    Returns None for an unfinished/unknown result.
    """
    result = (sess.result or "*").strip()
    if result == "1-0":
        verdict = "won" if sess.player == "white" else "lost"
    elif result == "0-1":
        verdict = "won" if sess.player == "black" else "lost"
    elif result in ("1/2-1/2", "1/2", "½-½"):
        verdict = "drew"
    else:
        return None

    # Did the game end in checkmate? The last played move's SAN ends in '#'.
    last_san = ""
    for node in reversed(sess.timeline):
        san = node.get("move_san")
        if san:
            last_san = san
            break
    is_mate = last_san.endswith("#")

    term = (sess.headers.get("Termination") or "").strip()
    low = term.lower()

    # Resolve the ending reason from the strongest available signal; leave it unstated rather
    # than guess one we can't back up.
    reason = None
    if is_mate:
        reason = "by checkmate"
    elif "abandon" in low:
        reason = "by abandonment (the opponent left)" if verdict == "won" else "by abandonment"
    elif "time" in low or "forfeit" in low:
        # Lichess "Time forfeit"; Chess.com "<player> won on time".
        reason = "on time (a player ran out of clock)"
    elif "resign" in low:
        reason = "by resignation"
    elif verdict == "drew":
        if "stalemate" in low:
            reason = "by stalemate"
        elif "repetition" in low:
            reason = "by repetition"
        elif "insufficient" in low:
            reason = "by insufficient material"
        elif "agree" in low:
            reason = "by agreement"
        elif "50" in low or "fifty" in low:
            reason = "by the fifty-move rule"
    elif low in ("normal", ""):
        # Lichess marks a non-flag decisive game "Normal"; if it wasn't mate it was a resignation.
        reason = "by resignation"

    sentence = f"Outcome: you {verdict} this game ({result})"
    sentence += f", {reason}." if reason else "."
    if term and term.lower() != "normal":
        sentence += f' The PGN records the termination as "{term}".'
    return sentence


def _time_control_phrase(headers) -> str:
    """The concrete clock, e.g. '10+0 (10 min/side)', so the coach can weigh a think time against
    the actual starting time (23s is huge in 2+1, trivial in 15+10). Empty string when the
    TimeControl isn't a sudden-death clock (correspondence / missing); the caller pairs it with the
    speed bucket."""
    tc = time_control_clock(headers.get("TimeControl"))
    if not tc:
        return ""
    base, inc = tc
    # Conventional notation is base-in-MINUTES + increment-in-seconds (10+0, 3+2). Drop to a
    # seconds form for sub-minute / non-whole-minute bases so we never print "0.5+0".
    if base >= 60 and base % 60 == 0:
        return f"{base / 60:g}+{inc:g} ({base / 60:g} min/side)"
    return f"{base:g}s+{inc:g} ({base:g}s/side)"


def _fmt_secs(seconds: float) -> str:
    """Human-friendly think time: '4s', '38s', '2m05s'."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _time_note(m, avg_spent: float | None) -> str:
    """A parenthetical like ' (took 1m20s, a long think)' for a flagged move, or '' when there's
    no clock data. Flags moves notably slower/faster than the player's own average so the coach can
    distinguish a deliberated misjudgement from a snap / time-scramble error."""
    spent = m.seconds_spent
    if spent is None:
        return ""
    note = f" (took {_fmt_secs(spent)}"
    if spent <= 10 and (m.clock_after is None or m.clock_after > 30):
        note += ", played quickly"
    elif avg_spent and avg_spent > 0:
        if spent >= max(3 * avg_spent, avg_spent + 20):
            note += ", a long think"
        elif spent <= 0.3 * avg_spent:
            note += ", played quickly"
    if m.clock_after is not None and m.clock_after <= 30:
        note += f"; only {_fmt_secs(m.clock_after)} left on the clock"
    return note + ")"


# A move the player got "right enough" not to lose ground.
_GOOD_CLASS = {"best", "good"}
# The errors that actually cost games (inaccuracies are tolerated by the "clean" detectors below).
_SERIOUS_CLASS = {"mistake", "blunder"}


def _player_won(sess) -> bool:
    return sess.result == ("1-0" if sess.player == "white" else "0-1")


def _player_lost(sess) -> bool:
    return sess.result == ("0-1" if sess.player == "white" else "1-0")


def _strengths(sess) -> list[str]:
    """Robust, can't-be-faked positive facts about the *whole game*, derived only from the sweep's
    per-move classification + win% (zero extra engine cost). The idea (Option 5): rather than hunt
    for a single "brilliant" move — where matching the engine on an obvious move isn't impressive —
    praise patterns the data already proves: a clean conversion, resilient defense, a solid opening,
    a clean endgame, high accuracy. Each is conservative and only fires when genuinely earned, so a
    forgettable game yields nothing and the summary stays honest. The prompt decides whether to cite
    one. Returns short factual strings, most-impressive first (capped)."""
    moves = sess.all_moves
    if not moves:
        return []
    out: list[str] = []

    # Clean conversion: from the first point you were clearly winning, no further mistakes/blunders,
    # and you actually won. The strongest "you closed it out" signal.
    win_idx = next((i for i, m in enumerate(moves) if m.win_before >= 80), None)
    if win_idx is not None:
        after = moves[win_idx:]
        if len(after) >= 4 and _player_won(sess) and not any(
            m.classification in _SERIOUS_CLASS for m in after
        ):
            out.append(
                f"Clean conversion: clearly winning by move {moves[win_idx].move_number}, then made "
                "no further mistakes or blunders and brought home the win."
            )

    # Resilient defense: a stretch of consecutive moves played from a clearly worse position with no
    # serious error, after which you clawed back toward equality (or at least didn't lose).
    best_run: list = []
    run: list = []
    for m in moves:
        if m.win_before <= 35 and m.classification not in _SERIOUS_CLASS:
            run.append(m)
            if len(run) > len(best_run):
                best_run = run
        else:
            run = []
    if len(best_run) >= 4:
        end_ply = best_run[-1].ply
        recovered = any(m.ply > end_ply and m.win_before >= 45 for m in moves) or not _player_lost(
            sess
        )
        if recovered:
            out.append(
                f"Resilient defense: held a clearly worse position for {len(best_run)} straight "
                "moves without a serious error and fought back toward equality."
            )

    # Solid opening: your first several moves were all engine-approved (no inaccuracy or worse).
    opening = [m for m in moves if m.move_number <= 10]
    if len(opening) >= 6 and all(m.classification in _GOOD_CLASS for m in opening):
        out.append(
            f"Solid opening: your first {len(opening)} moves were all engine-approved, with no "
            "inaccuracies."
        )

    # Clean endgame: once the game simplified into an endgame you made no mistakes or blunders.
    endgame = [m for m in moves if history._phase(m.fen_before, m.move_number) == "endgame"]
    if len(endgame) >= 4 and not any(m.classification in _SERIOUS_CLASS for m in endgame):
        out.append("Clean endgame: once the game reached an endgame you made no mistakes or blunders.")

    # Overall accuracy — a flat, hard-to-argue-with summary signal.
    acc = sess.accuracy_white if sess.player == "white" else sess.accuracy_black
    opp = sess.accuracy_black if sess.player == "white" else sess.accuracy_white
    if acc >= 90:
        out.append(f"High accuracy: {acc}% across the game.")
    elif acc >= 75 and acc >= opp + 8:
        out.append(f"You were the more accurate player ({acc}% to your opponent's {opp}%).")

    return out[:3]


def _sac_detail(m) -> dict | None:
    """If the player's move `m` is a *sound material sacrifice*, describe it; else None.

    A sacrifice = after the move one of your own pieces (a minor or more) is left en prise such that
    the opponent can win material on the exchange (history._is_hanging, a static SEE-lite), yet the
    move is sound (the caller only passes 'best'/'good' moves the engine approves). The #3 quiet
    filter lives in `invested = sacrificed - captured`: a plain recapture or equal trade nets ~0 and
    is rejected, so only moves that genuinely give up material (a quiet piece offer, or a capture that
    surrenders more than it takes — e.g. Bxh7+ giving a bishop for a pawn) survive. Fully engine-free
    (python-chess attackers/defenders over FENs already on the session)."""
    try:
        before = chess.Board(m.fen_before)
        after = chess.Board(m.fen_after)
        move = chess.Move.from_uci(m.move_uci)
    except (ValueError, AssertionError):
        return None
    player_color = before.turn
    captured = 0
    is_capture = before.is_capture(move)
    if is_capture:
        if before.is_en_passant(move):
            captured = 1
        else:
            captured = history._val(before.piece_at(move.to_square))
    # Most valuable own piece left hanging after the move.
    sacrificed = 0
    for sq, piece in after.piece_map().items():
        if piece.color == player_color and history._is_hanging(after, sq):
            sacrificed = max(sacrificed, history._val(piece))
    if sacrificed < 3:  # only a minor piece or more counts as a standout sacrifice
        return None
    invested = sacrificed - captured
    if invested < 2:  # a near-even trade / recapture is not a sacrifice
        return None
    return {
        "invested": invested,
        "sacrificed": sacrificed,
        "captured": captured,
        "is_capture": is_capture,
        "quiet": 0 if is_capture else 1,
        "gives_check": after.is_check(),
    }


def _sacrifices(sess, limit: int = 2):
    """The player's sound material sacrifices (#2 standout move), best first. Engine-free: reuses
    the sweep's classification + win% and a static material/attacker check. Skips moves played from
    an already-winning position (sac-ing while up a queen isn't impressive) and moves that don't keep
    the player at least roughly equal afterwards. Returns (MoveReview, detail) pairs."""
    out = []
    for m in sess.all_moves:
        if m.classification not in _GOOD_CLASS:
            continue
        if m.win_before >= 85:  # already winning — giving material back isn't a feat
            continue
        if m.win_after < 45:  # the engine must still rate the position equal-or-better
            continue
        detail = _sac_detail(m)
        if detail:
            out.append((m, detail))
    # Quiet (non-capture) sacrifices are harder to find; then bigger investment first.
    out.sort(key=lambda md: (md[1]["quiet"], md[1]["invested"]), reverse=True)
    return out[:limit]


def _game_facts(sess) -> str:
    """Pre-computed, engine-grounded facts about the whole game for the coach summary prompt.

    Everything here already exists on the session (accuracy, the flagged moves + their templated
    comments, the player's profile), so the Claude call only has to write — it never analyses.
    """
    side = "White" if sess.player == "white" else "Black"
    acc = sess.accuracy_white if sess.player == "white" else sess.accuracy_black
    opening = session_mod.resolve_opening(sess) or "unknown opening"
    tc_detail = _time_control_phrase(sess.headers)
    tc_phrase = f"{sess.speed}, {tc_detail}" if tc_detail else f"{sess.speed}"
    out = [
        f"Game: {sess.headers.get('White', '?')} vs {sess.headers.get('Black', '?')} "
        f"({sess.result}); {opening}; {tc_phrase} time control.",
        f"Reviewing {side}. Accuracy: {acc}% (opponent "
        f"{sess.accuracy_black if sess.player == 'white' else sess.accuracy_white}%).",
    ]
    outcome = _outcome_facts(sess)
    if outcome:
        out.append(outcome)
    # Average think time across the player's moves, so the coach can judge a mistake's timing
    # relative to this player's own pace (a long think vs a blitzed-out / time-scramble move).
    spents = [m.seconds_spent for m in sess.all_moves if m.seconds_spent is not None]
    avg_spent = sum(spents) / len(spents) if spents else None
    if avg_spent is not None:
        out.append(f"You averaged {_fmt_secs(avg_spent)} per move this game.")
    strengths = _strengths(sess)
    if strengths:
        out.append("Strengths in this game (engine-confirmed; acknowledge at most one, briefly):")
        for s in strengths:
            out.append(f"- {s}")
    sacs = _sacrifices(sess)
    if sacs:
        out.append("Standout move(s) — a sound material sacrifice the engine approves:")
        for m, d in sacs:
            num = f"{m.move_number}{'.' if m.color == 'white' else '...'}"
            check = " with check" if d["gives_check"] else ""
            out.append(
                f"- {num}{m.move_san}: gave up material{check} (a net ~{d['invested']} points) yet "
                f"the engine still rates the position fine for you ({round(m.win_after)}% win chance) "
                "— a genuinely hard move to find."
            )
    if sess.mistakes:
        out.append(f"{side}'s flagged moves (worst first):")
        worst = sorted(sess.mistakes, key=lambda m: m.win_swing, reverse=True)
        for m in worst[:8]:
            num = f"{m.move_number}{'.' if m.color == 'white' else '...'}"
            line = (
                f"- {num}{m.move_san} ({m.classification}, win {m.win_before}% -> {m.win_after}%, "
                f"drop {m.win_swing}); engine preferred {m.best_move_san}. {m.comment}".rstrip()
            )
            line += _time_note(m, avg_spent)
            out.append(line)
    else:
        out.append(f"{side} made no inaccuracies, mistakes or blunders — a clean game.")
    return "\n".join(out)


def _puzzle_solution_facts(puzzle: dict) -> tuple[str, str | None]:
    """The forced solution line in SAN, annotated 'your move' vs 'forced reply', plus the crux.

    Returns (line_text, key_move_san). The crux is the solver's first move (moves[1]); the setup
    move (moves[0]) is the opponent's move that created the puzzle. Best-effort -> ("", None).
    """
    try:
        board = chess.Board(puzzle["fen"])
        moves = puzzle.get("moves", [])
        parts: list[str] = []
        key_move_san: str | None = None
        for i, uci in enumerate(moves):
            mv = chess.Move.from_uci(uci)
            san = board.san(mv)
            board.push(mv)
            if i == 0:
                parts.append(f"(setup, opponent plays {san})")
            elif i % 2 == 1:
                parts.append(f"your move {san}")
                if key_move_san is None:
                    key_move_san = san
            else:
                parts.append(f"forced reply {san}")
        return " -> ".join(parts), key_move_san
    except (ValueError, KeyError):
        return "", None


def _move_verdict(fen_before: str, uci: str, correct: bool) -> str:
    """One engine-grounded line about a move the player tried, from the position it was played in.

    Best-effort: with no engine (or any failure) it degrades to just naming the move and whether it
    matched the solution, so the coach never invents an evaluation.
    """
    try:
        info = lines.engine_line(fen_before, move=uci, settle_material=True)
        mv = info.get("move") if info else None
        if mv:
            san = mv.get("move_san", uci)
            wb, wa = round(mv.get("win_before", 0)), round(mv.get("win_after", 0))
            if correct:
                tail = "matches the solution; engine agrees" if mv.get("is_engine_best") else \
                    "a good move the engine also accepts"
                return f"{san} (you played this): win% {wb} -> {wa}, {tail}."
            ref = mv.get("refutation_line_san") or []
            base = f"{san} (you played this): WRONG, win% {wb} -> {wa}"
            cls = mv.get("classification")
            if cls and cls not in ("best", "good"):
                base += f" ({cls})"
            if ref:
                base += f"; refuted by {' '.join(ref[:4])}"
            return base + "."
    except Exception:  # noqa: BLE001 - engine grounding is optional; never break the coach
        pass
    return f"{uci} (you played this) — {'matched the solution' if correct else 'not the solution'}."


def _puzzle_facts(
    puzzle: dict,
    outcome: str,
    your_move: str | None = None,
    tried: list[dict] | None = None,
) -> str:
    """Pre-computed facts for the puzzle coach, leading with the THEMES (the headline signal).

    The solution line is ground truth; engine facts are best-effort grounding (mate distance /
    material swing for the crux; an engine verdict for EACH move the player tried, with the
    refutation when a tried move was wrong). With no engine the coach still has the solution line +
    themes to teach from.
    """
    side = puzzle.get("side_to_move", "white")
    themes = puzzle.get("themes", []) or []
    solve_fen = puzzle.get("solve_fen") or puzzle.get("fen", "")
    line_text, key_move_san = _puzzle_solution_facts(puzzle)

    out = [
        f"Themes (the motif to teach): {', '.join(themes) if themes else 'unlabelled tactic'}.",
        f"{side.capitalize()} to move. Puzzle rating: {int(round(float(puzzle.get('rating', 1500))))}.",
        f"Position (FEN): {solve_fen}",
        "This is a VERIFIED forced solution from a curated puzzle. Explain why it works; do NOT "
        "second-guess it or propose alternatives.",
        f"Solution line: {line_text}." if line_text else "",
    ]

    # Best-effort engine grounding for the crux move (mate distance / eval / win%).
    try:
        info = lines.engine_line(solve_fen, depth=config.DEFAULT_DEPTH)
        if info and not info.get("error"):
            out.append(
                f"Engine on the key move {key_move_san or info.get('best_san')}: "
                f"eval {info.get('eval')}, ~{round(info.get('win_percent', 0))}% win for the side to move."
            )
    except Exception:  # noqa: BLE001 - engine grounding is optional; never break the coach
        pass

    # Engine-grounded verdicts for every move the player actually tried (the correct ones they found
    # AND the wrong one that ended it), so the coach can speak to their specific attempt — not just
    # the canonical solution. Prefer the per-move `tried` log; fall back to a single `your_move`.
    if tried:
        out.append("Moves the player tried, in order (engine-grounded — address these specifically):")
        for t in tried:
            out.append("- " + _move_verdict(t.get("fen_before") or solve_fen, t.get("uci", ""),
                                             bool(t.get("correct"))))
    elif outcome == "failed" and your_move:
        out.append("Move the player tried (engine-grounded — address it specifically):")
        out.append("- " + _move_verdict(solve_fen, your_move, False))

    return "\n".join(p for p in out if p)


def _mistake_facts(puzzle: dict, tried: list[dict] | None) -> str:
    """Facts for a 'from your games' mistake puzzle — the game-analysis framing (win% drop + better
    move + refutation via `_engine_facts`), NOT the themes/forced-line puzzle framing. Best-effort.
    """
    fen = puzzle.get("fen", "")
    side = puzzle.get("side_to_move", "white")
    reviewed = puzzle.get("reviewed_side", side)
    opp = puzzle.get("black") if reviewed == "white" else puzzle.get("white")
    played_san = puzzle.get("played_san") or puzzle.get("played_uci") or "their move"
    cls = puzzle.get("classification") or "mistake"

    out = [
        f"This position is from the PLAYER'S OWN past game"
        + (f" (vs {opp}, {puzzle.get('speed', 'unknown')})." if opp else ".")
        + f" {side.capitalize()} to move.",
        f"Position (FEN): {fen}",
        f"In the actual game the player played {played_san} here, flagged as a {cls}.",
    ]
    ef = _engine_facts(fen, puzzle.get("played_uci"))
    if ef:
        out.append("Engine on this position and the game move:\n" + ef)
    if tried:
        out.append("Moves the player tried in this puzzle (engine-grounded — address these):")
        for t in tried:
            out.append("- " + _move_verdict(fen, t.get("uci", ""), bool(t.get("correct"))))
    return "\n".join(p for p in out if p)


def _mistake_role(outcome: str, tried: list[dict] | None) -> str:
    """Coach role prompt for a mistake puzzle (solved = found something better; failed = didn't)."""
    if outcome != "failed":
        return (
            "You are a chess coach. This is a position from the player's OWN past game where they "
            "went wrong, replayed as practice. They have now found a move the engine rates as good "
            "(better than, or as good as, what they played in the game). Affirm the improvement by "
            "name, then explain concretely — using the engine facts — WHY their original game move "
            "was off and what the right idea is, so they recognise it next time. Be encouraging."
        )
    return (
        "You are a chess coach. This is a position from the player's OWN past game where they went "
        "wrong, replayed as practice; they did not find a better move this time. Explain concretely "
        "why their original game move was off (use its engine refutation from the facts) and teach "
        "the stronger idea step by step. If they tried other moves that also fail (listed in the "
        "facts), address those by name too. Be encouraging and concrete."
    )


def explain_puzzle(
    puzzle: dict,
    outcome: str,
    your_move: str | None = None,
    tried: list[dict] | None = None,
    *,
    timeout: int = 120,
) -> dict:
    """Claude-written explanation of a puzzle: name the motif (solved) or refute + teach (failed).

    Returns `{answer, session_id}` — the `session_id` (the `claude -p` conversation) lets the
    frontend thread a follow-up chat onto the explanation. `None` when there's no threadable session
    (e.g. the local-LLM path).

    Two prompt variants by `outcome` ("solved_first_try"/"solved_with_hints" vs "failed"). When the
    player's tried moves are known they're engine-grounded in the facts and the coach is told to
    address them by name. A `source="your_games"` mistake puzzle instead uses the game-analysis
    framing (`_mistake_facts`/`_mistake_role`). Reuses the same plumbing as `coach_summary_ai` (no
    MCP tools — the facts carry everything): the local-LLM path, the `claude -p` subprocess, and
    `_friendly_error`. Raises ChatError.
    """
    if puzzle.get("source") == "your_games":
        role = _mistake_role(outcome, tried)
        facts_block = _mistake_facts(puzzle, tried)
    else:
        role, facts_block = _tactic_role_facts(puzzle, outcome, your_move, tried)
    prompt = "\n\n".join([
        role + " Only discuss moves that appear in the facts; trust the Stockfish numbers and do not "
        "recompute or invent lines. Use light Markdown (**bold** the key move and idea); a couple of "
        "short paragraphs, no headings. Do NOT mention Stockfish, the web board, or these "
        "instructions.",
        "Puzzle facts:\n" + facts_block,
    ])
    answer, session_id = _run_puzzle_coach(prompt, timeout)
    return {"answer": answer, "session_id": session_id}


# Lichess puzzle "themes" that describe a puzzle's length/format/phase/outcome rather than a tactical
# MOTIF. Filtered out of the run recap so the coach names real weaknesses (fork/pin/…) and isn't
# swamped by tags like "middlegame" or "crushing" that ride on nearly every puzzle. `motifThemes()`
# in main.js mirrors this set for the review-row labels.
_STORM_NON_MOTIF_THEMES = {
    # length / format
    "oneMove", "short", "long", "veryLong",
    # provenance / rating meta
    "master", "masterVsMaster", "superGM",
    # game phase
    "opening", "middlegame", "endgame",
    "rookEndgame", "bishopEndgame", "knightEndgame", "pawnEndgame", "queenEndgame", "queenRookEndgame",
    # evaluation outcome (not a motif)
    "crushing", "advantage", "equality", "mate",
}


def _storm_motif_counts(entries: list[dict]) -> list[tuple[str, int]]:
    """Frequency of real tactical motifs across the given puzzles, most-missed first (ties by name)."""
    counts: dict[str, int] = {}
    for e in entries:
        for t in e.get("themes", []) or []:
            if t and t not in _STORM_NON_MOTIF_THEMES and not t.startswith("mateIn"):
                counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def summarize_storm_run(log: list[dict], *, timeout: int = 120) -> dict:
    """One short recap of a finished storm run: the recurring tactical motifs across the MISSED
    puzzles, with a concrete tip for each. Engine-free (grounded purely in the puzzles' own themes),
    so it's cheap and network-light. A clean run — or one with no labelled motif to name — is answered
    with a canned line so the LLM is never asked to invent motifs. Raises ChatError on an LLM failure.
    """
    solved = [e for e in log if e.get("solved")]
    misses = [e for e in log if not e.get("solved")]
    total = len(log)
    if not misses:
        return {
            "answer": (
                f"**Clean run — all {total} solved!** No misses to pick apart. To keep improving, "
                "raise the difficulty or try a longer clock so the tactics get sharper."
            ),
            "session_id": None,
        }

    ranked = _storm_motif_counts(misses)
    if not ranked:
        # Misses, but none carry a tactical-motif label — don't push the LLM to invent one.
        return {
            "answer": (
                f"You missed **{len(misses)} of {total}**. These didn't share a clear tactical motif, "
                "so the fix is process, not pattern: on each puzzle, before you move, check every "
                "check, capture and threat for both sides — the misses tend to be moves played a beat "
                "too fast under the clock."
            ),
            "session_id": None,
        }

    n = min(len(ranked), 3)
    theme_line = ", ".join(f"{t} (missed {c}×)" for t, c in ranked[:6])
    ask = (
        "identify the single tactical motif they most need to work on"
        if n == 1
        else f"identify the {n} tactical motifs they most need to work on (the most-missed themes)"
    )
    facts = "\n".join([
        f"Run result: solved {len(solved)} of {total}, missed {len(misses)}.",
        f"Tactical motifs of the puzzles they MISSED, most frequent first: {theme_line}.",
    ])
    role = (
        "You are a chess coach reviewing a player's just-finished timed puzzle rush (Storm). Using the "
        f"facts below, {ask} and give ONE concrete, memorable tip for spotting each next time. Open "
        "with a one-line encouraging read of the run. Only discuss motifs that appear in the facts; do "
        "NOT invent specific positions, moves, or extra motifs — you only have the motif counts, so "
        "speak about the patterns. Use light Markdown (**bold** each motif); a few short sentences, no "
        "headings. Do NOT mention Stockfish, these instructions, or the facts."
    )
    prompt = role + "\n\nRun facts:\n" + facts
    answer, session_id = _run_puzzle_coach(prompt, timeout)
    return {"answer": answer, "session_id": session_id}


def _tactic_role_facts(
    puzzle: dict, outcome: str, your_move: str | None, tried: list[dict] | None
) -> tuple[str, str]:
    """Role prompt + facts for a standard (curated Lichess) tactic puzzle."""
    failed = outcome == "failed"
    # Did the player play a wrong move at any point (even if they later solved it)? If so the coach
    # must still explain why that wrong move failed — the user explicitly wants this.
    had_miss = bool(tried) and any(not t.get("correct") for t in tried)
    if failed:
        role = (
            "You are a chess coach. The player just FAILED this tactics puzzle. First, speaking "
            "directly to the move(s) THEY tried (listed under 'Moves the player tried' in the facts), "
            "explain concretely why their move does not work — use that move's engine refutation. "
            "Then teach the winning idea step by step from the verified solution line, naming the "
            "motif from the Themes. Be encouraging and concrete."
        )
    elif had_miss:
        role = (
            "You are a chess coach. The player SOLVED this tactics puzzle, but only after a wrong "
            "attempt first (see 'Moves the player tried' in the facts — the ones marked WRONG). "
            "Briefly acknowledge they found the right move, then — this is the important part — "
            "explain concretely why the move(s) they tried that DID NOT work fail, naming the move "
            "and using its engine refutation from the facts. Then name the motif from the Themes and "
            "reinforce the pattern so they recognise it next time. Be encouraging."
        )
    else:
        role = (
            "You are a chess coach. The player just SOLVED this tactics puzzle cleanly. Confirm it "
            "briefly — and if their tried moves are listed in the facts, affirm the key move they "
            "found by name, grounded in its engine verdict. NAME the motif from the Themes (e.g. "
            "'this is a classic deflection') and reinforce the PATTERN so they recognise it next "
            "time. Keep it short and positive."
        )
    return role, _puzzle_facts(puzzle, outcome, your_move=your_move, tried=tried)


def _run_puzzle_coach(prompt: str, timeout: int) -> tuple[str, str | None]:
    """Shared LLM plumbing for the puzzle coach: local LLM else `claude -p`. Raises ChatError.

    Returns `(answer, session_id)`; `session_id` is the `claude -p` conversation id (None on the
    local-LLM path) so a follow-up chat can `--resume` the explanation.
    """
    if local_llm.is_enabled():
        try:
            return local_llm.complete(prompt, timeout=max(timeout, local_llm.DEFAULT_TIMEOUT)), None
        except local_llm.LocalLLMError as exc:
            raise ChatError(str(exc))

    claude = shutil.which("claude")
    if not claude:
        raise ChatError(
            "The `claude` CLI isn't on PATH, so the AI puzzle coach is unavailable. Install the "
            "Claude CLI (or set a local AI model in Settings) to get explanations."
        )
    cmd = [claude, "-p", prompt, "--output-format", "json"]
    env = _child_env()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_REPO_ROOT), env=env
        )
    except subprocess.TimeoutExpired:
        raise ChatError("Claude took too long to explain the puzzle (timed out).")
    if proc.returncode != 0:
        raise ChatError(_friendly_error(proc.stderr or proc.stdout))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ChatError(_friendly_error(proc.stdout))
    answer = (data.get("result") or "").strip()
    if _is_login_response(data, answer):
        raise ChatError(_LOGIN_HINT)
    if data.get("is_error") or data.get("subtype") not in (None, "success") or not answer:
        raise ChatError(_friendly_error(answer or proc.stdout))
    return answer, data.get("session_id")


def coach_summary_ai(sess, *, timeout: int = 120) -> str:
    """A richer, Claude-WRITTEN end-of-game coaching summary, grounded in pre-computed facts.

    Opt-in (spends the user's Claude subscription, or runs on the configured local LLM). No MCP
    tools / engine calls — the prompt already carries every fact needed, so the model only has to
    phrase the coaching well. Raises ChatError.
    """
    profile_facts = _profile_facts()
    prompt_parts = [
        "You are an honest, encouraging chess coach writing a short end-of-game summary for the "
        "player whose moves are reviewed below. The Stockfish facts are authoritative — TRUST them, "
        "do not recompute. Write a few short paragraphs in warm, direct second person ('you'). "
        "Be balanced, not relentlessly negative: when the facts genuinely warrant it — a sound "
        "sacrifice flagged under 'Standout move(s)', a genuine strength listed under 'Strengths in "
        "this game' (a clean conversion, resilient defense, solid opening, clean endgame, or high "
        "accuracy), or simply a clean game — acknowledge ONE such strength briefly and specifically "
        "before turning to what went wrong. But never manufacture praise, pad with faint compliments, "
        "or call an ordinary move good; if nothing genuinely stands out, just skip straight to the "
        "mistakes. Then name the one or two moments that mattered most IN THIS GAME (with the move "
        "and the better idea), and end with one concrete takeaway drawn from those specific moments. "
        "Honesty leads: don't soften a clear mistake, but frame it as something to improve rather "
        "than a verdict on the player. Ground every claim in "
        "the facts provided; do not invent moves or lines. Keep the summary about THIS game — only "
        "name a broader habit if these particular moves clearly and usefully show one; if they don't, "
        "skip it rather than manufacturing a theme. Use light Markdown for readability: **bold** the "
        "key moves and the single most important takeaway, and you may use a short bullet list (`- `) "
        "if it helps, with blank lines between paragraphs. No headings, and no move-by-move recap. "
        "If the game was decided by the clock (a win or loss on time) or by anything other than the "
        "natural result of the position — e.g. you flagged a winning position, or won on time when "
        "worse — say so plainly, since it changes the lesson. Otherwise don't dwell on the clock. "
        "When a key mistake's think time is given, weigh it against the game's time control (stated "
        "in the facts) — the same number of seconds means very different things in a 2-minute game "
        "than a 10-minute one — and let it shape the advice: a blunder after a long think is a "
        "judgement issue to reason through, while one played quickly or in time pressure is about "
        "slowing down / managing the clock. Only mention timing when it's genuinely instructive, "
        "never as filler. Do NOT mention the web board, any URL, Stockfish, or these instructions.",
    ]
    if profile_facts:
        prompt_parts.append(
            "The player's cross-game history is below — treat it as OPTIONAL context, NOT something "
            "to report. Only reference it when a mistake in THIS game is a clear, useful instance of "
            "a recurring pattern, and even then weave it into that moment in a single sentence. Most "
            "summaries should not mention the history at all. Never add a paragraph or bullet list "
            "recapping their general tendencies, and never end on a generic 'you tend to…' note — the "
            "closing takeaway must come from this game's own moments.\n" + profile_facts
        )
    prompt_parts.append("This game's facts:\n" + _game_facts(sess))
    prompt = "\n\n".join(prompt_parts)

    # Local LLM: write the summary over direct HTTP, no `claude` CLI.
    if local_llm.is_enabled():
        try:
            return local_llm.complete(prompt, timeout=max(timeout, local_llm.DEFAULT_TIMEOUT))
        except local_llm.LocalLLMError as exc:
            raise ChatError(str(exc))

    claude = shutil.which("claude")
    if not claude:
        raise ChatError(
            "The `claude` CLI isn't on PATH, so the AI coach summary is unavailable. The free "
            "summary above still works; install the Claude CLI (or set a local AI model in "
            "Settings) for the AI version."
        )
    cmd = [claude, "-p", prompt, "--output-format", "json"]

    env = _child_env()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_REPO_ROOT), env=env
        )
    except subprocess.TimeoutExpired:
        raise ChatError("Claude took too long to write the summary (timed out).")
    if proc.returncode != 0:
        raise ChatError(_friendly_error(proc.stderr or proc.stdout))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ChatError(_friendly_error(proc.stdout))
    answer = (data.get("result") or "").strip()
    if _is_login_response(data, answer):
        raise ChatError(_LOGIN_HINT)
    if data.get("is_error") or data.get("subtype") not in (None, "success") or not answer:
        raise ChatError(_friendly_error(answer or proc.stdout))
    return answer


def ask(
    question: str,
    *,
    fen: str | None = None,
    last_move: str | None = None,
    move_fen: str | None = None,
    session_id: str | None = None,
    use_profile: bool = False,
    timeout: int = 120,
) -> dict:
    """Ask headless Claude a question about a position. Returns {answer, session_id}.

    `fen` is the board the user is viewing (for "what should I do here?"); `last_move`/`move_fen`
    are the move in question and the position it was played from (for "why is this bad?"). When the
    move is the one available at the current board they coincide and we analyse once.

    `use_profile` opts the question into personalised coaching: the current player's cross-game
    history profile is injected into the prompt. Off by the caller to save tokens.

    Raises ChatError (with a friendly message) on any failure.
    """
    # The move is "at the current board" when it has no separate origin position (timeline node).
    move_at_current = bool(last_move) and (not move_fen or move_fen == fen)
    current_facts = _engine_facts(fen, last_move if move_at_current else None)
    move_facts = (
        _engine_facts(move_fen, last_move) if (last_move and not move_at_current and move_fen) else None
    )
    profile_facts = _profile_facts() if use_profile else None
    speed_context = _speed_context()
    prompt = _compose_prompt(
        question, fen, last_move, move_fen, current_facts, move_facts, profile_facts,
        speed_context,
    )

    # Local LLM: answer over direct HTTP, no `claude` CLI. The prompt already embeds every engine
    # fact, so no tools are needed (local models are unreliable at tool-calling anyway).
    if local_llm.is_enabled():
        try:
            return local_llm.chat(prompt, session_id=session_id)
        except local_llm.LocalLLMError as exc:
            raise ChatError(str(exc))

    claude = shutil.which("claude")
    if not claude:
        raise ChatError(
            "The `claude` CLI isn't on PATH, so in-browser chat is unavailable. Use the Claude "
            "Code terminal to ask 'why?' instead, or set a local AI model in Settings."
        )

    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
    ]
    # Pre-approve the chess MCP tools so Claude can fetch deeper/alternative lines the embedded
    # facts don't cover.
    cmd += ["--mcp-config", str(_MCP_CONFIG), "--allowedTools", _ALLOWED_TOOLS]
    if session_id:
        cmd += ["--resume", session_id]

    env = _child_env()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_REPO_ROOT), env=env
        )
    except subprocess.TimeoutExpired:
        raise ChatError("Claude took too long to respond (timed out). Try again or use the terminal.")

    if proc.returncode != 0:
        raise ChatError(_friendly_error(proc.stderr or proc.stdout))

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ChatError(_friendly_error(proc.stdout))

    answer = data.get("result") or ""
    if _is_login_response(data, answer):
        raise ChatError(_LOGIN_HINT)
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ChatError(_friendly_error(answer or proc.stdout))

    return {"answer": answer, "session_id": data.get("session_id")}
