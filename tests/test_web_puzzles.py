"""Puzzle HTTP API happy path on the vendored baseline (no download, no engine).

Uses the committed baseline shard, so this exercises the real selection/validation path through
FastAPI. State is redirected to a tmp DATA_DIR so the suite never touches the user's store.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.core import puzzles as puzzles_mod
from server.web import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    # No download: this suite runs on the vendored baseline only (per the module docstring). Leaving
    # downloads on makes `/next` spawn a background shard-warm thread that hits the real GitHub
    # manifest — a hidden network call that also races on the module-global manifest cache.
    monkeypatch.setattr(config, "PUZZLE_DOWNLOAD", False)
    return TestClient(app_module.create_app())


def test_config_enabled_on_baseline(client):
    cfg = client.get("/api/puzzle/config").json()
    assert cfg["enabled"] is True
    assert cfg["your_rating"] == 1500  # fresh state seed
    assert isinstance(cfg["themes_available"], list) and cfg["themes_available"]


def test_next_returns_a_solvable_puzzle_without_the_solution(client):
    nx = client.get("/api/puzzle/next").json()
    assert nx["id"] and nx["fen"] and nx["side_to_move"] in ("white", "black")
    assert "moves" not in nx  # the solution is never sent to the client


def test_has_engine_detects_a_windows_style_managed_path(monkeypatch):
    """On Windows the managed engine is C:\\...\\stockfish.exe (backslashes); the old '/'-only check
    read that as "no engine" and disabled From-your-games + interleave. Simulate a Windows path."""
    from server.web import routes_puzzles as rp

    win_path = r"C:\Users\me\AppData\Roaming\Tintin\data\engine\stockfish.exe"
    monkeypatch.setattr(rp.shutil, "which", lambda p: None)  # not resolvable on PATH
    monkeypatch.setattr(rp.os, "sep", "\\")
    monkeypatch.setattr(rp.os, "altsep", "/")
    monkeypatch.setattr(rp.os.path, "exists", lambda p: p == win_path)
    monkeypatch.setattr(rp.config, "STOCKFISH_PATH", win_path)
    assert rp._has_engine() is True


def test_has_engine_falls_back_to_existing_path_on_this_os(monkeypatch, tmp_path):
    from server.web import routes_puzzles as rp

    engine = tmp_path / "stockfish"
    engine.write_text("")  # exists but not executable / not on PATH
    monkeypatch.setattr(rp.shutil, "which", lambda p: None)
    monkeypatch.setattr(rp.config, "STOCKFISH_PATH", str(engine))
    assert rp._has_engine() is True


def test_interleave_can_swap_in_a_mistake_puzzle(client, monkeypatch):
    """With interleave on + a mistake puzzle available, a tactics request can serve an own-game
    position instead — carrying the played move for the grey "you played" arrow."""
    from server.web import routes_puzzles as rp

    fake = {"id": "g1:white:3", "key": "g1:white:3", "source": "your_games",
            "fen": "8/8/8/8/8/8/8/8 w - - 0 1", "side_to_move": "white", "motifs": [],
            "played_uci": "e2e4", "played_san": "e4", "best_uci": "d2d4", "win_drop": 12.3}
    monkeypatch.setattr(rp, "_has_engine", lambda: True)
    monkeypatch.setattr(rp.puzzle_mistakes, "next_mistake_puzzle", lambda *a, **k: dict(fake))
    monkeypatch.setattr(config, "PUZZLE_MISTAKE_INTERLEAVE", True)
    monkeypatch.setattr(config, "PUZZLE_MISTAKE_INTERLEAVE_PROB", 1.0)  # force the swap
    nx = client.get("/api/puzzle/next").json()
    assert nx["source"] == "your_games"
    assert nx["played_uci"] == "e2e4"  # surfaced for the grey played-move arrow
    assert nx["win_drop"] == 12.3  # the original mistake's win% cost, for the badge


def test_interleave_off_serves_curated_tactics(client, monkeypatch):
    monkeypatch.setattr(config, "PUZZLE_MISTAKE_INTERLEAVE", False)
    nx = client.get("/api/puzzle/next").json()
    assert nx.get("source") != "your_games" and nx["id"]


def test_interleave_toggle_roundtrips_through_settings_route(client):
    """Guards the whole toggle path: the SettingsPatch field, KEYS persistence, and live apply
    (a missing SettingsPatch field would silently drop the toggle)."""
    assert "puzzle_mistake_interleave" in client.get("/api/settings").json()["settings"]
    r = client.post("/api/settings", json={"puzzle_mistake_interleave": False}).json()
    assert r["settings"]["puzzle_mistake_interleave"] is False
    assert config.PUZZLE_MISTAKE_INTERLEAVE is False
    r = client.post("/api/settings", json={"puzzle_mistake_interleave": True}).json()
    assert r["settings"]["puzzle_mistake_interleave"] is True


def test_solve_flow_updates_rating(client, monkeypatch):
    # The fresh state uses a RANDOM user_seed, so the picked puzzle varies run-to-run; a small slice
    # of the baseline has rd>=PUZZLE_MAX_RD (unrated). Force every solved puzzle to count so this
    # exercises the Glicko path deterministically.
    monkeypatch.setattr(config, "PUZZLE_MAX_RD", 9999)
    nx = client.get("/api/puzzle/next").json()
    puzzle = puzzles_mod.get_puzzle(nx["id"])
    moves = puzzle["moves"]
    idx = 1
    last = None
    while True:
        last = client.post("/api/puzzle/move", json={"id": nx["id"], "uci": moves[idx]}).json()
        if last.get("is_complete") or not last.get("correct"):
            break
        idx += 2
    assert last["correct"] and last["is_complete"]
    assert last["rating"]["rating_after"] != last["rating"]["rating_before"]
    # State persisted: the streak shows in /state.
    assert client.get("/api/puzzle/state").json()["streak"] >= 1


def test_wrong_move_allows_retry_without_revealing_solution(client):
    nx = client.get("/api/puzzle/next").json()
    # A deliberately wrong UCI (not the solution move).
    r = client.post("/api/puzzle/move", json={"id": nx["id"], "uci": "a1a1"}).json()
    assert r["correct"] is False
    assert r["can_retry"] is True
    # The solution is NOT handed over on a miss — the user keeps trying or presses Show solution.
    assert "expected_uci" not in r and "solution_uci" not in r


def test_show_solution_reveals_line(client):
    nx = client.get("/api/puzzle/next").json()
    r = client.post("/api/puzzle/giveup", json={"id": nx["id"]}).json()
    assert r["solution_uci"] and r["solution_san"]
    # After showing the solution the puzzle is finished, so resume reports nothing to resume.
    assert client.get("/api/puzzle/current").json()["finished"] is True


def test_current_resumes_in_progress_puzzle(client):
    nx = client.get("/api/puzzle/next").json()
    cur = client.get("/api/puzzle/current").json()
    assert cur["active"] is True and cur["finished"] is False
    assert cur["id"] == nx["id"]
    assert cur["side_to_move"] in ("white", "black")
    assert "moves" not in cur  # never leaks the solution


def test_move_without_active_puzzle_is_a_conflict(client):
    r = client.post("/api/puzzle/move", json={"id": "nope", "uci": "e2e4"})
    assert r.status_code == 409


# --- Puzzle storm (timed rush) over the HTTP surface -------------------------------------------

def test_storm_start_serves_a_puzzle_and_scoreboard(client):
    view = client.post("/api/puzzle/storm/start").json()
    assert view["active"] is True and view["ended"] is False
    assert view["score"] == 0 and view["remaining"] > 0
    assert view["puzzle"]["id"] and view["puzzle"]["side_to_move"] in ("white", "black")
    assert "moves" not in view["puzzle"]  # never leak the solution
    st = client.get("/api/puzzle/storm/state").json()
    assert st["active"] is True and "high" in st


def test_storm_full_solve_scores_then_next(client):
    view = client.post("/api/puzzle/storm/start").json()
    moves = puzzles_mod.get_puzzle(view["puzzle"]["id"])["moves"]
    last = None
    for i in range(1, len(moves), 2):
        last = client.post("/api/puzzle/storm/move", json={"uci": moves[i]}).json()
        if last.get("puzzle_done"):
            break
    assert last["solved"] is True and last["score"] == 1
    nxt = client.post("/api/puzzle/storm/start")  # a fresh run resets the score
    assert nxt.json()["score"] == 0


def test_storm_wrong_move_records_a_miss(client):
    client.post("/api/puzzle/storm/start")
    res = client.post("/api/puzzle/storm/move", json={"uci": "a1a1"}).json()
    assert res["correct"] is False and res["misses"] == 1


def test_storm_end_persists_and_clears(client):
    client.post("/api/puzzle/storm/start")
    assert client.post("/api/puzzle/storm/end").json()["ended"] is True
    assert client.get("/api/puzzle/storm/state").json()["active"] is False


def test_storm_review_logs_a_missed_puzzle(client):
    view = client.post("/api/puzzle/storm/start").json()
    pid = view["puzzle"]["id"]
    client.post("/api/puzzle/storm/move", json={"uci": "a1a1"})  # a wrong move resolves the puzzle
    log = client.get("/api/puzzle/storm/review").json()["log"]
    assert len(log) == 1
    entry = log[0]
    assert entry["id"] == pid and entry["solved"] is False and entry["your_move"] == "a1a1"
    assert entry["fen"] and isinstance(entry["themes"], list)


def test_storm_review_logs_a_solved_puzzle(client):
    view = client.post("/api/puzzle/storm/start").json()
    moves = puzzles_mod.get_puzzle(view["puzzle"]["id"])["moves"]
    for i in range(1, len(moves), 2):
        if client.post("/api/puzzle/storm/move", json={"uci": moves[i]}).json().get("puzzle_done"):
            break
    log = client.get("/api/puzzle/storm/review").json()["log"]
    assert log and log[-1]["solved"] is True


def test_puzzle_solution_reveals_the_line_for_review(client):
    view = client.post("/api/puzzle/storm/start").json()
    pid = view["puzzle"]["id"]
    sol = client.get("/api/puzzle/solution", params={"id": pid}).json()
    full = puzzles_mod.get_puzzle(pid)["moves"]
    assert sol["solution_uci"] == full[1:]  # setup move omitted; solver's line only
    assert len(sol["solution_san"]) == len(full) - 1 and sol["solve_fen"]


def test_puzzle_solution_unknown_id_404s(client):
    assert client.get("/api/puzzle/solution", params={"id": "nope"}).status_code == 404
