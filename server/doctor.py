"""Setup self-check: `uv run python -m server.doctor`.

Verifies the three things a fresh install needs — a new-enough Python, a working
Stockfish binary, and (optionally) the `claude` CLI for the in-browser chat — and
prints exactly what's missing with a copy-pasteable fix. Exit code 0 means the core
(Python + Stockfish) is ready; the `claude` CLI is reported but never fails the check.
"""
from __future__ import annotations

import shutil
import sys

from server import config

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def _check_python() -> bool:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    mark = OK if ok else BAD
    print(f"{mark} Python {v.major}.{v.minor}.{v.micro}")
    if not ok:
        print("    Need Python 3.11+. With uv this is automatic — run the install script "
              "(see README) so uv fetches a compatible Python.")
    return ok


def _check_stockfish() -> bool:
    path = config.STOCKFISH_PATH
    resolved = shutil.which(path) or (path if "/" in path else None)
    if resolved is None:
        print(f"{BAD} Stockfish: not found")
        print(f"    {config.stockfish_install_hint()}")
        return False
    # Confirm it actually launches and speaks UCI, not just that a file exists.
    try:
        import chess.engine

        eng = chess.engine.SimpleEngine.popen_uci(resolved)
        try:
            name = eng.id.get("name", "Stockfish")
        finally:
            eng.quit()
        print(f"{OK} Stockfish: {name}  ({resolved})")
        return True
    except Exception as exc:  # noqa: BLE001 - report any launch failure plainly
        print(f"{BAD} Stockfish at {resolved} would not start: {exc}")
        print(f"    {config.stockfish_install_hint(resolved)}")
        return False


def _local_llm_enabled() -> bool:
    return bool((config.LOCAL_LLM_BASE_URL or "").strip())


def _check_claude() -> bool:
    path = shutil.which("claude")
    if path:
        print(f"{OK} claude CLI: {path}")
        return True
    if _local_llm_enabled():
        print(f"{OK} AI features: using a local AI model ({config.LOCAL_LLM_BASE_URL}) — "
              "the claude CLI isn't needed.")
        return True
    print(f"{WARN} claude CLI: not found (optional)")
    print("    Only needed for the in-browser 'why?' chat and the Claude Code terminal "
          "workflow. Install from https://code.claude.com/docs/en/quickstart and run `claude login`, "
          "or set a local AI model in Settings.")
    return True  # optional: never fails the overall check


def status() -> dict:
    """Structured self-check for the web UI (``GET /api/doctor``).

    Lightweight on purpose — it resolves binaries on PATH rather than launching Stockfish, so it's
    cheap to call on every page load. Never raises. ``claude`` is flagged ``optional`` because the
    core review works without it (only the AI chat + AI coach summary need it).
    """
    v = sys.version_info
    sf_path = config.STOCKFISH_PATH
    sf_resolved = shutil.which(sf_path) or (sf_path if "/" in sf_path else None)
    claude_path = shutil.which("claude")
    # A configured local AI model serves the chat/coach over direct HTTP, so the `claude` CLI is
    # not needed at all — report the check satisfied so the UI doesn't nag to install it.
    local_llm_on = _local_llm_enabled()
    return {
        "python": {
            "ok": (v.major, v.minor) >= (3, 11),
            "detail": f"{v.major}.{v.minor}.{v.micro}",
        },
        "stockfish": {
            "ok": bool(sf_resolved),
            "path": sf_resolved or "",
            "hint": "" if sf_resolved else config.stockfish_install_hint(),
            # macOS/Apple-Silicon only: flags an Intel build running under Rosetta 2 so the UI can
            # offer a one-click swap to the native arm64 engine. Best-effort; {suboptimal:False} off-mac.
            "arch": config.stockfish_arch_report(sf_resolved) if sf_resolved else {"suboptimal": False},
        },
        "claude": {
            "ok": bool(claude_path) or local_llm_on,
            "optional": True,
            "path": claude_path or "",
            "detail": "not needed — using local AI" if (local_llm_on and not claude_path) else "",
            "hint": ""
            if (claude_path or local_llm_on)
            else "Needed only for the in-browser AI chat and the AI coach summary. Install from "
            "https://code.claude.com/docs/en/quickstart, then run `claude login` — or set a local "
            "AI model in Settings.",
        },
    }


def main() -> int:
    print("Chess Review MCP — setup check\n")
    py_ok = _check_python()
    sf_ok = _check_stockfish()
    _check_claude()  # advisory only

    print()
    if py_ok and sf_ok:
        print(f"{OK} Core is ready. Review a game with:")
        print("    uv run python scripts/run_web.py example_pgns/game1.pgn white")
        return 0
    print(f"{BAD} Setup incomplete — fix the items marked above and re-run `uv run python -m server.doctor`.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
