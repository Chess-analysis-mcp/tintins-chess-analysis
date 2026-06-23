"""Local ECO/opening name lookup, so games whose PGN lacks Opening/ECO headers still get
named (e.g. Chess.com exports — Lichess ships the names, Chess.com bulk exports often don't).

Engine-free and deterministic, like the motif heuristics in `history.py`. We vendor the
Lichess `chess-openings` dataset (`server/data/eco/{a..e}.tsv`, ~3.7k named lines) and key it
by board EPD (FEN minus the move/halfmove counters) so transpositions collapse to the same
position. Classification walks a game's positions in play order and keeps the DEEPEST match —
the most specific named line the game actually reached. Same dataset Lichess uses, so names
match what you'd see there.
"""

from __future__ import annotations

import csv
import functools
import io
from pathlib import Path
from typing import Optional

import chess
import chess.pgn

_DATA = Path(__file__).resolve().parent.parent / "data" / "eco"


@functools.lru_cache(maxsize=1)
def _book() -> dict[str, tuple[str, str]]:
    """EPD (position key) -> (eco, name). Built once, lazily, on first lookup.

    Replays each line's movetext to its final position; ~3.7k short lines, a one-time
    ~100-300ms cost paid by the first game that needs classifying, then cached for the process.
    Best-effort: a missing/corrupt data dir yields an empty book (callers degrade to no name).
    """
    book: dict[str, tuple[str, str]] = {}
    if not _DATA.is_dir():
        return book
    for tsv in sorted(_DATA.glob("*.tsv")):
        try:
            with tsv.open(encoding="utf-8") as fh:
                reader = csv.reader(fh, delimiter="\t")
                for row in reader:
                    if len(row) != 3 or row[0] == "eco":  # skip header / malformed rows
                        continue
                    eco, name, movetext = row
                    game = chess.pgn.read_game(io.StringIO(movetext))
                    if game is None:
                        continue
                    board = game.end().board()
                    book[board.epd()] = (eco, name)
        except (OSError, ValueError):
            continue
    return book


def classify_from_fens(fens: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Deepest (eco, name) match across a game's positions, or (None, None).

    `fens` are positions in play order — pass `ReviewSession.timeline` FENs straight in
    (no re-parse, no engine). Overwriting on every hit leaves the deepest match.
    """
    book = _book()
    if not book:
        return (None, None)
    best: tuple[Optional[str], Optional[str]] = (None, None)
    for fen in fens:
        try:
            epd = chess.Board(fen).epd()
        except ValueError:
            continue
        hit = book.get(epd)
        if hit:
            best = hit
    return best


def classify_from_pgn(pgn: str) -> tuple[Optional[str], Optional[str]]:
    """Deepest (eco, name) match for a full PGN string (convenience for callers without a
    pre-built timeline, e.g. tests/backfill). Replays the mainline and reuses the FEN path."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        return (None, None)
    fens: list[str] = []
    board = game.board()
    for move in game.mainline_moves():
        board.push(move)
        fens.append(board.fen())
    return classify_from_fens(fens)
