# One-command setup for Chess Review MCP (Windows).
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Installs uv (which manages Python for you), installs Stockfish, builds the project
# environment, optionally records your chess username, and runs a self-check. Safe to
# re-run: every step is skipped if it's already done.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Ok($m)   { Write-Host "[ok] $m" -ForegroundColor Green }
function Info($m) { Write-Host "> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

# Report install progress to the browser loading splash. The launcher points CHESS_INSTALL_PROGRESS
# at a progress.js sitting next to the splash's loading.html; the splash re-loads it as a <script>
# and moves its bar. Unset (a plain `install.ps1` run) -> a no-op. Written atomically (tmp+rename)
# so the splash never reads a half-written file. See frontend/loading.html for the reader side.
function Progress($pct, $step) {
    if (-not $env:CHESS_INSTALL_PROGRESS) { return }
    $safe = ($step -replace '\\', '\\') -replace '"', '\"'
    $line = "window.__setInstallProgress && window.__setInstallProgress({ pct: $pct, step: `"$safe`" });"
    try {
        $tmp = "$($env:CHESS_INSTALL_PROGRESS).tmp"
        Set-Content -Path $tmp -Value $line -Encoding UTF8 -NoNewline
        Move-Item -Force -Path $tmp -Destination $env:CHESS_INSTALL_PROGRESS
    } catch {}
}

Write-Host "Chess Review MCP - installer" -ForegroundColor White
Write-Host ""
Progress 5 "Preparing setup..."

# 1) uv -------------------------------------------------------------------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Ok "uv already installed ($(uv --version))"
} else {
    Progress 8 "Installing the package manager (uv)..."
    Info "Installing uv (manages Python + dependencies for this project)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Warn "uv installed but not on PATH - open a new terminal and re-run install.ps1"
        exit 1
    }
    Ok "uv installed ($(uv --version))"
}

# 2) Build the project environment ----------------------------------------------------
# Built before Stockfish so the download fallback below can run via `uv run python`.
Progress 25 "Downloading Python & dependencies (largest step)..."
Info "Setting up the Python environment with uv (first run downloads Python + deps)..."
uv sync
Ok "Environment ready"
Progress 70 "Environment ready"

# 3) Stockfish - try winget/choco, else download the official static build. ------------
Progress 72 "Setting up the chess engine (Stockfish)..."
if (Get-Command stockfish -ErrorAction SilentlyContinue) {
    Ok "Stockfish already installed"
} else {
    Info "Installing Stockfish engine..."
    # Wrapped so a package-manager failure (which throws under PowerShell 7's native-command
    # error handling) still falls through to the direct-download fallback below.
    try {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id=Stockfish.Stockfish -e --accept-source-agreements --accept-package-agreements
        } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
            choco install stockfish -y
        }
    } catch {
        Warn "Package-manager install didn't complete; trying a direct download instead."
    }
    # winget/choco often don't put stockfish on PATH for this session (or aren't present), so
    # re-check; if it's still missing, download the official static build into the app's managed
    # engine dir (auto-detected by the app - no admin, no PATH changes).
    if (Get-Command stockfish -ErrorAction SilentlyContinue) {
        Ok "Stockfish installed"
    } else {
        Info "Downloading the official Stockfish engine (no package manager needed)..."
        # The downloader reports real byte-percent into this band, so the splash bar actually moves.
        $env:CHESS_INSTALL_PROGRESS_BAND = "72,92"
        try { $SfPath = uv run python scripts/download_stockfish.py } catch { $SfPath = $null }
        if ($SfPath) {
            Ok "Stockfish downloaded ($SfPath)"
        } else {
            Warn "Couldn't install or download Stockfish automatically."
            Warn "Install it from https://stockfishchess.org/download/ and re-run install.ps1,"
            Warn "or set the Stockfish path in the app's Settings panel."
        }
    }
}

# 4) Record your chess username -------------------------------------------------------
# Saved to the user-level settings.json (shared by app + MCP), NOT a tracked file — so the working
# tree stays clean and the launcher's one-click update can fast-forward without conflicts.
Progress 94 "Finishing setup..."
Write-Host ""
Info "Your Lichess/Chess.com username lets the tool tell which side is 'you' in a game."
$ChessUser = Read-Host "Username (press Enter to skip)"
if ($ChessUser) {
    $py = @"
import sys
from server.core import settings
settings.update({'username': sys.argv[1]})
"@
    uv run python -c $py $ChessUser
    Ok "Saved username"
} else {
    Warn "Skipped - set it later in the app's Settings panel if you want auto side-detection."
}

# 5) Self-check -----------------------------------------------------------------------
Progress 97 "Almost ready..."
Write-Host ""
uv run python -m server.doctor

Progress 98 "Starting the board..."
Write-Host ""
Write-Host "Done." -ForegroundColor White
Write-Host "Easiest: double-click `"Tintin's AI Chess Analysis.bat`" to open the board with your latest Lichess game."
Write-Host "Or try a review from the terminal:"
Write-Host "    uv run python scripts/run_web.py example_pgns/game1.pgn white"
Write-Host "Or open Claude Code in this folder and ask it to analyze a game."
