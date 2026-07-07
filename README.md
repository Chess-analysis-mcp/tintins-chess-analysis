# Chess Review MCP

[![CI](https://github.com/Chess-analysis-mcp/tintins-chess-analysis/actions/workflows/ci.yml/badge.svg)](https://github.com/Chess-analysis-mcp/tintins-chess-analysis/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A chess coach you can actually **talk to**, and one that **doesn't make things up**. Ask why a move
was a mistake or what you should have played, and get a straight answer **in words**, grounded in
real **Stockfish** lines instead of guessed. Under the hood it reviews your game with the engine and
finds exactly where you went wrong; the difference is it then explains it. Works with games from
**anywhere** (Lichess, Chess.com, or any PGN you can paste). Both Lichess and Chess.com games can
be fetched by username, your latest game auto-loads on launch, and new **Chess.com games sync
automatically** into your history. Lichess players get a few extras. It runs two ways: from the **Claude Code
terminal** (as an MCP server) and as an **interactive web board** that share one engine and one
analysis, so they never disagree.

![Chess Review board: the board with played, best, and refutation arrows, an eval bar, a win graph, the mistake list, the Snowie AI coach, and the Games panel](docs/screenshots/chess_new_pipeline.png)

> **Requires a Claude subscription** *(or a local model)***.** The AI-coach explanations and chat
> (Snowie) — the whole "explained in words" part — run on **headless `claude`, using your existing
> Claude subscription** (no API key, no per-token billing). You need the [`claude`
> CLI](https://docs.claude.com/en/docs/claude-code/overview) installed and logged in
> (`claude login`). Prefer to stay fully offline? You can instead point the AI coach at a **local
> model** (Ollama, LM Studio, …) with no Claude account at all — see
> [Run the AI on a local model](#run-the-ai-on-a-local-model-instead-of-claude). Either way, the
> engine review itself (mistake list, eval bar, win graph, arrows) works without any of it.

> **New here? Pick your goal:**
> - 🎯 **I just want to review my games** → [get the app](#-i-just-want-to-review-my-games) (download on Mac, or double-click the launcher on Windows/Linux — it sets itself up).
> - 🤖 **I want chess analysis inside Claude Code** → [run the installer](#-i-want-it-inside-claude-code).

---

## Features

- **Full-game review** → an ordered list of your inaccuracies / mistakes / blunders with per-side
  accuracy, using Lichess-style win%-drop thresholds (5 / 10 / 15).
- **Explanations in words.** Every flagged move gets a concrete, engine-grounded comment (the
  better move, its line, and how your move gets punished). No guessing: it's built from the engine
  sweep.
- **Interactive board** (chessground): replay each mistake, try your own moves, free-explore down
  any line.
- **Eval bar + Lichess-style win graph** that orient to the side you're reviewing (black-on-bottom
  when you played black). Click the graph or use ← / → to scrub the whole game.
- **Move arrows:** gray = the move you played, green = engine best moves (live **multi-PV** with
  **progressive deepening**, thicker arrow = better move), red = the refutation of a move you try and yellow = threats (toggle **Show threats** or press `t`) to show what the opponent is threatening to play next.
- **Chess.com auto-sync.** If your Chess.com username is set, your latest games will be fetched
 and analyzed (alternatively you cna use the ⟳ Sync button on the Chess.com tab).
- **Insights panel.** Recurring themes and stats aggregated across every analyzed game in a chosen
  period (today / past 7 days / past 30 days / all time). Insights inclulde record, average accuracy, your most common mistake motifs, weakest phase, and most-played openings.
- **In-browser AI coach (Snowie).** A "why? / what now?" chat powered by headless `claude -p`
  (your Claude subscription), fed pre-computed engine facts so answers are grounded, not estimated.
- **Cross-game history + coaching profile.** Every reviewed game is saved locally, tagged with
  recurring mistake motifs (hung pieces, missed forks, back-rank, time trouble…), and rolled up
  into a per-player profile. With **"Personalize AI coach with my history"** on (in **⚙ Settings**),
  Claude can draw on your recurring patterns — only when they actually bear on the position at hand.

<!-- TODO: screenshot of the mistake list + the engine-grounded comment box.
     Show the right-sidebar "Mistakes" list and, below the board, the green comment box for a
     selected blunder (e.g. "Nf3 is a blunder: your win chance falls from 80.8% to 56.9% ...").
     Save as docs/screenshots/mistakes-and-comment.png -->
<!-- ![Mistake list and comment](docs/screenshots/mistakes-and-comment.png) -->

---

## How it works

One Python process holds one Stockfish engine pool and one in-memory review session. The MCP server
(for Claude Code) and the FastAPI web server run in that **same process** and share the session, so
the terminal and the board are always looking at the same analysis.

<p align="center">
  <img src="docs/chess_mcp_architecture.svg" alt="Architecture: Claude Code (terminal) and a browser board both connect into one Python process holding an MCP server, a shared Stockfish engine + session core, and a FastAPI web server; the backend shells out to headless claude -p for in-browser chat." width="660">
</p>

The browser's "why?" chat is the only part that reaches outside the process: the FastAPI backend
shells out to headless `claude -p` (your subscription). 

---

## Installation

There are only **two** things you might want, so pick one:

| Your goal | Go to |
| --- | --- |
| 🎯 **Just review my chess games** (from Lichess, Chess.com, or any PGN) | [I just want to review my games](#-i-just-want-to-review-my-games) |
| 🤖 **Use chess analysis inside Claude Code** (the MCP server) | [I want it inside Claude Code](#-i-want-it-inside-claude-code) |

Either way you need **nothing installed first** — no Python, no Stockfish. The app/installer fetches
everything (it uses [uv](https://docs.astral.sh/uv/) to download a compatible Python for you).

### 🎯 I just want to review my games

Zero terminal required. Open the app and it brings up a chess board in your browser, ready to
review a game from **any source** — paste or upload a PGN, or, if you
play on **Lichess** or **Chess.com**, just give it your username and it loads your recent games for you.

<a id="review-mac"></a>
**macOS — the app.** Download the zip from the
[Releases page](https://github.com/Chess-analysis-mcp/tintins-chess-analysis/releases). Unzipping it
gives you **`Tintin's AI Chess Analysis.app`**. Drag that into **Applications**, and open it.

- **First open:** it's unsigned, so macOS blocks it once. Double-click it (you'll get a "cannot
  verify" / "blocked" message — click **Done**), then go to **System Settings → Privacy &
  Security**, scroll down to the message about the app, and click **Open Anyway → Open**. You only
  do this once. *(On older macOS you can instead right-click the app → **Open** → **Open**.)*
- The app installs Stockfish + its Python env on first run (needs internet once), then opens the
  board. Its environment and your games/settings live in
  `~/Library/Application Support/Tintin AI Chess Analysis/`, **outside** the app, so your data
  survives updates. Setup problems show in a dialog; logs go to that folder's `launch.log`.

<a id="review-windows-linux"></a>
**Windows / Linux — the double-click launcher.** Three steps, no terminal, no GitHub account needed.

**Step 1 — download the project as a ZIP.** On this project's GitHub page, scroll to the **top**,
click the green **`< > Code`** button, and choose **Download ZIP** (see the circled buttons below).

<p align="center">
  <img src="docs/screenshots/install_image.png" alt="On the GitHub page, click the green 'Code' button near the top right, then 'Download ZIP'." width="560">
</p>

**Step 2 — unzip it.** Double-click the downloaded `.zip` to extract it. You'll get a folder named
something like **`tintins-chess-analysis`** — open it. (On Windows, if it opens "inside" the zip
first, drag the folder out to your Desktop, or right-click the zip → **Extract All**.)

**Step 3 — double-click the launcher** inside that folder:

- **Windows:** **`Tintin's AI Chess Analysis.bat`** (if SmartScreen warns: **More info → Run anyway**)
- **Linux:** **`Tintin's AI Chess Analysis.command`**

The **first launch** installs everything (uv + Stockfish + the env) and can take a couple of
minutes — a loading page opens in your browser while it works. Every launch after opens straight to
the board.

**Once the board is open (any platform), load a game from wherever you play** — via the **Games**
panel (☰), which has four tabs:

- **Paste PGN** — works for *everyone*. Paste any PGN, or **Upload .pgn** a file (e.g. a Chess.com
  export of one *or many* games), pick your side, and click **Analyze**; all games land in **My
  games**. This is the universal path — no account of any kind needed.
- **Lichess** — type any handle to fetch that player's recent games,
  and click **"Set as my account"** to make it yours: that drives **My games**, your coaching
  profile, and (in the app) auto-loading your latest game on launch.
- **Chess.com** — Enter any chess.com username to fetch that player's recent games, or press 
  **⟳ Sync new games** to retrieve any new games of your own if your Chess.com username is set.
- **My games** — your previously-analyzed games, to reopen instantly.

Your account/username is set under **⚙ Settings → Your username** (or the Lichess panel's button)
and saved server-side, so it's the same everywhere. **To quit:** just close the browser tab — the
server stops a few seconds later (and the launcher's terminal window closes itself).

### 🤖 I want it inside Claude Code

Run the one-command installer from the repo root, then reload Claude Code — the `chess` MCP server
is already registered in `.mcp.json` (no path editing; it runs via `uv`, which is machine-independent).

```bash
# macOS / Linux
./install.sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script is safe to re-run (each step is skipped if already done), prompts for your username, and
finishes with a self-check you can re-run any time:

```bash
uv run python -m server.doctor
```

Then open Claude Code in this folder, paste a PGN, and say *"analyze this game."* You also get the
web board for free — see [Usage](#usage).

<details>
<summary>Advanced: build the macOS .app yourself, or skip uv</summary>

**Build the `.app` from source** (instead of downloading it) — produces the same bundle the
Releases page ships:

```bash
./scripts/build_app.sh        # → Tintin's AI Chess Analysis.app (in the repo root)
```

Drag it into **/Applications** and open it (first time, unsigned: double-click, then **System
Settings → Privacy & Security → Open Anyway**). The bundle is
immutable at runtime; its Python env + your data live under
`~/Library/Application Support/Tintin AI Chess Analysis/`. To customise the icon, replace
`assets/app_icon.png` (1024×1024) or drop an `assets/AppIcon.icns`, then rebuild. It still needs the
network on first run and the `claude` CLI for the in-browser chat.

**Skip uv (plain venv / conda).** The project is a standard `pyproject.toml`, so any Python 3.11+
environment works:

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # or conda
pip install -r requirements.txt
```

Then in `.mcp.json` set `"command"` to that interpreter's absolute path (e.g.
`/abs/path/.venv/bin/python`) and `"args"` to `["-m", "server.mcp_server"]`, and run scripts with
that interpreter instead of `uv run python`.

</details>

### Prerequisites (what the app/installer handles for you)

- **Stockfish** engine (the installer adds it via `brew` / `apt` / `winget`, and if no package
  manager is available it downloads the official static build automatically, so no manual setup is
  needed on any platform). The tool auto-detects a normal install, so no path configuration is
  needed. To use a custom build, set `STOCKFISH_PATH`.
- **Internet connection** for the web board's first load (chessground / chess.js come from a CDN, so
  there's no Node/npm build step).
- **A Claude subscription + the `claude` CLI** (Claude Code), installed and logged in
  (`claude login`) — required for the AI-coach explanations and chat (Snowie) and for the Claude
  Code terminal workflow. It runs on your **existing subscription**, no API key or per-token
  billing. The web board's bare engine review (mistakes, eval bar, arrows) works without it, but the
  plain-English explanations — the point of the tool — do not.

---

## Usage

### Option A: the web board (no Claude Code required)

The quickest way to review a game. Pass a PGN file and which color you played:

```bash
uv run python scripts/run_web.py example_pgns/game1.pgn white
```

It analyzes the game (~20 to 45s depending on length), opens your browser to
`http://127.0.0.1:8765`, and you can:

1. Click a mistake in the sidebar → the board jumps to that position (gray arrow = the move you
   played) and a written explanation appears.
2. Drag a piece to try a better move → eval bar + a verdict update; a red arrow shows the
   refutation if it's bad.
3. Toggle **Show best move** to see the engine's top move(s) as green arrows that sharpen as the
   search deepens.
4. Scrub the whole game with ← / → or by clicking the win graph; **Back** undoes one ply when
   you're exploring a line.
5. Ask **"why is this bad?"** or **"what should I do here?"** in the chat panel.

The third argument is your color: `white`, `black`, or `auto` (infer from the PGN headers).

**Browse & reopen past games (the Games panel).** A collapsible third column (toggle with the **☰
Games** button) lists games you can open in the board. The panel has four tabs: **My games**
(your previously-analyzed local games), **Lichess**, **Chess.com**, and **Paste PGN** — paste 
a PGN from anywhere, pick which color you played (or leave it on *auto*), and click **Analyze**. 
Click any game (or paste PGN data) and the board opens **immediately** — you can step through 
the moves with ← / → while the engine analysis runs in the background; the eval bar, win graph, 
mistake list, comments, and best-move arrows fill in as soon as it finishes.

**Bulk-analyze a Chess.com export (multiple games at once).** Chess.com lets you download many
games as a single PGN file. In the **Paste PGN** tab, hit **Upload .pgn** (or paste the file's
contents) — the app detects all the games, analyzes them one-by-one in the background (showing
"Game *k* of *N*"), and files every one under **My games**. It figures out which handle is *you*
(the player present in all the games) automatically; if it can't, type your username in the
optional box. Once a handle is recognized this way it's remembered, so future games from that
account (Chess.com *or* Lichess) fold into the same history and coaching profile.

<!-- TODO: screenshot of the best-move arrows.
     Toggle "Show best move" on a quiet middlegame position so two green arrows show, one bold
     (best) and one thin (a slightly worse alternative). Save as docs/screenshots/best-move-arrows.png -->
<!-- ![Best-move arrows](docs/screenshots/best-move-arrows.png) -->

<!-- TODO: screenshot of the win graph.
     Capture the graph strip under the board across a full game, showing the two-tone fill,
     colored dots at the mistakes, and the vertical current-move marker. Save as
     docs/screenshots/win-graph.png -->
<!-- ![Win graph](docs/screenshots/win-graph.png) -->

### Option B: from the Claude Code terminal (MCP)

With the server registered in `.mcp.json`, open Claude Code in this directory (reload so it picks
up the `chess` server), then:

1. Paste a PGN and say *"analyze this game"* → Claude calls `mcp__chess__analyze_game`, narrates the
   mistakes, and gives you the board URL.
2. Ask *"why was move 4 bad?"* → Claude calls `mcp__chess__get_engine_line` and explains using the
   returned best line + refutation.

Tools exposed: `mcp__chess__analyze_game`, `mcp__chess__get_engine_line`, `mcp__chess__goto_mistake`,
`mcp__chess__get_player_profile` (your cross-game coaching profile, see below).

#### Example (terminal)

> **You:** *(paste PGN)* analyze this as white
>
> **Claude:** Your accuracy: 92.7%, a clean game. 3 flagged moments…
> 1. **Move 4: Nf3** was the big one. After 3…Nd4?? the crushing reply was **4. c3!**, kicking the
>    knight with nowhere to go (~+3.7). Instead 4. Nf3 invited 4…Nxf3+ and dropped to roughly equal.
> 2. **Move 10: Qf3** (a mistake, but you were already winning)…
>
> 📊 Open the interactive board: http://127.0.0.1:8765

### Snowie — the in-browser AI coach

**Snowie** is the chat panel: it answers position-aware questions using your Claude subscription.
Each question is handed the **current board** (for *"what should I do here?"*) and the **move in
question** (for *"why is this bad?"*), each with pre-computed Stockfish facts, so Snowie reasons
from real lines. Follow-up questions remember the conversation.

<!-- TODO: screenshot of the chat panel with a Q&A.
     Show "Why is Nd4 bad here?" and Claude's grounded answer rendered with bold/lists. Save as
     docs/screenshots/chat.png -->
<!-- ![Ask why chat](docs/screenshots/chat.png) -->

<!-- > **Note on billing:** in-browser chat uses your subscription's separate **Agent SDK credit** (not
> per-token API billing). If it's exhausted, you'll get a friendly message, so just ask in the
> Claude Code terminal instead, which uses your normal interactive limits. -->

### Run the AI on a local model (instead of Claude)

The chat + AI coach can run on a model **entirely on your own machine** instead of your Claude
subscription. This is the fully-offline path for the AI: it needs **no Claude subscription, no
login, and not even the `claude` CLI**. The app talks to your local model directly over HTTP.

It works with **any local server that exposes an OpenAI-compatible `/v1/chat/completions`
endpoint**, which is almost all of them: [Ollama](https://ollama.com), LM Studio, llama.cpp's
server, a [LiteLLM](https://github.com/BerriAI/litellm) proxy, and others. **Ollama is the
easiest**, so it gets a one-click setup; everything else is two fields.

**With Ollama (one click):**

1. Install Ollama and pull a model: `ollama pull qwen2.5-coder` (any chat model works; it just needs
   to be running, e.g. `ollama serve`).
2. In the board, open **⚙ Settings → Advanced → Local AI model** and click **Detect Ollama**. It
   finds the URL, lists the models you've pulled, and you pick one and **Save**.

That's it. The chat and AI coach now run locally; clear the field to switch back to Claude.

**With anything else (two fields):** under the same setting, fill in:

- **URL** — your server's base URL. The app appends `/v1/chat/completions` for you, so any of these
  forms work: `http://localhost:11434` (Ollama), `http://localhost:1234/v1` (LM Studio),
  `http://localhost:8080/v1` (llama.cpp), `http://localhost:4000` (a LiteLLM proxy).
- **Model** — the model name that server should serve (e.g. `qwen2.5-coder`).

Equivalently, set `CHESS_LOCAL_LLM_BASE_URL` and `CHESS_LOCAL_LLM_MODEL` (see the env-var table).

> **Notes.** Quality depends entirely on the local model: small models explain noticeably worse
> than Claude, and an older or slower machine may answer slowly (local generation can take a while,
> so the app waits patiently). The Stockfish analysis is always local and is unaffected by this
> setting. The terminal MCP workflow still uses Claude; this option covers the in-browser chat and
> the AI coach summary.

---

## Game history & coaching profile

Every game you review is **saved locally** so the tool can learn your recurring weaknesses over
time. This is best-effort and fully local: history can never break a review, and nothing leaves
your machine (the chat is the only outbound call, and your profile is only attached to it when you
opt in).

- **What's saved.** `analyze_game` appends one compact JSON record per reviewed game to
  `<DATA_DIR>/history/games.jsonl`. `DATA_DIR` defaults to a per-user app-data folder (macOS:
  `~/Library/Application Support/Tintin AI Chess Analysis/data`) that both Claude Code **and** the
  double-click app use, so they share one history/cache automatically. Re-analyzing the same game —
  even at a deeper depth — supersedes the old record rather than duplicating it.
- **Mistake motifs.** Each flagged mistake is tagged with cheap, engine-free heuristics in three
  buckets: things you *did* (e.g. `hung_piece`, `pawn_grab`), things you *missed* (`missed_fork`,
  `missed_mate`, `missed_capture`), and things you *allowed* (`allowed_fork`, `allowed_mate`,
  `back_rank`), plus `time_trouble` when your clock was low (read from `[%clk]` PGN comments).
- **Game mode awareness.** Each game is tagged with its time-format — **bullet / blitz / rapid /
  classical / correspondence** (derived from the `TimeControl` header) — because what counts as a
  mistake differs by mode: a blunder in a 1-minute bullet game is far more forgivable than in a
  long classical one. The flagging thresholds **scale by mode** (blitz is the baseline; faster
  modes are more lenient, slower modes stricter — on top of the per-skill scaling), the coaching
  profile breaks your stats down **by mode**, and both the chat and terminal review judge a game
  against its own mode's expectations.
- **Coaching profile.** Records roll up into a **hybrid** profile: a `recent` sliding window (so
  weaknesses you've fixed fade out) plus a `lifetime` view, with an "improving / slipping" trend.
  Get it from the terminal with `mcp__chess__get_player_profile`, or let the board's chat use it.
- **Personalized chat.** The **"Personalize AI coach with my history"** option in **⚙ Settings**
  (on by default) attaches the profile to your chat questions so Claude can connect the position to
  your recurring patterns. It's designed to stay subtle — Claude only brings up your history when it
  genuinely sharpens the answer, not in every reply.

### Who is "you"? (identity & aliases)

History is keyed to a canonical player, not a username, so games across your Lichess and Chess.com
accounts merge into one profile. Set `CHESS_USERNAME` to your main handle and list any other handles
in `CHESS_ALIASES` (comma-separated, e.g. `"dpdemler, my_other_lichess"`); they all fold into one
player and also drive `player="auto"` side detection. For multiple people sharing the install, a
hand-maintained `<DATA_DIR>/identities.json` alias map takes precedence.

To turn history off entirely, set `CHESS_HISTORY=0`.

---

## Configuration

### In-app Settings panel (no file editing)

Click **⚙ Settings** in the board header to change the common options without touching any files —
your **username**, **other accounts** (aliases that fold into one profile), an optional **Lichess
token**, and, under *Advanced*, the profile windows, the **Stockfish path**, and an optional
**local AI model** (run the chat + coach on your own machine — see [above](#run-the-ai-on-a-local-model-instead-of-claude)).
Saving writes `<DATA_DIR>/settings.json` and applies immediately.

**Precedence: `settings.json` (the panel) overrides the environment (`.mcp.json`), which overrides
the built-in defaults.** Both the standalone app *and* the MCP server read `settings.json` at
startup, so a change you make in the app also takes effect for the Claude Code workflow — your
username is unified across both. (You can still set everything via environment variables below;
the panel is just the no-files path. The auto-detected aliases from a bulk Chess.com import live in
`identities.json` and are merged on top.)

### Environment variables

All settable via environment variables too (sensible defaults shown); `settings.json` wins where set:

| Variable | Default | Purpose |
| --- | --- | --- |
| `STOCKFISH_PATH` | *(auto-detected)* | Path to the Stockfish binary. Auto-detected from your `PATH` and common install locations; only set this for a custom build or an unusual location. |
| `CHESS_USERNAME` | `JohnDoe` | Used by `player="auto"` to pick your side from PGN headers. |
| `CHESS_DEFAULT_DEPTH` | `18` | Depth for on-demand single-position analysis. |
| `CHESS_SWEEP_DEPTH` | `16` | Depth for the full-game sweep (keeps long games fast). |
| `CHESS_ENGINE_POOL_SIZE` | `2` | Reused Stockfish processes. |
| `CHESS_WEB_HOST` / `CHESS_WEB_PORT` | `127.0.0.1` / `8765` | Web board address. |
| `CHESS_WEB_AUTOSTART` | `1` | Set `0` to stop the MCP server from launching the board. |
| `CHESS_ALIASES` | *(empty)* | Your other handles (comma-separated) that fold into `CHESS_USERNAME` for history + auto side-detection. |
| `CHESS_HISTORY` | `1` | Set `0` to disable saving game history & the coaching profile. |
| `CHESS_DATA_DIR` | *(per-user app-data dir)* | Where history (`games.jsonl`, profile, `identities.json`) is stored. Defaults to a user-level folder shared by Claude Code and the app (macOS: `~/Library/Application Support/Tintin AI Chess Analysis/data`); set `<repo>/.chess-review` to keep it in the checkout. |
| `CHESS_PROFILE_RECENT` | `100` | Games in the profile's `recent` sliding window. |
| `CHESS_PROFILE_LIFETIME` | `all` | Lifetime view span; positive N = last N games, `0` = omit it (pure sliding window). |
| `CHESS_SESSION_TTL` | `86400` | Seconds of inactivity before the server self-terminates (`0` disables the watchdog). |
| `CHESS_LOCAL_LLM_BASE_URL` | *(empty)* | Run the chat + AI coach on a local model instead of Claude (no `claude` CLI needed): the base URL of a server with an OpenAI-compatible `/v1/chat/completions` endpoint (Ollama `http://localhost:11434`, LM Studio `http://localhost:1234/v1`, llama.cpp, LiteLLM proxy). Blank = use the Claude subscription. |
| `CHESS_LOCAL_LLM_MODEL` | *(empty)* | The model name the local server should serve (e.g. `qwen2.5-coder`). Required when the base URL above is set. |

---

## Running the tests

```bash
uv sync --extra dev          # pulls in pytest (one time)
uv run python -m pytest
```

The suite is all fast unit/mock tests with no Stockfish, no network, and no `claude` CLI (the
engine, the Lichess API, and the chat are mocked), so it runs in well under a second and never
spends Agent-SDK credit. It also runs on every push / PR via GitHub Actions (`.github/workflows/ci.yml`).

---

## Project layout

```
server/
  config.py          # all tunables (env-driven)
  core/              # engine pool, evaluation math, game analysis, session, engine_line,
                     #   history (game records + motifs + coaching profile), lifecycle watchdog
  mcp_server.py      # MCP tools; boots the web server in a background thread
  claude_bridge.py   # headless `claude -p` for the chat (subscription)
  web/               # FastAPI app + board/chat routes + uvicorn runner
frontend/            # no-build single page (index.html + main.js + styles.css, CDN chessground)
scripts/             # run_web.py (standalone board), validation/smoke scripts
example_pgns/        # sample games (game1.pgn White, game2.pgn Black)
tests/               # pytest suite
```

---

## Limitations / notes

- The web board pulls chessground & chess.js from a CDN at runtime (no build step), so it needs
  internet on first load.
- In-browser chat requires the `claude` CLI installed and logged in, and draws from your Agent SDK
  credit; the terminal path is the zero-extra-cost fallback. (Or point it at a
  [local model](#run-the-ai-on-a-local-model-instead-of-claude) to skip Claude entirely.)
- Engine analysis is fixed-depth and cached for reproducibility, so evals can differ slightly from
  Lichess near classification boundaries. That's expected.

---

## Your data: what stays local, what leaves your machine

This tool is **local-first**, but it is not fully offline, so here's the honest breakdown:

- **Stays on your machine.** The Stockfish engine review (the mistake list, eval bar, win graph,
  arrows, and the templated move comments) is **100% local**. Your analyzed-game history and
  coaching profile are stored only under your app-data folder (see [Configuration](#configuration));
  nothing is uploaded.
- **The web board is loopback-only.** The board listens on `127.0.0.1` and is guarded against
  cross-site / DNS-rebinding requests, so other websites you visit can't reach it, read your game
  data, or spend your Claude quota.
- **The AI coach (Snowie) sends data to Anthropic.** The in-browser chat and AI-coach summaries run
  through the headless `claude` CLI **under your own Claude subscription**. To answer, they send the
  current position (FEN) and pre-computed engine facts to Claude. They also include a summary of
  your recurring-mistake profile, but **only if** you enable *"Personalize AI coach with my history"*
  in ⚙ Settings (off by default for new users; opt-in). No API key, no per-token billing, and the
  engine review works without `claude` at all if you'd rather keep everything local.
- **Lichess import is optional.** Fetching your games calls the public Lichess API with the username
  (and optional token) you provide. Skip it and paste PGNs instead, and nothing is sent to Lichess.

---

## FAQ

### How do I install the `claude` CLI, and how is it different from the Claude app?

The `claude` CLI (Claude Code) is a separate program that runs in your computer's **terminal**. It
is not the Claude desktop app or the claude.ai website, but it signs in with the **same Claude
subscription**, so there's no extra account, no API key, and no per-token bill. This tool uses the
CLI behind the scenes to write the plain-English coaching, which is why you need it installed and
logged in.

<details>
<summary><strong>Show installation steps</strong></summary>

<br>

Open a terminal and install Claude Code with one of these (the native install is recommended):

```bash
# macOS / Linux / WSL
curl -fsSL https://claude.ai/install.sh | bash

# Windows (PowerShell)
irm https://claude.ai/install.ps1 | iex
```

Or use a package manager: `brew install --cask claude-code` (macOS) or
`winget install Anthropic.ClaudeCode` (Windows).

Then run `claude` once and follow the prompt to log in with your Claude subscription. After that,
restart the app (or reload Claude Code) and the AI coach will work. Full details are in the
[Claude Code docs](https://docs.claude.com/en/docs/claude-code/overview).

</details>

> The engine review itself (the mistake list, eval bar, win graph, and arrows) works without the
> CLI. You only need it for the conversational coaching and the AI summaries.

---

## License & credits

This project is licensed under the **MIT License**. See [`LICENSE`](LICENSE).

It stands on the shoulders of others, with thanks:

- **[Stockfish](https://stockfishchess.org/)**: the chess engine doing the actual analysis.
  Stockfish is licensed under the **GNU GPL v3**; this project invokes it as a separate binary and
  does not include or modify its source. The app/launcher downloads the official Stockfish build
  when no system install is found.
- **[python-chess](https://python-chess.readthedocs.io/)** (GPL v3): PGN parsing, move
  generation, and board logic.
- **[chessground](https://github.com/lichess-org/chessground)** & **[chess.js](https://github.com/jhlywa/chess.js)**: the interactive board UI (loaded from a CDN).
- **Opening names** come from the **[Lichess `chess-openings`](https://github.com/lichess-org/chess-openings)** ECO dataset (CC0), vendored in `server/data/eco/`.
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[uvicorn](https://www.uvicorn.org/)**,
  **[Pydantic](https://docs.pydantic.dev/)**, and the **[MCP](https://modelcontextprotocol.io/)**
  Python SDK power the server.
- **[riskydissonance](https://github.com/riskydissonance)** and **[Leland Olney](https://github.com/JohnnyPrimus)**: chess.com api integration

Trademarks (Lichess, Chess.com, Claude, Anthropic) belong to their respective owners; this is an
independent project not affiliated with any of them.
