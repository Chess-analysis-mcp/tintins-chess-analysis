"""Single-position engine analysis, shared by the MCP `get_engine_line` tool and the
web `/api/evaluate` route.

Keeping this in `core` (rather than inline in `mcp_server`) is what guarantees the
terminal and the web board never disagree: both call `engine_line` with the same args
and get the same dict back.
"""
from __future__ import annotations

from typing import Optional

import chess

from server import config
from server.core import engine
from server.core.evaluation import classify
from server.core.game_analysis import _signed_cp


def eval_str(cp: int | None, mate: int | None) -> str:
    """Human-readable eval from the side-to-move perspective."""
    if mate is not None:
        return f"#{mate}" if mate > 0 else f"#-{abs(mate)}"
    pawns = (cp or 0) / 100.0
    return f"{pawns:+.2f}"


def eval_str_from_signed_cp(cp: int) -> str:
    """Like eval_str but takes a signed cp that may be a mate-equivalent magnitude."""
    if abs(cp) >= config.MATE_SCORE_CP:
        return "#" if cp > 0 else "#-"
    return f"{cp / 100.0:+.2f}"


def pv_to_san(board: chess.Board, pv_uci: list[str], max_plies: int = 12) -> list[str]:
    b = board.copy(stack=False)
    out: list[str] = []
    for uci in pv_uci[:max_plies]:
        try:
            mv = chess.Move.from_uci(uci)
            out.append(b.san(mv))
            b.push(mv)
        except (ValueError, AssertionError):
            break
    return out


def parse_move(board: chess.Board, move: str) -> chess.Move:
    """Accept either UCI (e2e4) or SAN (e4, Nf3) for the move argument."""
    try:
        mv = chess.Move.from_uci(move)
        if mv in board.legal_moves:
            return mv
    except ValueError:
        pass
    return board.parse_san(move)  # raises ValueError if illegal/ambiguous


def engine_line(
    fen: str,
    move: Optional[str] = None,
    depth: int = config.DEFAULT_DEPTH,
    multipv: int = 1,
) -> dict:
    """Evaluate a position (optionally after a candidate move) and return engine lines.

    Without `move`, returns the best move and principal variation for `fen`. With `move`
    (UCI like "g1f3" or SAN like "Nf3"), also returns how that move is classified and the
    engine's refutation / expected continuation after it.
    """
    board = chess.Board(fen)
    base = engine.analyse(fen, depth=depth, multipv=max(1, multipv))
    best = base.best
    best_line_san = pv_to_san(board, best.pv_uci)

    result: dict = {
        "fen": fen,
        "side_to_move": "white" if board.turn == chess.WHITE else "black",
        "depth": depth,
        "eval": eval_str(best.cp, best.mate),
        "eval_cp": round(_signed_cp(best.cp, best.mate)),
        "win_percent": round(best.win_percent, 1),
        "best_san": best_line_san[0] if best_line_san else None,
        "line_san": best_line_san,
        "line_uci": best.pv_uci[:12],
        "shapes": [],  # board annotations: deferred to Phase 7
    }

    if multipv > 1:
        result["lines"] = [
            {
                "eval": eval_str(ln.cp, ln.mate),
                "win_percent": round(ln.win_percent, 1),
                "line_san": pv_to_san(board, ln.pv_uci),
                "line_uci": ln.pv_uci[:12],
            }
            for ln in base.lines
        ]

    if move:
        try:
            mv = parse_move(board, move)
        except ValueError as exc:
            result["error"] = f"Illegal or unparseable move '{move}': {exc}"
            return result

        move_san = board.san(mv)
        win_before = best.win_percent  # best available for the mover
        after_board = board.copy(stack=False)
        after_board.push(mv)

        if after_board.is_game_over(claim_draw=True):
            outcome = after_board.outcome(claim_draw=True)
            if outcome and outcome.winner is None:
                win_after = 50.0
            elif outcome and outcome.winner == board.turn:
                win_after = 100.0  # the mover delivered mate
            else:
                win_after = 0.0
            refutation_san: list[str] = []
            refutation_uci: list[str] = []
            after_eval_cp = 0 if (outcome and outcome.winner is None) else (
                config.MATE_SCORE_CP if (outcome and outcome.winner == board.turn) else -config.MATE_SCORE_CP
            )
        else:
            after = engine.analyse(after_board.fen(), depth=depth, multipv=1).best
            win_after = 100.0 - after.win_percent  # back to the mover's perspective
            refutation_uci = after.pv_uci[:12]
            refutation_san = pv_to_san(after_board, after.pv_uci)
            after_eval_cp = -round(_signed_cp(after.cp, after.mate))

        # Board annotation (Phase 7): draw the punishing reply as a red arrow so the board
        # shows *why* the move is bad, not just the prose.
        if refutation_uci:
            fr = refutation_uci[0]
            result["shapes"] = [{"orig": fr[:2], "dest": fr[2:4], "brush": "red"}]

        is_best = best.pv_uci and mv.uci() == best.pv_uci[0]
        result["move"] = {
            "move_san": move_san,
            "move_uci": mv.uci(),
            "classification": classify(win_before, win_after, is_best=bool(is_best)),
            "win_before": round(win_before, 1),
            "win_after": round(win_after, 1),
            "win_swing": round(win_before - win_after, 1),
            "eval_after_cp": after_eval_cp,
            "eval_after": eval_str_from_signed_cp(after_eval_cp),
            "is_engine_best": bool(is_best),
            "better_move_san": result["best_san"],
            "refutation_line_san": refutation_san,
            "refutation_line_uci": refutation_uci,
        }

    return result
