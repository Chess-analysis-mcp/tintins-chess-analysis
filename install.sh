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

# Report install progress to the browser loading splash. The launcher points CHESS_INSTALL_PROGRESS
# at a `progress.js` sitting next to the splash's loading.html; the splash re-loads it as a <script>
# and moves its bar. Unset (a plain terminal `./install.sh`) → a no-op. Written atomically (tmp+mv)
# so the splash never reads a half-written file. See frontend/loading.html for the reader side.
progress() {
  [ -n "${CHESS_INSTALL_PROGRESS:-}" ] || return 0
  local pct="$1" step="${2:-}"
  step="${step//\\/\\\\}"; step="${step//\"/\\\"}"   # JS-string-safe
  { printf 'window.__setInstallProgress && window.__setInstallProgress({ pct: %s, step: "%s" });\n' \
      "$pct" "$step" > "$CHESS_INSTALL_PROGRESS.tmp" \
    && mv -f "$CHESS_INSTALL_PROGRESS.tmp" "$CHESS_INSTALL_PROGRESS"; } 2>/dev/null || true
}

bold "Chess Review MCP — installer"
echo
progress 5 "Preparing setup…"

# 1) uv — self-contained, no pre-existing Python needed. -------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv already installed ($(uv --version))"
else
  progress 8 "Installing the package manager (uv)…"
  info "Installing uv (manages Python + dependencies for this project)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or ~/.cargo/bin); make it visible for the rest of this run.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || { warn "uv installed but not on PATH — open a new terminal and re-run ./install.sh"; exit 1; }
  ok "uv installed ($(uv --version))"
fi

# 2) Build the project environment (downloads a compatible Python if needed). ----------
# Built before Stockfish so the download fallback below can run via `uv run python`.
progress 25 "Downloading Python & dependencies (largest step)…"
info "Setting up the Python environment with uv (first run downloads Python + deps)…"
uv sync
ok "Environment ready"
progress 70 "Environment ready"

# 3) Stockfish — try the OS package manager, else download the official static build. ---
progress 72 "Setting up the chess engine (Stockfish)…"
if command -v stockfish >/dev/null 2>&1; then
  ok "Stockfish already installed ($(command -v stockfish))"
else
  info "Installing Stockfish engine…"
  if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    # A GUI launcher can run translated under Rosetta 2 on Apple Silicon; the native
    # /opt/homebrew brew then refuses ("Cannot install under Rosetta 2 in ARM default prefix").
    # Re-exec brew natively when the hardware is arm64 and brew is the ARM-prefix build.
    BREW_ARCH=""
    if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null)" == "1" \
          && "$(brew --prefix 2>/dev/null)" == "/opt/homebrew" ]]; then
      BREW_ARCH="arch -arm64"
    fi
    $BREW_ARCH brew install stockfish || true
  elif command -v apt-get >/dev/null 2>&1; then
    { sudo apt-get update && sudo apt-get install -y stockfish; } || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y stockfish || true
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm stockfish || true
  fi
  if command -v stockfish >/dev/null 2>&1; then
    ok "Stockfish installed ($(command -v stockfish))"
  else
    # No package manager, or it failed / didn't put stockfish on PATH → download the official
    # static build into the app's managed engine dir (auto-detected; no sudo, no PATH changes).
    info "Downloading the official Stockfish engine (no package manager needed)…"
    # The downloader reports real byte-percent into this band, so the splash bar actually moves.
    if SF_PATH="$(CHESS_INSTALL_PROGRESS_BAND=72,92 uv run python scripts/download_stockfish.py)"; then
      ok "Stockfish downloaded ($SF_PATH)"
    else
      warn "Couldn't install or download Stockfish automatically."
      warn "Install it from https://stockfishchess.org/download/ and re-run this script,"
      warn "or set the Stockfish path in the app's ⚙ Settings panel."
    fi
  fi
fi

# 4) Record your chess username (optional). --------------------------------------------
# Saved to the user-level settings.json (shared by the app + MCP), NOT a tracked file — so the
# working tree stays clean and the launcher's one-click update can fast-forward without conflicts.
#
# Skipped entirely when non-interactive: the double-click launcher runs this installer with
# CHESS_NONINTERACTIVE=1 (and the user is watching the BROWSER splash, not this terminal), so a
# blocking `read` here would hang first-run forever — the server never starts, the splash never
# redirects, and the app looks frozen. The browser's own first-run prompt (#firstrun) collects the
# username instead. Also skip if stdin isn't a TTY (piped install), as a belt-and-suspenders guard.
progress 94 "Finishing setup…"
echo
if [ -n "${CHESS_NONINTERACTIVE:-}" ] || [ ! -t 0 ]; then
  info "Set your Lichess/Chess.com username on the app's first-run screen (or in ⚙ Settings)."
else
  info "Your Lichess/Chess.com username lets the tool tell which side is 'you' in a game."
  read -r -p "Username (press Enter to skip): " CHESS_USER || CHESS_USER=""
  if [[ -n "${CHESS_USER}" ]]; then
    uv run python - "$CHESS_USER" <<'PY'
import sys
from server.core import settings
settings.update({"username": sys.argv[1]})
PY
    ok "Saved username"
  else
    warn "Skipped — set it later in the app's ⚙ Settings panel if you want auto side-detection."
  fi
fi

# 5) Self-check. -----------------------------------------------------------------------
progress 97 "Almost ready…"
echo
uv run python -m server.doctor || true

progress 98 "Starting the board…"
echo
bold "Done."
echo "Easiest: double-click \"Tintin's AI Chess Analysis.command\" to open the board with your latest Lichess game."
echo "Or try a review from the terminal:"
echo "    uv run python scripts/run_web.py example_pgns/game1.pgn white"
echo "Or open Claude Code in this folder and ask it to analyze a game (the 'chess' MCP server is registered)."
