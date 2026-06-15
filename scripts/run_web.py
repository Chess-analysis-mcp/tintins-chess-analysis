"""Run the web board standalone (without the MCP/Claude Code stdio path).

Analyses a PGN, populates the shared ReviewSession, opens the browser, then serves the
FastAPI board in the foreground. This is the primary manual-test entry point for the board.

Usage:
    STOCKFISH_PATH=/usr/local/bin/stockfish \
      /opt/miniconda3/envs/chess-review/bin/python scripts/run_web.py example_pgns/game1.pgn white
"""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from server import config
from server.core import engine
from server.core import session as session_mod
from server.core.game_analysis import analyze_game
from server.web.app import create_app


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "example_pgns/game1.pgn"
    player = sys.argv[2] if len(sys.argv) > 2 else "auto"
    pgn = Path(path).read_text()

    print(f"Analysing {path} (player={player}) ...", flush=True)
    t = time.time()
    sess = analyze_game(pgn, player=player)
    session_mod.set_session(sess)
    print(
        f"Done in {time.time() - t:.1f}s — {len(sess.mistakes)} mistakes flagged "
        f"(player={sess.player}).",
        flush=True,
    )

    url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    print(f"Serving board at {url}  (Ctrl-C to stop)", flush=True)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(create_app(), host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
