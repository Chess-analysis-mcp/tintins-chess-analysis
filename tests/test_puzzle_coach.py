"""The puzzle coach facts builder + explain dispatch (engine + LLM mocked).

The facts block must LEAD with the themes (the headline signal that game analysis doesn't have),
carry the ground-truth solution line, and on a failure include the refutation of the user's move.
"""
from __future__ import annotations

from server import claude_bridge


PUZZLE = {
    "id": "z1",
    # Solver (White) to move; ...Kg8 was the setup, Ra8# the solution.
    "fen": "7k/5ppp/8/8/8/8/8/RR4K1 b - - 0 1",
    "solve_fen": "6k1/5ppp/8/8/8/8/8/RR4K1 w - - 0 1",
    "moves": ["h8g8", "a1a8"],
    "side_to_move": "white",
    "rating": 1100,
    "themes": ["backRankMate", "mate", "mateIn1"],
}


def _fake_engine_line(fen, move=None, **kw):
    if move:
        return {
            "move": {
                "move_san": "Kf1",
                "win_before": 99.0,
                "win_after": 12.0,
                "refutation_line_san": ["Ra8#"],
            }
        }
    return {"eval": "#1", "win_percent": 100.0, "best_san": "Ra8"}


def test_facts_lead_with_themes_and_carry_solution_line(monkeypatch):
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    facts = claude_bridge._puzzle_facts(PUZZLE, "solved_first_try")
    assert facts.startswith("Themes")
    assert "backRankMate" in facts
    assert "Solution line" in facts
    assert "Ra8" in facts  # the solution SAN appears
    assert "VERIFIED" in facts  # the "don't second-guess" framing


def test_failed_facts_include_the_users_move_and_refutation(monkeypatch):
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    facts = claude_bridge._puzzle_facts(PUZZLE, "failed", your_move="g1f1")
    assert "you played this" in facts
    assert "WRONG" in facts
    assert "Ra8#" in facts  # the refutation line is surfaced


def test_tried_moves_are_each_engine_grounded(monkeypatch):
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    tried = [
        {"uci": "a1a8", "fen_before": PUZZLE["solve_fen"], "correct": True, "ply_index": 1},
        {"uci": "g1f1", "fen_before": PUZZLE["solve_fen"], "correct": False, "ply_index": 1},
    ]
    facts = claude_bridge._puzzle_facts(PUZZLE, "failed", tried=tried)
    assert "Moves the player tried" in facts
    assert facts.count("you played this") == 2  # both tried moves are addressed
    assert "WRONG" in facts and "Ra8#" in facts  # the wrong try carries its engine refutation


def test_tried_moves_flow_through_explain_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    monkeypatch.setattr(claude_bridge.local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(claude_bridge.local_llm, "complete",
                        lambda prompt, **kw: captured.setdefault("prompt", prompt) or "ok")
    tried = [{"uci": "g1f1", "fen_before": PUZZLE["solve_fen"], "correct": False, "ply_index": 1}]
    claude_bridge.explain_puzzle(PUZZLE, "failed", tried=tried)
    assert "Moves the player tried" in captured["prompt"]


def test_solved_after_a_miss_still_explains_the_wrong_move(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    monkeypatch.setattr(claude_bridge.local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(claude_bridge.local_llm, "complete",
                        lambda prompt, **kw: captured.setdefault("prompt", prompt) or "ok")
    # Solved, but the tried log shows an earlier wrong move.
    tried = [
        {"uci": "g1f1", "fen_before": PUZZLE["solve_fen"], "correct": False, "ply_index": 1},
        {"uci": "a1a8", "fen_before": PUZZLE["solve_fen"], "correct": True, "ply_index": 1},
    ]
    claude_bridge.explain_puzzle(PUZZLE, "solved_with_hints", tried=tried)
    p = captured["prompt"]
    assert "only after a wrong attempt" in p  # the solved-after-miss role kicked in
    assert "DID NOT work" in p  # told to explain why their wrong move failed
    assert "WRONG" in p  # the wrong move's engine refutation is in the facts


def test_clean_solve_uses_the_positive_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    monkeypatch.setattr(claude_bridge.local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(claude_bridge.local_llm, "complete",
                        lambda prompt, **kw: captured.setdefault("prompt", prompt) or "ok")
    tried = [{"uci": "a1a8", "fen_before": PUZZLE["solve_fen"], "correct": True, "ply_index": 1}]
    claude_bridge.explain_puzzle(PUZZLE, "solved_first_try", tried=tried)
    assert "SOLVED this tactics puzzle cleanly" in captured["prompt"]
    assert "only after a wrong attempt" not in captured["prompt"]


def test_facts_degrade_without_engine(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no engine")

    monkeypatch.setattr(claude_bridge.lines, "engine_line", _boom)
    facts = claude_bridge._puzzle_facts(PUZZLE, "solved_first_try")
    # Still has themes + the ground-truth line even with the engine unavailable.
    assert facts.startswith("Themes")
    assert "Solution line" in facts


def test_explain_puzzle_uses_local_llm_when_enabled(monkeypatch):
    captured = {}

    def _complete(prompt, **kw):
        captured["prompt"] = prompt
        return "This is a classic **back-rank mate**."

    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    monkeypatch.setattr(claude_bridge.local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(claude_bridge.local_llm, "complete", _complete)

    out = claude_bridge.explain_puzzle(PUZZLE, "solved_first_try")
    assert "back-rank" in out["answer"]  # returns {answer, session_id} for the follow-up chat
    assert out["session_id"] is None  # local-LLM path has no threadable session
    assert "backRankMate" in captured["prompt"]  # themes made it into the prompt


def test_explain_puzzle_failed_prompt_asks_to_refute(monkeypatch):
    captured = {}

    def _complete(prompt, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(claude_bridge.lines, "engine_line", _fake_engine_line)
    monkeypatch.setattr(claude_bridge.local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(claude_bridge.local_llm, "complete", _complete)

    claude_bridge.explain_puzzle(PUZZLE, "failed", your_move="g1f1")
    assert "FAILED" in captured["prompt"]
    assert "you played this" in captured["prompt"]
