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


def test_solve_flow_updates_rating(client):
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
