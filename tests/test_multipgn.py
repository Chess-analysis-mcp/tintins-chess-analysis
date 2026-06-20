"""Splitting a multi-game PGN and detecting the uploader's handle (no engine — pure parsing)."""
from __future__ import annotations

from server.core import multipgn

# Two tiny games concatenated, the way Chess.com exports them (shared player "me", both colors).
TWO_GAMES = """[Event "Live Chess"]
[Site "Chess.com"]
[White "me"]
[Black "opp1"]
[Result "1-0"]

1. e4 {[%clk 0:10:00]} e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0

[Event "Live Chess"]
[Site "Chess.com"]
[White "opp2"]
[Black "me"]
[Result "0-1"]

1. d4 d5 2. c4 e6 3. Nc3 Nf6 0-1
"""


def test_split_pgn_counts_and_preserves_clocks():
    games = multipgn.split_pgn(TWO_GAMES)
    assert len(games) == 2
    # Original text preserved (clocks intact) so time-trouble motifs keep working.
    assert "[%clk" in games[0]
    assert multipgn.headers_of(games[0])["Black"] == "opp1"
    assert multipgn.headers_of(games[1])["White"] == "opp2"


def test_split_pgn_single_game_and_empty():
    assert len(multipgn.split_pgn("1. e4 e5 2. Nf3")) == 1  # header-less single game
    assert multipgn.split_pgn("") == []
    assert multipgn.split_pgn("   \n  ") == []
    # A header block with no moves is not a game.
    assert multipgn.split_pgn('[Event "x"]\n[White "a"]\n') == []


def test_detect_self_handle_common_player():
    games = multipgn.split_pgn(TWO_GAMES)
    assert multipgn.detect_self_handle(games) == "me"  # the only handle in BOTH games


def test_detect_self_handle_prefers_configured():
    # When two handles are common, a configured handle wins; otherwise it's ambiguous -> None.
    same_opp = TWO_GAMES.replace("opp2", "opp1")  # now "opp1" is also in both games
    games = multipgn.split_pgn(same_opp)
    assert multipgn.detect_self_handle(games) is None  # ambiguous: me + opp1 both common
    assert multipgn.detect_self_handle(games, prefer=["ME"]) == "me"  # case-insensitive preference
