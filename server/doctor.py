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


def _check_claude() -> bool:
    path = shutil.which("claude")
    if path:
        print(f"{OK} claude CLI: {path}")
        return True
    print(f"{WARN} claude CLI: not found (optional)")
    print("    Only needed for the in-browser 'why?' chat and the Claude Code terminal "
          "workflow. Install from https://claude.com/claude-code and run `claude login`.")
    return True  # optional: never fails the overall check


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
