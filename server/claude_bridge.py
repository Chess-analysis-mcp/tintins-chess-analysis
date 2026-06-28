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

from server import config
from server.core import history
from server.core import lines
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
    """Environment for the spawned `claude`.

    Sets `CHESS_WEB_AUTOSTART=0` so the child doesn't rebind the board port.

    Two routing modes:
    - **Local LLM** (`config.LOCAL_LLM_BASE_URL` set): point the child at a local/self-hosted
      model that speaks the Anthropic Messages API (Ollama native, LM Studio, llama.cpp, or a
      LiteLLM proxy) via `ANTHROPIC_BASE_URL`. The CLI still needs *some* auth token even though
      the local server ignores it, so we supply a dummy one if the env has neither. If
      `LOCAL_LLM_MODEL` is set we point every model tier the CLI might request at it.
    - **Subscription** (default): strip a stray/empty/stale `ANTHROPIC_API_KEY`, which headless
      `claude -p` would otherwise silently use and 401 on ("Invalid authentication credentials"),
      forcing the subscription login this feature is designed around.
    """
    env = {**os.environ, "CHESS_WEB_AUTOSTART": "0"}
    base_url = (config.LOCAL_LLM_BASE_URL or "").strip()
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
        if not env.get("ANTHROPIC_AUTH_TOKEN") and not env.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_AUTH_TOKEN"] = "local"
        model = (config.LOCAL_LLM_MODEL or "").strip()
        if model:
            for var in (
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
            ):
                env[var] = model
        return env
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


def coach_summary_ai(sess, *, timeout: int = 120) -> str:
    """A richer, Claude-WRITTEN end-of-game coaching summary, grounded in pre-computed facts.

    Opt-in (spends the user's Claude subscription). No MCP tools / engine calls — the prompt already
    carries every fact Claude needs, so it only has to phrase the coaching well. Raises ChatError.
    """
    claude = shutil.which("claude")
    if not claude:
        raise ChatError(
            "The `claude` CLI isn't on PATH, so the AI coach summary is unavailable. The free "
            "summary above still works; install the Claude CLI for the AI version."
        )
    profile_facts = _profile_facts()
    prompt_parts = [
        "You are an encouraging but honest chess coach writing a short end-of-game summary for the "
        "player whose moves are reviewed below. The Stockfish facts are authoritative — TRUST them, "
        "do not recompute. Write a few short paragraphs in warm, direct second person ('you'): name "
        "the one or two moments that mattered most IN THIS GAME (with the move and the better idea), "
        "and end with one concrete takeaway drawn from those specific moments. Ground every claim in "
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
    cmd = [claude, "-p", "\n\n".join(prompt_parts), "--output-format", "json"]

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
    claude = shutil.which("claude")
    if not claude:
        raise ChatError(
            "The `claude` CLI isn't on PATH, so in-browser chat is unavailable. Use the Claude "
            "Code terminal to ask 'why?' instead."
        )

    # The move is "at the current board" when it has no separate origin position (timeline node).
    move_at_current = bool(last_move) and (not move_fen or move_fen == fen)
    current_facts = _engine_facts(fen, last_move if move_at_current else None)
    move_facts = (
        _engine_facts(move_fen, last_move) if (last_move and not move_at_current and move_fen) else None
    )
    profile_facts = _profile_facts() if use_profile else None
    speed_context = _speed_context()
    cmd = [
        claude,
        "-p",
        _compose_prompt(
            question, fen, last_move, move_fen, current_facts, move_facts, profile_facts,
            speed_context,
        ),
        "--output-format",
        "json",
    ]
    # Local models are unreliable at tool-calling, and the prompt already embeds every engine
    # fact (`_engine_facts`), so skip the chess MCP tools when routed at a local LLM.
    if not (config.LOCAL_LLM_BASE_URL or "").strip():
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
