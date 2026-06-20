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

Write-Host "Chess Review MCP - installer" -ForegroundColor White
Write-Host ""

# 1) uv -------------------------------------------------------------------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Ok "uv already installed ($(uv --version))"
} else {
    Info "Installing uv (manages Python + dependencies for this project)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Warn "uv installed but not on PATH - open a new terminal and re-run install.ps1"
        exit 1
    }
    Ok "uv installed ($(uv --version))"
}

# 2) Stockfish ------------------------------------------------------------------------
if (Get-Command stockfish -ErrorAction SilentlyContinue) {
    Ok "Stockfish already installed"
} else {
    Info "Installing Stockfish engine..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id=Stockfish.Stockfish -e --accept-source-agreements --accept-package-agreements
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        choco install stockfish -y
    } else {
        Warn "Couldn't find winget or choco."
        Warn "Install Stockfish from https://stockfishchess.org/download/, then set STOCKFISH_PATH"
        Warn "in .mcp.json to the stockfish.exe path and re-run install.ps1."
        exit 1
    }
    Ok "Stockfish installed"
}

# 3) Build the project environment ----------------------------------------------------
Info "Setting up the Python environment with uv (first run downloads Python + deps)..."
uv sync
Ok "Environment ready"

# 4) Record your chess username -------------------------------------------------------
Write-Host ""
Info "Your Lichess/Chess.com username lets the tool tell which side is 'you' in a game."
$ChessUser = Read-Host "Username (press Enter to skip)"
if ($ChessUser) {
    $py = @"
import json, sys
p = '.mcp.json'
with open(p) as f:
    cfg = json.load(f)
cfg['mcpServers']['chess']['env']['CHESS_USERNAME'] = sys.argv[1]
with open(p, 'w') as f:
    json.dump(cfg, f, indent=2); f.write('\n')
"@
    uv run python -c $py $ChessUser
    Ok "Saved username to .mcp.json"
} else {
    Warn "Skipped - set CHESS_USERNAME in .mcp.json later if you want auto side-detection."
}

# 5) Self-check -----------------------------------------------------------------------
Write-Host ""
uv run python -m server.doctor

Write-Host ""
Write-Host "Done." -ForegroundColor White
Write-Host "Easiest: double-click `"Tintins AI Chess Analysis.bat`" to open the board with your latest Lichess game."
Write-Host "Or try a review from the terminal:"
Write-Host "    uv run python scripts/run_web.py example_pgns/game1.pgn white"
Write-Host "Or open Claude Code in this folder and ask it to analyze a game."
