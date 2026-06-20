#!/usr/bin/env bash
#
# One-command setup for Chess Review MCP (macOS / Linux).
#
#   ./install.sh
#
# Installs uv (which manages Python for you), installs Stockfish, builds the project
# environment, optionally records your chess username, and runs a self-check. Safe to
# re-run: every step is skipped if it's already done.
set -euo pipefail

cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
info() { printf '\033[34m›\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1"; }

bold "Chess Review MCP — installer"
echo

# 1) uv — self-contained, no pre-existing Python needed. -------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv already installed ($(uv --version))"
else
  info "Installing uv (manages Python + dependencies for this project)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or ~/.cargo/bin); make it visible for the rest of this run.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || { warn "uv installed but not on PATH — open a new terminal and re-run ./install.sh"; exit 1; }
  ok "uv installed ($(uv --version))"
fi

# 2) Stockfish — not a pip package, install via the OS package manager. ----------------
if command -v stockfish >/dev/null 2>&1; then
  ok "Stockfish already installed ($(command -v stockfish))"
else
  info "Installing Stockfish engine…"
  if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    brew install stockfish
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y stockfish
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y stockfish
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm stockfish
  else
    warn "Couldn't find a package manager (brew/apt/dnf/pacman)."
    warn "Install Stockfish from https://stockfishchess.org/download/ then re-run this script."
    exit 1
  fi
  ok "Stockfish installed ($(command -v stockfish))"
fi

# 3) Build the project environment (downloads a compatible Python if needed). ----------
info "Setting up the Python environment with uv (first run downloads Python + deps)…"
uv sync
ok "Environment ready"

# 4) Record your chess username in .mcp.json (optional). -------------------------------
echo
info "Your Lichess/Chess.com username lets the tool tell which side is 'you' in a game."
read -r -p "Username (press Enter to skip): " CHESS_USER || CHESS_USER=""
if [[ -n "${CHESS_USER}" ]]; then
  uv run python - "$CHESS_USER" <<'PY'
import json, sys
p = ".mcp.json"
with open(p) as f:
    cfg = json.load(f)
cfg["mcpServers"]["chess"]["env"]["CHESS_USERNAME"] = sys.argv[1]
with open(p, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
  ok "Saved username to .mcp.json"
else
  warn "Skipped — set CHESS_USERNAME in .mcp.json later if you want auto side-detection."
fi

# 5) Self-check. -----------------------------------------------------------------------
echo
uv run python -m server.doctor || true

echo
bold "Done."
echo "Easiest: double-click \"Tintins AI Chess Analysis.command\" to open the board with your latest Lichess game."
echo "Or try a review from the terminal:"
echo "    uv run python scripts/run_web.py example_pgns/game1.pgn white"
echo "Or open Claude Code in this folder and ask it to analyze a game (the 'chess' MCP server is registered)."
