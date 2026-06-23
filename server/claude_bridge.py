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

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_CONFIG = _REPO_ROOT / ".mcp.json"
_ALLOWED_TOOLS = "mcp__chess__get_engine_line,mcp__chess__analyze_game"

# How many candidate moves to pre-compute, and how close (in win%-points) an alternative
# must be to the best move to count as "also good" — so Claude can offer the more human,
# intuitive option instead of insisting on the single engine-best move.
_FACTS_MULTIPV = 3
_ALT_WIN_GAP = 5.0

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


def _child_env() -> dict:
    """Environment for the spawned `claude` so it uses the user's subscription login.

    Drops `CHESS_WEB_AUTOSTART` so the child doesn't rebind the board port, and strips
    *empty* `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` values. An empty key is never
    intentional but still makes the CLI attempt (and fail with a 401) API-key auth instead
    of falling back to the subscription login — a common Windows footgun.
    """
    env = {**os.environ, "CHESS_WEB_AUTOSTART": "0"}
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if key in env and not env[key].strip():
            env.pop(key)
    return env


def _engine_facts(fen: str | None, move: str | None) -> str | None:
    """Pre-compute the engine's verdict for this position/move so Claude never has to guess.

    Uses the same cached `engine_line` path as the board, so this is fast and consistent.
    """
    if not fen:
        return None
    try:
        info = lines.engine_line(fen, move=move, multipv=_FACTS_MULTIPV)
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
    return "\n".join(out) if out else None


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
        "natural one for a club player) rather than insisting on the single engine-top move. You may "
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


def _game_facts(sess) -> str:
    """Pre-computed, engine-grounded facts about the whole game for the coach summary prompt.

    Everything here already exists on the session (accuracy, the flagged moves + their templated
    comments, the player's profile), so the Claude call only has to write — it never analyses.
    """
    side = "White" if sess.player == "white" else "Black"
    acc = sess.accuracy_white if sess.player == "white" else sess.accuracy_black
    opening = session_mod.resolve_opening(sess) or "unknown opening"
    out = [
        f"Game: {sess.headers.get('White', '?')} vs {sess.headers.get('Black', '?')} "
        f"({sess.result}); {opening}; {sess.speed} time control.",
        f"Reviewing {side}. Accuracy: {acc}% (opponent "
        f"{sess.accuracy_black if sess.player == 'white' else sess.accuracy_white}%).",
    ]
    if sess.mistakes:
        out.append(f"{side}'s flagged moves (worst first):")
        worst = sorted(sess.mistakes, key=lambda m: m.win_swing, reverse=True)
        for m in worst[:8]:
            num = f"{m.move_number}{'.' if m.color == 'white' else '...'}"
            out.append(
                f"- {num}{m.move_san} ({m.classification}, win {m.win_before}% -> {m.win_after}%, "
                f"drop {m.win_swing}); engine preferred {m.best_move_san}. {m.comment}".rstrip()
            )
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
        "the one or two moments that mattered most (with the move and the better idea), draw out the "
        "underlying habit or theme, and end with one concrete thing to work on. Ground every claim "
        "in the facts provided; do not invent moves or lines. Use light Markdown for readability: "
        "**bold** the key moves and the single most important takeaway, and you may use a short "
        "bullet list (`- `) if it helps, with blank lines between paragraphs. No headings, and no "
        "move-by-move recap. Do NOT mention the web board, any URL, Stockfish, or these instructions.",
    ]
    if profile_facts:
        prompt_parts.append(
            "The player's cross-game history is below — use it to point out a recurring pattern only "
            "when this game genuinely shows one; otherwise ignore it.\n" + profile_facts
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
        "--mcp-config",
        str(_MCP_CONFIG),
        "--allowedTools",
        _ALLOWED_TOOLS,
    ]
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
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ChatError(_friendly_error(answer or proc.stdout))

    return {"answer": answer, "session_id": data.get("session_id")}
