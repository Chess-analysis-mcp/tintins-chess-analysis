"""PGN -> ordered mistake list -> ReviewSession.

We analyse every position along the mainline exactly once (results are cached in the
engine pool), then derive each move's before/after win% from consecutive positions:

    win_before(my move at P)  = best win% at P            (I am to move at P)
    win_after (my move at P)  = 100 - best win% at P+1    (opponent is to move at P+1)

Terminal positions (checkmate/stalemate/draw) are scored directly without the engine.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

import chess
import chess.pgn

from server import config
from server.core import engine
from server.core.evaluation import (
    aggregate_accuracy,
    classify,
    classify_speed,
    move_accuracy,
    thresholds_for_elo,
    thresholds_for_speed,
    win_percent_from_score,
)
from server.core.session import MoveReview, ReviewSession


@dataclass
class _PosEval:
    """Evaluation of a single position, from the side-to-move's perspective."""

    win_stm: float  # win% for the side to move
    cp_stm: float  # signed centipawns for the side to move (mate -> +/-MATE_SCORE_CP)
    best_pv_uci: list[str]  # principal variation (empty if terminal)
    is_terminal: bool


def _signed_cp(cp: int | None, mate: int | None) -> float:
    if mate is not None:
        return float(config.MATE_SCORE_CP) if mate > 0 else float(-config.MATE_SCORE_CP)
    return float(cp if cp is not None else 0)


def _evaluate_position(board: chess.Board, *, depth: int) -> _PosEval:
    """Evaluate `board` from the side-to-move's perspective, handling terminal cases."""
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        if outcome.winner is None:  # draw of any kind
            return _PosEval(win_stm=50.0, cp_stm=0.0, best_pv_uci=[], is_terminal=True)
        # There is a winner; the side to move is the one who is checkmated -> losing.
        side_to_move_won = outcome.winner == board.turn
        win = 100.0 if side_to_move_won else 0.0
        cp = float(config.MATE_SCORE_CP) if side_to_move_won else float(-config.MATE_SCORE_CP)
        return _PosEval(win_stm=win, cp_stm=cp, best_pv_uci=[], is_terminal=True)

    res = engine.analyse(board.fen(), depth=depth, multipv=1)
    best = res.best
    return _PosEval(
        win_stm=win_percent_from_score(best.cp, best.mate),
        cp_stm=_signed_cp(best.cp, best.mate),
        best_pv_uci=list(best.pv_uci),
        is_terminal=False,
    )


def _pv_to_san(board: chess.Board, pv_uci: list[str], *, max_plies: int = 12) -> list[str]:
    """Convert a UCI principal variation to SAN by replaying on a copy of `board`."""
    b = board.copy(stack=False)
    sans: list[str] = []
    for uci in pv_uci[:max_plies]:
        try:
            move = chess.Move.from_uci(uci)
            sans.append(b.san(move))
            b.push(move)
        except (ValueError, AssertionError):
            break
    return sans


def resolve_player(headers: dict[str, str], player: str) -> str:
    """Resolve player='white'|'black'|'auto' to a concrete color."""
    p = (player or "auto").lower()
    if p in ("white", "black"):
        return p
    # auto: match any of my handles (CHESS_USERNAME + CHESS_ALIASES) against the PGN headers.
    mine = {config.USERNAME.lower().strip()} | {a for _, a in config.USERNAME_ALIASES}
    mine.discard("")
    if headers.get("White", "").lower().strip() in mine:
        return "white"
    if headers.get("Black", "").lower().strip() in mine:
        return "black"
    return "white"


# Lichess ratings run noticeably higher than chess.com / FIDE for the same player, so we pull
# them down to a common scale before mapping Elo -> thresholds. Rough and time-control-dependent;
# tune to taste. (chess.com is taken as the baseline at offset 0.)
_ELO_OFFSETS = {"lichess": -200, "chesscom": 0}

# Named sensitivity presets -> a representative normalized Elo.
_SENSITIVITY_ELO = {"casual": 1000.0, "default": 1500.0, "strong": 2000.0, "master": 2400.0}


def _detect_platform(headers: dict[str, str]) -> str | None:
    blob = " ".join(headers.get(k, "") for k in ("Site", "Link", "Event")).lower()
    if "lichess" in blob:
        return "lichess"
    if "chess.com" in blob or "chesscom" in blob:
        return "chesscom"
    return None


def _resolve_review_elo(
    headers: dict[str, str], me: str, elo: int | None, sensitivity: str | None
) -> tuple[float | None, str | None]:
    """Resolve the normalized review Elo + where it came from.

    Priority: explicit `elo` (taken as already-normalized) > named `sensitivity` > the PGN's
    WhiteElo/BlackElo for the reviewed side (normalized by detected platform) > None (default).
    """
    if elo is not None:
        return float(elo), "explicit"
    if sensitivity and sensitivity.lower() in _SENSITIVITY_ELO:
        return _SENSITIVITY_ELO[sensitivity.lower()], f"sensitivity:{sensitivity.lower()}"
    raw = headers.get("WhiteElo" if me == "white" else "BlackElo", "").strip()
    if raw.isdigit():
        platform = _detect_platform(headers)
        return float(int(raw) + _ELO_OFFSETS.get(platform, 0)), (platform or "pgn")
    return None, None


def _depth_for_elo(elo: float | None) -> int:
    """Deepen the sweep for stronger players so small win%-drop cutoffs aren't just noise."""
    base = config.SWEEP_DEPTH
    if elo is None:
        return base
    if elo >= 2300:
        return max(base, 20)
    if elo >= 1900:
        return max(base, 18)
    return base


def analyze_game(
    pgn: str,
    player: str = "auto",
    *,
    depth: int | None = None,
    elo: int | None = None,
    sensitivity: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ReviewSession:
    """Analyse a PGN and build a ReviewSession for `player`'s mistakes.

    Mistake thresholds adapt to skill: pass `elo` (normalized scale) or a named `sensitivity`
    ("casual"/"default"/"strong"/"master"), else the reviewed side's Elo is read from the PGN
    (normalized for the detected platform). Stronger -> smaller win%-drop cutoffs + deeper sweep.

    `on_progress(done, total)` is called after each position is evaluated (the slow, ~linear part
    of the sweep), so a caller can drive a progress bar. Best-effort: exceptions in the callback
    are swallowed so a misbehaving reporter can never break a review.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError("Could not parse a game from the provided PGN.")

    headers = dict(game.headers)
    me = resolve_player(headers, player)
    my_turn = chess.WHITE if me == "white" else chess.BLACK

    review_elo, elo_source = _resolve_review_elo(headers, me, elo, sensitivity)
    speed = classify_speed(headers.get("TimeControl"), headers.get("Event"))
    # Cutoffs adapt to BOTH skill (Elo) and mode: faster time controls are more forgiving,
    # slower ones stricter, with blitz as the unchanged anchor.
    thresholds = thresholds_for_speed(thresholds_for_elo(review_elo), speed)
    depth = depth or _depth_for_elo(review_elo)

    # Replay the mainline, collecting (board_before, move) pairs plus each move's remaining
    # clock from [%clk] comments (None when the PGN has no clocks). We still ignore NAGs and
    # variations by only following the first variation (== the mainline).
    board = game.board()
    steps: list[tuple[chess.Board, chess.Move]] = []
    clocks: list[float | None] = []  # remaining seconds for the side that just moved, per ply
    node = game
    while node.variations:
        node = node.variations[0]
        steps.append((board.copy(stack=False), node.move))
        clocks.append(node.clock())
        board.push(node.move)
    final_board = board

    # Evaluate every position once: the position before each move, plus the final one. This is
    # the slow part of the sweep (one fixed-depth engine call per ply ⇒ roughly linear time), so
    # we report progress here for the web board's progress bar.
    pos_evals: list[_PosEval] = []
    total_positions = len(steps) + 1

    def _report(done: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(done, total_positions)
        except Exception:  # pragma: no cover - a broken reporter must never break a review
            pass

    for before, _move in steps:
        pos_evals.append(_evaluate_position(before, depth=depth))
        _report(len(pos_evals))
    pos_evals.append(_evaluate_position(final_board, depth=depth))
    _report(len(pos_evals))

    all_my_moves: list[MoveReview] = []
    white_accs: list[float] = []
    black_accs: list[float] = []

    for i, (before, move) in enumerate(steps):
        mover_is_white = before.turn == chess.WHITE
        eval_at = pos_evals[i]
        eval_next = pos_evals[i + 1]

        # From the mover's perspective.
        win_before = eval_at.win_stm
        win_after = 100.0 - eval_next.win_stm
        cp_before = eval_at.cp_stm
        cp_after = -eval_next.cp_stm
        acc = move_accuracy(win_before, win_after)

        if mover_is_white:
            white_accs.append(acc)
        else:
            black_accs.append(acc)

        if before.turn != my_turn:
            continue  # only build full reviews for my moves

        best_uci = eval_at.best_pv_uci[0] if eval_at.best_pv_uci else move.uci()
        is_best = move.uci() == best_uci
        classification = classify(win_before, win_after, is_best=is_best, thresholds=thresholds)

        best_line_san = _pv_to_san(before, eval_at.best_pv_uci)
        best_move_san = best_line_san[0] if best_line_san else before.san(move)

        # Engine-grounded explanation for flagged moves. Uses only data already computed
        # in the sweep (eval_next is the cached eval of the position after the played move),
        # so this adds no engine calls and no LLM calls.
        comment = ""
        if classification in ("inaccuracy", "mistake", "blunder"):
            after_board = before.copy(stack=False)
            after_board.push(move)
            followup_san = _pv_to_san(after_board, eval_next.best_pv_uci, max_plies=6)
            comment = _mistake_comment(
                before.san(move),
                classification,
                round(win_before, 1),
                round(win_after, 1),
                round(win_before - win_after, 1),
                best_move_san,
                best_line_san,
                followup_san,
            )

        review = MoveReview(
            ply=i + 1,
            move_number=before.fullmove_number,
            color="white" if mover_is_white else "black",
            move_san=before.san(move),
            move_uci=move.uci(),
            fen_before=before.fen(),
            fen_after=_fen_after(before, move),
            eval_before=round(cp_before, 1),
            eval_after=round(cp_after, 1),
            win_before=round(win_before, 1),
            win_after=round(win_after, 1),
            win_swing=round(win_before - win_after, 1),
            classification=classification,
            best_move_san=best_move_san,
            best_line_uci=eval_at.best_pv_uci[:12],
            best_line_san=best_line_san,
            accuracy=round(acc, 1),
            comment=comment,
            clock_after=clocks[i],
            opp_clock=clocks[i - 1] if i >= 1 else None,
        )
        all_my_moves.append(review)

    mistakes = [
        m for m in all_my_moves if m.classification in ("inaccuracy", "mistake", "blunder")
    ]

    timeline = _build_timeline(steps, pos_evals, final_board, all_my_moves, mistakes, my_turn)

    session = ReviewSession(
        pgn=pgn,
        player=me,
        headers=headers,
        result=headers.get("Result", "*"),
        speed=speed,
        accuracy_white=round(aggregate_accuracy(white_accs), 1),
        accuracy_black=round(aggregate_accuracy(black_accs), 1),
        all_moves=all_my_moves,
        mistakes=mistakes,
        current_index=0,
        timeline=timeline,
        review_elo=review_elo,
        elo_source=elo_source,
        thresholds=list(thresholds),
        sweep_depth=depth,
    )
    return session


def _win_white(pe: "_PosEval", turn: chess.Color) -> float:
    """Win% from White's perspective, given whose move it is at that position."""
    return pe.win_stm if turn == chess.WHITE else 100.0 - pe.win_stm


def _build_timeline(
    steps: list[tuple[chess.Board, chess.Move]],
    pos_evals: list["_PosEval"],
    final_board: chess.Board,
    all_my_moves: list[MoveReview],
    mistakes: list[MoveReview],
    my_turn: chess.Color,
) -> list[dict]:
    """One entry per position (node 0..N). Each non-final node carries its OUTGOING move,
    the engine's best move there, and (for the player's moves) the classification."""
    cls_by_ply = {m.ply: m.classification for m in all_my_moves}
    mistake_index_by_ply = {m.ply: i for i, m in enumerate(mistakes)}

    nodes: list[dict] = []
    for k in range(len(steps) + 1):
        is_final = k == len(steps)
        board = final_board if is_final else steps[k][0]
        turn = board.turn
        node: dict = {
            "node": k,
            "fen": board.fen(),
            "win_white": round(_win_white(pos_evals[k], turn), 1),
            "color": "white" if turn == chess.WHITE else "black",
            "move_number": board.fullmove_number,
        }
        if not is_final:
            before, move = steps[k]
            eval_at = pos_evals[k]
            best_uci = eval_at.best_pv_uci[0] if eval_at.best_pv_uci else None
            ply = k + 1
            node.update(
                {
                    "ply": ply,
                    "move_san": before.san(move),
                    "move_uci": move.uci(),
                    "best_uci": best_uci,
                    "best_san": before.san(chess.Move.from_uci(best_uci)) if best_uci else None,
                    "is_my_move": before.turn == my_turn,
                    "classification": cls_by_ply.get(ply),
                    "mistake_index": mistake_index_by_ply.get(ply),
                }
            )
        nodes.append(node)
    return nodes


def _mistake_comment(
    move_san: str,
    classification: str,
    win_before: float,
    win_after: float,
    swing: float,
    best_move_san: str,
    best_line_san: list[str],
    followup_san: list[str],
) -> str:
    """Concrete written explanation of a mistake, stitched from engine data we already have."""
    article = "an" if classification == "inaccuracy" else "a"
    parts = [
        f"{move_san} is {article} {classification}: your win chance falls from "
        f"{win_before}% to {win_after}% (−{swing})."
    ]
    if best_move_san:
        line = " ".join(best_line_san[:5])
        parts.append(f"Stronger was {best_move_san}" + (f" — {line}." if line else "."))
    if followup_san:
        parts.append(f"After {move_san}, the engine line runs {' '.join(followup_san)}.")
    return " ".join(parts)


def _fen_after(before: chess.Board, move: chess.Move) -> str:
    b = before.copy(stack=False)
    b.push(move)
    return b.fen()
