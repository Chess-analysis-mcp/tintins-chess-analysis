"""Web board API tests.

The non-engine routes (session/legal-moves) run instantly; /evaluate needs Stockfish
(set STOCKFISH_PATH). The key assertion is that /evaluate agrees with the shared
`lines.engine_line` path the terminal uses.
"""
from __future__ import annotations

import time

import chess
from fastapi.testclient import TestClient

from server import claude_bridge
from server.core import history
from server.core import lichess
from server.core import lines
from server.core import session as session_mod
from server.core.game_analysis import analyze_game
from server.core.session import ReviewSession
from server.web import jobs
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


def test_app_config_defaults_off():
    from server import config

    # Read live values; with the launcher's CHESS_APP_MODE unset, app_mode is off.
    body = client.get("/api/app-config").json()
    assert body["app_mode"] is config.APP_MODE
    assert "default_username" in body


def test_app_config_reports_app_mode(monkeypatch):
    from server import config

    monkeypatch.setattr(config, "APP_MODE", True)
    monkeypatch.setattr(config, "USERNAME", "thedarktintin")
    body = client.get("/api/app-config").json()
    assert body == {"app_mode": True, "default_username": "thedarktintin"}


# --- history / lichess / progressive-analyze routes (engine kept out via monkeypatch) ---
def test_history_route_lists_my_games(monkeypatch, tmp_path):
    monkeypatch.setattr(history.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(history.config, "USERNAME", "thedarktintin")
    rec = analyze_game(SAMPLE_PGN, player="white")  # SAMPLE_PGN's White is thedarktintin
    history.append_record(history.build_game_record(rec), data_dir=str(tmp_path))

    body = client.get("/api/history").json()
    assert body["player_id"] == "thedarktintin"
    assert len(body["games"]) == 1
    g = body["games"][0]
    assert g["has_pgn"] is True and g["pgn"].strip().startswith("[Event")
    assert g["reviewed_side"] == "white"


def test_lichess_route_monkeypatched(monkeypatch):
    class FakeGame:
        def to_dict(self):
            return {"game_id": "abcd1234", "white": "me", "black": "you", "result": "1-0",
                    "speed": "blitz", "opening": "Sicilian", "date": "2026.06.01", "pgn": "[Event ...]"}

    monkeypatch.setattr(lichess, "fetch_user_games", lambda *a, **k: [FakeGame()])
    res = client.get("/api/lichess/games?username=me").json()
    assert res["count"] == 1 and res["games"][0]["game_id"] == "abcd1234"

    def boom(*a, **k):
        raise lichess.LichessError("rate limit hit; use LICHESS_TOKEN")

    monkeypatch.setattr(lichess, "fetch_user_games", boom)
    r = client.get("/api/lichess/games?username=me")
    assert r.status_code == 502 and "rate limit" in r.json()["error"]


def test_analyze_route_background_then_ready(monkeypatch):
    """POST /api/analyze runs in the background (status pending->ready) and sets the session."""
    monkeypatch.setattr(jobs.config, "HISTORY_ENABLED", False)  # don't touch real history
    fake = ReviewSession(pgn="x", player="black", headers={"White": "a", "Black": "b"})
    monkeypatch.setattr(jobs, "_analyze_game", lambda pgn, player="auto", on_progress=None: fake)

    session_mod.clear_session()
    assert client.post("/api/analyze", json={"pgn": ""}).status_code == 400  # empty rejected
    # With a real (slow) sweep this returns "pending"; the instant stub may already be "ready".
    assert client.post("/api/analyze", json={"pgn": "1. e4 e5"}).json()["status"] in ("pending", "ready")

    for _ in range(40):
        st = client.get("/api/analysis-status").json()
        if st["status"] in ("ready", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "ready"
    assert client.get("/api/session").json()["player"] == "black"


def test_analyze_batch_splits_and_records(monkeypatch, tmp_path):
    """POST /api/analyze-batch splits a multi-game PGN, reviews each game, and folds the uploader's
    handle into "my games" so all of them are recorded under one player_id."""
    d = str(tmp_path)
    monkeypatch.setattr(jobs.config, "DATA_DIR", d)
    monkeypatch.setattr(history.config, "DATA_DIR", d)
    monkeypatch.setattr(history.config, "USERNAME", "thedarktintin")
    monkeypatch.setattr(history.config, "USERNAME_ALIASES", [])

    two_games = (
        '[Event "Live Chess"]\n[Site "Chess.com"]\n[White "me2"]\n[Black "opp1"]\n[Result "1-0"]\n\n'
        "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n\n"
        '[Event "Live Chess"]\n[Site "Chess.com"]\n[White "opp2"]\n[Black "me2"]\n[Result "0-1"]\n\n'
        "1. d4 d5 2. c4 e6 0-1\n"
    )

    # Stub the engine: a ReviewSession whose reviewed side echoes the requested player.
    def fake_analyze(pgn, player="auto", on_progress=None):
        import chess.pgn, io
        h = dict(chess.pgn.read_headers(io.StringIO(pgn)))
        return ReviewSession(pgn=pgn, player=player, headers=h, result=h.get("Result", "*"))

    monkeypatch.setattr(jobs, "_analyze_game", fake_analyze)

    session_mod.clear_session()
    assert client.post("/api/analyze-batch", json={"pgn": "not a game"}).status_code == 400

    res = client.post("/api/analyze-batch", json={"pgn": two_games, "player": "auto"}).json()
    assert res["total_games"] == 2
    assert res["self_handle"] == "me2"  # the handle common to both games
    assert res["first_side"] == "white"  # me2 is White in the first game

    for _ in range(60):
        st = client.get("/api/analysis-status").json()
        if st["status"] in ("ready", "error"):
            break
        time.sleep(0.05)
    assert st["status"] == "ready" and st["total_games"] == 2

    # Both games landed in "my games" (me2 folded into CHESS_USERNAME via an auto-written alias).
    body = client.get("/api/history").json()
    assert body["player_id"] == "thedarktintin"
    assert len(body["games"]) == 2
    assert {g["reviewed_side"] for g in body["games"]} == {"white", "black"}


def test_ping_arms_app_liveness():
    from server.core import app_liveness

    app_liveness._armed = False
    app_liveness._closing_at = None
    assert client.post("/api/ping").json() == {"ok": True}
    assert app_liveness._armed is True  # the heartbeat armed the watchdog
    # The close beacon starts the close countdown; a fresh heartbeat cancels it (refresh-safe).
    assert client.post("/api/closing").json() == {"ok": True}
    assert app_liveness._closing_at is not None
    client.post("/api/ping")
    assert app_liveness._closing_at is None
    app_liveness._armed = False  # leave clean


def test_settings_get_and_update(monkeypatch, tmp_path):
    """GET returns effective settings; POST persists + applies live (and app-config reflects it)."""
    from server import config
    from server.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "USERNAME", "envname")
    monkeypatch.setattr(config, "USERNAME_ALIASES", [])

    got = client.get("/api/settings").json()
    assert got["settings"]["username"] == "envname"
    assert "stockfish_ok" in got

    res = client.post("/api/settings", json={"username": "panelname", "aliases": "chesscom:alt"}).json()
    assert res["settings"]["username"] == "panelname"
    # Live config (and therefore the app-config the frontend reads) reflects the change.
    assert config.USERNAME == "panelname"
    assert client.get("/api/app-config").json()["default_username"] == "panelname"
    # Persisted to settings.json under DATA_DIR.
    assert (tmp_path / "settings.json").exists()


def test_settings_rejects_bad_stockfish(monkeypatch, tmp_path):
    from server.core import settings as settings_mod

    monkeypatch.setattr(settings_mod.config, "DATA_DIR", str(tmp_path))
    r = client.post("/api/settings", json={"stockfish_path": "/definitely/not/stockfish"})
    assert r.status_code == 400 and "Stockfish" in r.json()["error"]


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
