"""MCP server exposing the chess-review brains to Claude Code.

Tools:
  - analyze_game(pgn, player)      -> game summary + populates the shared ReviewSession
  - get_engine_line(fen, move, ..) -> grounded engine line / refutation for follow-ups
  - goto_mistake(index)            -> anchor terminal narration to a specific mistake

Run as the MCP stdio server:
    /opt/miniconda3/envs/chess-review/bin/python -m server.mcp_server
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from server import config
from server.core import engine
from server.core import lines
from server.core.game_analysis import analyze_game as _analyze_game
from server.core import session as session_mod
from server.web import runner as web_runner

mcp = FastMCP("chess")


@mcp.tool()
def analyze_game(pgn: str, player: str = "auto") -> dict:
    """Analyse a full game from PGN and find the player's mistakes.

    Args:
        pgn: The game in PGN format (Lichess/Chess.com exports work; comments and
            variations are ignored).
        player: Which side to review: "white", "black", or "auto" (infer from headers).

    Returns a summary with per-side accuracy and an ordered list of the player's
    inaccuracies/mistakes/blunders. Each mistake has an `index` usable with
    `goto_mistake`, and a `fen_before` usable with `get_engine_line` for follow-ups.
    The full result is stored in a shared session that the (future) web board reads.
    """
    sess = _analyze_game(pgn, player=player)
    session_mod.set_session(sess)

    summary = session_mod.summarize_session(sess)
    board_url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    summary["board_url"] = board_url
    summary["note"] = (
        f"Open the interactive board at {board_url} to replay each mistake and try "
        "alternatives. Or ask 'why was move N bad?' here and I'll use get_engine_line."
    )
    return summary


@mcp.tool()
def get_engine_line(
    fen: str,
    move: Optional[str] = None,
    depth: int = config.DEFAULT_DEPTH,
    multipv: int = 1,
) -> dict:
    """Evaluate a position (optionally after a candidate move) and return engine lines.

    This is the grounding for "why?" follow-ups. Without `move`, it returns the best
    move and principal variation for `fen`. With `move` (UCI like "g1f3" or SAN like
    "Nf3"), it also returns how that move is classified and the engine's refutation /
    expected continuation after it — i.e. concretely *why* it is good or bad.

    Args:
        fen: Position in FEN.
        move: Optional candidate move to evaluate (UCI or SAN).
        depth: Search depth (fixed for reproducibility). Defaults to 18.
        multipv: Number of alternative lines to return for `fen`.
    """
    return lines.engine_line(fen, move, depth, multipv)


@mcp.tool()
def goto_mistake(index: int) -> dict:
    """Move the review cursor to mistake #index and return the position before it.

    Use the `index` values from `analyze_game`'s mistake list. Returns the FEN one move
    before the mistake so narration (and the web board) stays in sync.
    """
    return session_mod.goto_core(index)


def main() -> None:
    if config.WEB_AUTOSTART:
        web_runner.start_in_thread()
    try:
        mcp.run()
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
