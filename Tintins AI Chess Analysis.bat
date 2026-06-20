@echo off
REM Tintin's AI Chess Analysis - double-click launcher (Windows).
REM
REM First run: installs everything (uv + Stockfish + deps) via install.ps1.
REM Every run: starts the board and opens your most recent Lichess game in the browser.
REM
REM Closing the browser tab quits the app: the server stops and this window closes itself.
REM You can also close this window directly to quit.
REM (First time: if Windows SmartScreen warns, click "More info" then "Run anyway".)

cd /d "%~dp0"

if "%CHESS_WEB_HOST%"=="" set "CHESS_WEB_HOST=127.0.0.1"
if "%CHESS_WEB_PORT%"=="" set "CHESS_WEB_PORT=8765"
set "URL=http://%CHESS_WEB_HOST%:%CHESS_WEB_PORT%"

REM Make a uv installed in the usual spot visible without a fresh shell.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

REM Already running? Just open the browser and stop.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/api/app-config' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo Tintin's AI Chess Analysis is already running - opening %URL%
  start "" "%URL%"
  exit /b 0
)

REM First-run install: no uv, or the project env hasn't been built yet.
where uv >nul 2>&1
if errorlevel 1 goto install
if not exist ".venv" goto install
goto launch

:install
echo First-time setup - installing Tintin's AI Chess Analysis (this happens only once)...
powershell -ExecutionPolicy Bypass -File install.ps1
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

:launch
echo Starting Tintin's AI Chess Analysis... keep this window open; close it to quit.
set "CHESS_APP_MODE=1"
uv run python scripts\run_web.py --serve
