"""Tests for persistent game history + coaching profile (engine-free, fast)."""
from __future__ import annotations

import json

import chess
import pytest

from server.core import history
from server.core.session import MoveReview, ReviewSession


def _mistake(**kw) -> MoveReview:
    base = dict(
        ply=21,
        move_number=11,
        color="white",
        move_san="Qd5",
        move_uci="d1d5",
        # white queen d1, black king e8, black pawns c6 & e6 (both attack d5), white king e1
        fen_before="4k3/8/2p1p3/8/8/8/8/3QK3 w - - 0 11",
        fen_after="4k3/8/2p1p3/3Q4/8/8/8/4K3 b - - 1 11",
        eval_before=20.0,
        eval_after=-880.0,
        win_before=55.0,
        win_after=20.0,
        win_swing=35.0,
        classification="blunder",
        best_move_san="Kf2",
        best_line_uci=["e1f2"],
        best_line_san=["Kf2"],
        accuracy=10.0,
        comment="Qd5 hangs the queen.",
    )
    base.update(kw)
    return MoveReview(**base)


def _session(**kw) -> ReviewSession:
    m = _mistake()
    base = dict(
        pgn="[Event \"x\"]\n\n1. d4 e5 *",
        player="white",
        headers={
            "White": "thedarktintin",
            "Black": "opponent",
            "Site": "https://lichess.org/abcd1234",
            "Result": "0-1",
            "WhiteElo": "1500",
            "BlackElo": "1480",
            "UTCDate": "2026.06.15",
            "Opening": "Test Opening",
            "ECO": "B01",
            "TimeControl": "600+0",
        },
        result="0-1",
        accuracy_white=70.0,
        accuracy_black=82.0,
        all_moves=[m],
        mistakes=[m],
        timeline=[
            {"node": 0, "ply": 1, "move_uci": "d2d4"},
            {"node": 1, "ply": 2, "move_uci": "e7e5"},
            {"node": 2, "ply": 21, "move_uci": "d1d5"},
            {"node": 3},
        ],
        review_elo=1300.0,
        thresholds=[5.0, 10.0, 15.0],
        sweep_depth=16,
    )
    base.update(kw)
    return ReviewSession(**base)


# --- motif heuristics ------------------------------------------------------------------
def test_hung_piece_tag():
    motifs = history.tag_motifs(
        "4k3/8/2p1p3/8/8/8/8/3QK3 w - - 0 11", "d1d5", "e1f2", 35.0, 20.0
    )
    assert "hung_piece" in motifs


def test_pawn_grab_tag():
    # white pawn e4 takes black pawn d5
    motifs = history.tag_motifs("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1", "e4d5", None, 20.0, 0.0)
    assert "pawn_grab" in motifs


def test_missed_capture_tag():
    # best move grabs a free knight on a6; played move is a quiet king move
    motifs = history.tag_motifs(
        "4k3/8/n7/8/8/8/8/R3K3 w - - 0 1", "e1e2", "a1a6", 18.0, 0.0
    )
    assert "missed_capture" in motifs


def test_missed_mate_tag():
    motifs = history.tag_motifs(
        "4k3/8/8/8/8/8/8/3QK3 w - - 0 1", "d1d2", "d1d8", 90.0, history.config.MATE_SCORE_CP - 1
    )
    assert "missed_mate" in motifs


def test_illegal_move_is_safe():
    assert history.tag_motifs("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "a1a8", None, 0.0, 0.0) == []


def test_missed_fork_tag():
    # White missed Nb5-c7+, a knight forking the black king (e8) and rook (a8).
    motifs = history.tag_motifs(
        "r3k3/8/8/1N6/8/8/8/4K3 w - - 0 1", "e1d1", "b5c7", 40.0, 0.0
    )
    assert "missed_fork" in motifs


def test_no_fork_on_single_target():
    # Knight to f7 only attacks one valuable target here -> not a fork.
    motifs = history.tag_motifs(
        "4k3/8/8/4N3/8/8/8/4K3 w - - 0 1", "e1e2", "e5f7", 10.0, 0.0
    )
    assert "missed_fork" not in motifs


def test_allowed_fork_tag():
    # After white's quiet pawn move e2-e3, black plays ...Nd4-c2+ forking Ke1 and Ra1.
    motifs = history.tag_motifs(
        "4k3/8/8/8/3n4/8/4P3/R3K3 w - - 0 1", "e2e3", None, 30.0, 0.0
    )
    assert "allowed_fork" in motifs
    # Control: remove the rook so the knight only hits the king -> no fork.
    control = history.tag_motifs(
        "4k3/8/8/8/3n4/8/4P3/4K3 w - - 0 1", "e2e3", None, 30.0, 0.0
    )
    assert "allowed_fork" not in control


def test_allowed_mate_and_back_rank():
    # White king on g1 boxed by f2/g2/h2; black rook on a2. White blunders Kg1-h1, and
    # ...Ra1# is a back-rank mate.
    motifs = history.tag_motifs(
        "6k1/5ppp/8/8/8/8/r4PPP/6K1 w - - 0 1", "g1h1", None, 90.0, 0.0
    )
    assert "allowed_mate" in motifs
    assert "back_rank" in motifs


def test_back_rank_weakness_structural():
    # White king g1 boxed by f2/g2/h2; black rook on the open e-file (no white pawn on e) ->
    # structural back-rank weakness, even without an immediate mate.
    assert history._back_rank_weak(
        chess.Board("4r1k1/pppp1ppp/8/8/8/8/PPP2PPP/5RK1 b - - 0 1"), chess.WHITE
    )
    # With luft (h2-h3 played) the king has an escape square -> not weak.
    assert not history._back_rank_weak(
        chess.Board("4r1k1/pppp1ppp/8/8/8/7P/PPP2PP1/5RK1 b - - 0 1"), chess.WHITE
    )


# --- time-trouble motif ----------------------------------------------------------------
def test_time_control_base():
    assert history._time_control_base("600+0") == 600
    assert history._time_control_base("300+5") == 300
    assert history._time_control_base("600") == 600
    assert history._time_control_base("-") is None
    assert history._time_control_base("1/259200") is None  # correspondence
    assert history._time_control_base("") is None


def test_time_motifs():
    assert history.time_motifs(None, None, 600) == []        # no clock data -> graceful
    assert history.time_motifs(300, 300, 600) == []          # both healthy
    assert history.time_motifs(20, 200, 600) == ["time_trouble"]   # absolute low (<=30s)
    assert history.time_motifs(50, 600, 600) == ["time_trouble"]   # <=10% of base
    assert history.time_motifs(80, 400, 600) == ["time_trouble"]   # far behind opp & <=20% base
    assert history.time_motifs(200, 220, 600) == []          # behind but not actually low


def test_time_trouble_in_record(tmp_path):
    m = _mistake(clock_after=15.0, opp_clock=210.0)  # 15s left, opponent has 210s
    sess = _session(all_moves=[m], mistakes=[m])  # headers TimeControl is "600+0"
    rec = history.build_game_record(sess, data_dir=str(tmp_path))
    mm = rec["mistakes"][0]
    assert mm["clock_after"] == 15.0 and mm["opp_clock"] == 210.0
    assert "time_trouble" in mm["motifs"]


def test_no_time_trouble_without_clocks(tmp_path):
    rec = history.build_game_record(_session(), data_dir=str(tmp_path))  # default clocks = None
    assert rec["mistakes"][0]["clock_after"] is None
    assert "time_trouble" not in rec["mistakes"][0]["motifs"]


# --- profile -> prompt formatting ------------------------------------------------------
def test_format_profile_for_prompt():
    assert history.format_profile_for_prompt({"games_analyzed": 0}) is None
    assert history.format_profile_for_prompt({"recent": {"games": 0}}) is None

    recent = {
        "window": 25,
        "games": 25,
        "avg_accuracy": 85.0,
        "results": {"win": 14, "loss": 9, "draw": 2},
        "top_motifs": [{"motif": "hung_piece", "count": 5}, {"motif": "pawn_grab", "count": 2}],
        "weakest_phase": "endgame",
    }
    lifetime = {
        "games": 60,
        "avg_accuracy": 78.0,
        "results": {"win": 30, "loss": 25, "draw": 5},
        "top_motifs": [{"motif": "pawn_grab", "count": 12}],
        "weakest_phase": "middlegame",
    }
    text = history.format_profile_for_prompt(
        {"games_analyzed": 60, "recent": recent, "lifetime": lifetime}
    )
    assert "Recent form (last 25 games)" in text
    assert "Lifetime (60 games)" in text
    assert "hanging pieces" in text  # motif slug rendered as a human label
    assert "improving" in text  # 85% recent vs 78% lifetime -> improving trend

    # With only the recent view (lifetime disabled / same set), no lifetime or trend line.
    solo = history.format_profile_for_prompt({"games_analyzed": 25, "recent": recent})
    assert "Recent form" in solo and "Lifetime" not in solo and "Trend" not in solo


# --- record building -------------------------------------------------------------------
def test_record_fields(tmp_path):
    rec = history.build_game_record(_session(), data_dir=str(tmp_path))
    assert rec["schema_version"] == history.SCHEMA_VERSION
    assert rec["reviewed_side"] == "white"
    assert rec["platform"] == "lichess"
    assert rec["player_name"] == "thedarktintin"
    assert rec["player_id"] == "thedarktintin"  # no identities.json -> raw handle
    assert rec["player_result"] == "loss"  # 0-1 from White's side
    assert rec["accuracy"] == 70.0
    assert rec["counts"]["blunder"] == 1
    assert rec["game_url"] == "https://lichess.org/abcd1234"
    assert rec["player_elo"] == 1500 and rec["opponent_elo"] == 1480
    assert rec["game_id"] and len(rec["game_id"]) == 16
    assert rec["mistakes"][0]["phase"] == "endgame"
    assert "hung_piece" in rec["mistakes"][0]["motifs"]
    assert rec["speed"] == "rapid"  # TimeControl 600+0
    assert rec["pgn"] == _session().pgn  # raw PGN stored so the board can reopen the game


def test_history_rows_filters_and_marks_pgn(tmp_path):
    d = str(tmp_path)
    mine = history.build_game_record(_session(), data_dir=d)
    history.append_record(mine, data_dir=d)
    # A game with no stored pgn (pre-migration record) and a different player.
    other = {**mine, "game_id": "other1", "player_id": "someone_else", "pgn": None}
    history.append_record(other, data_dir=d)

    rows = history.history_rows(player_id="thedarktintin", data_dir=d)
    assert len(rows) == 1  # filtered to my player_id
    row = rows[0]
    assert row["has_pgn"] is True and row["pgn"] == _session().pgn
    assert row["reviewed_side"] == "white" and row["speed"] == "rapid"

    # Unfiltered includes both; the pgn-less one is flagged has_pgn=False.
    all_rows = history.history_rows(data_dir=d)
    assert {r["has_pgn"] for r in all_rows} == {True, False}


def _speed_rec(game_id, speed, blunders, when):
    """Minimal hand-built record carrying a speed, for the per-mode aggregation test."""
    return {
        "schema_version": 1,
        "game_id": game_id,
        "reviewed_side": "white",
        "analyzed_at": when,
        "player_id": "p",
        "platform": "lichess",
        "player_name": "p",
        "player_result": "loss",
        "accuracy": 70.0,
        "speed": speed,
        "opening": "Test",
        "counts": {"inaccuracy": 0, "mistake": 0, "blunder": blunders},
        "phase_loss": {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0},
        "mistakes": [],
    }


def test_speed_breakdown_in_profile(tmp_path):
    d = str(tmp_path)
    # Two blitz games (one blunder each) and one rapid game, distinct game_ids so none dedupe.
    for r in (
        _speed_rec("g1", "blitz", 1, "2026-06-15T00:00:01Z"),
        _speed_rec("g2", "blitz", 1, "2026-06-15T00:00:02Z"),
        _speed_rec("g3", "rapid", 0, "2026-06-15T00:00:03Z"),
    ):
        history.append_record(r, data_dir=d)
    profile = history.build_profile("p", data_dir=d)
    by_speed = {s["speed"]: s for s in profile["recent"]["by_speed"]}
    assert by_speed["blitz"]["games"] == 2
    assert by_speed["rapid"]["games"] == 1
    assert by_speed["blitz"]["blunders_per_game"] == 1.0  # one blunder per game
    # The prompt block names the modes when more than one was played.
    block = history.format_profile_for_prompt(profile)
    assert "By mode:" in block and "blitz" in block and "rapid" in block


# --- identity resolution ---------------------------------------------------------------
def test_identity_alias_merges_accounts(tmp_path):
    (tmp_path / "identities.json").write_text(
        json.dumps(
            {
                "dima": {
                    "display_name": "Dima",
                    "aliases": [
                        {"platform": "lichess.org", "name": "thedarktintin"},
                        {"platform": "chesscom", "name": "dpdemler"},
                    ],
                }
            }
        )
    )
    pid, platform, name = history.resolve_identity(
        {"White": "thedarktintin", "Site": "https://lichess.org/x"}, "white", data_dir=str(tmp_path)
    )
    assert pid == "dima" and platform == "lichess"
    # the chess.com account folds into the same player_id
    pid2, _, _ = history.resolve_identity(
        {"Black": "dpdemler", "Site": "https://chess.com/game/y"}, "black", data_dir=str(tmp_path)
    )
    assert pid2 == "dima"


def test_identity_env_aliases(tmp_path, monkeypatch):
    # The .mcp.json setup path: CHESS_USERNAME + CHESS_ALIASES, no identities.json.
    monkeypatch.setattr(history.config, "USERNAME", "thedarktintin")
    monkeypatch.setattr(
        history.config, "USERNAME_ALIASES", [("chesscom", "dpdemler"), (None, "myalt")]
    )
    # primary handle -> canonical CHESS_USERNAME (original case preserved)
    pid, _, _ = history.resolve_identity(
        {"White": "thedarktintin", "Site": "https://lichess.org/x"}, "white", data_dir=str(tmp_path)
    )
    assert pid == "thedarktintin"
    # platform-qualified alias folds in
    pid2, _, _ = history.resolve_identity(
        {"Black": "dpdemler", "Site": "https://chess.com/y"}, "black", data_dir=str(tmp_path)
    )
    assert pid2 == "thedarktintin"
    # bare alias matches on any platform
    pid3, _, _ = history.resolve_identity(
        {"White": "myalt", "Site": "https://lichess.org/z"}, "white", data_dir=str(tmp_path)
    )
    assert pid3 == "thedarktintin"
    # a platform-qualified alias does NOT match the wrong platform
    pid4, _, _ = history.resolve_identity(
        {"White": "dpdemler", "Site": "https://lichess.org/w"}, "white", data_dir=str(tmp_path)
    )
    assert pid4 == "dpdemler"  # unmapped -> raw handle


def test_ensure_self_alias_folds_uploaded_handle(tmp_path, monkeypatch):
    """Uploading your own Chess.com games should make them show up under "my" player_id."""
    d = str(tmp_path)
    monkeypatch.setattr(history.config, "USERNAME", "thedarktintin")
    monkeypatch.setattr(history.config, "USERNAME_ALIASES", [])

    # Before: a chess.com handle that differs from CHESS_USERNAME is its own (unmapped) id.
    headers = {"Black": "thedarktintin2", "Site": "https://chess.com/x"}
    pid_before, _, _ = history.resolve_identity(headers, "black", data_dir=d)
    assert pid_before == "thedarktintin2" != history.my_player_id(data_dir=d)

    canonical = history.ensure_self_alias("thedarktintin2", platform="Chess.com", data_dir=d)
    assert canonical == history.my_player_id(data_dir=d) == "thedarktintin"

    # After: the same chess.com game now resolves to "me".
    pid_after, _, _ = history.resolve_identity(headers, "black", data_dir=d)
    assert pid_after == "thedarktintin"

    # Idempotent: a second call doesn't add a duplicate alias, and an already-mapped handle is a no-op.
    history.ensure_self_alias("thedarktintin2", platform="Chess.com", data_dir=d)
    history.ensure_self_alias("thedarktintin", data_dir=d)  # == CHESS_USERNAME, already me
    ids = json.loads((tmp_path / "identities.json").read_text())
    assert len(ids["thedarktintin"]["aliases"]) == 1


# --- storage: append + dedupe ----------------------------------------------------------
def test_append_and_dedupe(tmp_path):
    d = str(tmp_path)
    history.record_game(_session(), data_dir=d)
    history.record_game(_session(), data_dir=d)  # same game_id+side -> supersedes
    records = history.load_records(data_dir=d)
    assert len(records) == 1
    # the raw file has two lines; dedupe is a read-time concern
    raw_lines = (tmp_path / "history" / "games.jsonl").read_text().strip().splitlines()
    assert len(raw_lines) == 2


def test_records_for_two_sides_are_distinct(tmp_path):
    d = str(tmp_path)
    history.record_game(_session(player="white"), data_dir=d)
    history.record_game(_session(player="black", headers={**_session().headers}), data_dir=d)
    assert len(history.load_records(data_dir=d)) == 2


# --- profile aggregation (hybrid: recent window + lifetime) ----------------------------
def test_profile_aggregates(tmp_path):
    d = str(tmp_path)
    history.record_game(_session(), data_dir=d)
    profile = history.build_profile("thedarktintin", data_dir=d)
    assert profile["games_analyzed"] == 1
    rec = profile["recent"]
    assert rec["avg_accuracy"] == 70.0
    assert rec["results"]["loss"] == 1
    assert rec["mistake_totals"]["blunder"] == 1
    assert rec["weakest_phase"] == "endgame"
    assert any(m["motif"] == "hung_piece" for m in rec["top_motifs"])
    assert rec["openings"][0]["opening"] == "Test Opening"
    assert (tmp_path / "profiles" / "thedarktintin.json").exists()


def _profile_rec(game_id, accuracy, result, motifs, when, player_id="p"):
    """Minimal hand-built record for profile-window tests (no engine/session needed)."""
    return {
        "schema_version": 1,
        "game_id": game_id,
        "reviewed_side": "white",
        "analyzed_at": when,
        "player_id": player_id,
        "platform": "lichess",
        "player_name": player_id,
        "result": result,
        "player_result": {"1-0": "win", "0-1": "loss"}.get(result, "draw"),
        "opening": "Test",
        "eco": "B01",
        "accuracy": accuracy,
        "counts": {"inaccuracy": 0, "mistake": 0, "blunder": len(motifs)},
        "phase_loss": {"opening": 0.0, "middlegame": 10.0, "endgame": 0.0},
        "mistakes": [{"motifs": motifs, "phase": "middlegame"}],
    }


def test_profile_hybrid_recent_vs_lifetime(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(history.config, "PROFILE_RECENT_WINDOW", 2)
    monkeypatch.setattr(history.config, "PROFILE_LIFETIME", None)  # all history
    data = [(60.0, ["hung_piece"]), (70.0, ["hung_piece"]), (90.0, ["pawn_grab"]), (95.0, [])]
    for i, (acc, mot) in enumerate(data):
        history.append_record(
            _profile_rec(f"g{i}", acc, "1-0", mot, f"2026-06-1{i}T00:00:00Z"), data_dir=d
        )
    prof = history.build_profile("p", data_dir=d)
    assert prof["games_analyzed"] == 4
    # recent = last 2 games (a sliding window)
    assert prof["recent"]["games"] == 2 and prof["recent"]["window"] == 2
    assert prof["recent"]["avg_accuracy"] == 92.5  # (90 + 95) / 2
    # lifetime = all 4
    assert prof["lifetime"]["games"] == 4
    assert prof["lifetime"]["avg_accuracy"] == 78.8  # (60+70+90+95)/4 rounded
    # old "hung_piece" weakness has fallen out of the recent window
    recent_motifs = {m["motif"] for m in prof["recent"]["top_motifs"]}
    assert "hung_piece" not in recent_motifs
    assert "hung_piece" in {m["motif"] for m in prof["lifetime"]["top_motifs"]}


def test_profile_lifetime_disabled_is_pure_window(tmp_path, monkeypatch):
    d = str(tmp_path)
    monkeypatch.setattr(history.config, "PROFILE_RECENT_WINDOW", 2)
    monkeypatch.setattr(history.config, "PROFILE_LIFETIME", 0)  # disable lifetime view
    for i in range(3):
        history.append_record(
            _profile_rec(f"g{i}", 80.0, "1-0", [], f"2026-06-0{i + 1}T00:00:00Z"), data_dir=d
        )
    prof = history.build_profile("p", data_dir=d)
    assert "lifetime" not in prof  # pure sliding window
    assert prof["recent"]["games"] == 2


def test_get_profile_no_history(tmp_path):
    from server.core import session as session_mod

    session_mod.clear_session()
    out = history.get_profile(data_dir=str(tmp_path))
    assert "error" in out and out["known_players"] == []
