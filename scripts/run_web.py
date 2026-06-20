"""Run the web board standalone (without the MCP/Claude Code stdio path).

Two modes:

  - `scripts/run_web.py <pgn> <side> [elo]` — analyse a PGN up front, populate the shared
    ReviewSession, open the browser, then serve. The primary manual-test entry point.
  - `scripts/run_web.py --serve` (or no args) — just serve the board with an empty session and
    open the browser. This is the "app mode" launch used by the double-click launchers (which set
    CHESS_APP_MODE=1): the frontend then auto-loads the user's most recent Lichess game.

Usage:
    STOCKFISH_PATH=/usr/local/bin/stockfish \
      /opt/miniconda3/envs/chess-review/bin/python scripts/run_web.py example_pgns/game1.pgn white [elo]

The optional 3rd arg is the player's Elo (overrides the PGN); omit it to read Elo from the PGN
headers (or fall back to default sensitivity).
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
from server.core import history
from server.core import session as session_mod
from server.core import settings
from server.core.game_analysis import analyze_game
from server.web.app import create_app


def main() -> int:
    settings.apply_saved()  # settings.json (set via the app's Settings panel) overrides env config
    args = sys.argv[1:]
    serve_only = not args or args[0] == "--serve"

    if not serve_only:
        path = args[0]
        player = args[1] if len(args) > 1 else "auto"
        elo = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
        pgn = Path(path).read_text()

        print(f"Analysing {path} (player={player}, elo={elo or 'from PGN/default'}) ...", flush=True)
        t = time.time()
        sess = analyze_game(pgn, player=player, elo=elo)
        session_mod.set_session(sess)
        # Persist for the Games panel + coaching profile (best-effort), like the MCP analyze_game tool.
        if config.HISTORY_ENABLED:
            try:
                history.record_game(sess)
            except Exception as exc:  # never let history break the launcher
                print(f"[chess-history] could not record game: {exc}", file=sys.stderr, flush=True)
        print(
            f"Done in {time.time() - t:.1f}s — {len(sess.mistakes)} mistakes flagged "
            f"(player={sess.player}).",
            flush=True,
        )
    else:
        # App-mode launch: no PGN up front. The frontend auto-loads the most recent Lichess game
        # (it reads /api/app-config; the launcher sets CHESS_APP_MODE=1).
        print("Starting Chess Review (app mode) — your most recent Lichess game opens in the browser.", flush=True)

    url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    print(f"Serving board at {url}  (keep this window open; close it or press Ctrl-C to quit)", flush=True)
    if config.WEB_OPEN:  # CHESS_WEB_OPEN=0 keeps the browser from opening (e.g. tests/headless)
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(create_app(), host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
