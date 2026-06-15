"""Configuration for the chess review server.

All tunables live here so the engine, analysis, and MCP layers agree on defaults.
Values can be overridden via environment variables.
"""
from __future__ import annotations

import os

# Path to the Stockfish binary. Defaults to "stockfish" on PATH; the .mcp.json
# registration sets this explicitly to /usr/local/bin/stockfish.
STOCKFISH_PATH: str = os.environ.get("STOCKFISH_PATH", "stockfish")

# Depth used for on-demand single-position analysis (get_engine_line, REPL checks).
# Fixed depth keeps evals reproducible and cacheable.
DEFAULT_DEPTH: int = int(os.environ.get("CHESS_DEFAULT_DEPTH", "18"))

# Depth used when sweeping every ply of a full game. Lower than DEFAULT_DEPTH so a
# full-game review finishes in reasonable time; positions can be re-deepened on
# demand via get_engine_line.
SWEEP_DEPTH: int = int(os.environ.get("CHESS_SWEEP_DEPTH", "16"))

DEFAULT_MULTIPV: int = int(os.environ.get("CHESS_DEFAULT_MULTIPV", "1"))

# Engine process pool size. 1-2 is plenty for a single-user local tool. Default 2 so the
# web /evaluate route and a concurrent MCP call don't serialise behind one engine.
ENGINE_POOL_SIZE: int = int(os.environ.get("CHESS_ENGINE_POOL_SIZE", "2"))

# Per-engine UCI options.
ENGINE_THREADS: int = int(os.environ.get("CHESS_ENGINE_THREADS", "2"))
ENGINE_HASH_MB: int = int(os.environ.get("CHESS_ENGINE_HASH_MB", "128"))

# Centipawn magnitude treated as "mate-equivalent" when converting mate scores.
MATE_SCORE_CP: int = 10000

# Used by analyze_game(player="auto") to pick which side is "me" from PGN headers.
USERNAME: str = os.environ.get("CHESS_USERNAME", "thedarktintin")

# Web board (Phase 4). The FastAPI server runs in the same process as the MCP server,
# sharing the one engine pool and ReviewSession. WEB_AUTOSTART=0 disables the autostart
# (e.g. when driving the web server standalone via scripts/run_web.py).
WEB_HOST: str = os.environ.get("CHESS_WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.environ.get("CHESS_WEB_PORT", "8765"))
WEB_AUTOSTART: bool = os.environ.get("CHESS_WEB_AUTOSTART", "1") != "0"
