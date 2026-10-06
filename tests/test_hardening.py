"""Regression tests for a bug sweep: LAN-client limits, puzzle-state durability, cache/history
consistency, batch failure status, Event-less multi-game PGNs, engine request bounds, and clearing
the Stockfish path. No engine, no network."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from server import config
from server.core import history, multipgn, puzzle_rating
from server.core import settings as settings_mod
from server.core.session import ReviewSession
from server.web import jobs
from server.web.app import create_app


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    for attr in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY_HEADER", "STOCKFISH_PATH", "WEB_HOST",
                 "PLAYER_ELO"):
        monkeypatch.setattr(config, attr, getattr(config, attr))
    return tmp_path


def _client(local: bool) -> TestClient:
    app = create_app()
    return TestClient(app) if local else TestClient(app, client=("192.168.1.50", 54321))


# --- another device on the network can't redirect credentials or pick what runs here ------------

@pytest.mark.parametrize("key,value", [
    ("local_llm_base_url", "http://attacker.example/v1"),
    ("local_llm_api_key_header", "X-Leak"),
    ("stockfish_path", "/bin/sh"),
    ("web_host", "0.0.0.0"),
])
def test_remote_client_cannot_change_local_only_settings(data_dir, key, value):
    before = settings_mod.effective()[key]
    res = _client(False).post("/api/settings", json={key: value})
    assert res.status_code == 403
    assert settings_mod.effective()[key] == before


def test_remote_client_may_resend_unchanged_values_and_save_others(data_dir):
    current = settings_mod.effective()
    payload = {k: current[k] for k in settings_mod.LOCAL_ONLY_KEYS}
    payload["player_elo"] = "1400"
    res = _client(False).post("/api/settings", json=payload)
    assert res.status_code == 200
    assert settings_mod.effective()["player_elo"] == "1400"


def test_local_client_can_change_the_ai_server_url(data_dir):
    res = _client(True).post("/api/settings", json={"local_llm_base_url": "http://localhost:1234/v1"})
    assert res.status_code == 200
    assert config.LOCAL_LLM_BASE_URL == "http://localhost:1234/v1"


def test_remote_client_cannot_apply_updates_or_replace_the_engine(data_dir):
    remote = _client(False)
    assert remote.post("/api/apply-update", json={"now": True}).status_code == 403
    assert remote.post("/api/fix-stockfish-arch").status_code == 403


# --- clearing the Stockfish path goes back to auto-detect, live ---------------------------------

def test_blank_stockfish_path_reverts_to_auto_detect(data_dir, monkeypatch):
    monkeypatch.setattr(config, "_resolve_stockfish", lambda: "/auto/stockfish")
    settings_mod.apply({"stockfish_path": "/custom/stockfish"})
    assert config.STOCKFISH_PATH == "/custom/stockfish"
    settings_mod.apply({"stockfish_path": ""})
    assert config.STOCKFISH_PATH == "/auto/stockfish"


# --- puzzle state is never silently destroyed ----------------------------------------------------

def test_corrupt_puzzle_state_is_backed_up_before_reset(tmp_path):
    real = puzzle_rating._state_path(str(tmp_path))
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    state = puzzle_rating.load_state(data_dir=str(tmp_path))
    assert state["rating"] == puzzle_rating._default_state()["rating"]
    backups = [n for n in os.listdir(os.path.dirname(real)) if ".bak-" in n]
    assert len(backups) == 1
    with open(os.path.join(os.path.dirname(real), backups[0]), encoding="utf-8") as fh:
        assert fh.read() == "{not json"


def test_transient_read_error_does_not_overwrite_puzzle_state(tmp_path, monkeypatch):
    real = puzzle_rating._state_path(str(tmp_path))
    saved = puzzle_rating._default_state()
    saved["rating"] = 1873.0
    puzzle_rating.save_state(saved, data_dir=str(tmp_path))

    import builtins
    real_open = builtins.open

    def locked(file, *a, **k):
        if str(file) == real and "r" in (a[0] if a else k.get("mode", "r")):
            raise PermissionError("locked by another process")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", locked)
    puzzle_rating.load_state(data_dir=str(tmp_path))
    monkeypatch.setattr(builtins, "open", real_open)
    assert puzzle_rating.load_state(data_dir=str(tmp_path))["rating"] == 1873.0


def test_puzzle_state_save_leaves_no_temp_files(tmp_path):
    for _ in range(3):
        puzzle_rating.save_state(puzzle_rating._default_state(), data_dir=str(tmp_path))
    folder = os.path.dirname(puzzle_rating._state_path(str(tmp_path)))
    assert not [n for n in os.listdir(folder) if n.endswith(".tmp")]


# --- a cached game missing from history gets recorded --------------------------------------------

def _sess(moves=("e2e4", "e7e5")) -> ReviewSession:
    timeline = [{"node": i, "move_uci": m} for i, m in enumerate(moves)] + [{"node": len(moves)}]
    return ReviewSession(pgn="1. e4 e5 *", player="white", headers={"White": "a", "Black": "b"},
                         result="*", timeline=timeline)


def test_cache_hit_missing_from_history_is_recorded(data_dir, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ENABLED", True)
    sess = _sess()
    monkeypatch.setattr(jobs.analysis_cache, "load", lambda *a, **k: sess)
    recorded = []
    monkeypatch.setattr(jobs.history, "record_game", lambda s, *a, **k: recorded.append(s))
    jobs.start("1. e4 e5 *", "white")
    assert recorded == [sess]


def test_cache_hit_already_in_history_is_not_rerecorded(data_dir, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_ENABLED", True)
    sess = _sess()
    history.append_record({"game_id": history._game_id(sess), "reviewed_side": "white"})
    monkeypatch.setattr(jobs.analysis_cache, "load", lambda *a, **k: sess)
    recorded = []
    monkeypatch.setattr(jobs.history, "record_game", lambda s, *a, **k: recorded.append(s))
    jobs.start("1. e4 e5 *", "white")
    assert recorded == []


def test_batch_where_every_game_fails_reports_an_error(data_dir, monkeypatch):
    monkeypatch.setattr(jobs.analysis_cache, "load", lambda *a, **k: None)

    def boom(*a, **k):
        raise ValueError("bad game")

    monkeypatch.setattr(jobs, "_analyze_game", boom)
    with jobs._lock:
        jobs._state["token"] += 1
        token = jobs._state["token"]
    jobs._run_batch(["x", "y"], ["white", "white"], None, None, token)
    st = jobs.status()
    assert st["status"] == "error" and "bad game" in st["error"]


# --- multi-game PGNs without an [Event] tag ------------------------------------------------------

def test_split_pgn_without_event_tags():
    text = (
        '[White "a"]\n[Black "b"]\n\n1. e4 e5 1-0\n\n'
        '[White "c"]\n[Black "d"]\n\n1. d4 { [%clk 0:03:00] } d5 0-1\n'
    )
    games = multipgn.split_pgn(text)
    assert len(games) == 2
    assert '[White "c"]' in games[1] and "[%clk 0:03:00]" in games[1]


def test_split_pgn_keeps_multiline_comments_in_one_game():
    text = '[Event "x"]\n[White "a"]\n\n1. e4 {a comment\n[%clk 0:01:00]} e5 1-0\n'
    assert len(multipgn.split_pgn(text)) == 1


# --- engine request bounds -----------------------------------------------------------------------

def test_best_moves_clamps_depth_and_multipv(data_dir, monkeypatch):
    from server.web import routes_board

    seen = {}

    def fake_line(fen, depth, multipv, **k):
        seen.update(depth=depth, multipv=multipv)
        return {"side_to_move": "white", "line_uci": [], "line_san": [], "win_percent": 50,
                "eval": "+0.00", "lines": []}

    monkeypatch.setattr(routes_board.lines, "engine_line", fake_line)
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    res = _client(True).post("/api/best-moves", json={"fen": fen, "depth": 99, "multipv": 500})
    assert res.status_code == 200
    assert seen == {"depth": routes_board._MAX_DEPTH, "multipv": routes_board._MAX_MULTIPV}
    assert json.loads(res.text)["depth"] == routes_board._MAX_DEPTH
