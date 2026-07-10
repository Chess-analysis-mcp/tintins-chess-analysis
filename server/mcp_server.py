"""MCP server exposing the chess-review brains to Claude Code.

Tools:
  - analyze_game(pgn, player)      -> game summary + populates the shared ReviewSession
  - get_engine_line(fen, move, ..) -> grounded engine line / refutation for follow-ups
  - goto_mistake(index)            -> anchor terminal narration to a specific mistake

Run as the MCP stdio server:
    /opt/miniconda3/envs/chess-review/bin/python -m server.mcp_server
"""
from __future__ import annotations

import re
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

from server import claude_bridge
from server import config
from server.core import analysis_cache
from server.core import engine
from server.core import history
from server.core import lichess
from server.core import lifecycle
from server.core import lines
from server.core.game_analysis import analyze_game as _analyze_game
from server.core import puzzle_flow
from server.core import puzzle_mistakes
from server.core import puzzle_rating
from server.core import puzzle_session
from server.core import puzzles as puzzles_mod
from server.core import session as session_mod
from server.core import settings
from server.web import runner as web_runner
from server.web.routes_puzzles import _has_engine

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
    analysis_cache.store(sess)  # so reopening this game on the board is instant

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


@mcp.tool()
def next_puzzle(
    theme: Optional[str] = None,
    difficulty: Optional[str] = None,
    weakness: bool = False,
    source: str = "lichess",
) -> dict:
    """Serve the next tactics puzzle to solve, near the user's puzzle rating.

    Terminal parity with the web board: this sets the SAME shared puzzle session the board uses, so
    you can drive a solve from Claude Code and it stays in sync. Present the returned position to the
    user (side to move + "find the best move"), let them find a move, then call `solve_puzzle`. The
    solution is deliberately NOT returned.

    Args:
        theme: Optional theme filter (e.g. "fork", "pin", "backRankMate"); comma-separated for
            several. Ignored when `weakness` is set.
        difficulty: "easier" or "harder" to nudge the target rating one band from yours.
        weakness: Bias selection toward the user's weak themes (from game history + puzzle stats).
        source: "lichess" (curated tactics, rated) or "your_games" (a position you blundered in a
            past game, engine-validated + unrated; needs Stockfish).

    Returns the position (`fen` the solver faces, `side_to_move`), `themes`, the puzzle `rating`
    (curated only), your `your_rating`, and an `id`. Solve it with `solve_puzzle`.
    """
    lifecycle.touch()
    if not config.PUZZLES_ENABLED:
        return {"error": "Puzzle mode is disabled (CHESS_PUZZLES=0)."}
    state = puzzle_rating.load_state()

    if source == "your_games":
        if not _has_engine():
            return {"error": "Mistake puzzles need Stockfish to check your moves."}
        puzzle = puzzle_mistakes.next_mistake_puzzle(state)
        if not puzzle:
            return {"error": "No mistake puzzles yet - analyse some of your games first."}
        puzzle_session.set_current(puzzle)
        puzzle_mistakes.mark_mistake_served(state, puzzle["key"])
        puzzle_rating.save_state(state)
        opp = puzzle.get("black") if puzzle.get("reviewed_side") == "white" else puzzle.get("white")
        return {
            "id": puzzle["id"],
            "source": "your_games",
            "fen": puzzle["fen"],
            "side_to_move": puzzle.get("side_to_move", "white"),
            "motifs": puzzle.get("motifs", []),
            "your_rating": int(round(state["rating"])),
            "unrated": True,
            "from_game": {
                "vs": opp, "speed": puzzle.get("speed"), "date": puzzle.get("date"),
                "game_url": puzzle.get("game_url"),
            },
            "note": (
                "This is a position the user themselves went wrong in, replayed as UNRATED practice. "
                "Show the FEN / side to move and ask for their move, then call solve_puzzle. Any move "
                "the engine rates close to best is accepted (several may pass)."
            ),
        }

    themes = None
    if weakness:
        themes = puzzles_mod.weakness_themes(state) or None
    elif theme:
        themes = [t.strip() for t in theme.split(",") if t.strip()] or None
    puzzle = puzzles_mod.next_puzzle(
        state["rating"],
        themes=themes,
        exclude=set(state.get("seen_ids", [])),
        seed=state.get("user_seed"),
        difficulty=difficulty,
        rd=state.get("rd"),
    )
    if not puzzle:
        return {"error": "No puzzles available."}
    puzzle_rating.mark_seen(state, puzzle["id"])
    puzzle_rating.save_state(state)
    puzzle_session.set_current(puzzle)
    return {
        "id": puzzle["id"],
        "source": "lichess",
        "fen": puzzle.get("solve_fen") or puzzle["fen"],  # after the auto-played setup move
        "side_to_move": puzzle.get("side_to_move", "white"),
        "themes": puzzle.get("themes", []),
        "rating": int(round(float(puzzle.get("rating", 1500)))),
        "your_rating": int(round(state["rating"])),
        "trained_themes": themes or [],
        "note": (
            "Show the user the side to move and ask them to find the best move (do NOT reveal the "
            "solution - it isn't provided). Pass their move(s) to solve_puzzle (UCI or SAN). For a "
            "multi-move puzzle the opponent's replies are forced and played automatically."
        ),
    }


@mcp.tool()
def solve_puzzle(moves: str, explain: bool = True) -> dict:
    """Submit the user's solution move(s) for the puzzle currently loaded by `next_puzzle`.

    Drives the SAME shared puzzle session as the web board. Accepts UCI ("g1f3") or SAN ("Nf3"),
    one move or a whole line (space/comma separated) — for a multi-move puzzle the opponent's forced
    replies are auto-played between the user's moves. A wrong move fails the puzzle (Lichess-style)
    and, for curated puzzles, updates the Glicko rating once.

    Args:
        moves: The user's move(s), UCI or SAN, e.g. "Nf3" or "g1f3 f1c4".
        explain: When true (default), also return an engine-grounded facts block (themes, the
            verified solution line, and a verdict on each move the user tried) so you can explain
            WHY the solution works / why their move failed — narrate it yourself, don't re-derive.

    Returns `{correct, is_complete, ...}`: on completion, the `rating` delta (curated) and, when
    `explain`, a `coach_facts` string to ground your explanation. If the puzzle isn't finished
    (a correct but non-final move), call `solve_puzzle` again with the next move.
    """
    lifecycle.touch()
    if not config.PUZZLES_ENABLED:
        return {"error": "Puzzle mode is disabled (CHESS_PUZZLES=0)."}
    prog = puzzle_session.get_current()
    if prog is None:
        return {"error": "No active puzzle - call next_puzzle first."}
    if prog.finished:
        return {"error": "This puzzle is already finished - call next_puzzle for another."}

    tokens = [t for t in re.split(r"[,\s]+", moves.strip()) if t]
    if not tokens:
        return {"error": "No move given."}

    is_mistake = prog.puzzle.get("source") == "your_games"
    if is_mistake:
        # Mistake puzzles are single-move, eval-threshold, unrated.
        out = puzzle_flow.apply_mistake_move(prog, tokens[0])
    else:
        out = puzzle_flow.apply_solver_moves(prog, tokens)

    result: dict = dict(out)
    solved = bool(out.get("is_complete")) and not out.get("failed")
    if out.get("is_complete") or out.get("failed"):
        result["outcome"] = _solve_outcome(prog, solved)
        if explain:
            result["coach_facts"] = _coach_facts(prog, result["outcome"], is_mistake)
            result["note"] = (
                "Explain the result to the user grounded in coach_facts: name the motif on a solve, "
                "or refute their move then teach the idea on a miss. Then offer next_puzzle."
            )
    elif out.get("correct"):
        result["note"] = "Correct so far - ask for the next move and call solve_puzzle again."
    return result


def _solve_outcome(prog, solved: bool) -> str:
    """Map the finished attempt onto the coach's outcome vocabulary."""
    if not solved:
        return "failed"
    return "solved_with_hints" if prog.hints_used > 0 else "solved_first_try"


def _coach_facts(prog, outcome: str, is_mistake: bool) -> str:
    """The engine-grounded facts block for the caller to narrate. Best-effort -> '' on any error."""
    try:
        if is_mistake:
            return claude_bridge._mistake_facts(prog.puzzle, prog.tried)
        return claude_bridge._puzzle_facts(prog.puzzle, outcome, tried=prog.tried)
    except Exception:  # pragma: no cover - facts are optional, never break the tool
        return ""


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
