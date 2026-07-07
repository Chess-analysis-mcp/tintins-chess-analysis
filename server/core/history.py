"""Persistent game history -> personalised coaching.

Each analysed game is turned into one compact JSON record (`build_game_record`) and appended
to `<DATA_DIR>/history/games.jsonl` (`record_game`). Records carry both the raw provenance
(`platform`, `player_name`) and a resolved canonical `player_id`, so one person's several
lichess/chess.com accounts fold into a single coaching profile via `<DATA_DIR>/identities.json`.

The JSONL is append-only; readers dedupe by keeping the latest record per
`(game_id, reviewed_side)` (`load_records`). From those records we aggregate a small,
prompt-ready `profile` (`build_profile`) cached at `<DATA_DIR>/profiles/<player_id>.json`.

Everything here is engine-free and deterministic: motif tags (`tag_motifs`) and phase
detection (`_phase`) are cheap static heuristics over the FENs/moves we already computed,
so history is essentially free to record and trivial to backfill when the heuristics improve.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import chess

from server import config
from server.core import openings
from server.core import session as session_mod
from server.core.evaluation import classify_speed
from server.core.session import ReviewSession

SCHEMA_VERSION = 1

# Static piece values for the "is this piece hanging" heuristic (king effectively infinite).
_PIECE_VALUE = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 100,
}


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def _data_dir(data_dir: Optional[str]) -> str:
    return data_dir if data_dir is not None else config.DATA_DIR


def _history_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "history", "games.jsonl")


def _identities_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "identities.json")


def _profile_path(player_id: str, data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "profiles", f"{_safe(player_id)}.json")


def _safe(name: str) -> str:
    """Filesystem-safe slug for a player_id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "unknown").strip("_") or "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# Identity resolution
# --------------------------------------------------------------------------------------
def _norm_platform(raw: str) -> str:
    """Normalise any platform spelling (Site URL, 'lichess.org', 'chesscom', ...) to a token."""
    s = (raw or "").lower()
    if "lichess" in s:
        return "lichess"
    if "chess.com" in s or "chesscom" in s:
        return "chesscom"
    return s.strip() or "unknown"


def _platform_from_headers(headers: dict) -> str:
    blob = " ".join(headers.get(k, "") for k in ("Site", "Link", "Event"))
    return _norm_platform(blob)


def load_identities(data_dir: Optional[str] = None) -> dict:
    """Read the alias map; missing/garbled file -> {} (history still works, just unmapped)."""
    path = _identities_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_identity(
    headers: dict, reviewed_side: str, data_dir: Optional[str] = None
) -> tuple[str, str, str]:
    """Resolve (player_id, platform, player_name) for the reviewed side.

    `player_id` is the canonical id from identities.json if an alias matches; otherwise it
    falls back to the raw handle (so unmapped accounts are still recorded, never merged by
    accident). An alias may omit `platform` to match a handle across every platform.
    """
    name = headers.get("White" if reviewed_side == "white" else "Black", "").strip()
    platform = _platform_from_headers(headers)
    name_lc = name.lower()

    # 1. Explicit identities.json (most specific; supports multiple people).
    for pid, info in load_identities(data_dir).items():
        for alias in (info or {}).get("aliases", []):
            a_name = str(alias.get("name", "")).strip().lower()
            if not a_name or a_name != name_lc:
                continue
            a_plat = alias.get("platform")
            if a_plat is None or _norm_platform(str(a_plat)) == platform:
                return pid, platform, name

    # 2. CHESS_USERNAME + CHESS_ALIASES from the env (the .mcp.json setup path): every listed
    #    handle folds into CHESS_USERNAME as the canonical player_id.
    if name_lc and config.USERNAME:
        if name_lc == config.USERNAME.lower():
            return config.USERNAME, platform, name
        for a_plat, a_name in config.USERNAME_ALIASES:
            if a_name == name_lc and (a_plat is None or _norm_platform(a_plat) == platform):
                return config.USERNAME, platform, name

    # 3. Unmapped: key by the raw handle so the game is still recorded (never merged blindly).
    fallback = name_lc or (config.USERNAME or "").lower() or "me"
    return fallback, platform, name


def _display_name(player_id: str, data_dir: Optional[str] = None) -> str:
    info = load_identities(data_dir).get(player_id) or {}
    return info.get("display_name") or player_id


def _resolves_to_me(handle: str, platform: Optional[str], data_dir: Optional[str]) -> bool:
    """True if `handle` already resolves to the canonical "me" (env or identities.json)."""
    handle_lc = (handle or "").strip().lower()
    if not handle_lc:
        return False
    if config.USERNAME and handle_lc == config.USERNAME.lower():
        return True
    plat = _norm_platform(platform) if platform else None
    for a_plat, a_name in config.USERNAME_ALIASES:
        if a_name == handle_lc and (a_plat is None or _norm_platform(a_plat) == plat):
            return True
    me = my_player_id(data_dir)
    for alias in (load_identities(data_dir).get(me) or {}).get("aliases", []):
        if str(alias.get("name", "")).strip().lower() == handle_lc:
            a_plat = alias.get("platform")
            if a_plat is None or plat is None or _norm_platform(str(a_plat)) == plat:
                return True
    return False


def ensure_self_alias(
    handle: str, platform: Optional[str] = None, data_dir: Optional[str] = None
) -> str:
    """Fold `handle` into the canonical "me" so uploaded games show up in "My games".

    When you upload your own (e.g. Chess.com) games, your handle there may differ from
    CHESS_USERNAME. This persists `handle` as an alias of `my_player_id()` in identities.json so
    `resolve_identity` maps those games — and any future ones from that account — to you. Idempotent
    and best-effort; returns the canonical player_id the handle now resolves to.
    """
    canonical = my_player_id(data_dir)
    handle = (handle or "").strip()
    if not handle or _resolves_to_me(handle, platform, data_dir):
        return canonical

    ids = load_identities(data_dir)
    entry = ids.get(canonical) or {}
    aliases = list(entry.get("aliases") or [])
    aliases.append({"name": handle, "platform": _norm_platform(platform) if platform else None})
    entry["aliases"] = aliases
    entry.setdefault("display_name", canonical)
    ids[canonical] = entry

    path = _identities_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ids, fh, ensure_ascii=False, indent=2)
    return canonical


# --------------------------------------------------------------------------------------
# Motif tagging + phase (cheap static heuristics, no engine)
# --------------------------------------------------------------------------------------
# Human-readable labels for the coaching profile / chat injection.
_MOTIF_LABELS = {
    "hung_piece": "hanging pieces (leaving a piece en prise)",
    "pawn_grab": "greedy pawn-grabbing",
    "missed_capture": "missing free material",
    "missed_fork": "missing forks",
    "allowed_fork": "walking into forks",
    "allowed_mate": "allowing forced mate",
    "back_rank": "back-rank weaknesses",
    "missed_mate": "missing forced mates",
    "time_trouble": "blundering in time pressure (low clock)",
}


def _time_control_base(time_control: str) -> Optional[float]:
    """Base seconds from a PGN TimeControl ("600+0", "300+5", "600"); None if unknown/correspondence."""
    tc = (time_control or "").strip()
    if not tc or tc in ("-", "?"):
        return None
    head = tc.split("+", 1)[0]
    if "/" in head:  # correspondence ("1/259200" = days), not a sudden-death clock
        return None
    try:
        base = float(head)
    except ValueError:
        return None
    return base if base > 0 else None


def time_motifs(
    clock_after: Optional[float], opp_clock: Optional[float], base: Optional[float]
) -> list[str]:
    """`time_trouble` when the move was made on a low clock, or far behind the opponent.

    Needs PGN [%clk] data; returns [] when clocks are absent (graceful on PGNs without timing).
    """
    if clock_after is None:
        return []
    low_absolute = clock_after <= 30 or (base is not None and clock_after <= 0.10 * base)
    much_less_than_opp = (
        opp_clock is not None
        and opp_clock > 0
        and clock_after <= 0.5 * opp_clock
        and clock_after <= (0.20 * base if base else 60)
    )
    return ["time_trouble"] if (low_absolute or much_less_than_opp) else []


def _val(piece: Optional[chess.Piece]) -> int:
    return _PIECE_VALUE.get(piece.piece_type, 0) if piece else 0


def _is_hanging(board: chess.Board, square: int) -> bool:
    """Static SEE-lite: is the piece on `square` left en prise (undefended, or won by a
    cheaper attacker)? `board` is the position with that piece already on the board."""
    piece = board.piece_at(square)
    if piece is None:
        return False
    attackers = board.attackers(not piece.color, square)
    if not attackers:
        return False
    defenders = board.attackers(piece.color, square)
    cheapest = min(_val(board.piece_at(sq)) for sq in attackers)
    return (not defenders) or cheapest < _val(piece)


def _is_fork(board: chess.Board, move: chess.Move) -> bool:
    """Does `move` (by board.turn) land a piece that forks >= 2 valuable enemy targets?

    A "valuable" target is the enemy king (check) or a piece worth at least as much as the
    forking piece. We require either a check or an undefended target (so it actually wins),
    and that the forking piece isn't itself simply hanging to a cheaper piece.
    """
    forker = board.turn
    b = board.copy(stack=False)
    b.push(move)
    pf = b.piece_at(move.to_square)
    if pf is None or pf.color != forker:
        return False
    a_val = _val(pf)
    targets = [
        (sq, b.piece_at(sq))
        for sq in b.attacks(move.to_square)
        if b.piece_at(sq)
        and b.piece_at(sq).color != forker
        and (b.piece_at(sq).piece_type == chess.KING or _val(b.piece_at(sq)) >= a_val)
    ]
    if len(targets) < 2:
        return False
    gives_check = any(p.piece_type == chess.KING for _, p in targets)
    undefended = any(
        p.piece_type != chess.KING and not b.attackers(not forker, sq)
        for sq, p in targets
    )
    if not (gives_check or undefended):
        return False
    # The forking piece must not just hang for free (then the opponent escapes by taking it).
    enemy = b.attackers(not forker, move.to_square)
    if enemy:
        own = b.attackers(forker, move.to_square)
        if not own and min(_val(b.piece_at(s)) for s in enemy) < a_val:
            return False
    return True


def _allowed_opponent_fork(board: chess.Board) -> bool:
    """In the position after our move (board.turn = opponent), can the opponent fork us?"""
    return any(_is_fork(board, mv) for mv in board.legal_moves)


def _allowed_mate_in_1(board: chess.Board) -> Optional[chess.Move]:
    """The opponent's mate-in-1 in this position, if any (board.turn = opponent)."""
    for mv in board.legal_moves:
        board.push(mv)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return mv
    return None


def _is_back_rank_mate(board: chess.Board, mate_move: chess.Move, victim: chess.Color) -> bool:
    """Is `mate_move` a rook/queen mate delivered on `victim`'s back rank?"""
    piece = board.piece_at(mate_move.from_square)
    if piece is None or piece.piece_type not in (chess.ROOK, chess.QUEEN):
        return False
    back = 0 if victim == chess.WHITE else 7
    return chess.square_rank(mate_move.to_square) == back


def _back_rank_weak(board: chess.Board, color: chess.Color) -> bool:
    """Structural back-rank weakness for `color`: king boxed on its back rank (no luft) while
    the opponent has a rook/queen on a file with no friendly pawn (i.e. it can reach the rank)."""
    king_sq = board.king(color)
    if king_sq is None:
        return False
    back = 0 if color == chess.WHITE else 7
    if chess.square_rank(king_sq) != back:
        return False
    forward = back + (1 if color == chess.WHITE else -1)
    king_file = chess.square_file(king_sq)
    # No luft: every square in front of the king is occupied by one of the king's own pieces.
    for df in (-1, 0, 1):
        f = king_file + df
        if 0 <= f <= 7:
            occ = board.piece_at(chess.square(f, forward))
            if occ is None or occ.color != color:
                return False  # an escape square exists
    opp = not color
    for sq, piece in board.piece_map().items():
        if piece.color == opp and piece.piece_type in (chess.ROOK, chess.QUEEN):
            f = chess.square_file(sq)
            file_pawns = any(
                board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, color)
                for r in range(8)
            )
            if not file_pawns:
                return True
    return False


def tag_motifs(
    fen_before: str,
    move_uci: str,
    best_uci: Optional[str],
    win_swing: float,
    eval_before: float,
) -> list[str]:
    """Best-effort motif tags for one flagged move, from data we already have (no engine).

    Tags fall into three buckets: what we did wrong with our move (`pawn_grab`, `hung_piece`),
    what we missed (`missed_capture`, `missed_fork`, `missed_mate`), and what we let the
    opponent do (`allowed_fork`, `allowed_mate`, `back_rank`). All are static (<= 2 ply of
    pure python-chess) and deterministic. Conservative on purpose — they run only on
    already-flagged mistakes, so a true-positive bias is fine. The schema reserves `motifs`
    for exactly this, so records can be re-tagged offline with no re-analysis.
    """
    motifs: list[str] = []
    try:
        board = chess.Board(fen_before)
        move = chess.Move.from_uci(move_uci)
    except (ValueError, AssertionError):
        return motifs
    if move not in board.legal_moves:
        return motifs

    mover = board.turn

    # --- what we did with our move ---
    if board.is_capture(move):
        if board.is_en_passant(move) or _val(board.piece_at(move.to_square)) == 1:
            motifs.append("pawn_grab")

    # --- what we missed (the engine's best move) ---
    if best_uci:
        try:
            best = chess.Move.from_uci(best_uci)
        except (ValueError, AssertionError):
            best = None
        if best is not None and best != move and best in board.legal_moves:
            if board.is_capture(best) and not board.is_en_passant(best):
                if _val(board.piece_at(best.to_square)) >= 3:
                    motifs.append("missed_capture")
            if _is_fork(board, best):
                motifs.append("missed_fork")

    # --- the position after our move (opponent to move) ---
    after = board.copy(stack=False)
    after.push(move)

    if _is_hanging(after, move.to_square):
        motifs.append("hung_piece")

    if not after.is_game_over():
        if _allowed_opponent_fork(after):
            motifs.append("allowed_fork")
        mate_move = _allowed_mate_in_1(after)
        if mate_move is not None:
            motifs.append("allowed_mate")
            if _is_back_rank_mate(after, mate_move, mover):
                motifs.append("back_rank")
        if "back_rank" not in motifs and _back_rank_weak(after, mover):
            motifs.append("back_rank")

    # missed_mate: a forced mate was available for the mover and we didn't play it.
    if eval_before >= config.MATE_SCORE_CP - 1000:
        motifs.append("missed_mate")

    return motifs


def _view_summary(agg: dict) -> str:
    """One-line summary of an aggregate view (accuracy, top motifs, weakest phase)."""
    bits = []
    if agg.get("avg_accuracy") is not None:
        r = agg.get("results", {})
        bits.append(
            f"accuracy {agg['avg_accuracy']}% "
            f"({r.get('win', 0)}W-{r.get('loss', 0)}L-{r.get('draw', 0)}D)"
        )
    motifs = agg.get("top_motifs", [])
    if motifs:
        named = ", ".join(
            f"{_MOTIF_LABELS.get(m['motif'], m['motif'])} (×{m['count']})" for m in motifs[:4]
        )
        bits.append(f"recurring: {named}")
    if agg.get("weakest_phase"):
        bits.append(f"weakest phase {agg['weakest_phase']}")
    return "; ".join(bits)


def format_profile_for_prompt(profile: dict) -> Optional[str]:
    """Render the hybrid profile as a compact coaching block for the chat prompt (None if empty)."""
    recent = profile.get("recent") or {}
    if not recent.get("games"):
        return None
    out = [
        "The user's play profile — use it to personalise advice and point out recurring patterns "
        "when relevant (don't force it if it doesn't apply):"
    ]
    window = recent.get("window")
    scope = f"last {window} games" if window else f"all {recent['games']} games"
    out.append(f"- Recent form ({scope}): {_view_summary(recent)}.")

    # Per-mode breakdown — only worth stating when the player has played more than one mode,
    # so coaching can weigh the CURRENT game's mode and note where patterns diverge by speed.
    by_speed = [s for s in recent.get("by_speed", []) if s.get("speed") != "unknown"]
    if len(by_speed) >= 2:
        parts = []
        for s in by_speed:
            acc = f"{s['avg_accuracy']}% acc" if s.get("avg_accuracy") is not None else "acc n/a"
            parts.append(f"{s['speed']} ×{s['games']} ({acc}, {s['blunders_per_game']} blunders/game)")
        out.append(
            "- By mode: " + "; ".join(parts) + ". Mistake tolerance differs by mode — judge the "
            "current game against its own mode (faster time controls warrant more lenient "
            "expectations)."
        )

    lifetime = profile.get("lifetime") or {}
    # Only show lifetime if it covers a different (larger) set than the recent window.
    if lifetime.get("games") and lifetime["games"] != recent["games"]:
        out.append(f"- Lifetime ({lifetime['games']} games): {_view_summary(lifetime)}.")
        ra, la = recent.get("avg_accuracy"), lifetime.get("avg_accuracy")
        if ra is not None and la is not None and abs(ra - la) >= 2:
            trend = "improving" if ra > la else "slipping"
            out.append(
                f"- Trend: {trend} — recent accuracy {ra}% vs lifetime {la}%. "
                "Weight the recent form more heavily."
            )
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# End-of-game coaching blurb
# --------------------------------------------------------------------------------------
def _grade_phrase(acc: float) -> str:
    if acc >= 90:
        return "Excellent game"
    if acc >= 80:
        return "Solid game"
    if acc >= 70:
        return "A mixed game"
    return "A rough game"


def _tally_phrase(blunders: int, mistakes: int, inaccuracies: int) -> str:
    """e.g. '1 blunder, 3 mistakes and 2 inaccuracies' — zeros dropped, plurals handled."""
    parts = []
    for n, noun in ((blunders, "blunder"), (mistakes, "mistake"), (inaccuracies, "inaccuracy")):
        if n:
            word = noun if n == 1 else (noun[:-1] + "ies" if noun.endswith("y") else noun + "s")
            parts.append(f"{n} {word}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _dominant_motif(mistakes: list) -> Optional[str]:
    """The most common motif across this game's flagged moves (engine-free; reuses tag_motifs)."""
    counts: Counter = Counter()
    for m in mistakes:
        best_uci = m.best_line_uci[0] if m.best_line_uci else None
        for motif in tag_motifs(m.fen_before, m.move_uci, best_uci, m.win_swing, m.eval_before):
            counts[motif] += 1
    return counts.most_common(1)[0][0] if counts else None


def _is_recurring(motif: str, data_dir: Optional[str]) -> bool:
    """True if `motif` is a repeated theme in the player's recent profile (best-effort)."""
    try:
        profile = get_profile(my_player_id(data_dir), data_dir)
        for entry in (profile.get("recent") or {}).get("top_motifs", []):
            if entry.get("motif") == motif and entry.get("count", 0) >= 2:
                return True
    except Exception:
        return False
    return False


def coach_summary(sess: ReviewSession, data_dir: Optional[str] = None) -> Optional[str]:
    """A short, engine-free end-of-game coaching blurb grounded in this game's flagged moves.

    Same philosophy as MoveReview.comment: templated + deterministic, no engine and no Claude
    call, so it's free and always available. Three beats — overall accuracy/tally, the single
    costliest moment (with the better move), and the recurring thread to watch (tied to the
    player's profile when the same theme shows up across games). Returns None for a clean game.
    """
    side = "White" if sess.player == "white" else "Black"
    acc = sess.accuracy_white if sess.player == "white" else sess.accuracy_black
    mistakes = sess.mistakes
    if not mistakes:
        return f"Clean game — {acc}% accuracy as {side}, no inaccuracies, mistakes or blunders flagged."

    counts = Counter(m.classification for m in mistakes)
    tally = _tally_phrase(
        counts.get("blunder", 0), counts.get("mistake", 0), counts.get("inaccuracy", 0)
    )
    parts = [f"{_grade_phrase(acc)} — {acc}% accuracy as {side}, with {tally}."]

    worst = max(mistakes, key=lambda m: m.win_swing)
    num = f"{worst.move_number}{'.' if worst.color == 'white' else '...'}"
    better = f" {worst.best_move_san} was stronger." if worst.best_move_san else ""
    parts.append(
        f"Your costliest moment was {num}{worst.move_san} "
        f"({worst.classification}, −{round(worst.win_swing)}%).{better}"
    )

    motif = _dominant_motif(mistakes)
    if motif:
        label = _MOTIF_LABELS.get(motif, motif)
        tie = " — also a recurring theme across your recent games" if _is_recurring(motif, data_dir) else ""
        parts.append(f"The thread to watch: {label}{tie}.")

    return " ".join(parts)


def _phase(fen: str, move_number: int) -> str:
    """opening / middlegame / endgame from material + move number (heuristic)."""
    try:
        board = chess.Board(fen)
    except (ValueError, AssertionError):
        return "middlegame"
    pieces = [p for p in board.piece_map().values() if p.piece_type not in (chess.KING, chess.PAWN)]
    queens = sum(1 for p in pieces if p.piece_type == chess.QUEEN)
    if len(pieces) <= 6 or (queens == 0 and len(pieces) <= 8):
        return "endgame"
    if move_number <= 12:
        return "opening"
    return "middlegame"


# --------------------------------------------------------------------------------------
# Record building
# --------------------------------------------------------------------------------------
def _int_or_none(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _clean_date(headers: dict) -> Optional[str]:
    raw = (headers.get("UTCDate") or headers.get("Date") or "").strip()
    if not raw or "?" in raw:
        return None
    return raw.replace(".", "-")


def _player_result(result: str, side: str) -> Optional[str]:
    if result == "1-0":
        return "win" if side == "white" else "loss"
    if result == "0-1":
        return "win" if side == "black" else "loss"
    if result == "1/2-1/2":
        return "draw"
    return None


def _game_url(headers: dict) -> Optional[str]:
    for key in ("Site", "Link"):
        val = headers.get(key, "").strip()
        if val.startswith("http"):
            return val
    return None


def _full_move_ucis(sess: ReviewSession) -> list[str]:
    ucis = [n["move_uci"] for n in sess.timeline if n.get("move_uci")]
    if ucis:
        return ucis
    return [m.move_uci for m in sess.all_moves]  # fallback (reviewed side only)


def _game_id(sess: ReviewSession) -> str:
    blob = "".join(_full_move_ucis(sess))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def build_game_record(sess: ReviewSession, data_dir: Optional[str] = None) -> dict:
    """Turn a ReviewSession into one JSONL-ready coaching record."""
    headers = sess.headers
    side = sess.player
    player_id, platform, player_name = resolve_identity(headers, side, data_dir)

    base = _time_control_base(headers.get("TimeControl", ""))
    counts: Counter = Counter()
    phase_loss = {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0}
    mistakes = []
    for m in sess.mistakes:
        best_uci = m.best_line_uci[0] if m.best_line_uci else None
        phase = _phase(m.fen_before, m.move_number)
        counts[m.classification] += 1
        phase_loss[phase] = phase_loss.get(phase, 0.0) + m.win_swing
        motifs = tag_motifs(m.fen_before, m.move_uci, best_uci, m.win_swing, m.eval_before)
        motifs += time_motifs(m.clock_after, m.opp_clock, base)
        mistakes.append(
            {
                "ply": m.ply,
                "move_number": m.move_number,
                "color": m.color,
                "san": m.move_san,
                "uci": m.move_uci,
                "best_san": m.best_move_san,
                "best_uci": best_uci,
                "classification": m.classification,
                "win_before": round(m.win_before, 1),
                "win_after": round(m.win_after, 1),
                "win_drop": round(m.win_swing, 1),
                "phase": phase,
                "fen_before": m.fen_before,
                "clock_after": m.clock_after,
                "opp_clock": m.opp_clock,
                "motifs": motifs,
            }
        )

    plies = max(len(sess.timeline) - 1, 0) or len(sess.all_moves)
    accuracy = sess.accuracy_white if side == "white" else sess.accuracy_black

    # Opening/ECO: trust the PGN headers (Lichess ships them) but fall back to a local lookup
    # when absent (Chess.com bulk exports often omit them) so those games still get named.
    # Engine-free; reuses the timeline FENs we already have.
    eco = headers.get("ECO") or None
    opening = headers.get("Opening") or None
    if not opening:
        fens = [n.get("fen") for n in sess.timeline if n.get("fen")]
        eco2, name2 = openings.classify_from_fens(fens)
        eco = eco or eco2
        opening = name2

    return {
        "schema_version": SCHEMA_VERSION,
        "game_id": _game_id(sess),
        "reviewed_side": side,
        "analyzed_at": _now_iso(),
        "player_id": player_id,
        "platform": platform,
        "player_name": player_name,
        "date": _clean_date(headers),
        "white": headers.get("White", "?"),
        "black": headers.get("Black", "?"),
        "result": sess.result,
        "player_result": _player_result(sess.result, side),
        "eco": eco,
        "opening": opening,
        "time_control": headers.get("TimeControl") or None,
        "speed": classify_speed(headers.get("TimeControl"), headers.get("Event")),
        "player_elo": _int_or_none(headers.get("WhiteElo" if side == "white" else "BlackElo", "")),
        "opponent_elo": _int_or_none(headers.get("BlackElo" if side == "white" else "WhiteElo", "")),
        "game_url": _game_url(headers),
        # Raw PGN so the web board can REOPEN a past game (re-analyse on click). Small
        # (~1-3 KB); records written before this field simply lack it (rows mark has_pgn=False).
        "pgn": sess.pgn,
        "sweep_depth": sess.sweep_depth,
        "review_elo": sess.review_elo,
        "thresholds": sess.thresholds,
        "ply_count": plies,
        "accuracy": round(accuracy, 1),
        "counts": {k: counts.get(k, 0) for k in ("inaccuracy", "mistake", "blunder")},
        "phase_loss": {k: round(v, 1) for k, v in phase_loss.items()},
        "mistakes": mistakes,
    }


# --------------------------------------------------------------------------------------
# Storage (append-only JSONL; readers dedupe)
# --------------------------------------------------------------------------------------
def append_record(record: dict, data_dir: Optional[str] = None) -> None:
    path = _history_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_records(
    player_id: Optional[str] = None, data_dir: Optional[str] = None
) -> list[dict]:
    """All games, deduped to the latest record per (game_id, reviewed_side).

    Optionally filtered to one `player_id`. Bad/blank lines are skipped, not fatal.
    """
    path = _history_path(data_dir)
    latest: dict[tuple, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (rec.get("game_id"), rec.get("reviewed_side"))
                prev = latest.get(key)
                if prev is None or rec.get("analyzed_at", "") >= prev.get("analyzed_at", ""):
                    latest[key] = rec
    except FileNotFoundError:
        return []
    records = list(latest.values())
    if player_id is not None:
        records = [r for r in records if r.get("player_id") == player_id]
    return records


def list_players(data_dir: Optional[str] = None) -> list[str]:
    return sorted({r.get("player_id") for r in load_records(data_dir=data_dir) if r.get("player_id")})


def my_player_id(data_dir: Optional[str] = None) -> str:
    """Canonical player_id for the configured user (CHESS_USERNAME, remapped via identities.json).

    The env path in `resolve_identity` folds CHESS_USERNAME + aliases onto config.USERNAME, so that
    is the join key for "my games". If identities.json maps that handle to another canonical id,
    honour it. Lowercased to match how handles are stored. Used to filter the web history list.
    """
    handle = (config.USERNAME or "").strip()
    handle_lc = handle.lower()
    for pid, info in load_identities(data_dir).items():
        for alias in (info or {}).get("aliases", []):
            if str(alias.get("name", "")).strip().lower() == handle_lc:
                return pid
    return handle_lc or "me"


def history_rows(player_id: Optional[str] = None, data_dir: Optional[str] = None) -> list[dict]:
    """Compact, newest-first list of analysed games for the web history panel.

    Filtered to `player_id` when given (the panel passes `my_player_id()` for "just my games").
    Each row reuses fields already on the record — no recompute — plus `has_pgn` so the frontend
    knows whether the game can be reopened (records written before PGNs were stored can't be).
    """
    records = sorted(
        load_records(player_id=player_id, data_dir=data_dir),
        key=lambda r: r.get("analyzed_at", ""),
        reverse=True,
    )
    # "Is this me?" is computed at READ time against the CURRENT identity config, not from the
    # record's frozen player_id — so a game keyed by a raw handle (e.g. a chess.com game recorded
    # before that handle was folded into "me") still tints once the handle is added in Settings.
    me = my_player_id(data_dir)
    rows = []
    for r in records:
        pgn = r.get("pgn")
        rows.append(
            {
                "game_id": r.get("game_id"),
                "player_id": r.get("player_id"),  # lets the panel tint games that are "you"
                "is_me": (
                    r.get("player_id") == me
                    or _resolves_to_me(
                        r.get("player_name") or r.get("player_id") or "",
                        r.get("platform"),
                        data_dir,
                    )
                ),
                "reviewed_side": r.get("reviewed_side"),
                "white": r.get("white"),
                "black": r.get("black"),
                "player_result": r.get("player_result"),
                "accuracy": r.get("accuracy"),
                "speed": r.get("speed") or "unknown",
                "opening": r.get("opening") or r.get("eco"),
                "date": r.get("date"),
                "counts": r.get("counts") or {},
                "game_url": r.get("game_url"),
                "has_pgn": bool(pgn),
                "pgn": pgn,
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Insights (time-windowed aggregate for the board's Insights panel)
# --------------------------------------------------------------------------------------
def _record_day(r: dict) -> str:
    """The day a game belongs to, as YYYY-MM-DD: when it was PLAYED (the PGN date) if known,
    else when it was analysed. Lexicographic compare works for cutoffs."""
    d = (r.get("date") or "").strip()
    return d if d else (r.get("analyzed_at") or "")[:10]


def insights(days: Optional[int] = None, data_dir: Optional[str] = None) -> dict:
    """Aggregate stats + recurring themes for the configured user's games in a time window.

    `days` limits to games from the last N days (by played date, falling back to analysed
    date); None/0 = all history. Reuses `_aggregate`, plus human labels on the motifs so the
    frontend can render them directly.
    """
    me = my_player_id(data_dir)
    records = load_records(data_dir=data_dir)
    # With an identity configured, insights are about "you". Without one (e.g. a paste-only user
    # who never set a username), aggregate everything — same philosophy as the "My games" list.
    if (config.USERNAME or "").strip():
        records = [
            r
            for r in records
            if r.get("player_id") == me
            or _resolves_to_me(
                r.get("player_name") or r.get("player_id") or "", r.get("platform"), data_dir
            )
        ]
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        records = [r for r in records if _record_day(r) >= cutoff]

    agg = _aggregate(records)
    for m in agg.get("top_motifs", []):
        m["label"] = _MOTIF_LABELS.get(m["motif"], m["motif"])
    return {"player_id": me, "days": days or 0, **agg}


# --------------------------------------------------------------------------------------
# Derived profile (rebuildable cache)
# --------------------------------------------------------------------------------------
def _aggregate(records: list[dict]) -> dict:
    """Aggregate a list of game records into one stats view (accuracy, motifs, phases, openings)."""
    agg: dict = {"games": len(records)}
    if not records:
        return agg

    accs = [r["accuracy"] for r in records if r.get("accuracy") is not None]
    results: Counter = Counter(r.get("player_result") for r in records if r.get("player_result"))
    counts: Counter = Counter()
    motifs: Counter = Counter()
    phase_loss = {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0}
    openings: dict[str, dict] = {}
    by_speed: dict[str, dict] = {}

    for r in records:
        for k, v in (r.get("counts") or {}).items():
            counts[k] += v
        for k, v in (r.get("phase_loss") or {}).items():
            phase_loss[k] = phase_loss.get(k, 0.0) + v
        for m in r.get("mistakes", []):
            motifs.update(m.get("motifs", []))
        op = r.get("opening") or r.get("eco") or "Unknown"
        st = openings.setdefault(op, {"games": 0, "acc_sum": 0.0})
        st["games"] += 1
        if r.get("accuracy") is not None:
            st["acc_sum"] += r["accuracy"]
        # Per-mode (bullet/blitz/rapid/...) breakdown, so coaching can apply mode-appropriate
        # expectations and call out where a player's patterns differ by speed.
        sp = r.get("speed") or "unknown"
        sps = by_speed.setdefault(sp, {"games": 0, "acc_sum": 0.0, "acc_n": 0, "blunders": 0})
        sps["games"] += 1
        if r.get("accuracy") is not None:
            sps["acc_sum"] += r["accuracy"]
            sps["acc_n"] += 1
        sps["blunders"] += (r.get("counts") or {}).get("blunder", 0)

    games = len(records)
    agg.update(
        {
            "avg_accuracy": round(sum(accs) / len(accs), 1) if accs else None,
            "results": {k: results.get(k, 0) for k in ("win", "loss", "draw")},
            "mistake_totals": {k: counts.get(k, 0) for k in ("inaccuracy", "mistake", "blunder")},
            "mistakes_per_game": {
                k: round(counts.get(k, 0) / games, 2) for k in ("inaccuracy", "mistake", "blunder")
            },
            "top_motifs": [{"motif": k, "count": v} for k, v in motifs.most_common(8)],
            "phase_loss_total": {k: round(v, 1) for k, v in phase_loss.items()},
            "weakest_phase": max(phase_loss, key=phase_loss.get) if any(phase_loss.values()) else None,
            "by_speed": [
                {
                    "speed": k,
                    "games": v["games"],
                    "avg_accuracy": round(v["acc_sum"] / v["acc_n"], 1) if v["acc_n"] else None,
                    "blunders_per_game": round(v["blunders"] / v["games"], 2) if v["games"] else None,
                }
                for k, v in sorted(by_speed.items(), key=lambda kv: -kv[1]["games"])
            ],
            "openings": sorted(
                (
                    {
                        "opening": k,
                        "games": v["games"],
                        "avg_accuracy": round(v["acc_sum"] / v["games"], 1) if v["games"] else None,
                    }
                    for k, v in openings.items()
                ),
                key=lambda o: -o["games"],
            )[:10],
        }
    )
    return agg


def build_profile(player_id: str, data_dir: Optional[str] = None) -> dict:
    """Build a hybrid coaching profile: a "recent form" sliding window + a "lifetime" view.

    The split lets coaching adapt as a player improves (recent weaknesses surface; old, fixed ones
    fade out of the window). Window sizes come from config: `PROFILE_RECENT_WINDOW` (last N games;
    <=0 = all) and `PROFILE_LIFETIME` (None = all history, positive N = last N, 0 = omit the
    lifetime view so the profile is a pure sliding window). Both recompute from the full history.
    """
    records = sorted(
        load_records(player_id=player_id, data_dir=data_dir),
        key=lambda r: r.get("analyzed_at", ""),
    )
    profile: dict = {
        "player_id": player_id,
        "display_name": _display_name(player_id, data_dir),
        "games_analyzed": len(records),
        "generated_at": _now_iso(),
    }
    if not records:
        return profile

    recent_n = config.PROFILE_RECENT_WINDOW
    recent_records = records if recent_n <= 0 else records[-recent_n:]
    profile["recent"] = {"window": recent_n if recent_n > 0 else None, **_aggregate(recent_records)}

    lifetime_n = config.PROFILE_LIFETIME
    if lifetime_n != 0:  # 0 disables the lifetime view (pure sliding window)
        lifetime_records = records if lifetime_n is None else records[-lifetime_n:]
        profile["lifetime"] = _aggregate(lifetime_records)

    profile["recent_games"] = [
        {
            "date": r.get("date"),
            "opening": r.get("opening") or r.get("eco"),
            "accuracy": r.get("accuracy"),
            "result": r.get("player_result"),
            "blunders": (r.get("counts") or {}).get("blunder", 0),
        }
        for r in records[-8:]
    ]
    return profile


def write_profile(player_id: str, data_dir: Optional[str] = None) -> dict:
    profile = build_profile(player_id, data_dir)
    path = _profile_path(player_id, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    return profile


# --------------------------------------------------------------------------------------
# Public entry points (used by the MCP tools)
# --------------------------------------------------------------------------------------
def record_game(sess: ReviewSession, data_dir: Optional[str] = None) -> dict:
    """Append the game to history and refresh the player's profile cache. Returns the record."""
    record = build_game_record(sess, data_dir)
    append_record(record, data_dir)
    write_profile(record["player_id"], data_dir)
    return record


def get_profile(player_id: Optional[str] = None, data_dir: Optional[str] = None) -> dict:
    """Profile for `player_id`, or for the current session's player when omitted."""
    if player_id is None:
        sess = session_mod.get_session()
        if sess is None:
            return {
                "error": "No player_id given and no game analysed yet.",
                "known_players": list_players(data_dir),
            }
        player_id, _, _ = resolve_identity(sess.headers, sess.player, data_dir)
    return build_profile(player_id, data_dir)
