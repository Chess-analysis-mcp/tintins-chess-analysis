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

REM Set when we re-launch ourselves after applying a git update (see the apply block). On a relaunch
REM we skip the splash (already open) and the update step (already done), and just start the board.
set "RELAUNCHED="
if /I "%~1"=="--updated" set "RELAUNCHED=1"

REM Make a uv installed in the usual spot visible without a fresh shell.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"

REM Already running? Just open the browser and stop.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/api/app-config' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo Tintin's AI Chess Analysis is already running - opening %URL%
  start "" "%URL%"
  exit /b 0
)

REM Open a loading splash in the browser RIGHT NOW so first-time users see progress while the
REM (slow, one-time) install + engine download run - instead of a blank screen that looks frozen.
REM It polls the board URL and swaps itself for the real app once the server is up; CHESS_WEB_OPEN=0
REM (below) stops the server opening a second tab. (%CD:\=/% turns the path into a file:// URL.)
REM Copy the splash into a writable scratch dir so the installer can drop a sibling progress.js beside
REM it - the splash re-loads that file to advance a real progress bar during the slow first-run install.
REM A failed copy just opens the in-place splash (spinner only, no bar; no regression).
set "SPLASH_DIR=%TEMP%\tintin-chess-splash"
set "SPLASH=file:///%CD:\=/%/frontend/loading.html#%CHESS_WEB_HOST%:%CHESS_WEB_PORT%"
if not exist "%SPLASH_DIR%" mkdir "%SPLASH_DIR%" >nul 2>&1
copy /Y "frontend\loading.html" "%SPLASH_DIR%\loading.html" >nul 2>&1
if exist "%SPLASH_DIR%\loading.html" (
  type nul > "%SPLASH_DIR%\progress.js"
  set "CHESS_INSTALL_PROGRESS=%SPLASH_DIR%\progress.js"
  set "SPLASH=file:///%SPLASH_DIR:\=/%/loading.html#%CHESS_WEB_HOST%:%CHESS_WEB_PORT%"
)
if not defined RELAUNCHED start "" "%SPLASH%"

REM Apply a staged update (dropped by the in-app "Update now" button) before starting. Best-effort.
REM Skipped on a relaunch (already applied). cmd reads this .bat from disk as it runs, so after a git
REM pull (which can rewrite this file) we re-launch a FRESH copy and exit, rather than keep executing
REM a file that changed underneath us.
if not defined RELAUNCHED if exist ".update-requested" call :apply_update
if defined RELAUNCH (
  start "" "%~f0" --updated
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
set "CHESS_WEB_OPEN=0"
uv run python scripts\run_web.py --serve
goto :eof

REM --- staged-update apply (called via "call :apply_update"; git pull or source-zip extract) ------
:apply_update
echo Applying a staged update...
if not exist ".git" goto apply_zip
where git >nul 2>&1 && goto apply_git
echo   Git isn't installed, so updating by download instead - install Git from
echo   https://git-scm.com/downloads for faster updates.
goto apply_zip
:apply_git
git stash --include-untracked -q 2>nul
git pull --ff-only -q 2>nul
git stash pop -q 2>nul
uv sync >nul 2>&1
set "RELAUNCH=1"
goto apply_done
:apply_zip
REM The download updater never touches the launcher scripts, so no re-launch is needed here.
where uv >nul 2>&1 || goto apply_nouv
uv run python scripts\apply_update.py
if not errorlevel 1 uv sync >nul 2>&1
goto apply_done
:apply_nouv
echo   Couldn't update automatically (neither Git nor uv is available).
echo   Download the latest version from the Releases page instead.
:apply_done
del ".update-requested" >nul 2>&1
goto :eof
