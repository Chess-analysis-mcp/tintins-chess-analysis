"""Process-wide review session state shared between the MCP tools and (later) the web layer.

The MCP `analyze_game` tool *writes* the session; `goto_mistake` mutates `current_index`;
the future FastAPI board will *read* it. Keeping this a single in-memory singleton is the
explicit design choice from the plan (one process, one session)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from server.core.evaluation import Classification


class MoveReview(BaseModel):
    """Full review of a single one of *my* moves."""

    ply: int  # 1-based half-move number in the game
    move_number: int  # full-move number (e.g. 4 for "4. Nf3")
    color: str  # "white" | "black" (whose move this is)
    move_san: str
    move_uci: str
    fen_before: str
    fen_after: str
    eval_before: float  # centipawns from my perspective (best available), mate -> +/-MATE
    eval_after: float  # centipawns from my perspective after my move
    win_before: float  # win% from my perspective (best available)
    win_after: float  # win% from my perspective after my move
    win_swing: float  # win_before - win_after (>=0 means I lost ground)
    classification: Classification
    best_move_san: str
    best_line_uci: list[str] = Field(default_factory=list)
    best_line_san: list[str] = Field(default_factory=list)
    accuracy: float
    comment: str = ""  # engine-grounded prose explanation (mistakes only); no LLM/extra engine cost
    # Clock context (seconds), parsed from PGN [%clk] when present — both None if the PGN has no
    # clocks. clock_after = my remaining time after this move; opp_clock = opponent's remaining
    # at their previous move. Powers the time-trouble motif.
    clock_after: Optional[float] = None
    opp_clock: Optional[float] = None


class ReviewSession(BaseModel):
    """Everything about one analysed game."""

    pgn: str
    player: str  # "white" | "black" — whose mistakes we reviewed
    headers: dict[str, str] = Field(default_factory=dict)
    result: str = "*"
    # Game speed bucket (bullet/blitz/rapid/classical/correspondence/unknown), from the
    # TimeControl header. Lets coaching apply mode-appropriate expectations.
    speed: str = "unknown"
    accuracy_white: float = 100.0
    accuracy_black: float = 100.0
    all_moves: list[MoveReview] = Field(default_factory=list)  # every move by `player`
    mistakes: list[MoveReview] = Field(default_factory=list)  # inaccuracy/mistake/blunder
    current_index: int = 0  # index into `mistakes`
    explore_fen: Optional[str] = None
    # Skill-adaptive review: the Elo we tuned the mistake thresholds to (normalized scale),
    # where it came from, the resulting (inaccuracy, mistake, blunder) win%-drop cutoffs, and
    # the sweep depth used. review_elo None -> default 5/10/15 thresholds.
    review_elo: Optional[float] = None
    elo_source: Optional[str] = None
    thresholds: Optional[list[float]] = None
    sweep_depth: Optional[int] = None
    # Per-node timeline of the whole game (both sides): one entry per position from the
    # start (node 0) to the final position. Powers the win graph, arrow-key navigation,
    # and the move/best arrows on the board. Each entry is a plain dict (see build_timeline).
    timeline: list[dict] = Field(default_factory=list)


# Module-level singleton.
_SESSION: Optional[ReviewSession] = None


def set_session(session: ReviewSession) -> None:
    global _SESSION
    _SESSION = session


def get_session() -> Optional[ReviewSession]:
    return _SESSION


def clear_session() -> None:
    global _SESSION
    _SESSION = None


def summarize_session(sess: ReviewSession) -> dict:
    """Compact, JSON-friendly summary of a session.

    Shared by the MCP `analyze_game` tool and the web `GET /api/session` route so both
    surfaces present an identical mistake list.
    """
    mistakes = [
        {
            "index": i,
            "ply": m.ply,
            "move_number": m.move_number,
            "color": m.color,
            "move_san": m.move_san,
            "classification": m.classification,
            "win_swing": m.win_swing,
            "eval_before": round(m.eval_before / 100.0, 2),
            "eval_after": round(m.eval_after / 100.0, 2),
            "best_move_san": m.best_move_san,
            "fen_before": m.fen_before,
            "move_uci": m.move_uci,
            "comment": m.comment,
            "node_index": m.ply - 1,  # the timeline node whose outgoing move is this mistake
        }
        for i, m in enumerate(sess.mistakes)
    ]
    return {
        "result": sess.result,
        "player": sess.player,
        "white": sess.headers.get("White", "?"),
        "black": sess.headers.get("Black", "?"),
        "opening": sess.headers.get("Opening", sess.headers.get("ECO", "")),
        "speed": sess.speed,
        "time_control": sess.headers.get("TimeControl") or None,
        "accuracy_white": sess.accuracy_white,
        "accuracy_black": sess.accuracy_black,
        "num_my_moves": len(sess.all_moves),
        "num_mistakes": len(sess.mistakes),
        "mistakes": mistakes,
        "current_index": sess.current_index,
        "review_elo": sess.review_elo,
        "elo_source": sess.elo_source,
        "thresholds": sess.thresholds,
        "sweep_depth": sess.sweep_depth,
    }


def goto_core(index: int) -> dict:
    """Move the review cursor to mistake `index` and return the position before it.

    Shared by the MCP `goto_mistake` tool and the web `GET /api/position/{index}` route.
    Returns an `error` key (rather than raising) so both surfaces handle it uniformly.
    """
    sess = get_session()
    if sess is None:
        return {"error": "No game analysed yet. Call analyze_game first."}
    if not sess.mistakes:
        return {"error": "The analysed game has no flagged mistakes."}
    if index < 0 or index >= len(sess.mistakes):
        return {"error": f"index out of range 0..{len(sess.mistakes) - 1}"}

    sess.current_index = index
    sess.explore_fen = None
    m = sess.mistakes[index]
    prompt = (
        f"Move {m.move_number} ({m.color}): you played {m.move_san} "
        f"({m.classification}, lost {m.win_swing}% win chance). "
        f"It's {m.color} to move — find something better."
    )
    return {
        "index": index,
        "ply": m.ply,
        "move_number": m.move_number,
        "color": m.color,
        "fen": m.fen_before,
        "move_played_san": m.move_san,
        "classification": m.classification,
        "best_move_san": m.best_move_san,
        "best_line_san": m.best_line_san,
        "prompt": prompt,
    }
