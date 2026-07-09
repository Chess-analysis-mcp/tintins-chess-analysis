#!/usr/bin/env bash
#
# Build a double-clickable macOS .app bundle for Tintin's AI Chess Analysis.
#
#   ./scripts/build_app.sh
#
# Produces  dist/Tintin's AI Chess Analysis.app  — drag it to /Applications and double-click.
#
# This is "Option A": a thin wrapper bundle. The .app embeds a read-only copy of the project
# under Contents/Resources/repo and ships a launcher that, on first run, installs uv + Stockfish
# (idempotent), builds the Python environment in a WRITABLE support dir (so the bundle itself
# stays immutable), then serves the board in app mode. Closing the browser tab quits it
# (app-liveness watchdog), exactly like the .command launcher.
#
# Not self-contained like a PyInstaller build: it still needs the network on first run to fetch
# uv + Python + Stockfish, and the in-browser chat still needs the user's own `claude` CLI.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

APP_NAME="Tintin's AI Chess Analysis"
BUNDLE_ID="com.thedarktintin.chessanalysis"
# Single source of truth: pyproject.toml's version (same value the runtime reads via config.APP_VERSION
# and the update-check compares against the latest GitHub release tag). Falls back to 0.0.0.
VERSION="$(grep -m1 -E '^[[:space:]]*version[[:space:]]*=' pyproject.toml 2>/dev/null | sed -E 's/.*"([^"]+)".*/\1/')"
VERSION="${VERSION:-0.0.0}"

# Build the bundle at the repo root so it's easy to find / double-click (gitignored).
DIST="$REPO_ROOT"
APP="$DIST/$APP_NAME.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
REPO_IN_APP="$RES/repo"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
info() { printf '\033[34m›\033[0m %s\n' "$1"; }

bold "Building $APP_NAME.app"

# 1) Clean + scaffold the bundle layout. -----------------------------------------------
# Once a .app has been launched, macOS tags it with `com.apple.provenance` and App Management
# protection blocks Terminal from deleting/overwriting its contents (EPERM on rm). Renaming the
# bundle to a NON-.app path is a parent-dir op that's still allowed, and the renamed dir is no
# longer treated as an app, so it can then be removed. So: rename-out-of-the-way, then delete.
if [ -e "$APP" ]; then
  info "Removing previous bundle…"
  OLD="$DIST/.old-$$.tmp"
  if mv "$APP" "$OLD" 2>/dev/null && rm -rf "$OLD" 2>/dev/null; then
    ok "Previous bundle removed"
  else
    rm -rf "$OLD" 2>/dev/null || true
    echo "ERROR: couldn't remove the existing bundle at:" >&2
    echo "  $APP" >&2
    echo "macOS App Management protection is blocking it. Either:" >&2
    echo "  • drag '$APP_NAME.app' to the Trash in Finder and re-run this script, or" >&2
    echo "  • grant your terminal 'App Management' permission in" >&2
    echo "    System Settings → Privacy & Security → App Management, then re-run." >&2
    exit 1
  fi
fi
mkdir -p "$MACOS" "$RES" "$REPO_IN_APP"

# 2) Copy the project in, excluding dev/local/generated junk. ---------------------------
# We need: server/, frontend/, scripts/, pyproject.toml, uv.lock. We deliberately drop the
# venv, git, caches, local data, and the dist dir we're writing into.
info "Copying project into the bundle…"
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude '.chess-review/' \
  --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.DS_Store' \
  --exclude '.claude/' \
  --exclude 'dist/' \
  --exclude 'node_modules/' \
  --exclude "$APP_NAME.app/" \
  "$REPO_ROOT"/ "$REPO_IN_APP"/
ok "Project copied"

# 3) Info.plist. -----------------------------------------------------------------------
ICON_LINE=""
if [ -f "$REPO_ROOT/assets/AppIcon.icns" ]; then
  cp "$REPO_ROOT/assets/AppIcon.icns" "$RES/AppIcon.icns"
  ICON_LINE='	<key>CFBundleIconFile</key>
	<string>AppIcon</string>'
  ok "Bundled custom icon (assets/AppIcon.icns)"
elif [ -f "$REPO_ROOT/assets/app_icon.png" ] && command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  info "Generating AppIcon.icns from assets/app_icon.png…"
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for SZ in 16 32 64 128 256 512; do
    sips -z "$SZ" "$SZ"       "$REPO_ROOT/assets/app_icon.png" --out "$ICONSET/icon_${SZ}x${SZ}.png"   >/dev/null
    sips -z $((SZ*2)) $((SZ*2)) "$REPO_ROOT/assets/app_icon.png" --out "$ICONSET/icon_${SZ}x${SZ}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"
  ICON_LINE='	<key>CFBundleIconFile</key>
	<string>AppIcon</string>'
  ok "Generated icon from assets/app_icon.png"
else
  info "No icon found — using the default app icon. Add assets/AppIcon.icns or assets/app_icon.png to customise."
fi

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleVersion</key>
	<string>$VERSION</string>
	<key>CFBundleShortVersionString</key>
	<string>$VERSION</string>
	<key>CFBundleExecutable</key>
	<string>launcher</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
$ICON_LINE
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<!-- Accessory/agent app: no Dock icon, no menu bar. Our executable is a shell script that runs a
	     server (the real UI is the browser tab), so it never registers with the window server. Without
	     this, macOS shows the "still launching" Dock bounce indefinitely. LSUIElement (not
	     LSBackgroundOnly) still lets the osascript error dialogs above display. The app quits itself
	     via the app-liveness watchdog when the browser tab is closed, so no Dock icon is needed. -->
	<key>LSUIElement</key>
	<true/>
</dict>
</plist>
PLIST
ok "Wrote Info.plist"

# 4) The launcher (Contents/MacOS/launcher). -------------------------------------------
# Runs when the .app is double-clicked. A GUI launch has a minimal PATH and NO terminal, so we:
#   - add the usual Homebrew / uv locations to PATH ourselves,
#   - log to a file (there's no console to print to),
#   - report fatal setup errors with a native dialog (osascript).
cat > "$MACOS/launcher" <<'LAUNCHER'
#!/bin/bash
# Tintin's AI Chess Analysis — .app launcher. Generated by scripts/build_app.sh.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../Resources/repo" && pwd)"

# Writable runtime home OUTSIDE the (read-only) bundle: the Python env + all user data live here,
# so the bundle stays immutable and your games/settings survive an app update.
SUPPORT="$HOME/Library/Application Support/Tintin AI Chess Analysis"
ENV_DIR="$SUPPORT/venv"
DATA_DIR="$SUPPORT/data"
LOG="$SUPPORT/launch.log"
mkdir -p "$SUPPORT" "$DATA_DIR"

# No console on a GUI launch — capture everything to a log file.
exec >>"$LOG" 2>&1
echo "=== launch $(date) ==="

# GUI launches get a bare PATH (no Homebrew, no ~/.local/bin). Add the usual spots so uv, brew,
# stockfish and claude are found.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin"

HOST="${CHESS_WEB_HOST:-127.0.0.1}"
PORT="${CHESS_WEB_PORT:-8765}"
URL="http://${HOST}:${PORT}"

die() {  # show a native error dialog, then exit non-zero
  /usr/bin/osascript -e "display dialog \"$1\" with title \"Tintin's AI Chess Analysis\" buttons {\"OK\"} default button \"OK\" with icon caution" >/dev/null 2>&1 || true
  exit 1
}

# Report install progress to the browser loading splash (see frontend/loading.html). The splash
# re-loads a sibling progress.js as a <script> and moves its bar; here we just rewrite that file
# atomically (tmp+mv). A no-op until CHESS_INSTALL_PROGRESS is set below.
progress() {
  [ -n "${CHESS_INSTALL_PROGRESS:-}" ] || return 0
  local pct="$1" step="${2:-}"
  step="${step//\\/\\\\}"; step="${step//\"/\\\"}"
  { printf 'window.__setInstallProgress && window.__setInstallProgress({ pct: %s, step: "%s" });\n' \
      "$pct" "$step" > "$CHESS_INSTALL_PROGRESS.tmp" \
    && mv -f "$CHESS_INSTALL_PROGRESS.tmp" "$CHESS_INSTALL_PROGRESS"; } 2>/dev/null || true
}

# Download the official static Stockfish straight from GitHub (no Homebrew needed) into $1, picking
# the build for this CPU and falling back to a more compatible one if it won't run. Returns non-zero
# on failure. curl-downloaded binaries aren't quarantined, so they run without a Gatekeeper prompt.
download_stockfish() {
  local dest="$1" arch tag tmp asset member
  arch="$(uname -m)"
  tag="sf_18"
  local candidates=()
  if [ "$arch" = "arm64" ]; then
    candidates=( "stockfish-macos-m1-apple-silicon" )
  elif sysctl -n machdep.cpu.leaf7_features 2>/dev/null | grep -q AVX2; then
    candidates=( "stockfish-macos-x86-64-avx2" "stockfish-macos-x86-64-sse41-popcnt" "stockfish-macos-x86-64" )
  else
    candidates=( "stockfish-macos-x86-64-sse41-popcnt" "stockfish-macos-x86-64" )
  fi
  mkdir -p "$(dirname "$dest")"
  for asset in "${candidates[@]}"; do
    member="stockfish/$asset"
    tmp="$(mktemp -d)"
    echo "Fetching $asset ($tag)…"
    if curl -fsSL -o "$tmp/sf.tar" "https://github.com/official-stockfish/Stockfish/releases/download/$tag/$asset.tar" \
       && tar -xf "$tmp/sf.tar" -C "$tmp" "$member" 2>/dev/null \
       && mv -f "$tmp/$member" "$dest" 2>/dev/null; then
      chmod +x "$dest"; rm -rf "$tmp"
      # Confirm this build actually runs on this CPU (an over-aggressive variant would crash).
      if printf 'uci\nquit\n' | "$dest" 2>/dev/null | grep -q "uciok"; then
        return 0
      fi
      echo "$asset didn't run on this CPU — trying a more compatible build…"
      rm -f "$dest"
    else
      rm -rf "$tmp"
    fi
  done
  return 1
}

# Already running? Just (re)open the browser and stop — don't start a second server.
if curl -fsS "${URL}/api/app-config" >/dev/null 2>&1; then
  echo "Already running — opening ${URL}"
  open "$URL" || true
  exit 0
fi

# A GUI launch shows NO window while the (slow, one-time) install + engine download run — so users
# think the app is frozen. Open a loading splash in the browser immediately: it polls the board URL
# and replaces itself with the real app the moment the server is up. CHESS_WEB_OPEN=0 (set below)
# stops the server opening a second tab. Spaces in the bundle path → %20 for a valid file:// URL.
# Copy the splash into the writable support dir so we can drop a sibling progress.js next to it (the
# bundle itself is read-only, so we can't write beside the in-bundle copy). The splash re-loads that
# file to advance a real progress bar during the slow first-run install. A failed copy falls back to
# the in-bundle splash (spinner only, no bar; no regression).
SPLASH_DIR="$SUPPORT/splash"
SPLASH="file://${REPO// /%20}/frontend/loading.html#${HOST}:${PORT}"
if mkdir -p "$SPLASH_DIR" 2>/dev/null && cp "$REPO/frontend/loading.html" "$SPLASH_DIR/loading.html" 2>/dev/null; then
  : > "$SPLASH_DIR/progress.js" 2>/dev/null || true   # clear any stale progress from a prior run
  export CHESS_INSTALL_PROGRESS="$SPLASH_DIR/progress.js"
  SPLASH="file://${SPLASH_DIR// /%20}/loading.html#${HOST}:${PORT}"
fi
echo "Opening loading splash: $SPLASH"
open "$SPLASH" || true
progress 5 "Preparing setup…"

# 1) uv — manages Python + deps (self-contained; no pre-existing Python needed).
if ! command -v uv >/dev/null 2>&1; then
  progress 8 "Installing the package manager (uv)…"
  echo "Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh \
    || die "Could not install 'uv'. Check your internet connection and open the app again."
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "'uv' was installed but isn't on PATH. Open a Terminal, run: curl -LsSf https://astral.sh/uv/install.sh | sh"
progress 20 "Package manager ready"

# 2) Stockfish — the chess engine (not a pip package). Prefer a system install / Homebrew; on a
#    clean Mac with neither, download the official static binary from GitHub (no Homebrew needed)
#    into the data dir. That path is exported as STOCKFISH_PATH *and* auto-detected by the app (it
#    lives under CHESS_DATA_DIR — see config._managed_stockfish_path), so it's found with zero config.
SF_MANAGED="$DATA_DIR/engine/stockfish"
progress 22 "Setting up the chess engine (Stockfish)…"
if command -v stockfish >/dev/null 2>&1; then
  echo "Stockfish found on PATH: $(command -v stockfish)"
elif [ -x "$SF_MANAGED" ]; then
  echo "Using previously downloaded Stockfish: $SF_MANAGED"
  export STOCKFISH_PATH="$SF_MANAGED"
elif command -v brew >/dev/null 2>&1; then
  echo "Installing Stockfish via Homebrew…"
  brew install stockfish || die "Could not install Stockfish via Homebrew. See $LOG."
else
  echo "No Stockfish and no Homebrew — downloading Stockfish…"
  download_stockfish "$SF_MANAGED" \
    || die "Could not download Stockfish. Check your internet connection and reopen the app, or install it from https://stockfishchess.org/download/."
  export STOCKFISH_PATH="$SF_MANAGED"
  echo "Stockfish ready: $SF_MANAGED"
fi

# 3) Build the Python environment in the writable support dir (NOT inside the bundle).
export UV_PROJECT_ENVIRONMENT="$ENV_DIR"
progress 45 "Downloading Python & dependencies (largest step)…"
echo "Syncing Python environment into $ENV_DIR …"
# --no-install-project: install only the locked DEPENDENCIES, not the project package itself.
#   run_web.py imports server.* via sys.path, so the package never needs building/installing — and
#   skipping it means uv writes NOTHING inside the (read-only, App-Management-protected) bundle.
# --frozen: never touch uv.lock (which lives in the read-only bundle).
( cd "$REPO" && uv sync --frozen --no-install-project ) \
  || die "Could not set up the Python environment. See $LOG."
progress 96 "Starting the board…"

# 4) Serve the board in app mode. Run the venv's python directly (no terminal, shallow process
#    tree) so closing the browser tab — or quitting the app — stops the server cleanly.
export CHESS_APP_MODE=1
export CHESS_APP_BUNDLE=1          # read-only bundle → update-check offers a download, not self-update
export CHESS_DATA_DIR="$DATA_DIR"
export CHESS_WEB_OPEN=0            # the splash tab opened above redirects itself — don't open another
export PYTHONDONTWRITEBYTECODE=1   # don't try to write .pyc into the read-only bundle
echo "Starting board at ${URL}"
cd "$REPO"
exec "$ENV_DIR/bin/python" "$REPO/scripts/run_web.py" --serve
LAUNCHER
chmod +x "$MACOS/launcher"
ok "Wrote launcher"

# 5) Tag the bundle so Finder/LaunchServices picks it up immediately. -------------------
touch "$APP"

echo
bold "Done."
echo "Built: $APP"
echo
echo "Try it:   open \"$APP\""
echo "Install:  drag it into /Applications (then double-click)."
echo "First open (unsigned app): double-click, then System Settings -> Privacy & Security -> Open Anyway."
echo "Logs:     ~/Library/Application Support/Tintin AI Chess Analysis/launch.log"
