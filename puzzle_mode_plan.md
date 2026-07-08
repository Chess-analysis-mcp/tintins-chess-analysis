# Puzzle Mode — Design & Implementation Plan

A **puzzle trainer** built into the existing chess-review app: the user solves tactical puzzles
(Lichess-style), their rating moves with a faithful **Glicko-2** system, and — the part Lichess
doesn't do — an **LLM coach explains *why* each solution works**, grounded in the puzzle's known
solution line, its theme tags, and (optionally) live Stockfish facts.

This document is the build spec. It follows the same phase-by-phase shape as
`chess_mcp_implementation_plan.md`: each phase has an **objective**, **tasks**, and **acceptance
criteria**. Don't move on until the criteria pass.

> **Status: P0–P4 BUILT.** The last two P4 items are now done: the **MCP tool surface**
> (`next_puzzle`/`solve_puzzle`, sharing the one `puzzle_session` with the board) and the timed
> **"puzzle storm"** rush mode. P0–P2 committed as `54d3575`; **P3 (drift-aware downloads +
> weakness targeting) and P3.5 (mistake puzzles from your own games) are now implemented** in the
> working tree (branch `puzzle-mode`, uncommitted). **Two P4 polish items — the day-over-day daily
> streak and a (deliberately discrete) rating-curve visualisation — are now built**, and the P3.5
> mistake-puzzle **severity curve has been empirically calibrated** against real user data + the
> Lichess rating distribution (see §9 P4 and §11.7). What's left in P4 is the optional MCP tool
> surface (`next_puzzle`/`solve_puzzle`) and the optional timed "puzzle storm" mode. Per-phase
> status is called out in §9. The existing app (phases 0–7) is the substrate this builds on.
>
> **Done so far:**
> - **P0** — `scripts/build_puzzle_shards.py` (slices the CC0 Lichess DB into 100-pt, theme-
>   stratified bands, ~10k/band; zstd decode → stdlib gzip); vendored offline baseline
>   `server/data/puzzles/baseline.jsonl.gz` (3,450 puzzles, 150/band); dense shards + `manifest.json`
>   published to the **separate** data repo `Chess-analysis-mcp/tintins-chess-puzzles`, release
>   `puzzles-v1`.
> - **P1** — `core/puzzles.py` (baseline load, `next_puzzle`, `validate_step` w/ mate-leaf
>   tolerance), `core/puzzle_rating.py` (faithful Glicko-2 + `state.json`, RD-gate), `core/
>   puzzle_session.py` (current-puzzle singleton), `web/routes_puzzles.py`
>   (`config`/`next`/`move`/`hint`/`giveup`/`state`/`current`), and the frontend
>   `[ Analyze | Puzzles ]` toggle + focused solve layout + square-blink/confetti feedback.
> - **P2** — `claude_bridge.explain_puzzle` + `_puzzle_facts` + `_move_verdict`,
>   `/api/puzzle/explain`, wired Explain button (three prompt variants: failed / solved-after-miss /
>   clean solve).
> - Post-P2 UX iteration (not in the original spec): tabbed Settings with a **Puzzles** tab, a
>   solve-animation toggle (`CHESS_PUZZLE_ANIMATIONS`), 3-state verdict feedback, the RD-gate raised
>   90 → 130, the first-miss rating loss surfaced on the final card, and reload-resume.
> - **P3** — `server/core/puzzle_shards.py` (throttled `ensure_manifest`, `ensure_band` with sha256
>   verify + atomic write, LRU `_prune`, RD-aware `bands_for`/`ensure_bands_around` warm-up),
>   `puzzles._downloaded_pool`/`_merged_pool` (baseline + downloaded, deduped) + `weakness_themes`
>   (game-motif→theme map ∪ puzzle `by_theme`), knobs `CHESS_PUZZLE_MANIFEST_INTERVAL` /
>   `CHESS_PUZZLE_RD_PER_BAND`, `/api/puzzle/next?weakness=1`, and the rail's weakness toggle +
>   easier/harder + weakest-theme stats card.
> - **P3.5** — `server/core/puzzle_mistakes.py` (history-derived selection, skill-relative
>   swing-percentile ordering, recurring-motif tie-break, spaced-rep `practiced` gating), the
>   `source=your_games` discriminator across `/next`/`/move`/`/giveup`/`/current` (eval-threshold
>   validation via `lines.engine_line`, **unrated**), the game-analysis coach branch in
>   `claude_bridge.explain_puzzle` (`_mistake_facts`/`_mistake_role`), and the "From your games"
>   frontend skin (source toggle gated on the engine, amber badge, replay-in-full-game link).
> - **P4 (partial)** — two polish items shipped:
>   - **Daily streak** — `puzzle_rating.touch_daily_streak` + state fields
>     `daily_streak`/`best_daily_streak`/`last_active_date` (consecutive UTC days with ≥1 completed
>     puzzle, any source, solved or failed; idempotent within a day). Advanced from **both** the
>     tactics path (`record_result`) and the mistake path (`record_practice_result`), surfaced on
>     `/api/puzzle/config` + `/api/puzzle/state`, and shown as a quiet 🔥 flame chip in the rail
>     header.
>   - **Discrete rating curve** — a minimal bar sparkline (one bar per *rated* attempt, green up /
>     red down, +net label), rendered under the rail from the `history` the state endpoint already
>     returns; hidden until ≥2 rated points. Deliberately discrete (not a continuous line) to keep
>     solve mode calm per §7A.
>   - **Severity-curve calibration (§11.7 resolved, "option B")** — the P3.5 mistake-puzzle
>     `_target_percentile` band was retuned `0.90/0.25 → 0.95/0.35` after checking it against a real
>     137-mistake user distribution (a ~1400 was targeting only ~p62 of their own swings; now ~p70).
>     The skill clamp `800–2200` was **validated, not changed**: it ≈ the 2nd–98th percentile of the
>     Lichess Rapid distribution (fits N(1500, σ≈335): p2≈812, p98≈2188).
> - Tests: `test_puzzle_rating` (+daily-streak), `test_puzzles` (+merged-pool/weakness),
>   `test_puzzle_coach`, `test_web_puzzles`, `test_puzzle_shards`, `test_mistake_puzzles` —
>   **188 passed**, all fast, no network, no Stockfish. Live-verified: real-repo shard download +
>   sha256 reject, a full mistake-puzzle solve (badge, engine-validated acceptance, Glicko
>   untouched), the daily-streak advance across both sources, and the retuned severity band on real
>   user data.

---

## 0. The idea, and why it fits this app

The app already analyses *your own games* and explains your mistakes in words grounded in engine
lines. Puzzle mode is the complementary, *proactive* half of the same coaching loop: instead of
waiting for you to blunder in a real game, it feeds you tactical positions, tracks your rating,
and — uniquely — has the coach **teach the pattern** behind each solution.

The reason this is a natural fit (not a bolt-on):

- **Board, engine, and chat already exist.** chessground + chess.js for the board, the
  `_EnginePool` for grounding, `claude_bridge` (subscription `claude -p`, *and* the local-LLM
  path) for explanations, FastAPI for the API, and a per-OS `DATA_DIR` with established
  download-on-demand + LRU-cache + background-check patterns.
- **The coaching profile already knows the user's weaknesses.** `history.tag_motifs` /
  `build_profile` produce per-player weakness themes (missed forks, back-rank, loose conversion…).
  Puzzle selection can **bias toward those weaknesses**, closing the loop between "games you played"
  and "puzzles you train" — something Lichess's puzzle system can't do because it doesn't analyse
  your full games the way we do.
- **Offline-first stays intact.** Puzzles ship/download as small static data; solutions are
  pre-verified; the engine and LLM are *enhancements*, not requirements. Everything degrades
  gracefully with no network, mirroring the rest of the app.

### What's genuinely new (the only hard/novel work)

1. A **developer-side shard build script** that slices the Lichess puzzle DB into small,
   rating-banded, theme-stratified files and publishes them as GitHub Release assets.
2. A **Glicko-2** implementation + persistence.
3. A **puzzle-specific coach prompt** — the LLM input is meaningfully different from game analysis
   (see §6), and getting that framing right is the difference between "rephrases the engine" and
   "teaches the motif".

Everything else reuses patterns already in the codebase.

---

## 1. Data source & licensing

- **Source:** the official **Lichess puzzle database** (`lichess_db_puzzle.csv.zst`), **CC0**
  (public domain) — no attribution legally required, but we'll credit Lichess anyway.
- **Format (per row):** `PuzzleId, FEN, Moves, Rating, RatingDeviation, Popularity, NbPlays,
  Themes, GameUrl, OpeningTags`.
  - `FEN` is the position **before** the opponent's setup move.
  - `Moves` is a UCI sequence: **the first move is auto-played** (it's the move that creates the
    puzzle), then the solver must find the rest. Replies are forced; at a mate-in-1 leaf any
    mating move is acceptable.
  - `Themes` is the gold mine: `fork`, `pin`, `skewer`, `deflection`, `discoveredAttack`,
    `backRankMate`, `sacrifice`, `mateIn2`/`mateIn3`, `endgame`/`middlegame`/`opening`, `short`,
    `crushing`, `advantage`, etc. These drive both selection and the coach prompt.
- **Scale:** ~4.5M puzzles. We never ship or download the whole thing to a user (see §2).

---

## 2. Storage & distribution strategy (decided)

### The core constraint

The Lichess DB is a **single monolithic file** with **no remote query API** ("give me 1200–1300
`fork` puzzles" isn't a thing). So targeting by rating/theme means **we pre-slice it ourselves**
and host the slices.

### Decision: self-hosted, rating-banded, theme-stratified shards on GitHub Releases

**Build side (developer, one-time per DB refresh):** `scripts/build_puzzle_shards.py` downloads
the full DB, slices it into:

- **Rating bands** of 100 points across the playable range (≈ 600–2900 → ~23 bands; merge sparse
  extreme bands so each still meets the floor).
- Within each band, **stratify across themes** and cap at ~**10,000 puzzles/band** (`band_1200_1300.jsonl.zst`, ~2–3 MB each).
- A **`manifest.json`** listing every shard: band range, puzzle count, per-theme counts, byte
  size, and a **sha256 checksum** (so the client can verify downloads and know what exists without
  guessing filenames).

These are uploaded as **release assets in a SEPARATE, dedicated puzzle-data repo** (e.g.
`<org>/tintins-chess-puzzles`), tag `puzzles-v1`. Stable public URLs:
`https://github.com/<org>/tintins-chess-puzzles/releases/download/puzzles-v1/band_1200_1300.jsonl.zst`.

**Why a separate repo (decided).** GitHub has **no "hidden but publicly downloadable" release** —
every published release shows in the Releases tab, and *draft* releases (which are hidden) require
auth to download their assets, so they can't serve the app's unauthenticated fetch. To keep the app
repo's **Releases tab purely app versions**, the puzzle shards live in their own repo:
- App Releases stay clean — nothing to explain to users.
- **Independent versioning:** re-shard / refresh puzzles (`puzzles-v2`, …) without touching the app
  version or its releases; the two lifecycles are genuinely separate.
- Same mechanics — release assets are still CDN-backed stable URLs.
- The client just points `CHESS_PUZZLE_SHARD_REPO` at the data repo.

*(Partial in-repo alternatives, for reference: marking puzzle releases **pre-release** keeps the
"Latest" badge on the app version but they still appear in the list; **GitHub Pages** from a
`gh-pages` branch hides them from Releases entirely and fits our small total size, but is more setup
and less download-optimized than Release CDN assets. Ranking: **separate repo > Pages >
pre-release**.)*

**GitHub limits (confirmed fine for a free org account):** per-asset limit is **2 GB** (our shards
are ~MB); total release storage/asset count is effectively **unlimited** and does **not** count
against the repo's Git quota. Even sharding the *entire* DB is ~23 files of ~10 MB. Non-issue.

### The "≥100 puzzles per band" floor (required)

Because the source distribution is bell-curved (huge near ~1500, sparse at the extremes), enforce
the floor in **two layers**:

1. **Vendored baseline-coverage shard, in-repo (no download).** ~150 puzzles **per band across the
   entire rating range**, stratified by theme → ~23 bands × 150 ≈ **~4k puzzles ≈ ~1 MB
   compressed**. Guarantees **every band has ≥100 puzzles instantly and offline**, including bands
   the user isn't near. This *is* the "just in case" fallback. Lives at e.g.
   `data/puzzles/baseline.jsonl.zst` (committed, like the ECO data in `data/eco/`).
2. **Dense on-demand band shards** (the ~10k/band files above) downloaded around the user's actual
   rating ± a couple of bands, topped up on drift (§3).

**Build-script floor rules:** target 10k/band, **floor 100**; for a sparse band include *all*
available puzzles; if the entire DB has <100 for a band, take everything and **widen** that band
(merge adjacent) so the ≥100 guarantee holds.

### Client-side storage — flat files, NO SQLite (decided)

We **never** ship or build the monolithic SQLite DB. Since only a small per-user subset is ever on
disk, a database engine buys nothing:

- A shard is just a compressed JSONL file. It's small enough to **load into memory and filter by
  theme in Python** — a few thousand dicts is trivial. No SQLite, no schema, no migrations, no
  multi-GB import step. (SQLite would only earn its keep for the full ~4.5M-row monolith, which we
  explicitly do not ship.)
- Downloaded shards land under `<DATA_DIR>/puzzles/` (gitignored, shared by all entry points like
  the rest of `DATA_DIR`).
- **LRU cap** on accumulated shards (mirror `ANALYSIS_CACHE_MAX`): keep ~last N bands / ~50 MB,
  prune oldest. `CHESS_PUZZLE_CACHE_MAX` (default e.g. 12 bands, `0` = unbounded).

### Randomness — same-elo users get different puzzles

Shards are **static files on GitHub**, so everyone who downloads a given band gets byte-identical
bytes. Per-user variety therefore comes from **selection**, not from the download:

1. **Per-user random seed + client-side shuffle (core mechanism).** On first run, generate and
   persist a `user_seed` in `state.json`. `next_puzzle` shuffles the candidate pool (after the
   rating/theme/`seen_ids` filter) with that seed. A band holds ~10k puzzles but a user solves only
   hundreds, so two same-elo users diverge almost completely even from an identical file — at
   effectively zero extra storage. This is the main answer to "their subsets shouldn't be the same".
2. **Optional variant shards (only if you want the *downloaded bytes* to differ).** Build several
   variants per band (`band_1200_1300_v0…v4`, disjoint 10k slices) and have the client pick a
   random variant (seeded by `user_seed`) to download. Now different users literally hold different
   files. Costs a bit more GitHub storage (still trivial) and a touch more build complexity; adopt
   only if #1's "same file, different order" isn't enough for you. **Recommendation: ship #1; keep
   #2 in reserve.**

### Avoiding repeats (the Lichess mechanism, replicated)

Lichess writes a `puzzle_round` record per `(user, puzzleId)` the moment a puzzle is *served*
(solved or failed) and excludes any seen ID from future selection — a persisted "seen set"
subtracted at selection time. We do the same, locally:

- `state.json` holds a **`seen_ids`** set, written when a puzzle is *served* (not just solved), so a
  **failed** puzzle isn't re-served either. `next_puzzle(..., exclude=seen_ids)` subtracts it.
- When the unsolved/unseen pool in the user's bands runs low, that's a **drift/top-up trigger**
  (§3): fetch the next band or another variant. So the user effectively never repeats until they've
  genuinely exhausted thousands of puzzles.
- `seen_ids` can be capped/pruned (oldest first) if it ever grows large, but at JSONL-id sizes even
  100k ids is tiny — no real concern.

### Config knobs (new, mirroring existing `config.py` style)

```
CHESS_PUZZLES=1                 # master on/off (like CHESS_TABLEBASE)
CHESS_PUZZLE_SHARD_REPO=...     # dedicated puzzle-data repo, e.g. <org>/tintins-chess-puzzles
                                #   (NOT the app repo — keeps app Releases tab app-only)
CHESS_PUZZLE_SHARD_TAG=puzzles-v1
CHESS_PUZZLE_CACHE_MAX=12       # LRU band cap; 0 = unbounded
CHESS_PUZZLE_DOWNLOAD=1         # allow background shard fetch; 0 = baseline-only/offline
CHESS_PUZZLE_RD_PER_BAND=100    # RD-aware cache width: cache ±ceil(rd/this) neighbouring bands
                                #   (clamped ~±2…±6) so high-RD calibration warms a wider Elo spread
```

---

## 3. Dynamic, drift-aware shard fetching

The user's instinct — *"download a different subset once my rating or mistakes change"* — works
cleanly precisely because we control the shards. We already recompute rating + the coaching profile
after each puzzle/game, so we hook those as events.

**Source of truth = the separate data repo's release assets (required).** Every dense band shard
and the `manifest.json` are fetched from the **`CHESS_PUZZLE_SHARD_REPO`** release
(`CHESS_PUZZLE_SHARD_TAG`, default `Chess-analysis-mcp/tintins-chess-puzzles` / `puzzles-v1`) — the
GitHub Releases CDN URL
`https://github.com/<repo>/releases/download/<tag>/<band>.jsonl.gz` (and `.../manifest.json`),
**not** the app repo and **not** any bundled data beyond the vendored baseline. `ensure_manifest()`
pulls the manifest from that release; `ensure_band()` downloads the named band asset from the same
release and verifies it against the manifest sha256 before use. (The knobs already exist in
`config.py` — wire the download path to them.)

- **On rating change:** if the Glicko rating crosses out of the central cached band (or the
  unseen pool in the current band drops below a threshold — see `seen_ids`, §2), background-fetch
  the now-adjacent band(s) from the data-repo release. Always keep the user's band **± 2 bands**
  cached as the steady-state floor (warm-up below, stretch above).

- **Widen the cached window while the rating is still calibrating (required).** A brand-new user
  seeds at `rating=1500, rd=350`, and the first ~10–20 puzzles swing the rating by hundreds of
  points as Glicko homes in — so a tight ±2-band cache thrashes (download, jump two bands, re-
  download). Instead, **scale the number of neighbouring bands fetched by the current RD**: while RD
  is high (uncalibrated) pull a **wider spread** of neighbouring-Elo bands so the fast early jumps
  land on already-cached puzzles; as RD settles toward its floor, contract back to the steady ±2.
  A simple, faithful mapping (tune empirically): cache the user's band ± `ceil(rd / RD_PER_BAND)`
  bands, clamped to a sane max (e.g. ±6) and never below ±2 — so a fresh `rd≈350` user warms a broad
  band of neighbouring puzzles up front, and a settled `rd≈60` user only keeps the tight window.
  This keeps early calibration smooth and offline-friendly without ever fetching the whole DB. The
  `CHESS_PUZZLE_CACHE_MAX` LRU cap still bounds total disk, and everything stays throttled +
  best-effort (a failed fetch just falls back to the vendored baseline).
- **On weakness change:** when a new dominant theme emerges from the profile (`_dominant_motif` /
  `format_profile_for_prompt` already surface this), fetch a small **theme top-up** — biased toward
  that theme within the user's bands.
- **Mechanics:** all **throttled, background, best-effort**, exactly like `updates.py`'s release
  check. A failed download is a silent miss → fall back to the vendored baseline shard. Verify each
  download against the manifest sha256 before use.
- **Storage stays bounded** by the LRU cap above.

> This means: the very first session a brand-new user plays runs entirely on the **vendored
> baseline** (instant, offline). As they solve and a real rating emerges, dense shards layer in
> around it in the background. As they improve or their weaknesses shift, the cached set follows
> them. No full-DB download, ever.

---

## 4. Rating system — Glicko-2 (decided)

Match Lichess as closely as practical.

- Track **rating, RD (deviation), and volatility (σ)** per the Glicko-2 spec. Default seed for a
  new user: `rating=1500, rd=350, vol=0.06` (Glicko-2 defaults).
- **Each puzzle is an opponent** at its stored `Rating`. Optionally use the puzzle's stored
  `RatingDeviation` as the opponent RD; or treat puzzles as fixed (RD≈0) opponents — simpler, and
  fine for a single-user trainer. (Lichess only counts a puzzle toward your rating once the
  puzzle's own RD is low/established; we can mirror that by **only rating attempts on puzzles with
  `RatingDeviation < CHESS_PUZZLE_MAX_RD`**, default ~90, else play it "unrated".)
- **Update cadence:** Glicko-2 is formally defined over *rating periods* (batched games), but
  Lichess effectively updates per-puzzle with a tiny period. Do the same: **update after each
  solved/failed puzzle.**
- **Result mapping:** solved on first try → score 1; failed (wrong move before completing) → score
  0. Hints/give-up → score 0 (and don't offer rating credit). No partial credit (keeps it Lichess-
  like and simple).
- **Persistence:** `<DATA_DIR>/puzzles/state.json`:
  ```json
  {
    "rating": 1453.2, "rd": 61.0, "vol": 0.0591,
    "user_seed": 8412739,
    "seen_ids": ["abc12", "..."],
    "solved_ids": ["abc12", "..."],
    "streak": 4, "best_streak": 11,
    "history": [{"id":"abc12","puzzle_rating":1480,"result":1,"rating_after":1465,"date":"..."}],
    "by_theme": {"fork": {"seen": 30, "solved": 22}, "...": {}}
  }
  ```
  `user_seed` (generated once on first run) drives the per-user selection shuffle so same-elo users
  diverge; `seen_ids` is the served-puzzle set subtracted from selection (repeat-avoidance, §2);
  `solved_ids` ⊆ `seen_ids`.
  `by_theme` gives a puzzle-specific weakness view (separate from the game-derived profile, but
  combinable for selection).

New module: `server/core/puzzle_rating.py` — pure math + load/save, no engine, fully unit-testable
against published Glicko-2 worked examples.

---

## 5. Backend architecture (new modules, mirroring `server/core` + `server/web`)

```
server/core/
  puzzles.py            # shard load/cache, manifest fetch, puzzle selection, move validation
  puzzle_rating.py      # Glicko-2 update + state persistence (state.json)
  puzzle_session.py     # in-memory "current puzzle" state (singleton, like session.py)
server/web/
  routes_puzzles.py     # the HTTP API (below)
scripts/
  build_puzzle_shards.py  # DEVELOPER-side: slice full DB -> shards + manifest, for release upload
data/puzzles/
  baseline.jsonl.zst      # vendored ~150/band fallback (committed)
```

### `core/puzzles.py` responsibilities

- `ensure_manifest()` — fetch/cache `manifest.json` (throttled, like update checks).
- `ensure_band(rating)` / `ensure_theme_topup(theme, rating)` — background download + verify +
  LRU-prune; always succeed-or-fallback to baseline.
- `next_puzzle(rating, themes=None, exclude=solved_ids)` — pick a puzzle near `rating` (±band),
  optionally theme-filtered (weakness-biased), not already solved. Returns the parsed puzzle
  (FEN, solution UCI list, themes, rating, etc.).
- `validate_step(puzzle, ply_index, uci)` — is this the expected solution move at this step? Handle
  the mate-in-1 leaf branch-tolerance (any legal mate accepted). Returns
  `{correct, is_complete, opponent_reply_uci}`.

### `core/puzzle_session.py`

A singleton like `session.py`'s `ReviewSession`, holding the **current puzzle** + progress:
current ply in the solution, attempts, whether hints were used, start time. Lets the board and a
future MCP tool share state, consistent with the app's "one process, one session" design.

### HTTP API (`routes_puzzles.py`, all under `/api/puzzle`, sync handlers)

- `GET  /api/puzzle/next?theme=&difficulty=` → select + load a puzzle; auto-plays the setup move;
  returns `{id, fen, side_to_move, themes, rating, your_rating}` (NOT the solution).
- `POST /api/puzzle/move {id, uci}` → `validate_step`; returns `{correct, is_complete,
  opponent_reply, expected?}`. On a wrong move, the puzzle is **failed** (Glicko applied) but we
  still return enough to drive the explain flow.
- `POST /api/puzzle/hint {id}` → reveal the piece to move / next move (marks the attempt unrated).
- `POST /api/puzzle/giveup {id}` → reveal full solution, score 0.
- `POST /api/puzzle/explain {id, outcome}` → the **coach** (see §6); the puzzle-specific
  `claude_bridge` path.
- `GET  /api/puzzle/state` → current rating, RD, streak, by-theme stats (for a stats panel).
- `GET  /api/puzzle/config` → `{enabled, your_rating, themes_available, has_engine, has_llm}` for
  the frontend to gate UI.

All endpoints `lifecycle.touch()` like the rest, and are wrapped so puzzle features can **never**
break the board (best-effort, mirroring `CHESS_HISTORY` / `CHESS_TABLEBASE`).

---

## 6. The LLM coach for puzzles — why the input differs (the important part)

Game analysis today is **retrospective and engine-derived**: "you were at +X, your move dropped you
to Y, here's the better move and its refutation" (`claude_bridge._engine_facts` / `_game_facts` /
`coach_summary_ai`). For puzzles the framing **flips**, and the facts block should be built fresh.

### What's structurally different

1. **The solution is ground truth, not an engine opinion.** Tell the model so explicitly:
   *"This is a verified forced solution from a curated puzzle. Explain why it works; do not
   second-guess it or propose alternatives."* (Prevents hedging / re-deriving.)
2. **Theme tags are a first-class signal we don't have in game analysis.** Lead with them. The
   coach should **name the motif** ("this is a classic deflection: …").
3. **It's a multi-move forced sequence, not a single move.** The coach explains an *idea over
   several plies* — the key/quiet move, the forced replies, the point resistance breaks — not one
   move's win% delta.
4. **"Why the tempting wrong move fails" carries more weight.** Pedagogically, puzzles are about
   refuting the natural-but-wrong candidate. On a **failed** attempt, lead with refuting the user's
   actual move (engine-grounded) *then* teach the right idea.

### The puzzle facts block (new builder, e.g. `claude_bridge.explain_puzzle`)

Inputs assembled server-side (so the LLM never estimates):

- `themes`: the Lichess tag list (the headline signal).
- `side_to_move`, starting FEN, puzzle `rating`.
- `solution_line`: the full forced PV in **SAN**, annotated as *your move* vs *forced reply*.
- `key_move`: the crux move + a one-line engine fact (mate distance or eval / material swing) so
  the coach can say "wins the queen" / "forces mate in 3" with grounding — reuse `lines.engine_line`.
- `outcome`: `solved_first_try` | `solved_with_hints` | `failed`.
- **If failed:** `your_move` + its **engine refutation** (what the opponent plays to punish it) —
  reuse the existing refutation machinery (`engine_line` `shapes`/line).
- *Optional:* the single most **tempting alternative** + why it fails (one extra `engine_line` call).

### Two prompt variants by outcome

- **Solved:** "Confirm and name the motif; reinforce the *pattern* so they recognise it next time;
  keep it short." (Positive reinforcement, pattern-labelling.)
- **Failed:** "First explain concretely why *their* move doesn't work (use the refutation), then
  teach the winning idea step by step." (Diagnose → teach.)

### Reuse vs. new

- **Reuse:** the `claude -p` subprocess plumbing, the **local-LLM** path (`core/local_llm.py`), the
  friendly auth/limit error handling, and `format_profile_for_prompt` (optional personalisation —
  "you've missed forks before, here's another").
- **New:** a dedicated **puzzle prompt template** + the **puzzle facts builder** above. Keep it in
  `claude_bridge.py` next to `coach_summary_ai` (or a small `puzzle_coach.py` if it grows).
- **Offline:** like the rest, engine facts are best-effort; with no engine the coach still gets the
  ground-truth solution line + themes and can teach from those alone.

---

## 6A. Variant: "mistake puzzles" from the user's own games

A second puzzle *source* alongside the Lichess shards: resurface positions where **the user
themselves blundered in a past game** as practice. The most differentiated part of the feature —
Lichess can't do it, because it doesn't analyse your whole games the way this app does.

### Why it slots in cleanly

The raw material already exists. Every flagged mistake is in `history.games.jsonl` + the analysis
cache as a `MoveReview`: `fen_before`, played `move_uci`, engine `best`/`best_line_san`, `win_swing`,
the refutation, and a `motif` tag. A mistake puzzle is mostly a **re-framing of data already
computed** — no new download, no fresh engine sweep to *create* one.

### The defining difference: no single forced solution

Unlike a curated tactic, a mistake position is often positional — **several moves are acceptable**.
That changes three things vs. §1–6:

1. **Acceptance is eval-based, not line-matching.** Reuse `ReviewSession.thresholds` (the skill-/
   mode-scaled 5/10/15 win%-drop cutoffs already in the app): **accept any move whose win% drop is
   below the inaccuracy threshold.** Positional spots → several moves pass; a real missed tactic →
   only one passes. One rule, both cases. (Validate with a single `lines.engine_line` call on the
   submitted move.)
2. **They need the engine at solve time** (no pre-verified static line), so — unlike the static
   shards — mistake puzzles are an **engine-backed track, not an offline-data track**. Fine, since
   Stockfish is always local; just note they're unavailable if the engine is missing.
3. **They do NOT feed Glicko.** Difficulty is uncalibrated and the positions are self-selected from
   the user's own losses, so mixing them in would pollute the rating. Keep them a **separate,
   unrated "Practice from your games" track** with its own light stats (attempted/solved, by-motif).

### Coach reuse — the symmetry with §6

Mistake puzzles reuse the **game-analysis coach facts** (`claude_bridge._engine_facts` /
`_game_facts`: win% drop, better move, refutation), *not* the themes+forced-line builder of §6. So
the two puzzle types map onto the two coach styles: **standard puzzle → "teach the motif from the
forced line"; mistake puzzle → "why your move was off and whether your new try is better."** The
explanation is essentially the existing per-mistake `comment`, re-framed as a quiz result.

### Selection + spaced repetition (where the value is)

- Pull flagged mistakes for "me" from `history.load_records` + the cached sessions.
- **Order by skill-relative severity (decided).** These stay **unrated** (never touch Glicko), but
  the *queue order* adapts to the player's strength. Win% swing doubles as an "obviousness" proxy:
  a huge swing is a blunder you'd spot in hindsight; a small-but-flagged swing is the subtle stuff.
  So **shift the target severity down as rating rises**:
  - **Skill proxy:** the player's standard-puzzle Glicko rating, falling back to the **resolved
    review Elo** (`game_analysis._resolve_review_elo` → Skill level / PGN Elo) when they've done no
    standard puzzles yet — so ordering works from day one.
  - **Target a band, not a point, in the user's *own* swing distribution:** map rating → a
    percentile of their flagged-mistake swings (low rating → top of the distribution = big
    blunders; high rating → lower end = nuanced misses) and order by closeness to that band. Using
    percentiles auto-calibrates per player (a 2000's "nuanced" ≠ a 900's "nuanced") with no
    hand-tuned absolute thresholds.
  - Beginners thus drill their **big blunders**; stronger players get the **subtle, hard-to-find**
    positions they'd actually benefit from, not the hangs they'd self-correct.
- **Secondary boosts:** `_criticality` ("only-move" / hard-to-find) and **recurring motif** (the
  coaching profile already knows the user repeats forks/back-rank/etc.) tie-break toward the most
  instructive positions within the skill-appropriate band.
- **Space them Anki-style (the gate):** track `practiced: {key: {last, successes}}` in `state.json`.
  Re-solve a position correctly a couple of times → it retires; fail it → it returns sooner.
- `key = (game_id, reviewed_side, node_index)` — stable, dedupes across re-analyses.

### Visual distinction (required)

Make it unmistakable these are personal, not curated:

- A **distinct accent colour** + a **"From your game" badge** carrying the matchup, mode and date
  ("vs Opponent · blitz · Mar 3"). Standard tactic puzzles keep the neutral style.
- A **"replay in the full game"** link that opens the position in the normal review board
  (`goto`/`/position`), so the puzzle connects back to the game it came from.
- May **interleave occasionally** into the main puzzle flow (configurable, e.g. ~1 in 6) **but
  always badged**, plus a dedicated **"From your games"** filter/tab.

### Backend shape

Fold into the puzzle modules with a **`source` discriminator** (`"lichess" | "your_games"`) rather
than a parallel stack:

- `core/puzzles.py` gains `next_mistake_puzzle(...)` (history-derived) beside `next_puzzle(...)`.
- `routes_puzzles.py`: `GET /api/puzzle/next?source=your_games`; `/move` branches on source
  (eval-threshold validation for `your_games`, exact-line for `lichess`); `/explain` picks the
  matching coach builder.
- No engine sweep to enumerate — candidates come straight from stored `MoveReview`s; only the
  *submitted move* is evaluated live.

### Offline caveat

Mistake puzzles require the engine for validation, so they're gated on `has_engine` in
`/api/puzzle/config`; with no engine, only the static Lichess track shows.

---

## 7. Frontend (no-build, mirroring `frontend/main.js` patterns)

- **A "Puzzles" mode** — a new tab/entry alongside the games panel (reuse the panel/drawer system).
  Switching to it loads `GET /api/puzzle/next`.
- **Board reuse:** the existing chessground board, oriented to `side_to_move`; eval bar optional
  (can hide it during solving to avoid spoiling). Auto-play the setup move with a brief animation,
  then accept the user's move.
- **Per-ply validation:** on each user move, `POST /api/puzzle/move`; correct → animate the forced
  reply and continue; wrong → red-flash, mark failed, surface **"Explain"**.
- **Solved/failed panel:** show ✓/✗, rating delta (e.g. "1453 → 1465 (+12)"), streak, and the
  **"Explain why"** button → `POST /api/puzzle/explain` (renders with the existing
  `renderMarkdown`). Next puzzle button.
- **Theme / difficulty controls:** "train my weaknesses" toggle (uses the profile), optional theme
  picker, easier/harder difficulty. A small stats card (rating curve from `state.history`).
- **Arrows reuse:** the existing `setAutoShapes` brushes (grey/green/red) work for showing the
  solution / refutation after the fact.
- **"From your games" styling (§6A):** a distinct accent + "From your game" badge (matchup · mode ·
  date), a "replay in full game" link, and eval-threshold feedback ("✓ good — engine also likes
  Nf3") instead of a single right answer. Badged even when interleaved into the main flow.

All gated on `GET /api/puzzle/config.enabled` so a build without puzzle data simply doesn't show
the tab.

---

## 7A. UI / visual design (decided)

**Guiding principle: one focal point.** Game-review mode is intentionally dense (eval bar, win
graph, mistakes list, comments) — right for analysis. Puzzles are the opposite: **solve mode is
calm and uncluttered, the board dominant, everything else a quiet rail.** So puzzle mode does **not**
reuse the 3-column analysis layout; it collapses to **board + a single slim side rail**, hiding the
win graph and mistakes panel entirely. Target feel: chess.com's *clarity*, not its chrome — and
distinctly ours (dark Lichess-ish palette already in `styles.css`).

### Entry — top-level mode toggle (DECIDED)

A prominent header switch **`[ Analyze | Puzzles ]`** (peer activities), **not** a tab buried in the
Games drawer. Clicking *Puzzles* swaps the whole workspace to the focused solve layout.

```
┌──────────────────────────────────────────────────────────┐
│  ♞ Tintin's Chess          [ Analyze | Puzzles ]    ⚙     │
├───────────────────────────────┬──────────────────────────┤
│                               │  Puzzle  ·  1487          │  rating (quiet)
│                               │  ●●●●○  streak 4          │
│         (chessground          │  ┌──────────────────────┐ │
│            board,             │  │  Black to move       │ │  THE prompt = hero
│        oriented to            │  │  Find the best move  │ │
│         side-to-move)         │  └──────────────────────┘ │
│                               │   ◦ Hint    ◦ Skip        │  ghost buttons (muted)
│                               │  ── after solve ──        │
│                               │  ✓ Solved!  +8  → 1495    │
│                               │  Theme: Fork              │
│                               │  [ ✨ Explain why ]       │
│                               │  [ Next puzzle → ]        │
└───────────────────────────────┴──────────────────────────┘
```

- **The prompt card is the hero element** — "Black to move / Find the best move", large type, one
  clear instruction.
- **Eval bar hidden while solving** (it spoils), revealed only after; **no win graph** at all.
- **Hint/Skip are ghost (low-emphasis) buttons** so they don't tempt; they earn attention only when
  stuck.
- The **result block replaces the prompt card in place** (no layout jump): ✓/✗, rating delta, theme
  tag, then Explain + Next.

### Colour — a puzzle accent distinct from the "correct" green

Don't add a new system; **derive a puzzle accent** so the mode reads as its own place without
clashing. Analysis accent is green (`--accent: #629924`) — perfect for *correct*, so **reserve green
for correctness** and give puzzle chrome (prompt-card border, rating pips) a **cool blue-teal**
`--puzzle-accent: ~#4a90c2`. Green must mean "right", not "puzzle mode".

### Post-solve feedback — square blink (DECIDED) + later piece pop

The tile of the **last piece moved** pulses at the end:
- **Fully correct → green pulse** (`--accent` #629924).
- **Partially correct → yellow pulse** (~#d9a441). "Partial" only applies to §6A mistake puzzles
  (several moves pass the threshold but one is best); standard tactics are pass/fail → green or the
  red wrong-move state only.
- **Wrong → destination square red flash + gentle shake**, piece returns; no modal, no penalty drama.

**Implementation — a decoupled CSS overlay, don't fight chessground internals.** Lay a transparent
layer exactly over the board (same `--board-size`, `position:absolute`) and drop one cell into it,
positioned from the square's file/rank **respecting orientation** (the board flips for Black). Pure
CSS keyframes pulse it ~3× then it self-removes. Sketch:

```css
.sq-blink { position:absolute; width:12.5%; height:12.5%; border-radius:3px;
            pointer-events:none; animation: sq-pulse .42s ease-in-out 3; }
.sq-blink.ok   { box-shadow: inset 0 0 0 9999px rgba(98,153,36,.55); }   /* green */
.sq-blink.part { box-shadow: inset 0 0 0 9999px rgba(217,164,65,.55); }  /* yellow */
@keyframes sq-pulse { 0%,100%{opacity:0} 50%{opacity:1} }
```
```js
function squareXY(square, orientation){            // 'e4' -> {left,top} %
  let f = square.charCodeAt(0)-97, r = 8-(+square[1]);
  if (orientation === 'black'){ f = 7-f; r = 7-r; }
  return { left: f*12.5 + '%', top: r*12.5 + '%' };
}
```
This survives chessground redraws, is trivially themeable, and **the same overlay layer is exactly
where the later richer animation lives** — a scale-pop/glow on the moved piece on a *perfect* solve.
So the blink now is a clean stepping-stone, not throwaway.

### "From your game" skin (§6A)

Same layout, visually unmistakable: a **warm-amber badge** ("From your game · vs Opponent · blitz ·
Mar 3") above the prompt, an amber left-border on the prompt card (vs teal), a **"↩ replay in full
game"** link in the result block, and coaching-tone wording ("Better than your game move ✓" rather
than "Solved!").

### Elegance touches (restraint by default)

- **Forced replies animate smoothly** (chessground's built-in animation) with a short beat, so the
  solution *plays* rather than snaps.
- **Optional auto-advance** to the next puzzle after a correct solve + brief pause (settings toggle),
  for flow-state grinding.
- **No confetti, no sounds by default** (optional later) — the single green pulse is the reward.

---

## 8. Optional: MCP tool surface (terminal parity)

Consistent with the app's "everything the board does, a tool can do" design, optionally add MCP
tools so puzzles are drivable from Claude Code too:

- `mcp__chess__next_puzzle(theme?, difficulty?)` → position + your rating.
- `mcp__chess__solve_puzzle(uci...)` → validate + rating update + a coach explanation.

Lower priority than the board; the board is the primary UX. Can come after Phase P3.

---

## 9. Phased build plan

Each phase is independently shippable and leaves the app working.

### Phase P0 — Shard pipeline & data (developer-side)
**Objective:** turn the Lichess DB into hosted shards + a vendored baseline.
**Tasks:** write `scripts/build_puzzle_shards.py` (download full DB → band/theme slices + manifest
+ checksums); enforce the ≥100 floor + band-merge rule; produce `data/puzzles/baseline.jsonl.zst`
(commit it); upload dense shards as `puzzles-v1` release assets.
**Acceptance:** manifest lists every band with count ≥100; baseline shard loads offline and yields
a puzzle for any band; a dense shard downloads + verifies against its sha256.

### Phase P1 — Core solve loop (no LLM, no fancy selection)
**Objective:** solve a puzzle end-to-end on the board with rating updates.
**Tasks:** `core/puzzles.py` (load baseline, `next_puzzle`, `validate_step`), `puzzle_rating.py`
(Glicko-2 + `state.json`), `puzzle_session.py`, `routes_puzzles.py` (`next`/`move`/`giveup`/
`state`/`config`); frontend per the **§7A UI design** — top-level `Analyze | Puzzles` toggle, the
focused board + slim-rail solve layout, hero prompt card, and the **green/yellow square-blink**
overlay (red flash + shake on wrong).
**Acceptance:** select → solve/fail a puzzle; rating + streak persist across restart; wrong move
fails correctly; mate-in-1 branch tolerance works; **correct solve pulses the moved tile green
(yellow for a §6A partial), wrong flashes red** and the blink survives a board redraw/orientation
flip. Unit tests: Glicko-2 vs a published worked example; `validate_step` on hand-built puzzles
(incl. the leaf-branch case).

### Phase P2 — The puzzle coach (LLM)
**Objective:** "Explain why" grounded in solution line + themes + (optional) engine facts.
**Tasks:** the puzzle facts builder + `explain_puzzle` prompt variants (solved/failed) in
`claude_bridge`; `/api/puzzle/explain`; wire the Explain button; reuse local-LLM + error handling.
**Acceptance:** a solved puzzle gets a correct, motif-naming explanation; a failed puzzle's
explanation refutes the user's actual move *and* teaches the solution; works with no engine
(solution+themes only) and via the local-LLM path; no-`claude` degrades gracefully (amber banner
pattern).

### Phase P3 — Drift-aware downloads & weakness targeting  ✅ DONE
**Objective:** the cached shard set follows the user's rating + weaknesses, pulled from the separate
data-repo release.
**Tasks:** `ensure_manifest()` + background `ensure_band`/`ensure_theme_topup` fetching band shards
+ `manifest.json` **from the `CHESS_PUZZLE_SHARD_REPO`/`_TAG` release** (verify each against the
manifest sha256); `next_puzzle` reads downloaded bands, not just the vendored baseline; **RD-aware
window width** — cache ± `ceil(rd / RD_PER_BAND)` bands (clamped ~±2…±6) so early, high-RD
calibration warms a broad spread of neighbouring-Elo puzzles and contracts to ±2 as RD settles; LRU
prune to `CHESS_PUZZLE_CACHE_MAX`; "train my weaknesses" toggle wired to the coaching profile;
theme/difficulty controls; stats card. (Config knobs `CHESS_PUZZLE_DOWNLOAD` /
`CHESS_PUZZLE_CACHE_MAX` / `CHESS_PUZZLE_SHARD_REPO` / `CHESS_PUZZLE_SHARD_TAG` already exist —
currently unused; this phase wires them.)
**Acceptance:** the manifest + a band shard are fetched from the **data-repo release** (not the app
repo) and sha256-verified before use; crossing a band boundary triggers a background fetch of the
adjacent band(s); a **fresh high-RD user caches a wider neighbouring-Elo spread** than a settled
low-RD user (verifiable from the fetched band set); a weakness theme biases selection; cache stays
under the LRU cap; everything best-effort (network off / bad checksum → the vendored baseline still
serves puzzles).

### Phase P3.5 — Mistake puzzles from the user's own games (§6A)  ✅ DONE
**Objective:** a second, engine-backed puzzle source drawn from the user's flagged mistakes.
**Tasks:** `next_mistake_puzzle` (history-derived selection; **skill-relative severity ordering** —
rating→swing-percentile band, skill proxy = puzzle Glicko → `_resolve_review_elo` fallback;
`_criticality` + recurring-motif tie-breaks; spaced-repetition via `practiced` in `state.json`);
eval-threshold validation (`lines.engine_line` + `ReviewSession.thresholds`); the game-analysis
coach branch in `/explain`; the `source` discriminator across `/next`/`/move`/`/explain`; frontend
"From your game" badge + accent + replay link + optional interleaving; gate on `has_engine`.
**Acceptance:** a past blunder appears as a puzzle with the right badge/metadata; **multiple good
moves are accepted** (any within the inaccuracy threshold) while a clear slip is rejected; **queue
order is skill-relative** (a low-rated profile surfaces big-swing blunders first; a high-rated one
surfaces smaller-swing/high-criticality misses first); the explanation uses the game-analysis
facts; re-solving retires it / failing resurfaces it; it does **not** change the Glicko rating; the
replay link opens the source game.

### Phase P4 (optional) — MCP tools + polish  ◐ PARTIAL
**Objective:** terminal parity + rough-edge cleanup.
**Tasks:** `next_puzzle`/`solve_puzzle` MCP tools; rating-curve visualisation ✅; daily streak ✅;
optional "puzzle storm"/rush timed mode.
**Done:**
- **Daily streak ✅** — `puzzle_rating.touch_daily_streak` (consecutive UTC days with ≥1 completed
  puzzle; idempotent per day; extends on a consecutive day, resets after a gap), new state fields
  `daily_streak`/`best_daily_streak`/`last_active_date`, advanced from both the tactics
  (`record_result`) and mistake (`record_practice_result`) paths so unrated practice still counts,
  exposed on `/config` + `/state`, rendered as a quiet 🔥 flame chip in the rail header
  (`renderDailyStreak`).
- **Rating-curve visualisation ✅ (discrete by design)** — `renderRatingCurve` draws a compact bar
  sparkline, one bar per *rated* attempt (up=green/down=red, capped at the last ~24, with a net
  delta label), from the `history` the `/state` endpoint already returns; hidden until ≥2 rated
  points. Kept discrete (not a continuous line chart) so it doesn't compete with the solve, per the
  §7A "one focal point" principle.
- **MCP tools ✅** — `next_puzzle`/`solve_puzzle` in `mcp_server.py`, driven through a shared
  `core/puzzle_flow.py` orchestration module so the board and the tools mutate the SAME
  `puzzle_session` and rate an attempt by exactly one rule (`score_attempt`). `solve_puzzle` accepts
  UCI or SAN, a single move or a whole line (opponent replies auto-played), supports curated +
  `your_games` sources, and returns an engine-grounded `coach_facts` block for Claude Code to
  narrate (no redundant `claude -p` subprocess, mirroring how `analyze_game` returns facts).
- **Puzzle storm ✅** — `core/puzzle_storm.py` (a singleton timed run reusing tactic selection + the
  shared session, injectable clock for tests), `/api/puzzle/storm/{start,move,next,state,end}`, and
  a focused frontend sub-mode (`[ Solve | ⚡ Storm ]` switch, big countdown, score/combo pips,
  bonus-time float, game-over card). **Unrated by design** — never touches Glicko; only a
  `storm_high`/`storm_best_combo` in `state.json`. Base clock `CHESS_PUZZLE_STORM_DURATION` (180s),
  combo milestones grant bonus time, a wrong move costs time and moves straight on.
**Acceptance:** puzzles drivable from Claude Code, board + tool share one `puzzle_session` (verified
by `test_mcp_puzzles.py`); a storm run scores solves, ramps difficulty, banks a highscore, and never
moves the rating (verified by `test_puzzle_storm.py` + storm cases in `test_web_puzzles.py`).

---

## 10. Testing notes (consistent with the existing suite)

All fast, no-network, no-Stockfish unit/mock tests, matching `tests/` conventions:

- `test_puzzle_rating` — Glicko-2 update against a **published worked example** (exact numbers);
  seed/persistence round-trip; **daily-streak** (idempotent within a UTC day, extends on a
  consecutive day, resets after a gap, advanced by a completed puzzle even on a fail).
- `test_puzzles` — `validate_step` on hand-built puzzles incl. the mate-in-1 leaf branch; selection
  respects rating band + theme filter + **`seen_ids` exclusion** (no repeats); the **`user_seed`
  shuffle is deterministic per seed but differs across seeds** (two seeds → different order on the
  same pool); **manifest verification** (sha256 mismatch → reject) with the network mocked (never
  hit GitHub).
- `test_puzzle_coach` — the **facts builder** produces the expected structured block for solved vs
  failed (engine + LLM mocked); themes lead; failed case includes the refutation slot.
- `test_web_puzzles` — FastAPI `TestClient`: `/api/puzzle/config` + `next`/`move` happy path on the
  vendored baseline (no download, no engine).
- `test_mistake_puzzles` (§6A) — history-derived selection on a fixture `games.jsonl`:
  **skill-relative ordering** (a low skill proxy surfaces the biggest-swing mistakes first, a high
  one surfaces the smaller-swing/high-criticality ones first, on the same fixture), motif priority +
  spaced-repetition retire/resurface; **multi-solution acceptance** via a mocked eval-threshold
  (several moves pass, a slip fails); confirms mistake puzzles **don't** touch the Glicko state.
  Engine mocked.

Pin any new tactic/branch logic with hand-built FENs that must (and must-not) pass, per the
existing "verify any new detector empirically" rule.

---

## 11. Open decisions / things to confirm before P0

1. **Puzzle RD gating:** mirror Lichess (only rate well-established puzzles, `RatingDeviation <
   ~90`) or rate every attempt? (Leaning: gate, for faithfulness — `CHESS_PUZZLE_MAX_RD`.)
2. **Band width:** 100-pt bands (more shards, tighter targeting) vs 200-pt (fewer files). (Leaning:
   100.)
3. **Dense shard cap:** 10k/band default — tune against real shard sizes after the build script
   runs once.
4. **Branch tolerance beyond mate-in-1:** accept *any* equally-winning move (engine-checked) at
   every step, or only the stored line + mate leaves? (Leaning: stored line + mate leaves for MVP;
   engine-checked alternatives are a P3+ nicety, and need the engine so they break offline.)
5. **Where puzzle weakness stats live:** standalone `by_theme` in `state.json` vs folding into the
   existing coaching profile. (Leaning: standalone, combined only at selection time.)
6. **Variety mechanism:** per-user seeded shuffle of one shared shard (no extra storage) vs. also
   building **variant shards** so the downloaded bytes differ per user. (Decided: ship the seeded
   shuffle; keep variant shards as an opt-in if "same file, different order" proves insufficient.)
7. **Mistake-puzzle difficulty (§6A): DECIDED — unrated, ordered by *skill-relative* severity.**
   Never feeds Glicko. Queue order shifts the target win%-swing band down as the player's rating
   rises (beginners → big blunders; stronger players → nuanced, high-`_criticality` misses), using
   a percentile of the user's own swing distribution; skill proxy = standard-puzzle Glicko, falling
   back to `_resolve_review_elo`. **Sub-question RESOLVED (data-backed, "option B").** Calibrated
   against a real 137-mistake user distribution: the percentile band was raised `0.90/0.25 →
   0.95/0.35` (a ~1400 was targeting only ~p62 of their own swings — too subtle; now ~p70), and the
   skill clamp `800–2200` was **validated** as ≈ the 2nd–98th percentile of the Lichess Rapid
   distribution (fits N(1500, σ≈335): p2≈812, p98≈2188), so it was kept unchanged. **Known
   limitation / future option C:** the map stays *percentile-linear*, so difficulty steps compress
   at the high end (a skewed swing distribution means 1800 vs 2200 land on near-identical puzzles).
   A *swing-value-space* interpolation (even difficulty steps, at the cost of pushing mid-skill
   targets higher on a bottom-heavy distribution) would fix this; deferred as an opt-in curvature
   knob rather than a default, to be A/B'd once there's multi-user data.
8. **Mistake-puzzle acceptance band (§6A):** accept moves under the *inaccuracy* threshold, or a
   tighter custom band so near-misses still count as "not quite"? (Leaning: reuse the existing
   inaccuracy threshold for consistency with how games are graded.)

---

## 12. One-paragraph summary

Puzzle mode reuses almost all of the app's substrate (board, engine pool, `claude_bridge` +
local-LLM, `DATA_DIR`, download-on-demand, LRU cache, coaching profile). The only novel work is a
**developer-side shard build script** (slice the CC0 Lichess DB into rating-banded, theme-
stratified GitHub-Release shards + a vendored ≥100-per-band offline baseline), a faithful
**Glicko-2** rating system, and a **puzzle-specific coach prompt** whose facts block leads with the
puzzle's **themes** and **ground-truth forced solution line** (refuting the user's wrong move on a
failure) rather than the win%-drop framing of game analysis. Shards are plain compressed JSONL
(**no SQLite, no monolithic DB**), fetched in the background and following the user's rating and
weaknesses over time; a **per-user seeded shuffle + served-`seen_ids` exclusion** gives same-elo
users different, non-repeating puzzle streams from identical static files. A second source (§6A)
re-frames the user's **own past blunders** as engine-validated, multi-solution, **unrated** practice
puzzles — visually badged "From your game", spaced-repetition resurfaced, and explained with the
game-analysis coach rather than the themes coach. Nothing requires the full ~1.5 GB DB, and
everything degrades gracefully offline.
