"""MCP server exposing the chess-review brains to Claude Code.

Tools:
  - analyze_game(pgn, player)      -> game summary + populates the shared ReviewSession
  - get_engine_line(fen, move, ..) -> grounded engine line / refutation for follow-ups
  - goto_mistake(index)            -> anchor terminal narration to a specific mistake

Run as the MCP stdio server:
    /opt/miniconda3/envs/chess-review/bin/python -m server.mcp_server
"""
from __future__ import annotations

import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from server import config
from server.core import engine
from server.core import history
from server.core import lichess
from server.core import lifecycle
from server.core import lines
from server.core.game_analysis import analyze_game as _analyze_game
from server.core import session as session_mod
from server.core import settings
from server.web import runner as web_runner

mcp = FastMCP("chess")


@mcp.tool()
def analyze_game(
    pgn: str,
    player: str = "auto",
    elo: Optional[int] = None,
    sensitivity: Optional[str] = None,
) -> dict:
    """Analyse a full game from PGN and find the player's mistakes.

    Mistake sensitivity adapts to skill: stronger players get smaller win%-drop cutoffs (subtler
    errors flagged) and a slightly deeper sweep. If `elo`/`sensitivity` are omitted, the reviewed
    side's Elo is read from the PGN (normalized for Lichess vs Chess.com, whose scales differ).

    Args:
        pgn: The game in PGN format (Lichess/Chess.com exports work; comments and
            variations are ignored).
        player: Which side to review: "white", "black", or "auto" (infer from headers).
        elo: Override the player's strength (normalized scale) instead of reading the PGN.
        sensitivity: Or a named preset: "casual", "default", "strong", or "master".

    Returns a summary with per-side accuracy and an ordered list of the player's
    inaccuracies/mistakes/blunders. Each mistake has an `index` usable with `goto_mistake`,
    and a `fen_before` usable with `get_engine_line`. `review_elo`/`thresholds` show the
    sensitivity used. The full result is stored in the shared session the web board reads.
    """
    lifecycle.touch()
    sess = _analyze_game(pgn, player=player, elo=elo, sensitivity=sensitivity)
    session_mod.set_session(sess)

    summary = session_mod.summarize_session(sess)
    board_url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    summary["board_url"] = board_url
    # Auto-open the board so a first-time user never depends on the URL being printed.
    web_runner.open_board_once()
    # Persist the game for personalised coaching. Best-effort: history must never break a review.
    if config.HISTORY_ENABLED:
        try:
            rec = history.record_game(sess)
            summary["player_id"] = rec.get("player_id")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[chess-history] could not record game: {exc}", file=sys.stderr, flush=True)
    if sess.review_elo is not None:
        t = sess.thresholds or []
        sens = (
            f" Tuned to ~{round(sess.review_elo)} Elo ({sess.elo_source}); a move is flagged from "
            f"a {t[0] if t else 5}% win-chance drop."
        )
    else:
        sens = " Using default sensitivity (5/10/15% drops); no Elo found in the PGN."
    speed = summary.get("speed")
    mode = (
        f" This was a {speed} game — weigh the mistakes against {speed}-appropriate expectations "
        "(faster modes are more forgiving)."
        if speed and speed != "unknown"
        else ""
    )
    summary["note"] = (
        f"The interactive board has been opened in the browser at {board_url} — always "
        f"show this clickable link to the user on its own line so they can reopen it. "
        f"Replay each mistake and try alternatives there, or ask 'why was move N bad?' "
        f"here and I'll use get_engine_line.{sens}{mode}"
    )
    return summary


@mcp.tool()
def fetch_games(
    username: str = "me",
    max: int = config.LICHESS_DEFAULT_MAX,
    rated: Optional[bool] = None,
    perf: Optional[str] = None,
    color: Optional[str] = None,
    since_days: Optional[int] = None,
) -> dict:
    """Fetch a Lichess user's recent games (newest first) so the user doesn't paste PGNs.

    Returns a list of games with `game_id`, players, ratings, `result`, `speed`, `opening`,
    `date`, and the full `pgn`. Show the user the list and let them pick one, then call
    `analyze_game` with the chosen game's `pgn`. Public games need no auth; heavy users can set
    LICHESS_TOKEN to avoid IP rate limits.

    Args:
        username: Lichess handle. Defaults to "me" / empty -> the configured CHESS_USERNAME,
            so "analyze my recent games" works without typing a name.
        max: How many recent games to fetch (default 3).
        rated: True = rated only, False = casual only, None = both.
        perf: Comma-separated speed filter, e.g. "blitz,rapid" (bullet/blitz/rapid/classical).
        color: "white" or "black" to only return games the user played that color.
        since_days: Only games from the last N days.
    """
    lifecycle.touch()
    try:
        games = lichess.fetch_user_games(
            username, max=max, rated=rated, perf=perf, color=color, since_days=since_days
        )
    except lichess.LichessError as exc:
        return {"error": str(exc)}
    return {"count": len(games), "games": [g.to_dict() for g in games]}


@mcp.tool()
def fetch_game(game_id: str) -> dict:
    """Fetch one Lichess game by its id or URL; returns its `pgn` (+ metadata) for analyze_game.

    Accepts a bare game id ("abcd1234") or a full URL (e.g. https://lichess.org/abcd1234/black).
    Hand the returned `pgn` to `analyze_game` to review it.
    """
    lifecycle.touch()
    try:
        return lichess.fetch_game(game_id).to_dict()
    except lichess.LichessError as exc:
        return {"error": str(exc)}


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
    lifecycle.touch()
    return lines.engine_line(fen, move, depth, multipv)


@mcp.tool()
def goto_mistake(index: int) -> dict:
    """Move the review cursor to mistake #index and return the position before it.

    Use the `index` values from `analyze_game`'s mistake list. Returns the FEN one move
    before the mistake so narration (and the web board) stays in sync.
    """
    lifecycle.touch()
    return session_mod.goto_core(index)


@mcp.tool()
def get_player_profile(player_id: Optional[str] = None) -> dict:
    """Return a player's saved coaching profile: recurring patterns across all analysed games.

    Aggregates the persisted game history into accuracy, win/loss/draw counts, mistake rates,
    the most common mistake *motifs* (e.g. hung_piece, pawn_grab, missed_capture), which game
    phase leaks the most win%, per-opening results, and the most recent games. Use this to give
    personalised, trend-aware coaching ("you keep hanging pieces in the endgame") instead of
    judging a single game in isolation.

    Args:
        player_id: Whose profile to load. Omit to use the player from the most recently
            analysed game. One person's several lichess/chess.com accounts are folded into a
            single profile via the identities.json alias map. With no history yet, the result
            includes `known_players` you can pick from.
    """
    lifecycle.touch()
    return history.get_profile(player_id)


def main() -> None:
    settings.apply_saved()  # settings.json (set via the app's Settings panel) overrides env config
    lifecycle.start_watchdog()  # self-terminate after CHESS_SESSION_TTL of inactivity
    if config.WEB_AUTOSTART:
        web_runner.start_in_thread()
    try:
        mcp.run()
    finally:
        engine.shutdown()


if __name__ == "__main__":
    main()
