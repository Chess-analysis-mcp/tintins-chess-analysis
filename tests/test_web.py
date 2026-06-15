"""Web board API tests.

The non-engine routes (session/legal-moves) run instantly; /evaluate needs Stockfish
(set STOCKFISH_PATH). The key assertion is that /evaluate agrees with the shared
`lines.engine_line` path the terminal uses.
"""
from __future__ import annotations

import chess
from fastapi.testclient import TestClient

from server import claude_bridge
from server.core import lines
from server.core import session as session_mod
from server.core.game_analysis import analyze_game
from server.web.app import create_app

client = TestClient(create_app())

START_FEN = chess.STARTING_FEN

SAMPLE_PGN = """[Event "Test"]
[White "thedarktintin"]
[Black "opp"]
[Result "1-0"]

1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0
"""


def test_session_empty_then_populated():
    session_mod.clear_session()
    assert client.get("/api/session").json() == {"empty": True}

    sess = analyze_game(SAMPLE_PGN, player="white")
    session_mod.set_session(sess)
    body = client.get("/api/session").json()
    assert "empty" not in body
    assert body["player"] == "white"
    assert body["white"] == "thedarktintin"
    assert isinstance(body["mistakes"], list)


def test_timeline_and_node_index():
    sess = analyze_game(SAMPLE_PGN, player="white")
    session_mod.set_session(sess)

    tl = client.get("/api/timeline").json()
    assert tl["player"] == "white"
    nodes = tl["nodes"]
    # one node per position: 7 moves -> 8 nodes (incl. the final mated position).
    assert len(nodes) == 8
    assert nodes[0]["node"] == 0 and "win_white" in nodes[0]
    assert nodes[0]["best_uci"]  # non-final nodes carry their best move
    assert "move_uci" not in nodes[-1]  # final node has no outgoing move

    # Every mistake's node_index points at a node whose outgoing move is that mistake.
    body = client.get("/api/session").json()
    for m in body["mistakes"]:
        node = nodes[m["node_index"]]
        assert node["move_uci"] == m["move_uci"]
        assert node["mistake_index"] == m["index"]


def test_best_move_route():
    res = client.post("/api/best-move", json={"fen": START_FEN}).json()
    assert res["side_to_move"] == "white"
    assert res["uci"] and len(res["uci"]) == 4
    assert 0 <= res["win_percent"] <= 100


def test_legal_moves_start_position():
    res = client.post("/api/legal-moves", json={"fen": START_FEN}).json()
    assert res["turn"] == "white"
    assert res["check"] is False
    # 10 origin squares (8 pawns + 2 knights), 20 legal moves in total.
    assert len(res["dests"]) == 10
    assert sum(len(v) for v in res["dests"].values()) == 20
    assert sorted(res["dests"]["e2"]) == ["e3", "e4"]


def test_legal_moves_bad_fen():
    res = client.post("/api/legal-moves", json={"fen": "not a fen"})
    assert res.status_code == 400


def test_evaluate_matches_engine_line():
    """The board's /evaluate must agree with the terminal's engine_line path."""
    direct = lines.engine_line(START_FEN, move="d2d4")
    via_api = client.post("/api/evaluate", json={"fen": START_FEN, "move": "d2d4"}).json()
    assert via_api["move"]["classification"] == direct["move"]["classification"]
    assert via_api["move"]["win_after"] == direct["move"]["win_after"]
    assert via_api["move"]["move_san"] == direct["move"]["move_san"] == "d4"


def test_evaluate_illegal_move():
    res = client.post("/api/evaluate", json={"fen": START_FEN, "move": "e2e5"}).json()
    assert "error" in res


def test_evaluate_returns_refutation_shape():
    """A non-terminal move should carry a red refutation arrow for the board (Phase 7)."""
    res = client.post("/api/evaluate", json={"fen": START_FEN, "move": "a2a3"}).json()
    assert res["shapes"], "expected a refutation shape"
    shape = res["shapes"][0]
    assert shape["brush"] == "red"
    assert len(shape["orig"]) == 2 and len(shape["dest"]) == 2


def test_chat_route_mocked(monkeypatch):
    """The /api/chat route wires through claude_bridge.ask (mocked — no real claude -p call)."""
    def fake_ask(question, **kwargs):
        assert kwargs["fen"] == START_FEN
        return {"answer": "Because the knight on c6 hangs.", "session_id": "sess-123"}

    monkeypatch.setattr(claude_bridge, "ask", fake_ask)
    res = client.post(
        "/api/chat", json={"question": "why is this bad?", "fen": START_FEN}
    ).json()
    assert res["answer"].startswith("Because")
    assert res["session_id"] == "sess-123"


def test_chat_route_error_is_friendly(monkeypatch):
    def boom(question, **kwargs):
        raise claude_bridge.ChatError("Agent SDK credit exhausted — use the terminal.")

    monkeypatch.setattr(claude_bridge, "ask", boom)
    r = client.post("/api/chat", json={"question": "why?"})
    assert r.status_code == 503
    assert "terminal" in r.json()["error"]


def test_chat_empty_question():
    assert client.post("/api/chat", json={"question": "   "}).status_code == 400


def test_best_moves_multipv():
    res = client.post(
        "/api/best-moves", json={"fen": START_FEN, "depth": 12, "multipv": 3}
    ).json()
    assert res["side_to_move"] == "white"
    moves = res["moves"]
    assert 1 <= len(moves) <= 3
    assert all(len(m["uci"]) == 4 for m in moves)
    # multipv lines come back best-first
    wins = [m["win_percent"] for m in moves]
    assert wins == sorted(wins, reverse=True)
