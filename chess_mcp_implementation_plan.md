# Chess Review MCP — Implementation Plan

A local tool that analyses a chess game (PGN), finds the moves where I went wrong,
opens a real interactive board at each critical position, lets me try alternatives,
and — unlike Lichess — explains **in words why my move was bad** and why my candidate
alternatives do or don't work. The explanations come from Claude reading Stockfish's
lines, not just from engine arrows.

This document is the build spec. Work through it phase by phase. Each phase has an
**objective**, **tasks**, and **acceptance criteria** — don't move on until the criteria pass.

---

## STATUS (updated 2026-06-15)

**All phases (0–7) are complete and verified.** The tool is drivable from the Claude Code
terminal (paste a PGN → `analyze_game` → "why was move N bad?" → `get_engine_line`) AND from an
interactive web board that autostarts in the same process: chessground board, eval bar (oriented
to the reviewed side), Lichess-style win graph, arrow-key navigation, engine-grounded per-mistake
comments, played-move + best-move + refutation arrows, and an in-browser "why?" chat backed by
headless `claude -p` (subscription). See `server/web/`, `server/claude_bridge.py`, `frontend/`,
and `scripts/run_web.py`. Run the board standalone with `scripts/run_web.py <pgn> <color>`.

### What changed vs. this spec during implementation

- **Tooling: conda env, not `uv`.** `uv` is not installed. Everything runs through the explicit
  interpreter `/opt/miniconda3/envs/chess-review/bin/python`; deps were already present. Never
  use `conda activate`. See `CLAUDE.md`.
- **Stockfish 18** (spec said "16+"), at `/usr/local/bin/stockfish`.
- **Classification thresholds corrected to 5 / 10 / 15** win%-point drops (inaccuracy / mistake
  / blunder), **not** the 10/20/30 in §6. Lichess thresholds its `winningChances` scale [-1,1]
  at 0.1/0.2/0.3 → ×50 = 5/10/15 on the 0–100 win% scale. This reproduces Lichess's own labels
  on `example_pgns/game1.pgn` (8 flagged moves, matching severities). §6 below is annotated.
- **Two analysis depths.** `DEFAULT_DEPTH=18` for on-demand single-position lookups
  (`get_engine_line`); `SWEEP_DEPTH=16` for the full-game walk (keeps a 75-move game ~45s).
- **Each position analysed once.** The game walk evaluates every position a single time and
  derives `win_before`/`win_after` from consecutive positions (`win_after = 100 −
  opponent's best win%`), instead of two analyses per move. Cached by `(fen, depth, multipv)`.
- **Per-side accuracy for both colours** is computed (simple mean) even though only the
  reviewed player's moves become `MoveReview`s.
- **`get_engine_line` returns a refutation.** For a given move it returns its classification,
  `win_before/after`, the engine's **refutation line** after the move (the "why it's bad"), and
  the better move — accepting the move as **UCI or SAN**. The `shapes` field is present but empty
  (deferred to Phase 7). Illegal moves return an `error` key.
- **`analyze_game` returns `board_url: null`** with a note — the web server (Phase 4) isn't up
  yet, so there's nothing to open. The contract key is kept for forward-compatibility.
- **`goto_mistake` was implemented** (spec marked it optional) and returns a narration `prompt`.
- **`MoveReview` enriched** with `move_uci`, `move_number`, `color`, `best_line_san`, `win_swing`.
- **Files added:** `server/config.py`, `scripts/smoke_phase0.py`, `scripts/validate_phase2.py`,
  `CLAUDE.md`, and `tests/` (`test_evaluation`, `test_engine`, `test_game_analysis`).

### Recommendations for the remaining phases — see "§9. Future implementation notes" at the end.

---

## 0. Context and design decisions (read first)

- **Engine:** Stockfish (local binary), driven via `python-chess`'s `chess.engine`.
- **Board logic:** `python-chess` on the backend; `chess.js` on the frontend for legality.
- **Board UI:** `chessground` (Lichess's own MIT-licensed board component). It deliberately
  does **not** enforce rules — we feed it legal destinations computed by `chess.js`.
- **Mistake grading:** convert centipawns → win% with the Lichess sigmoid, then classify by
  the **drop in win%**, not by raw centipawn loss. (Details in §6.)
- **One process, shared state:** the MCP server and the FastAPI web server run in the **same
  Python process**, sharing one Stockfish engine pool and one in-memory "review session"
  object. The analyse tool writes the session; the board reads it. Do **not** spin up a new
  server per tool call.
- **Browser chat uses my Claude subscription, NOT the API.** The browser's "why?" questions
  are answered by invoking **Claude Code headless** (`claude -p`) on the backend, which is
  authenticated with my Pro/Max subscription. See §5.3 and the IMPORTANT note below.

> **IMPORTANT — subscription billing note (as of 2026-06-15):** programmatic Claude Code usage
> (`claude -p` and the Agent SDK) on subscription plans draws from a **separate monthly Agent SDK
> credit**, distinct from interactive usage limits. The browser chat therefore uses my
> subscription (no per-token API billing) but consumes that separate allowance. Keep this in mind;
> provide the Claude-Code-terminal fallback (§5.4) for when it's exhausted. Reference:
> https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan

---

## 1. Architecture

```
   Claude Code (terminal)                         Browser board (chessground)
   you paste PGN, ask                             you drag pieces, ask "why?"
            │  calls MCP tools                              │  HTTP / WebSocket
            ▼                                               ▼
   ┌─────────────────────────── one Python process ───────────────────────────┐
   │                                                                           │
   │   MCP server  ◄──────►   Shared core   ◄──────►   FastAPI web server      │
   │   (tools for Claude)     Stockfish pool          serves board + JSON API  │
   │                          + review session                                 │
   │                                                                           │
   └───────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │ backend shells out to `claude -p`
                                     │ (subscription auth) for in-browser chat
                                     ▼
                              Claude Code headless
```

Two human-facing surfaces (terminal + browser) both talk into one process. The "brains"
(Stockfish + analysis) live in the shared core. Claude's natural-language explanations come
from Claude Code — invoked interactively in the terminal, or headless from the backend to
power the browser chat panel.

---

## 2. Tech stack

| Concern              | Choice                                                        |
| -------------------- | ------------------------------------------------------------- |
| Language / runtime   | Python 3.11 — **conda env, not `uv`** (`/opt/miniconda3/envs/chess-review/bin/python`) |
| Chess logic (server) | `python-chess`                                                |
| Engine               | **Stockfish 18** binary, via `chess.engine.SimpleEngine` (`/usr/local/bin/stockfish`) |
| MCP server           | Official `mcp` Python SDK (`mcp[cli]`)                         |
| Web server           | `FastAPI` + `uvicorn`                                          |
| Realtime (optional)  | FastAPI WebSocket for live eval / streamed chat               |
| Board UI             | `chessground` (npm) + `chess.js` for legality                 |
| Frontend build       | Plain Vite + TypeScript (keep it simple)                      |
| In-browser chat      | `claude -p` headless (subprocess) or Python **Agent SDK**     |

---

## 3. Repository structure

```
chess-review-mcp/
├── pyproject.toml
├── .mcp.json                  # registers this server for Claude Code
├── README.md
├── stockfish/                 # or rely on system stockfish on PATH
├── server/
│   ├── __init__.py
│   ├── core/
│   │   ├── engine.py          # Stockfish pool, analyse(position) -> eval/pv/multipv
│   │   ├── evaluation.py      # cp -> win%, classification, accuracy
│   │   ├── game_analysis.py   # PGN -> list of MoveReview, build ReviewSession
│   │   └── session.py         # in-memory ReviewSession state (shared singleton)
│   ├── mcp_server.py          # MCP tools: analyze_game, get_engine_line, ...
│   ├── web/
│   │   ├── app.py             # FastAPI: serves frontend + JSON/WS API
│   │   ├── routes_board.py    # /position, /evaluate, /legal-moves
│   │   └── routes_chat.py     # /chat -> headless Claude Code
│   └── claude_bridge.py       # wraps `claude -p` invocation (subscription)
├── frontend/
│   ├── index.html
│   ├── src/
│   │   ├── main.ts            # chessground setup, drag handling
│   │   ├── api.ts             # fetch wrappers
│   │   ├── evalbar.ts
│   │   └── chat.ts            # chat panel -> /chat
│   └── vite.config.ts
└── tests/
```

---

## 4. Implementation phases

### Phase 0 — Environment

**Objective:** working Python env with Stockfish reachable.

**Tasks**
- `uv init`; add deps: `python-chess`, `mcp[cli]`, `fastapi`, `uvicorn`, `pytest`.
- Install Stockfish (`brew install stockfish` / apt / download). Confirm the binary path.
- Add a `STOCKFISH_PATH` config (env var, default to `stockfish` on PATH).

**Acceptance**
- A throwaway script opens the engine, evaluates the start position at fixed depth, prints
  the eval, and cleanly quits the engine.

---

### Phase 1 — Engine core + evaluation math

**Objective:** evaluate any FEN reproducibly; convert to win% and classify.

**Tasks**
- `core/engine.py`:
  - A small **engine pool** (reuse engine processes; don't spawn per call).
  - `analyse(fen, *, depth=18, multipv=1) -> AnalysisResult` returning, per line: score
    (cp, side-to-move relative, with mate handling), and the principal variation (list of UCI moves).
  - **Fix depth (or nodes/movetime) so evals are reproducible**, and cache by `(fen, depth)`.
- `core/evaluation.py` (see §6 for formulas):
  - `win_percent(cp) -> float` (0–100), with mate scores mapped to ~100/0.
  - `classify(win_before, win_after) -> "best"|"good"|"inaccuracy"|"mistake"|"blunder"`.
  - `move_accuracy(win_before, win_after) -> float` (0–100) (optional, nice readout).

**Acceptance**
- Same FEN + same depth → identical eval across runs (cache hit or deterministic).
- Unit tests: a known tactic position returns the expected best move; win% monotonic in cp.

---

### Phase 2 — Game analysis + review session

**Objective:** PGN in → ordered list of my mistakes → a `ReviewSession`.

**Tasks**
- `core/game_analysis.py`:
  - Parse PGN with `python-chess`. Determine which colour is "me" (from a `player` arg /
    PGN headers).
  - Walk every position. For each of **my** moves, compute win% before (best reply available)
    and win% after the move I actually played, both from my perspective; classify the drop.
  - Produce `MoveReview { ply, move_san, fen_before, fen_after, eval_before, eval_after,
    win_before, win_after, classification, best_move_san, best_line: [uci...], accuracy }`.
  - Keep only moves classified inaccuracy/mistake/blunder for the review list (keep all for an
    optional full accuracy summary).
- `core/session.py`:
  - A process-wide singleton `ReviewSession` holding: the full game, the list of mistakes,
    a `current_index`, and the active explore FEN. The MCP analyse tool **writes** this; the
    FastAPI board **reads** it. This shared object is what links terminal and browser.

**Acceptance**
- Feed a real Lichess PGN of mine; output a sensible, ordered mistake list whose classifications
  match intuition (and roughly match Lichess's own review on the same game).

---

### Phase 3 — MCP server

**Objective:** expose the brains to Claude Code as tools.

**Tools to implement** (`mcp_server.py`, using the `mcp` SDK):

1. **`analyze_game(pgn: str, player: "white"|"black" = auto) -> GameSummary`**
   - Runs Phase 2, populates the shared `ReviewSession`.
   - Returns a compact summary: result, per-side accuracy, and the mistake list
     (ply, move, classification, eval swing, best move) — enough for Claude to narrate.
   - **Side effect:** ensure the web server is running (it already is, see Phase 4) and
     return the local URL so the user can open the board. Do **not** start a new server here.

2. **`get_engine_line(fen: str, move: str | None = None, depth: int = 18, multipv: int = 1) -> LineResult`**
   - The "secret sauce" for follow-ups. Evaluates `fen` (optionally after forcing `move`),
     returns eval, win%, classification (if a move was given), and the engine's expected
     continuation as SAN + UCI, plus **board annotations** (see Phase 7): the key squares /
     arrows that explain the refutation.

3. **`goto_mistake(index: int) -> PositionState`** (optional)
   - Sets `ReviewSession.current_index` and returns the FEN one move before that mistake so the
     board and Claude stay in sync.

**Notes**
- Register the server in `.mcp.json` as name `chess` so tools are namespaced
  `mcp__chess__analyze_game`, `mcp__chess__get_engine_line`, etc.
- Keep tool outputs structured and concrete (evals, lines, what's hanging) so Claude's prose
  explanations are grounded rather than vague.

**Acceptance**
- From a Claude Code session in the project dir: paste a PGN, say "analyse this game". Claude
  calls `analyze_game`, narrates the mistakes, and (when asked "why was move N bad?") calls
  `get_engine_line` and explains using the returned line.

---

### Phase 4 — FastAPI web server (board backend)

**Objective:** serve the board and a live API, sharing the same session + engine.

**Tasks** (`web/app.py`, `web/routes_board.py`)
- Mount the built `frontend/` as static files; serve `index.html` at `/`.
- Start uvicorn **once**, in the same process as the MCP server (e.g. launch the web server in a
  background thread/task when the MCP server boots). One Stockfish pool, one `ReviewSession`.
- Endpoints:
  - `GET /session` → current review session (mistake list, current index, current FEN).
  - `GET /position/{index}` → FEN one move before mistake `index` + metadata for the board.
  - `POST /legal-moves` `{fen}` → legal destinations map (can also be done purely client-side
    with chess.js; expose for parity/validation).
  - `POST /evaluate` `{fen, move}` → after applying `move`: eval, win%, classification vs the
    best move here, and a one-line verdict + the refutation line for the board to draw.
- `webbrowser.open("http://localhost:PORT")` helper the analyse tool can call.

**Acceptance**
- Hitting `/session` after an `analyze_game` call returns the live mistakes.
- `/evaluate` returns sane evals that agree with the MCP path for the same position.

---

### Phase 5 — chessground frontend

**Objective:** a clean, Lichess-like interactive board.

**Tasks** (`frontend/src/main.ts`)
- Initialise `chessground`. Maintain game state with `chess.js`; on each move compute the
  legal `dests` map and feed it to chessground (chessground won't enforce rules itself).
- Load the review position from `/position/{index}`; show whose move it is and the prompt
  ("you played Xf6 here — find something better").
- On user move: validate with chess.js → `POST /evaluate` → update an **eval bar**, show the
  classification (good/inaccuracy/mistake/blunder) and the one-line verdict.
- UI: eval bar, move list, "next mistake / prev" controls, a "reset to position" button, and a
  free-explore toggle so I can keep playing moves down a line.
- Use chessground's shape API (`setShapes`/`drawable`) to render arrows and highlighted squares
  returned by the backend (Phase 7).

**Acceptance**
- Board looks and feels like the Lichess analysis board; dragging is smooth; illegal moves are
  rejected; eval bar updates per move.

---

### Phase 6 — In-browser chat on my subscription (headless Claude Code)

**Objective:** ask "why doesn't Nd4 work?" in the browser and get Claude's explanation,
**using my Claude subscription, not the API.**

**Tasks** (`server/claude_bridge.py`, `web/routes_chat.py`)
- `POST /chat` `{question, fen, last_move?, session_id?}`.
- The bridge invokes Claude Code headless via subprocess (start here; upgrade to the Python
  Agent SDK later for session management and lower latency):

  ```bash
  claude -p "<composed prompt: position FEN, the move/line in question, the user's question>" \
    --output-format json \
    --mcp-config .mcp.json \
    --allowedTools "mcp__chess__get_engine_line,mcp__chess__analyze_game" \
    [--resume <session_id>]
  ```

  - **Do NOT pass `--bare`** — it disables MCP discovery and forces `ANTHROPIC_API_KEY` auth,
    which would bypass the subscription. Normal `-p` uses the subscription login (`claude login`).
  - Parse the JSON: text answer is in `.result`; capture `.session_id` and thread follow-ups
    with `--resume <session_id>` so the conversation has context across questions in a review.
  - Pre-approving the chess tools with `--allowedTools` lets Claude call `get_engine_line`
    itself, so it reasons from real engine lines, not guesses.
- Return `{ answer, shapes? }` to the browser; render `answer` in the chat panel and any
  `shapes` on the board.
- (Optional, nicer UX) Use `--output-format stream-json --verbose --include-partial-messages`
  and a WebSocket to stream the answer token-by-token into the panel.

**Acceptance**
- In the browser, after trying a move, I type a question and get a grounded, position-aware
  explanation within a few seconds — billed to my subscription's Agent SDK credit, not the API.

---

### Phase 7 — "Why" on the board + polish

**Objective:** show, not just tell.

**Tasks**
- Have `get_engine_line` / `/evaluate` return **annotation shapes** describing the refutation:
  e.g. `[{ "orig": "d5", "dest": "c6", "brush": "red" }, { "orig": "c6", "brush": "yellow" }]`
  for "after d5, dxc6 wins the knight". Render with chessground's drawable shapes.
- Have Claude's chat answers optionally include a small structured `shapes` block (ask for it in
  the prompt / json-schema) so prose and board stay in sync.
- Polish: per-move accuracy readout, full-game accuracy summary, keyboard nav, position caching,
  and a "replay the engine's line" animation.

**Acceptance**
- Asking "why is Bxb2 bad here?" produces both a sentence ("your knight on c6 is hanging; after
  dxc6 you can't recapture") **and** arrows on the board showing the refutation.

---

## 5. Key contracts (summary)

**MCP tools**
- `analyze_game(pgn, player?) -> { result, accuracy_white, accuracy_black, mistakes: [MoveReview], board_url }`
- `get_engine_line(fen, move?, depth=18, multipv=1) -> { eval, win_percent, classification?, best_san, line_san: [...], line_uci: [...], shapes: [...] }`
- `goto_mistake(index) -> { fen, ply, prompt }`

**HTTP endpoints**
- `GET /session`, `GET /position/{index}`, `POST /legal-moves`, `POST /evaluate`, `POST /chat`

**ReviewSession (shared singleton)**
- `{ game, mistakes: [MoveReview], current_index, explore_fen }`

---

## 6. Key formulas (Lichess-style)

> Constants below approximate Lichess's published formulas. They're tunable — verify against the
> latest Lichess accuracy docs and adjust thresholds to taste.

**Centipawns → win% (for the side to move):**

```
win_percent(cp) = 50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1)
```

Clamp `cp` to roughly [-1000, 1000]; map mate-in-N to ~100 (winning) or ~0 (losing).

**Move classification** by drop in my win% (`d = win_before - win_after`, in win% points):

> **CORRECTED DURING IMPLEMENTATION.** The thresholds below (30/20/10) were too lenient and
> under-flagged mistakes. The values actually used (`server/core/evaluation.py`) are **15/10/5**,
> matching Lichess (which thresholds its `winningChances` scale [-1,1] at 0.3/0.2/0.1 → ×50).
> These reproduce Lichess's own labels on `example_pgns/game1.pgn`.

```
# ACTUAL (matches Lichess):        # original spec (do not use):
d >= 15  -> blunder                d >= 30  -> blunder
d >= 10  -> mistake                d >= 20  -> mistake
d >= 5   -> inaccuracy             d >= 10  -> inaccuracy
else     -> good / best            else     -> good / best
```

**Per-move accuracy% (optional readout):**

```
accuracy(d) = clamp(103.1668 * exp(-0.04354 * d) - 3.1669, 0, 100)   # d in win% points
```

Aggregate per-side accuracy = (a robust mean of) per-move accuracies, e.g. the harmonic-ish
weighted mean Lichess uses; a simple mean is fine to start.

---

## 7. Implementation notes / gotchas

- **One process, one engine pool, one session.** The biggest design risk is treating the MCP
  server and web server as separate apps. They aren't — share state explicitly.
- **Reproducible evals:** fix depth (or nodes) and cache `(fen, depth)`. Inconsistent evals make
  classifications jump around.
- **chessground does not enforce legality** — always compute `dests` with chess.js and pass them in.
- **Never put any API key or secret in the frontend.** All Claude/engine calls happen on the backend.
- **Subscription chat caveat (today's date forward):** `claude -p` / Agent SDK on a subscription
  draws from the separate monthly Agent SDK credit. Surface a friendly message if it errors with a
  billing/limit category, and point me to the terminal fallback (§5.4 below).
- **Latency:** each `claude -p` spawn is a few seconds. Use `--resume` to keep context; consider the
  Python Agent SDK to avoid re-spawning and to stream responses.
- **Quiet positional inaccuracies** are hard to explain in words even for a strong engine. Lean the
  review UX toward the moves with real win% swings; don't force a deep "why" on near-zero deltas.

### 5.4 Terminal fallback (always available)

Because the MCP server is consumed by Claude Code anyway, I can always just analyse and ask "why"
**in the Claude Code terminal** — no browser chat needed. That path uses my regular interactive
limits instead of the Agent SDK credit. Keep this working as the zero-config fallback.

---

## 8. How to start

1. Do Phase 0 and Phase 1 first; get one reproducible eval + the win% conversion with tests.
2. Then Phase 2 (real PGN → mistake list) and confirm it roughly matches Lichess's review.
3. Then Phase 3 so I can drive everything from the Claude Code terminal.
4. Then Phases 4–5 for the board, 6 for browser chat, 7 for polish.

Build incrementally and keep each phase's acceptance criteria green before moving on.
Ask me to provide a sample PGN when you reach Phase 2.

> Phases 0–3 are now done (sample PGNs live in `example_pgns/`). Resume at Phase 4.

---

## 9. Future implementation notes (written after Phases 0–3)

Things learned building the core that should shape the remaining phases.

### Process model for Phase 4 (the big one)
- The MCP server currently runs as a **stdio** process spawned by Claude Code. The spec wants
  the FastAPI web server in the **same** process sharing the engine pool + `ReviewSession`. The
  clean way: when `server.mcp_server` boots, also start uvicorn on a **background thread**
  (`uvicorn.Server(...).run()` in a thread, or an asyncio task) bound to a fixed localhost port.
  Both `mcp_server` and `web/app` import the same `server.core.engine` and `server.core.session`
  modules — since those hold module-level singletons, state is shared automatically. **Do not**
  create a second engine pool or a second session.
- `analyze_game` should then set `board_url` to `http://localhost:PORT` (and optionally
  `webbrowser.open` it). Today it returns `null` by design.
- Concurrency: `_EnginePool` is currently size 1 with a `Queue`. The web `/evaluate` endpoint and
  an MCP call could contend. Bump `ENGINE_POOL_SIZE` to 2 and confirm the pool's locking holds up
  under concurrent requests, or serialise engine access behind an async lock.

### Reuse what exists — don't duplicate logic
- `/evaluate` should call the **same** path as `get_engine_line` (move classification +
  refutation line) so the board and the terminal never disagree. Consider extracting the body of
  `get_engine_line` into a `core` function that both the MCP tool and the FastAPI route call.
- `/position/{index}` and `/session` map directly onto the existing `ReviewSession`
  (`mistakes[index].fen_before`, `current_index`). `goto_mistake` already does the cursor logic.

### Phase 6 (browser chat) — verified groundwork
- `claude -p "..." --output-format json` works on the subscription and returns `.result` +
  `.session_id` (tested 2026-06-15). Thread follow-ups with `--resume <session_id>`. Pass
  `--mcp-config .mcp.json --allowedTools mcp__chess__get_engine_line,mcp__chess__analyze_game`.
- **Latency caveat:** each spawn re-initialises and showed ~1.5–2s overhead plus tool round-trips.
  For a snappy chat panel, prefer the Python **Agent SDK** (persistent session) over re-spawning,
  and stream with `--output-format stream-json` over a WebSocket.
- Surface a friendly message if `claude -p` errors with a billing/limit category (Agent SDK
  credit exhausted) and fall back to the terminal (§5.4).

### Analysis quality / tuning (optional, do when it matters)
- **Accuracy aggregation** is a simple mean (`aggregate_accuracy`). Lichess uses a
  volatility-weighted/harmonic mean; upgrade only if the headline accuracy number feels off.
- **Depth vs. speed:** `SWEEP_DEPTH=16` is a good default; expose it as an `analyze_game` arg if
  long games feel slow, and let `get_engine_line` re-deepen specific positions (it already
  defaults to 18).
- **Borderline jitter:** moves right at a 5% boundary can flip vs. Lichess due to depth
  differences (e.g. game1's 11.cxd4 / 14.Bxd7+). This is expected; don't chase exact parity.
- **Multipv for "why":** when explaining a position, calling `get_engine_line(..., multipv=2/3)`
  gives the alternatives that make prose explanations richer ("your move vs. the two best tries").

### Phase 7 (shapes) — the contract is ready
- `get_engine_line` already returns an (empty) `shapes` list. Populate it from the refutation:
  the first move of `refutation_line_uci` → a red `{orig,dest}` arrow, the hanging/target square →
  a highlight. chessground consumes `{orig, dest, brush}` directly.

### Frontend (Phases 4–5)
- `uv` absent doesn't affect the frontend; Node 25 + npm are available for Vite. Keep the build
  output in `frontend/dist/` (already in `.gitignore`) and mount it as static files.