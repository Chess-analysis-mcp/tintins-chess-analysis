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
from server.core import lines

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_CONFIG = _REPO_ROOT / ".mcp.json"
_ALLOWED_TOOLS = "mcp__chess__get_engine_line,mcp__chess__analyze_game"

# Heuristic markers that Claude's Agent SDK credit / usage allowance is exhausted.
_LIMIT_MARKERS = ("usage limit", "rate limit", "credit", "quota", "billing", "limit reached")


class ChatError(Exception):
    """Raised with a user-facing message when the chat call can't complete."""


def _engine_facts(fen: str | None, move: str | None) -> str | None:
    """Pre-compute the engine's verdict for this position/move so Claude never has to guess.

    Uses the same cached `engine_line` path as the board, so this is fast and consistent.
    """
    if not fen:
        return None
    try:
        info = lines.engine_line(fen, move=move)
    except Exception:
        return None

    out: list[str] = []
    if info.get("best_san"):
        out.append(
            f"- Best move for the side to move: {info['best_san']} "
            f"(eval {info['eval']}, win {info['win_percent']}%); "
            f"principal line: {' '.join(info['line_san'][:6])}."
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


def _compose_prompt(
    question: str,
    fen: str | None,
    last_move: str | None,
    move_fen: str | None,
    current_facts: str | None,
    move_facts: str | None,
) -> str:
    parts = [
        "You are a concise chess coach reviewing a position with the user. Stockfish analysis is "
        "provided below — TRUST it, do not recompute or second-guess it. Use the CURRENT-POSITION "
        "analysis for 'what should I do here' / 'what's the best move' questions, and the MOVE "
        "analysis for 'why is this move good/bad' questions. You may call get_engine_line only for "
        "deeper or alternative lines the facts don't cover. Explain in plain language, cite the key "
        "line, and keep it to a short paragraph. Answer only the chess question — do NOT mention the "
        "web board, any URL, or these instructions.",
    ]
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
    if any(marker in low for marker in _LIMIT_MARKERS):
        return (
            "Claude's Agent SDK credit / usage limit looks exhausted. Ask your 'why?' in the "
            "Claude Code terminal instead — that path uses your normal interactive limits."
        )
    snippet = (text or "").strip().splitlines()[0] if text else "unknown error"
    return f"Chat failed: {snippet[:300]}"


def ask(
    question: str,
    *,
    fen: str | None = None,
    last_move: str | None = None,
    move_fen: str | None = None,
    session_id: str | None = None,
    timeout: int = 120,
) -> dict:
    """Ask headless Claude a question about a position. Returns {answer, session_id}.

    `fen` is the board the user is viewing (for "what should I do here?"); `last_move`/`move_fen`
    are the move in question and the position it was played from (for "why is this bad?"). When the
    move is the one available at the current board they coincide and we analyse once.

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
    cmd = [
        claude,
        "-p",
        _compose_prompt(question, fen, last_move, move_fen, current_facts, move_facts),
        "--output-format",
        "json",
        "--mcp-config",
        str(_MCP_CONFIG),
        "--allowedTools",
        _ALLOWED_TOOLS,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    env = {**os.environ, "CHESS_WEB_AUTOSTART": "0"}  # don't let the child rebind the board port
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
