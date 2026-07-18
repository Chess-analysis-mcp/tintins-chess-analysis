// Vendored locally (frontend/vendor) so the board works fully offline — no CDN dependency.
import { Chessground } from "/vendor/chessground.min.js";
import { Chess } from "/vendor/chess.min.js";

// --- state ---------------------------------------------------------------
const chess = new Chess();
let ground = null;

let timeline = []; // nodes 0..N for the whole game
let mistakes = [];
let player = "white"; // the reviewed side (drives the header label)
let orient = "white"; // board orientation; starts at `player` but the `f` hotkey flips it
// Current game's PGN + player names, so "Review other side" can re-open the same game reviewing
// the opponent without a refetch. Set when a game is opened (provisional) and on /session.
let currentPgn = null;
let gameWhite = "";
let gameBlack = "";
// URL of the current game on Lichess/Chess.com (from the PGN's Site/Link header), for the ↗ link
// in the board header. Null when the PGN carried no such URL (e.g. an offline/local PGN).
let currentGameUrl = null;

let cur = 0; // current timeline node (valid when !exploring)
let anchorNode = 0; // the review (mistake) node we started from
let currentMistake = -1;
let currentPrompt = "";

let exploring = false; // off the game line, free-playing variations
let exploreBaseNode = 0; // node we left the timeline from
// Verdict for the move just tried in explore mode, surfaced in the (always-visible) status
// banner under the board — the full #verdict panel lives far down the scrolling side column,
// so without this you'd never see "good / mistake / blunder" for a variation you played.
// null = none, "pending" = evaluating, move dict = result, {error:true} = failed.
let exploreVerdict = null;

let bestArrowOn = false;
// Live best-move arrows: progressively deepen and refine while you sit on a position,
// cancelled the moment the position changes, with a hard time cap so it never runs forever.
let bestArrows = [];
// Threat arrows (yellow): what the side that just moved is threatening to play next
// (a null-move engine search server-side). Toggled like the best-move arrows.
let threatArrowOn = false;
let threatArrows = [];
const THREAT_DEPTH = 16; // one fixed-depth probe is enough for "what's the threat?"
let searchGen = 0; // bumped on every position change to invalidate in-flight searches
const SEARCH_DEPTHS = [14, 18, 22]; // escalating precision; arrows update after each
const SEARCH_MAX_MS = 5000; // stop deepening after this, even if more depth is available
const SEARCH_DEBOUNCE_MS = 120; // coalesce rapid navigation before hitting the engine
let evalShapes = []; // extra board shapes from the last /api/evaluate (e.g. red refutation arrow)
// Chat context: always the position BEFORE the move in question + that move's SAN, so Claude
// can ground "why is this bad?" on the exact move regardless of timeline vs. explore mode.
let chatFen = null;
let chatMove = null;
let chatSession = null; // claude -p session id, threaded across questions
let chatGen = 0; // bumped on each game open; invalidates an in-flight restoreChat for the old game

// History panel + progressive (navigate-while-analyzing) open.
let analyzing = false; // true during phase 1: provisional PGN timeline, no engine evals yet
let historyMode = "normal"; // "normal" (local games) | "lichess" | "chesscom" | "paste"
let myPlayerId = ""; // configured user's id, for inferring side on lichess lookups
let pollTimer = null; // analysis-status poller
let batchInfo = null; // {total, self_handle, lastDone} while a multi-game upload is analyzing
let lichessCount = 5; // how many recent lichess games to show ("Load more" grows it)
let lichessUser = ""; // the handle currently shown in lichess mode (for "Load more")
const LICHESS_PAGE = 5; // initial count + how many more each "Load more"
let chesscomCount = 5; // paging for the Chess.com tab, same scheme as lichess
let chesscomUser = "";
// "My games": /api/history returns every analysed game; we render them in pages (inside the
// fixed-height scroll box) so the list starts short and grows on "Show more", not the page.
let historyGames = []; // all rows from the last /api/history fetch
let historyCount = 10; // how many to show now ("Show more" grows it)
const HISTORY_PAGE = 10; // initial count + how many more each "Show more"

// App mode (double-click launcher): on open, auto-load the user's most recent game.
// `appUsername` is the Lichess handle (config.LICHESS_USERNAME); it drives "open my latest game"
// and the Lichess panel placeholder. `chesscomUsername` is the configured chess.com handle — it
// drives the automatic chess.com sync on launch (new games are fetched + analyzed into My games)
// and the chess.com autoload for users without a Lichess handle.
let appMode = false;
let appUsername = "";
let chesscomUsername = "";
// Auto-sync the configured chess.com user's newest games on launch (a Settings option, default on):
// check the most recent `chesscomSyncMax` games and analyze any not already in history.
let chesscomSync = true;
let chesscomSyncMax = 5;
// Rapid-refresh detection: if the user hammers Load/Sync on the Chess.com tab (a fresh game not
// showing yet), surface a small "chess.com is slow to publish — upload the PGN instead" hint.
let chesscomRefreshTimes = [];
const CHESSCOM_REFRESH_WINDOW_MS = 20000;
const CHESSCOM_REFRESH_TRIGGER = 3;
// On-demand Claude-written end-of-game summary: generated when the user presses the button, or
// automatically per game when `coachAiAuto` is on (a Settings option). `coachAiToken` invalidates
// an in-flight request when a new game is opened so a stale summary never lands on the wrong game.
let coachAiAuto = false;
let coachAiToken = 0;
// Whether chat questions inject the cross-game coaching profile (a Settings option, default on).
let personalizeHistory = true;

// --- puzzle mode ---------------------------------------------------------
// A focused tactical trainer that reuses the same chessground board + chess.js instance. When
// `puzzleMode` is on the analysis layout is hidden (body.puzzle-mode) and onUserMove routes to the
// puzzle handler. `puzzleData` is the active puzzle from /api/puzzle/next; `puzzleSolveColor` is the
// side the user plays; `puzzleShapes` are the post-solve solution/refutation arrows.
let puzzleMode = false;
let puzzleConfigCache = null; // /api/puzzle/config result (rating, has_engine, has_llm)
let puzzleData = null; // current puzzle {id, fen, moves(not sent), side_to_move, ...}
let puzzleSolveColor = "white";
let puzzleDone = false; // true once solved/failed/given-up (board locked, result shown)
let puzzleFailed = false; // user played a wrong move
let puzzleHinted = false;
let puzzleAnimations = true; // Settings toggle: play the board solve/miss animations vs. text only
let puzzleAutoAdvance = false; // Settings toggle: auto-load the next puzzle a beat after a solve (default off)
let puzzleAdvanceTimer = null; // pending auto-advance timeout handle (cancelled on any nav/interaction)
// The rating summary from the FIRST wrong move (which already applied the Glicko loss). Kept so the
// final "Solved (after a miss)" card can surface that the rating already moved, rather than looking
// like nothing happened. Reset on each new puzzle.
let puzzleMissRating = null;
let puzzleBusy = false; // true while animating a forced reply (ignore input)
let puzzleShapes = []; // solution/refutation arrows shown after the fact
let puzzleLastMove = null; // [from, to] of the last move, for the square-blink
let puzzleStreak = 0; // server streak number (clean solves in a row)
let puzzleDailyStreak = 0; // consecutive days practiced (from the server)
let puzzleBestDaily = 0;
// Last few puzzle outcomes THIS session (true = solved clean/green, false = missed/red), shown as
// colour-coded pips beside the streak. Mirrored to sessionStorage so a reload keeps them.
let puzzleResults = [];
// Bumped every time a new puzzle is loaded/resumed. An in-flight move/solution handler captures it
// and bails after each await if it changed, so a stale callback from the PREVIOUS puzzle can never
// leak its result (or a forced reply) onto the puzzle now on the board.
let puzzleGen = 0;
// Snapshot of the last FINISHED puzzle (solved or solution-shown), so a "‹ Previous" button can
// restore it in review mode. This exists because auto-advance yanks you onto the next puzzle a beat
// after a solve, which otherwise strands you: the finished puzzle (and any "why?" you'd want to ask
// about it) is gone. Single-level: restoring clears it; skipping an UNSOLVED puzzle clears it too.
let prevPuzzleSnapshot = null;
// P3/P3.5 selection prefs. `puzzleSource` + the puzzle/analyze mode are persisted in localStorage
// (keyed to this host:port origin), so closing the app and reopening it via ANY launcher
// (.app/.command/.bat) returns to where you left off — the mode AND the sub-tab.
const PZ_MODE_KEY = "pzLastMode"; // "1" = was in puzzle mode
const PZ_SOURCE_KEY = "pzSource"; // "lichess" | "your_games"
function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
let puzzleSource = lsGet(PZ_SOURCE_KEY) || "lichess"; // "lichess" curated tactics | "your_games" own-game mistakes
let puzzleDifficulty = null; // null | "easier" | "harder"
let puzzleWeakness = false; // bias curated tactics toward the player's weak themes
// Follow-up chat after "Explain why": only exists once explain has run. Threads onto the
// explanation's claude session so questions have its context.
let puzzleChatSession = null; // claude -p session id from the explanation, for --resume
let puzzleChatFen = null; // the position the follow-up chat grounds on (the puzzle solve position)
let puzzleChatBusy = false;
// When set, the next analysis to become ready jumps to this timeline node (a "replay in full game"
// from a mistake puzzle). Consumed once in onAnalysisReady.
let pendingGotoPly = null;

// --- puzzle storm (timed rush) ---
// A sub-mode of puzzle mode: `stormShown` = the storm scoreboard is on screen (vs the Solve
// trainer); `stormRunning` = a run is live (routes board moves to onStormMove + ticks the clock).
let stormShown = false;
let stormRunning = false;
let stormPuzzle = null; // {id, fen, side_to_move, themes}
let stormBusy = false; // true while validating a move / animating a reply
let stormGen = 0; // bumped per storm puzzle, so a stale async handler bails
let stormTimerId = null;
let stormDeadline = 0; // client-side ms wall-clock the run ends at (server remaining is authoritative)
let stormScore = 0, stormCombo = 0;
let stormReviewEntries = []; // per-puzzle log of the just-finished run (for the post-run AI review)
let inStormReview = false; // true while reviewing one finished storm puzzle on the board
// Shared solution step-through (Storm review AND the normal Solve trainer, once a puzzle is finished).
let solutionPlay = null; // {fens, ucis, sans, lastMoves, idx, yourMove, solved}
let solutionGen = 0; // bumped to cancel an in-flight fetch/animation (leaving, new puzzle, manual scrub)

const $ = (id) => document.getElementById(id);
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

// --- chess helpers -------------------------------------------------------
function computeDests() {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}
function turnColor() {
  return chess.turn() === "w" ? "white" : "black";
}
function isPromotion(from, to) {
  return chess
    .moves({ verbose: true })
    .some((m) => m.from === from && m.to === to && m.flags.includes("p"));
}
function samePosition(fenA, fenB) {
  // compare board + side-to-move + castling + ep, ignore clocks
  return fenA.split(" ").slice(0, 4).join(" ") === fenB.split(" ").slice(0, 4).join(" ");
}
function pieceGlyph(san) {
  if (san.startsWith("O-O")) return "♚";
  return { N: "♞", B: "♝", R: "♜", Q: "♛", K: "♚" }[san[0]] || "♟";
}

// --- board rendering -----------------------------------------------------
function renderBoard() {
  const color = turnColor();
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: color,
    check: chess.inCheck(),
    movable: { color, dests: computeDests(), free: false, showDests: true },
  });
  drawArrows();
}

function arrowShape(uci, brush) {
  return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
}
// True when we're parked on a selected mistake's anchor: the position BEFORE your move, with you
// to move. Free browsing (no mistake selected, or scrubbed away) sits AFTER the last move instead.
function atMistakeAnchor() {
  return currentMistake >= 0 && cur === anchorNode;
}

// The timeline node whose move is "under review" at the cursor: at a mistake anchor it's this
// node's own OUTGOING move (you're before it, playing it back); while browsing it's the move that
// just landed us here (cur - 1). Returns -1 at the very start (no move to show).
function reviewedMoveNode() {
  return atMistakeAnchor() ? cur : cur - 1;
}

function drawArrows() {
  if (puzzleMode) {
    ground.setAutoShapes(puzzleShapes);
    return;
  }
  const shapes = [];
  // The move you actually played, drawn only at a mistake anchor. There the board sits on the
  // position BEFORE your move (you're to move, so the green best-move arrow is for YOUR side), so
  // the played move is this node's OUTGOING move. Grey = neutral "here's what you did", shown
  // alongside the green best move so you can compare what you did vs. what was best.
  if (!exploring && !analyzing && atMistakeAnchor() && timeline[cur] && timeline[cur].move_uci) {
    shapes.push(arrowShape(timeline[cur].move_uci, "grey"));
  }
  if (bestArrowOn) for (const a of bestArrows) shapes.push(a);
  if (threatArrowOn) for (const a of threatArrows) shapes.push(a);
  for (const s of evalShapes) shapes.push(s);
  // autoShapes (not setShapes): app-managed annotations that survive piece press/drag and
  // only change when we redraw — so the played-move arrow stays until you actually move.
  ground.setAutoShapes(shapes);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Map top engine moves → arrows. The best move is a bold arrow; alternatives are
// clearly thinner (with proportionally smaller heads, since chessground scales the arrowhead
// with stroke width) so the recommendation stands out at a glance.
function movesToArrows(moves, brush = "green", boldWidth = 13) {
  if (!moves.length) return [];
  const best = moves[0].win_percent;
  const out = [];
  for (let i = 0; i < moves.length; i++) {
    const delta = best - moves[i].win_percent;
    if (i > 0 && delta > 12) break; // only surface genuinely good alternatives
    // best = bold; alternatives start much thinner (≤7) and taper with how much worse.
    const lineWidth = i === 0 ? boldWidth : Math.max(4, 7 - delta);
    out.push({
      orig: moves[i].uci.slice(0, 2),
      dest: moves[i].uci.slice(2, 4),
      brush,
      modifiers: { lineWidth },
    });
  }
  return out;
}

// Refresh the engine-driven arrows (best moves + threats) for the current position. Bumps
// searchGen so any in-flight search for a previous position cancels itself; each enabled
// arrow kind then fetches independently.
function refreshBestMoves() {
  searchGen += 1; // cancel any in-flight search
  bestArrows = [];
  threatArrows = [];
  drawArrows();
  const myGen = searchGen;
  const fen = chess.fen();
  if (bestArrowOn) deepenBestMoves(fen, myGen);
  if (threatArrowOn) fetchThreats(fen, myGen);
}

// Run an escalating-depth best-move search; cancels itself on any position change (searchGen)
// and stops after SEARCH_MAX_MS.
async function deepenBestMoves(fen, myGen) {
  await sleep(SEARCH_DEBOUNCE_MS); // coalesce rapid arrow-key scrubbing
  if (myGen !== searchGen) return;
  const t0 = performance.now();
  for (const depth of SEARCH_DEPTHS) {
    if (myGen !== searchGen) return;
    let res;
    try {
      res = await fetch("/api/best-moves", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fen, depth, multipv: 3 }),
      }).then((r) => r.json());
    } catch (_) {
      return;
    }
    if (myGen !== searchGen) return; // superseded while the engine was thinking
    if (res && res.moves && res.moves.length) {
      bestArrows = movesToArrows(res.moves);
      drawArrows();
    }
    if (performance.now() - t0 > SEARCH_MAX_MS) break; // time cap
  }
}

// One fixed-depth null-move probe: what does the side that just moved threaten to play next?
// Drawn as yellow arrows, slightly thinner than the green best-move arrows.
async function fetchThreats(fen, myGen) {
  await sleep(SEARCH_DEBOUNCE_MS);
  if (myGen !== searchGen) return;
  let res;
  try {
    res = await fetch("/api/threats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen, depth: THREAT_DEPTH, multipv: 3 }),
    }).then((r) => r.json());
  } catch (_) {
    return;
  }
  if (myGen !== searchGen) return;
  if (res && res.moves) {
    threatArrows = movesToArrows(res.moves, "yellow", 11);
    drawArrows();
  }
}

// The eval bar matches board orientation: the side at the BOTTOM of the board fills from
// the bottom. White-at-bottom (reviewing white) → white fills up; black-at-bottom → black.
function applyEvalBarTheme() {
  const light = "#f0f0f0";
  const dark = "#2b2a27";
  const fill = $("evalbar-fill");
  const bar = $("evalbar");
  if (orient === "white") {
    fill.style.background = light;
    bar.style.background = dark;
  } else {
    fill.style.background = dark;
    bar.style.background = light;
  }
}
function setEvalBar(winWhite) {
  const w = winWhite == null ? 50 : winWhite; // phase-1 (no eval yet) -> neutral
  const bottomShare = orient === "white" ? w : 100 - w;
  $("evalbar-fill").style.height = `${clamp(bottomShare, 0, 100)}%`;
}

// --- verdict / status ----------------------------------------------------
function renderVerdict(payload) {
  if (!payload) return void ($("verdict").innerHTML = "");
  if (payload.error) return void ($("verdict").innerHTML = `<span class="line">${payload.error}</span>`);
  const m = payload.move;
  const refute = m.refutation_line_san.slice(0, 6).join(" ");
  const better = m.is_engine_best ? "Engine's top choice." : `Best was <b>${m.better_move_san}</b>.`;
  // "best" classification = within BEST_EPS of the top move. If it's NOT literally the engine's
  // top choice, show it as "good" so the badge doesn't contradict the "Best was …" text.
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  $("verdict").innerHTML =
    `<span class="tag ${label}">${label}</span>` +
    `<b>${m.move_san}</b> — win ${m.win_before}% → ${m.win_after}% ` +
    `(swing ${m.win_swing}, eval ${m.eval_after}). ${better}` +
    (refute ? `<div class="line">Reply: ${refute}</div>` : "");
}

function nodeLabel(i) {
  const n = timeline[i];
  if (!n || i === 0) return "the start";
  const prev = timeline[i - 1];
  return `${prev.move_number}${prev.color === "white" ? "." : "…"} ${prev.move_san}`;
}

// Compact verdict for the move just tried in explore mode, shown inline in the status banner.
function exploreVerdictHtml() {
  if (exploreVerdict === "pending") return ` <span class="line">evaluating…</span>`;
  if (!exploreVerdict) return "";
  if (exploreVerdict.error) return ` <span class="line">couldn't evaluate that move</span>`;
  const m = exploreVerdict;
  // Mirror renderVerdict: a "best" that isn't literally the engine's #1 reads as "good".
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  return (
    ` <span class="tag ${label}">${label}</span>` +
    `<b>${escapeHtml(m.move_san)}</b> — win ${m.win_before}% → ${m.win_after}%` +
    (m.is_engine_best ? "" : ` · best was <b>${escapeHtml(m.better_move_san || "")}</b>`)
  );
}

function updateStatus() {
  const el = $("status");
  if (exploring) {
    el.className = "status away";
    el.innerHTML =
      `🔍 Exploring a variation.${exploreVerdictHtml()} ` +
      `<button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else if (cur !== anchorNode) {
    el.className = "status away";
    el.innerHTML = `Viewing ${nodeLabel(cur)} — not the review move. <button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else {
    el.className = "status";
    // Grade the move under review (the mistake's own move at an anchor, else the last move played).
    const mv = reviewedMoveNode();
    const g = mv >= 0 && timeline[mv] ? classGlyph(timeline[mv].classification) : "";
    el.innerHTML = g + escapeHtml(currentPrompt || nodeLabel(cur));
  }
}

// --- navigation ----------------------------------------------------------
function gotoNode(n) {
  exploring = false;
  cur = clamp(n, 0, timeline.length - 1);
  evalShapes = [];
  // chat context: the "move in question" is the reviewed move for this cursor (the mistake's own
  // move at an anchor, else the move that just landed us here). chatFen is where it was played from.
  const mv = reviewedMoveNode();
  if (mv >= 0 && timeline[mv] && timeline[mv].move_san) {
    chatFen = timeline[mv].fen;
    chatMove = timeline[mv].move_san;
  } else {
    chatFen = timeline[cur] ? timeline[cur].fen : null;
    chatMove = null;
  }
  chess.load(timeline[cur].fen);
  renderBoard();
  setEvalBar(timeline[cur].win_white);
  renderVerdict(null);
  updateStatus();
  updateNav();
  renderGraph();
  highlightCurrentMove();
  refreshBestMoves();
}

function returnToReview() {
  gotoNode(anchorNode);
}

// Flip the board (hotkey `f`). The eval bar + win graph follow `orient`, so flip them too.
function flipBoard() {
  orient = orient === "white" ? "black" : "white";
  applyEvalBarTheme();
  renderBoard();
  setEvalBar(timeline[cur] ? timeline[cur].win_white : 50);
  renderGraph();
}

// Toggle the "Show best move" arrows from the keyboard (hotkey `l`), keeping the checkbox in sync.
function toggleBestArrows() {
  const box = $("best-toggle");
  box.checked = !box.checked;
  bestArrowOn = box.checked;
  refreshBestMoves();
}

// Toggle the yellow "Show threats" arrows from the keyboard (hotkey `t`), keeping the checkbox in sync.
function toggleThreatArrows() {
  const box = $("threat-toggle");
  box.checked = !box.checked;
  threatArrowOn = box.checked;
  refreshBestMoves();
}

function stepBack() {
  if (exploring) undoOne();
  else if (cur > 0) gotoNode(cur - 1);
}
function stepForward() {
  if (!exploring && cur < timeline.length - 1) gotoNode(cur + 1);
}

function undoOne() {
  chess.undo();
  if (samePosition(chess.fen(), timeline[exploreBaseNode].fen)) {
    gotoNode(exploreBaseNode); // rejoined the game line
    return;
  }
  chatFen = chess.fen(); // backed up mid-line: ask about the position, no single move
  chatMove = null;
  exploreVerdict = null; // no specific move under judgement at the backed-up position
  renderBoard();
  renderVerdict(null);
  updateStatus();
  renderGraph();
  syncExplore(); // refresh the eval bar for the new explored position
  refreshBestMoves(); // and the best-move arrows
}

async function syncExplore() {
  try {
    const info = await fetch("/api/best-move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: chess.fen() }),
    }).then((r) => r.json());
    setEvalBar(info.side_to_move === "white" ? info.win_percent : 100 - info.win_percent);
  } catch (_) {}
}

// --- user moves ----------------------------------------------------------
async function onUserMove(orig, dest) {
  if (stormRunning) return onStormMove(orig, dest);
  if (puzzleMode) return onPuzzleMove(orig, dest);
  const moverColor = turnColor();
  const fenBefore = chess.fen();
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");

  // Following the actual game move while on the timeline → just advance.
  if (!exploring && timeline[cur] && timeline[cur].move_uci === uci) {
    const fm = chess.move({ from: orig, to: dest, promotion: promo });
    chatFen = fenBefore;
    chatMove = (fm && fm.san) || null;
    cur += 1;
    renderBoard();
    setEvalBar(timeline[cur].win_white);
    renderVerdict(null);
    updateStatus();
    updateNav();
    renderGraph();
    highlightCurrentMove();
    refreshBestMoves();
    return;
  }

  // Otherwise we're exploring a variation.
  if (!exploring) {
    exploring = true;
    exploreBaseNode = cur;
  }
  const moveObj = chess.move({ from: orig, to: dest, promotion: promo });
  chatFen = fenBefore; // position before the move in question (consistent in explore mode)
  chatMove = (moveObj && moveObj.san) || null;
  evalShapes = [];
  exploreVerdict = "pending"; // banner shows "evaluating…" until the engine replies
  renderBoard();
  updateStatus();
  renderGraph();
  refreshBestMoves(); // live best-move arrows for the new position

  $("verdict").innerHTML = `<span class="line">Evaluating…</span>`;
  let res;
  try {
    const r = await fetch("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fen: fenBefore, move: uci }),
    });
    if (!r.ok) throw new Error(`engine error (${r.status})`);
    res = await r.json();
  } catch (err) {
    // Never leave the verdict stuck on "Evaluating…": surface the failure so the user
    // can retry instead of thinking the board froze.
    exploreVerdict = { error: true };
    updateStatus();
    renderVerdict({ error: "Couldn't evaluate that move — the engine may be busy or restarting. Try again." });
    return;
  }
  exploreVerdict = res.move || (res.error ? { error: true } : null);
  updateStatus(); // surface good/mistake/blunder in the always-visible banner under the board
  renderVerdict(res);
  if (res.move) {
    setEvalBar(moverColor === "white" ? res.move.win_after : 100 - res.move.win_after);
    evalShapes = res.shapes || []; // red refutation arrow drawn on the resulting position
    drawArrows();
  }
}

// --- win graph -----------------------------------------------------------
const GW = 1000;
const GH = 100;

function renderGraph() {
  const svg = $("graph");
  const n = timeline.length;
  if (n < 2) {
    svg.innerHTML = "";
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${GW} ${GH}`);
  const x = (i) => (i / (n - 1)) * GW;
  const y = (w) => GH - (w / 100) * GH;
  // Plot from the reviewed player's perspective, matching the eval bar: the filled area
  // grows from the bottom as YOUR side does better, so for black it reads black-on-bottom.
  // During phase-1 (analysing) nodes have no win_white yet -> treat as 50 (flat baseline).
  const hasEval = timeline.some((nd) => nd.win_white != null);
  const val = (nd) => {
    const w = nd.win_white == null ? 50 : nd.win_white;
    return orient === "white" ? w : 100 - w;
  };

  // Two-tone fill split at the eval curve, mirroring the eval bar: each side keeps its own
  // colour (light = White, dark = Black) and the reviewed player's side sits on the bottom.
  const pts = timeline.map((nd, i) => `${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`).join(" L");
  const belowArea = `M0,${GH} L${pts} L${GW},${GH} Z`; // bottom = the player's side
  const aboveArea = `M0,0 L${pts} L${GW},0 Z`; // top = the opponent's side
  const LIGHT = "rgba(236,234,228,0.22)"; // White
  const DARK = "rgba(0,0,0,0.45)"; // Black
  const bottomFill = orient === "white" ? LIGHT : DARK;
  const topFill = orient === "white" ? DARK : LIGHT;

  const line = timeline
    .map((nd, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`)
    .join(" ");

  const flagged = timeline.filter((nd) => nd.mistake_index != null);
  const mistakeDots = flagged
    .map(
      (nd) =>
        `<circle cx="${x(nd.node).toFixed(1)}" cy="${y(val(nd)).toFixed(1)}" r="3" ` +
        `fill="${classColor(nd.classification)}" vector-effect="non-scaling-stroke"/>`
    )
    .join("");

  // Transparent, full-height click targets over each mistake (a ply-wide band) so clicking a dot
  // reliably opens it via real SVG hit-testing — no fragile pixel math — while clicks elsewhere on
  // the graph hit nothing. `data-mi` is the index into the mistakes array (see onGraphClick).
  const half = GW / (n - 1) / 2;
  const mistakeHits = flagged
    .map(
      (nd) =>
        `<rect class="mdot-hit" data-mi="${nd.mistake_index}" pointer-events="all" ` +
        `x="${(x(nd.node) - half).toFixed(1)}" y="0" width="${(half * 2).toFixed(1)}" ` +
        `height="${GH}" fill="transparent"/>`
    )
    .join("");

  const cx = x(cur).toFixed(1);
  const cy = y(val(timeline[cur])).toFixed(1);
  const marker =
    `<line x1="${cx}" y1="0" x2="${cx}" y2="${GH}" stroke="#629924" stroke-width="1" vector-effect="non-scaling-stroke"/>` +
    `<circle cx="${cx}" cy="${cy}" r="4" fill="#629924" vector-effect="non-scaling-stroke"/>`;

  const analyzingNote = hasEval
    ? ""
    : `<text x="${GW / 2}" y="${GH / 2 - 4}" fill="#9c9890" font-size="9" text-anchor="middle" ` +
      `vector-effect="non-scaling-stroke">analyzing… moves are navigable now</text>`;

  svg.innerHTML =
    `<rect x="0" y="0" width="${GW}" height="${GH}" fill="#14130f"/>` +
    `<path d="${aboveArea}" fill="${topFill}"/>` +
    `<path d="${belowArea}" fill="${bottomFill}"/>` +
    `<line x1="0" y1="${GH / 2}" x2="${GW}" y2="${GH / 2}" stroke="#4a4843" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` +
    (hasEval
      ? `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
      : "") +
    mistakeDots +
    marker +
    analyzingNote +
    mistakeHits; // last = on top, so the transparent bands reliably catch clicks
}

function classColor(cls) {
  return (
    { inaccuracy: "#e0a800", mistake: "#e08000", blunder: "#dd3333" }[cls] || "#629924"
  );
}

// A small graded badge for a move's classification (only the reviewed player's moves carry one).
// `good` and opponent moves (null) get no glyph so the notation stays readable.
const GLYPHS = { blunder: "??", mistake: "?", inaccuracy: "?!", best: "✓" };
function classGlyph(cls) {
  const g = GLYPHS[cls];
  return g ? `<span class="glyph ${cls}">${g}</span>` : "";
}

// Clicking a flagged-mistake dot opens that mistake (same as the mistakes tab, via selectMistake) —
// detected by real SVG hit-testing of the transparent bands drawn in renderGraph (robust to the
// stretched viewBox). Clicking anywhere else on the graph scrubs to that ply (plain gotoNode jump).
function onGraphClick(ev) {
  const target = ev.target && ev.target.closest && ev.target.closest("[data-mi]");
  if (target) {
    selectMistake(Number(target.getAttribute("data-mi")));
    return;
  }
  const n = timeline.length;
  if (n < 2) return;
  const rect = $("graph").getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  gotoNode(Math.round(frac * (n - 1))); // gotoNode clamps to a valid node
}

// --- mistakes list -------------------------------------------------------
function renderMistakeList() {
  const ol = $("mistakes");
  ol.innerHTML = "";
  if (analyzing && !mistakes.length) {
    const li = document.createElement("li");
    li.className = "ph";
    li.textContent = "Analyzing… mistakes will appear here when the engine finishes.";
    ol.appendChild(li);
    return;
  }
  mistakes.forEach((m, i) => {
    const li = document.createElement("li");
    li.dataset.index = i;
    const num = `${m.move_number}${m.color === "white" ? "." : "…"}`;
    li.innerHTML =
      `<span class="move"><span class="dot ${m.classification}"></span>` +
      `<span class="piece-glyph">${pieceGlyph(m.move_san)}</span>${num} ${m.move_san}</span>` +
      `<span class="muted">${m.classification} −${m.win_swing}</span>`;
    li.addEventListener("click", () => selectMistake(i));
    ol.appendChild(li);
  });
}

async function selectMistake(i) {
  const myGen = chatGen;
  const pos = await fetch(`/api/position/${i}`).then((r) => r.json());
  if (myGen !== chatGen) return; // a different game opened while we were fetching
  currentMistake = i;
  currentPrompt = pos.error ? "" : pos.prompt;
  // Land on the position BEFORE the mistake (you're on the move) so the engine's best-move arrow
  // and any move you try are for YOUR side; the grey arrow still shows the move you actually played.
  anchorNode = mistakes[i].node_index;
  [...$("mistakes").children].forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.index) === i)
  );
  gotoNode(anchorNode);
  $("comment").textContent = mistakes[i].comment || "";
}

// --- scoreboard (game report header) -------------------------------------
// Headline stats for the reviewed side, derived entirely from the /session payload: opening,
// per-side accuracy, and counts of blunders / mistakes / inaccuracies (the `mistakes` array is
// exactly the reviewed player's flagged moves). Clicking a count chip jumps to the first of that
// class. Rendered from applySession (phase-2), so it only ever shows real numbers.
function renderScoreboard(session) {
  const board = $("scoreboard");
  if (!board) return;
  const reviewed = session.player === "black" ? "black" : "white";
  const sideLabel = reviewed === "white" ? "White" : "Black";
  const myAcc = reviewed === "white" ? session.accuracy_white : session.accuracy_black;
  const oppAcc = reviewed === "white" ? session.accuracy_black : session.accuracy_white;
  const counts = { blunder: 0, mistake: 0, inaccuracy: 0 };
  (session.mistakes || []).forEach((m) => {
    if (counts[m.classification] != null) counts[m.classification] += 1;
  });
  board.innerHTML =
    `<div class="sb-opening" title="Opening">${escapeHtml(session.opening || "—")}</div>` +
    `<div class="sb-acc">` +
    `<span class="sb-acc-main"><b>${myAcc}</b><span class="sb-acc-lbl">accuracy (${sideLabel})</span></span>` +
    `<span class="sb-acc-opp">opponent ${oppAcc}</span>` +
    `</div>` +
    `<div class="sb-counts">` +
    scoreboardChip("blunder", counts.blunder, "Blunders") +
    scoreboardChip("mistake", counts.mistake, "Mistakes") +
    scoreboardChip("inaccuracy", counts.inaccuracy, "Inaccuracies") +
    `</div>`;
  board.hidden = false;
  board.querySelectorAll(".chip[data-cls]").forEach((el) =>
    el.addEventListener("click", () => jumpToClass(el.dataset.cls))
  );
}

function scoreboardChip(cls, n, label) {
  return (
    `<button type="button" class="chip ${cls}" data-cls="${cls}" title="${label}">` +
    `<span class="chip-n">${n}</span> <span class="chip-lbl">${label}</span></button>`
  );
}

function jumpToClass(cls) {
  const i = mistakes.findIndex((m) => m.classification === cls);
  if (i >= 0) selectMistake(i);
}

// --- move list (clickable notation) --------------------------------------
// The full game as a compact, scrollable two-column notation panel. Built from the same `timeline`
// the graph/arrows use, so it works on the provisional (phase-1) timeline too — glyphs just fill in
// when engine analysis lands. Clicking a flagged move routes through selectMistake (surfacing its
// comment + anchor); any other move is a plain gotoNode jump.
function renderMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  ol.innerHTML = "";
  const plies = timeline.filter((nd) => nd.move_san); // skip the final (terminal) node
  if (!plies.length) return;
  const rows = new Map(); // move_number -> {w, b}
  for (const nd of plies) {
    if (!rows.has(nd.move_number)) rows.set(nd.move_number, { w: null, b: null });
    rows.get(nd.move_number)[nd.color === "white" ? "w" : "b"] = nd;
  }
  for (const [num, pair] of rows) {
    const li = document.createElement("li");
    li.className = "move-row";
    li.innerHTML = `<span class="moveno">${num}.</span>${plyCell(pair.w)}${plyCell(pair.b)}`;
    ol.appendChild(li);
  }
  ol.querySelectorAll(".ply[data-node]").forEach((el) =>
    el.addEventListener("click", () => onMoveClick(Number(el.dataset.node)))
  );
  highlightCurrentMove();
}

function plyCell(nd) {
  if (!nd) return `<span class="ply empty"></span>`;
  return `<span class="ply" data-node="${nd.node}">${classGlyph(nd.classification)}${nd.move_san}</span>`;
}

function onMoveClick(i) {
  const nd = timeline[i];
  if (nd && nd.mistake_index != null) selectMistake(nd.mistake_index);
  else gotoNode(i + 1); // show the position with the clicked move just completed
}

// Highlight the move at the current node and keep it scrolled into view (works in compact mode).
function highlightCurrentMove() {
  const ol = $("movelist");
  if (!ol) return;
  let active = null;
  ol.querySelectorAll(".ply[data-node]").forEach((el) => {
    // Highlight the move under review: the mistake's own move at an anchor, else the last move.
    const on = Number(el.dataset.node) === reviewedMoveNode();
    el.classList.toggle("active", on);
    if (on) active = el;
  });
  if (active) active.scrollIntoView({ block: "nearest" });
}

// Compact (a few rows, scrollable) <-> expanded (whole game). Default is compact.
function toggleMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  const expanded = ol.classList.toggle("expanded");
  ol.classList.toggle("compact", !expanded);
  $("movelist-expand").textContent = expanded ? "Collapse ▴" : "Show all ▾";
  highlightCurrentMove();
}

function updateNav() {
  $("back").disabled = !exploring && cur <= 0;
  $("fwd").disabled = exploring || cur >= timeline.length - 1;
  $("prev-mistake").disabled = currentMistake <= 0;
  $("next-mistake").disabled = currentMistake < 0 || currentMistake >= mistakes.length - 1;
}

// --- chat ("why?") -------------------------------------------------------
const escapeHtml = (s) =>
  s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// Themes that describe a puzzle's length/format/phase/outcome, not a tactical motif — mirrors
// `_STORM_NON_MOTIF_THEMES` in claude_bridge.py so a review row labels the real motif (fork/pin/…)
// rather than "middlegame". Falls back to the raw themes when nothing tactical is tagged.
const NON_MOTIF_THEMES = new Set([
  "oneMove", "short", "long", "veryLong", "master", "masterVsMaster", "superGM",
  "opening", "middlegame", "endgame",
  "rookEndgame", "bishopEndgame", "knightEndgame", "pawnEndgame", "queenEndgame", "queenRookEndgame",
  "crushing", "advantage", "equality", "mate",
]);
function motifThemes(themes) {
  const t = (themes || []).filter((x) => x && !NON_MOTIF_THEMES.has(x) && !/^mateIn\d/.test(x));
  return t.length ? t : (themes || []).filter((x) => x && !/^mateIn\d/.test(x));
}

// Minimal, safe markdown → HTML: escape first, then bold / italic / code / lists / paragraphs.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let inList = false;
  const inline = (s) =>
    s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  for (const raw of lines) {
    const line = raw.trim();
    const li = line.match(/^[-*]\s+(.*)/);
    if (li) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(li[1])}</li>`;
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      if (line) html += `<p>${inline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}

function addChatMsg(cls, text) {
  const d = document.createElement("div");
  d.className = `chat-msg ${cls}`;
  if (cls === "bot") d.innerHTML = renderMarkdown(text); // only the final answer is markdown
  else d.textContent = text;
  const box = $("chat-messages");
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

// Repopulate the chat panel from the server's in-memory transcript for the current game, so
// switching to another game and back shows the conversation you'd had. Best-effort: a failure
// just leaves the (already-cleared) panel empty.
async function restoreChat() {
  const myGen = chatGen;
  let hist;
  try {
    hist = await fetch("/api/chat-history").then((r) => r.json());
  } catch (_) {
    return;
  }
  if (myGen !== chatGen) return; // a newer game opened while we were fetching — don't clobber it
  const msgs = (hist && hist.messages) || [];
  $("chat-messages").innerHTML = "";
  for (const m of msgs) addChatMsg(m.role === "bot" ? "bot" : "user", m.text);
  chatSession = (hist && hist.session_id) || null;
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("chat-input");
  // Empty box → context-aware default (the placeholder becomes a one-click question).
  const typed = input.value.trim();
  const q =
    typed ||
    (chatMove
      ? `Why is ${chatMove} bad here?`
      : "What's the best move in this position, and why?");
  input.value = "";
  addChatMsg("user", q);
  $("chat-send").disabled = true;
  const pending = addChatMsg("bot pending", "Snowie is thinking… (a few seconds)");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        fen: chess.fen(), // the exact board on screen → "what should I do here?"
        last_move: chatMove, // the move in question → "why is this bad?"
        move_fen: chatFen, // the position that move was played from
        session_id: chatSession,
        use_profile: personalizeHistory, // personalize with cross-game history (Settings toggle)
      }),
    }).then((r) => r.json());
    pending.remove();
    if (res.error) {
      addChatMsg("bot err", res.error);
    } else {
      addChatMsg("bot", res.answer || "(no answer)");
      if (res.session_id) chatSession = res.session_id;
    }
  } catch (e) {
    pending.remove();
    addChatMsg("bot err", "Request failed: " + e);
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
}

// --- init ----------------------------------------------------------------
function applySession(session) {
  const sens = session.review_elo
    ? ` · sensitivity ~${Math.round(session.review_elo)} Elo`
    : "";
  // Names first (with an "open on Lichess/Chess.com" ↗ right after them), then the review details.
  // The names + link live in the always-visible board header so they're reachable even when the
  // Games panel is collapsed on narrow screens.
  currentGameUrl = session.game_url || null;
  setGameMeta(
    session.white,
    session.black,
    currentGameUrl,
    ` — ${session.result} · reviewing ${session.player} ` +
      `(acc W ${session.accuracy_white} / B ${session.accuracy_black}) · ${session.num_mistakes} mistakes${sens}`
  );
  mistakes = session.mistakes;
  // Remember the game (PGN + names) so "Review other side" can re-open it for the opponent.
  if (session.pgn) currentPgn = session.pgn;
  gameWhite = session.white || gameWhite;
  gameBlack = session.black || gameBlack;
  updateFlipReviewButton();
  renderMistakeList();
  renderScoreboard(session);
  renderCoach(session);
  // NB: prepareCoachAI() is intentionally NOT called here — it's run by the caller AFTER
  // applyTimeline(), so it sees the new game's timeline (applySession runs before applyTimeline).
}

// Free, engine-grounded templated blurb (rides on /api/session) — always shown when present.
// The templated quick summary + whether the user has manually expanded it while an AI summary is up.
let quickSummaryHasText = false;
let quickSummaryUserExpanded = false;
let coachAiReady = false;

function renderCoach(session) {
  const el = $("coach");
  if (!el) return;
  const text = (session && session.coach_summary) || "";
  el.textContent = text;
  quickSummaryHasText = !!text;
  quickSummaryUserExpanded = false;
  syncQuickSummary();
}

// Collapse the templated quick summary behind a toggle once the fuller AI (Snowie) summary is on
// screen — the AI one is the richer version, so the quick summary is redundant but kept one click
// away. With no AI summary the quick summary shows outright (no toggle).
function syncQuickSummary() {
  const el = $("coach");
  const toggle = $("coach-toggle");
  if (!el || !toggle) return;
  if (!quickSummaryHasText) {
    el.hidden = true;
    toggle.hidden = true;
    return;
  }
  if (!coachAiReady) {
    el.hidden = false;
    toggle.hidden = true;
    return;
  }
  // AI summary present: default to collapsed, expandable on demand.
  toggle.hidden = false;
  el.hidden = !quickSummaryUserExpanded;
  toggle.textContent = quickSummaryUserExpanded ? "▾ Hide quick summary" : "▸ Show quick summary";
}

// Claude-written summary (board column, bottom). The card shows its state; the button offers
// on-demand generation. Server caches per game, so re-requests don't spend Claude again.
function showCoachButton(show) {
  const btn = $("coach-ai-btn");
  if (btn) btn.hidden = !show;
}

function setCoachAI(state, text) {
  const el = $("coach-ai");
  if (!el) return;
  if (state === "hidden") {
    el.hidden = true;
    el.className = "coach-ai";
    el.textContent = "";
  } else if (state === "pending") {
    el.hidden = false;
    el.className = "coach-ai pending";
    el.textContent = "Snowie is writing up a full game summary…";
  } else if (state === "error") {
    el.hidden = false;
    el.className = "coach-ai err";
    el.textContent = text || "Couldn't generate the summary.";
  } else {
    el.hidden = false;
    el.className = "coach-ai";
    // Render bold / italics / lists / paragraphs (same safe markdown as the chat answers). The ⟳
    // button re-runs the summary on demand (spends Claude) — handy if it's a stale saved one.
    el.innerHTML =
      `<span class="coach-ai-tag">AI coach (Snowie)` +
      `<button id="coach-ai-refresh" class="coach-ai-refresh" type="button" ` +
      `title="Regenerate this summary (uses your Claude subscription)" aria-label="Regenerate summary">⟳</button>` +
      `</span>${renderMarkdown(text)}`;
    const rb = $("coach-ai-refresh");
    if (rb) rb.addEventListener("click", () => fetchCoachAI(true));
  }
  // Only a finished AI summary supersedes the templated quick one; while it's pending/errored the
  // quick summary stays visible so there's never a gap with no overview.
  coachAiReady = state === "ready";
  syncQuickSummary();
}

// Set up the AI-summary UI for the freshly-loaded game: show an already-saved summary outright,
// else auto-generate (if enabled), else just offer the button.
function prepareCoachAI(session) {
  coachAiToken++; // any earlier in-flight request is now stale
  setCoachAI("hidden");
  if (!timeline.length) { showCoachButton(false); return; }
  // Already generated for this game (this session, or restored from the cache on reopen) → show it
  // immediately, no button press and no second Claude call.
  if (session && session.coach_ai_text) {
    setCoachAI("ready", session.coach_ai_text);
    showCoachButton(false);
    return;
  }
  if (coachAiAuto) fetchCoachAI();
  else showCoachButton(true);
}

// Actually request the summary (button press, or the auto path). Always allowed — it only ever
// runs from an explicit user choice, so it spends Claude only when asked.
async function fetchCoachAI(force = false) {
  const tok = ++coachAiToken;
  showCoachButton(false);
  setCoachAI("pending");
  let res;
  try {
    res = await fetch("/api/coach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: !!force }),
    }).then((r) => r.json());
  } catch (_) {
    if (tok === coachAiToken) { setCoachAI("error", "Couldn't reach the summary service."); showCoachButton(true); }
    return;
  }
  if (tok !== coachAiToken) return; // a newer game superseded this request
  if (res && res.summary) {
    setCoachAI("ready", res.summary);
  } else {
    setCoachAI(res && res.error ? "error" : "hidden", res && res.error);
    showCoachButton(true); // let the user retry
  }
}

function applyTimeline(tl) {
  timeline = tl.nodes || [];
  player = tl.player || "white";
  orient = player; // orientation follows the reviewed side until the user flips (f)
  applyEvalBarTheme();
  renderMoveList();
}

async function loadAll() {
  // A fresh app session starts the Snowie chat clean. The transcript lives server-side in memory,
  // so a server that actually restarts is already empty — but if the same long-lived board is reused
  // across app launches (e.g. an MCP-hosted board that didn't exit on tab-close), the old conversation
  // would linger. A brand-new browser session (new launch / new tab) has no sessionStorage flag → wipe;
  // a refresh keeps the flag → preserves the chat (so refresh never loses state).
  try {
    if (!sessionStorage.getItem("chessAppSession")) {
      sessionStorage.setItem("chessAppSession", "1");
      await fetch("/api/chat-reset", { method: "POST" }).catch(() => {});
    }
  } catch (_) {}
  // Identity + app-mode come from the server (settings-backed), so one source of truth.
  try {
    const cfg = await fetch("/api/app-config").then((r) => r.json());
    appMode = !!cfg.app_mode;
    appUsername = (cfg.lichess_username || "").trim();
    chesscomUsername = (cfg.chesscom_username || "").trim();
    chesscomSync = cfg.chesscom_sync !== false; // default on
    chesscomSyncMax = Number(cfg.chesscom_sync_max) || 5;
    coachAiAuto = !!cfg.coach_ai_auto;
    personalizeHistory = cfg.personalize_history !== false; // default on
    puzzleAnimations = cfg.puzzle_animations !== false; // default on
    puzzleAutoAdvance = cfg.puzzle_auto_advance === true; // default off
  } catch (_) {}
  if (appUsername) $("lichess-user").placeholder = appUsername;
  if (chesscomUsername) $("chesscom-user").placeholder = chesscomUsername;
  if (appMode) startHeartbeat(); // so closing this tab quits the standalone app
  // Reveal the Analyze/Puzzles switch only when puzzle data is available. Awaited (not fire-and-
  // forget) so we know before touching the board whether to resume puzzle mode after a reload.
  let wantPuzzle = false;
  try {
    const cfg = await fetch("/api/puzzle/config").then((r) => r.json());
    if (cfg && cfg.enabled) {
      puzzleConfigCache = cfg;
      $("mode-switch").hidden = false;
      wantPuzzle = lsGet(PZ_MODE_KEY) === "1"; // persisted across full app restarts
    }
  } catch (_) {}
  checkSetup(); // surface a banner if Stockfish / the claude CLI is missing (fire-and-forget)
  checkUpdates(); // surface an "update available" banner in app mode (fire-and-forget)
  checkOnline(); // surface a banner if there's no internet (network features unavailable)

  // If the user opens a game while this startup load is still in flight (beginProvisional bumps
  // chatGen), abandon it — otherwise the stale session/timeline would clobber the new game's state.
  const myGen = chatGen;
  const session = await fetch("/api/session").then((r) => r.json());
  if (myGen !== chatGen) return;
  if (session.empty) {
    if (wantPuzzle) {
      await setPuzzleMode(true, { resume: true });
      return;
    }
    // App mode: try to auto-load the user's most recent Lichess game instead of an empty board.
    if (appMode && (await maybeAutoload())) return;
    $("game-meta").textContent =
      "Waiting to open a game — pick one from the Games panel, or run analyze_game.";
    return;
  }
  applySession(session);
  const tl = await fetch("/api/timeline").then((r) => r.json());
  if (myGen !== chatGen) return;
  applyTimeline(tl);
  // Skip the analysis board navigation when we're about to resume puzzle mode (it would clobber the
  // puzzle position); the game data is still loaded for when the user switches back to Analyze.
  if (!wantPuzzle) {
    if (mistakes.length) selectMistake(session.current_index ?? 0);
    else gotoNode(0);
  }
  restoreChat(); // restore any in-memory Q&A for the game already on the board (e.g. after a refresh)
  prepareCoachAI(session); // show a saved AI summary outright, else offer the button
  if (wantPuzzle) await setPuzzleMode(true, { resume: true });
}

// --- setup self-check banner ---------------------------------------------
// Hits /api/doctor (mirrors `python -m server.doctor`). A missing Stockfish is a blocker (red,
// always shown); a missing `claude` CLI only disables the AI chat + coach summary (amber, and
// dismissible — remembered so we don't nag). Fire-and-forget; failures are silent.
async function checkSetup() {
  const banner = $("setup-banner");
  if (!banner) return;
  let checks;
  try {
    checks = (await fetch("/api/doctor").then((r) => r.json())).checks || {};
  } catch (_) {
    return;
  }
  const sf = checks.stockfish || { ok: true };
  const cl = checks.claude || { ok: true };
  const arch = sf.arch || { suboptimal: false };

  if (!sf.ok) {
    showSetupBanner(
      banner,
      true,
      `<b>Stockfish engine not found.</b> ${escapeHtml(sf.hint || "Install Stockfish to analyze games.")}`,
      null
    );
  } else if (arch.suboptimal && localStorage.getItem("hideArchBanner") !== "1") {
    showArchFixBanner(banner);
  } else if (!cl.ok && localStorage.getItem("hideClaudeSetupBanner") !== "1") {
    showSetupBanner(
      banner,
      false,
      "<b>AI chat &amp; AI coach summary are off</b> — the <code>claude</code> CLI isn't installed. " +
        "Everything else works. Install it from " +
        '<a href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noopener">code.claude.com/docs</a>, ' +
        "then run <code>claude login</code>.",
      () => localStorage.setItem("hideClaudeSetupBanner", "1")
    );
  } else {
    banner.hidden = true;
  }
}

// Apple Silicon running the Intel Stockfish under Rosetta 2 (works, but slower for a search-heavy
// engine — the symptom of the old first-run install bug). Offer a one-click swap to the native
// arm64 build: POST /api/fix-stockfish-arch downloads it, pins it, and restarts the engine. Amber,
// dismissible (remembered so we don't nag).
function showArchFixBanner(banner) {
  banner.classList.remove("err");
  const dismiss = () => {
    banner.hidden = true;
    localStorage.setItem("hideArchBanner", "1");
  };
  banner.innerHTML =
    '<span class="sb-msg"><b>Stockfish is the Intel build running under Rosetta&nbsp;2.</b> ' +
    "It works, but the native Apple&nbsp;Silicon (arm64) engine is noticeably faster. " +
    '<button class="sb-fix" type="button">Download arm64 build</button></span>' +
    '<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>';
  banner.querySelector(".sb-x").addEventListener("click", dismiss);
  const fix = banner.querySelector(".sb-fix");
  fix.addEventListener("click", async () => {
    fix.disabled = true;
    fix.textContent = "Downloading…";
    let res;
    try {
      res = await fetch("/api/fix-stockfish-arch", { method: "POST" }).then((r) => r.json());
    } catch (_) {
      res = null;
    }
    if (res && res.ok) {
      banner.classList.remove("err");
      banner.innerHTML =
        '<span class="sb-msg"><b>Now using the native arm64 Stockfish.</b> Analyses will run faster.</span>' +
        '<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>';
      banner.querySelector(".sb-x").addEventListener("click", () => (banner.hidden = true));
    } else {
      fix.disabled = false;
      fix.textContent = "Retry";
      const msg = banner.querySelector(".sb-msg");
      if (msg && !msg.querySelector(".sb-err")) {
        const err = document.createElement("span");
        err.className = "sb-err";
        err.textContent = " " + ((res && res.error) || "Download failed — check your internet connection.");
        msg.appendChild(err);
      }
    }
  });
  banner.hidden = false;
}

function showSetupBanner(banner, isErr, msgHtml, onDismiss) {
  banner.classList.toggle("err", isErr);
  banner.innerHTML =
    `<span class="sb-msg">${msgHtml}</span>` +
    `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    if (onDismiss) onDismiss();
  });
  banner.hidden = false;
}

// --- offline notice ------------------------------------------------------
// Hits /api/connectivity (cached server-side reachability probe). When there's no internet, the
// network-only features won't work: Lichess game fetch + endgame tablebase always, and the Claude-
// backed AI chat / coach summary too — UNLESS a local LLM is configured (then AI stays available
// offline). Amber, informational, dismissible (remembered for the session so we don't nag).
async function checkOnline() {
  const banner = $("offline-banner");
  if (!banner) return;
  if (sessionStorage.getItem("hideOfflineBanner") === "1") return;
  let info;
  try {
    info = await fetch("/api/connectivity").then((r) => r.json());
  } catch (_) {
    return; // can't even reach our own server — leave it to the page-load failure to be visible
  }
  if (!info || info.online !== false) return; // online (or unknown) — nothing to warn about

  let msg =
    "<b>You're offline.</b> Local analysis (Stockfish) works as normal, but " +
    "<b>Lichess game fetch</b> and the <b>endgame tablebase</b> need internet and won't be available.";
  if (!info.local_llm) {
    // No local LLM, so the AI chat / coach summary go through Claude over the network.
    msg +=
      " The <b>AI chat &amp; coach summary</b> also won't work — they need internet (or a " +
      "local LLM, which you can set up in ⚙ Settings).";
  }
  msg += " Paste or upload a PGN to review a game.";
  showSetupBanner(banner, false, msg, () =>
    sessionStorage.setItem("hideOfflineBanner", "1")
  );
}

// --- update-available banner (app mode only) -----------------------------
// Hits /api/update-check (throttled GitHub release lookup). Non-blocking, dismissible notice when a
// newer release is out: info-blue for minor/patch, red for a major bump. Self-updatable installs
// (git/zip) get a one-click "Update now" (staged, applied by the launcher on the next reopen); the
// read-only .app gets a download link. Dismissal is remembered per version, so a newer release
// re-notifies. Fire-and-forget; failures are silent.
async function checkUpdates() {
  if (!appMode) return; // only nag end-user app launches, never MCP/dev sessions
  const banner = $("update-banner");
  if (!banner) return;
  let info;
  try {
    info = await fetch("/api/update-check").then((r) => r.json());
  } catch (_) {
    return;
  }
  if (!info || !info.update_available || !info.latest) return;
  if (localStorage.getItem("hideUpdateBanner") === info.latest) return; // dismissed this version

  const major = info.severity === "major";
  banner.classList.remove("guide");
  banner.classList.toggle("err", major); // red for major, else the info-blue .update
  banner.classList.toggle("update", !major);
  const v = escapeHtml(info.latest);
  const lead = major ? `<b>Major update v${v} available.</b>` : `<b>Update available — v${v}.</b>`;
  // git/zip self-update in place; the read-only .app needs a guided manual download.
  const how = info.can_self_update
    ? "Click Update now, then reopen the app to finish. Your games &amp; settings are kept."
    : "A new version is ready.";
  const action = info.can_self_update
    ? `<button class="sb-btn" type="button" id="update-now">Update now</button>`
    : `<button class="sb-btn" type="button" id="update-guide">How to update</button>`;

  banner.innerHTML =
    `<span class="sb-msg">${lead} ${how}</span>` +
    action +
    dismissX();
  wireDismiss(banner, info);
  const now = banner.querySelector("#update-now");
  if (now) now.addEventListener("click", () => applyUpdate(now));
  const guide = banner.querySelector("#update-guide");
  if (guide) guide.addEventListener("click", () => showAppUpdateGuide(banner, info));
  banner.hidden = false;
}

function dismissX() {
  return `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
}
function wireDismiss(banner, info) {
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    localStorage.setItem("hideUpdateBanner", info.latest); // remember per version
  });
}

// The .app is read-only at runtime, so it can't self-update — expand the banner into step-by-step
// install instructions (incl. the one-time unsigned-app "Open" step) and reassure that data is kept.
function showAppUpdateGuide(banner, info) {
  const url = escapeHtml(info.release_url || "#");
  banner.classList.add("guide"); // full-width stacked layout
  banner.innerHTML =
    `<span class="sb-msg"><b>Update to v${escapeHtml(info.latest)}</b>` +
    `<ol class="sb-steps">` +
    `<li><a href="${url}" target="_blank" rel="noopener">Download the latest version</a> from the Releases page (the <code>…-macos.zip</code>).</li>` +
    `<li>Double-click the downloaded <code>.zip</code> to unzip it.</li>` +
    `<li>Drag <b>Tintin's AI Chess Analysis.app</b> into your <b>Applications</b> folder, replacing the old one. (Any location works — it doesn't have to be Applications.)</li>` +
    `<li>First open: right-click the app → <b>Open</b> → <b>Open</b> (macOS asks once because the app isn't Apple-signed).</li>` +
    `<li>That's it — your games, analysis, and settings carry over automatically.</li>` +
    `</ol></span>` +
    dismissX();
  wireDismiss(banner, info);
}

// Stage a one-click update: POST /api/apply-update writes a sentinel the launcher applies on the
// next start. We can't reliably relaunch from a browser tab, so we just tell the user to reopen.
async function applyUpdate(btn) {
  btn.disabled = true;
  btn.textContent = "Staging…";
  const msg = $("update-banner").querySelector(".sb-msg");
  let res = {};
  try {
    res = await fetch("/api/apply-update", { method: "POST" }).then((r) => r.json());
  } catch (_) {}
  if (res && res.ok) {
    if (msg) msg.innerHTML = "<b>Update staged.</b> Quit and reopen the app to finish updating.";
    btn.textContent = "Quit now";
    btn.disabled = false;
    btn.onclick = () => {
      try {
        navigator.sendBeacon("/api/closing"); // app-liveness then shuts the server down
      } catch (_) {}
      window.close(); // works if the tab was script-opened; otherwise the message guides the user
    };
  } else {
    if (msg)
      msg.innerHTML =
        "<b>Couldn't stage the update.</b> " + escapeHtml((res && res.error) || "Try again later.");
    btn.textContent = "Update now";
    btn.disabled = false;
  }
}

// --- app mode: auto-open the latest Lichess game -------------------------
// App mode: let the server know when this tab is really gone, so it (and its terminal window) can
// quit. The reliable signal is `pagehide` → a close beacon; a slow heartbeat is just a backstop for
// the rare case pagehide never fires. We deliberately do NOT treat "lost focus / backgrounded" as
// closed — background tabs throttle timers, so a short heartbeat would false-quit during use.
let heartbeatTimer = null;
function startHeartbeat() {
  if (heartbeatTimer) return;
  const ping = () => fetch("/api/ping", { method: "POST", keepalive: true }).catch(() => {});
  ping();
  heartbeatTimer = setInterval(ping, 15000); // backstop only; server tolerates minutes of silence
  // Fires on tab close, navigation, and refresh. sendBeacon delivers even as the page unloads.
  window.addEventListener("pagehide", () => {
    try {
      navigator.sendBeacon("/api/closing");
    } catch (_) {}
  });
}

// App-mode empty board: first try the chess.com auto-sync (new games found -> the board shows the
// first of them while the rest analyze); else auto-load the configured user's latest game
// (Lichess, then chess.com); otherwise prompt for a username.
async function maybeAutoload() {
  if (chesscomSync && (await syncChesscom(true))) return true;
  if (appUsername) await autoOpenLatest(appUsername);
  else if (chesscomUsername) await autoOpenLatestChesscom(chesscomUsername);
  else showFirstRun("");
  return true;
}

// Auto-sync: ask the server to fetch the configured chess.com user's newest games and analyze the
// Record a manual Chess.com refresh (Load or Sync). When the user triggers it 3+ times inside a
// 20s window — the tell-tale of waiting on a just-finished game that chess.com hasn't published
// yet — reveal the "upload the PGN instead" hint.
function noteChesscomRefresh() {
  const now = Date.now();
  chesscomRefreshTimes.push(now);
  chesscomRefreshTimes = chesscomRefreshTimes.filter((t) => now - t <= CHESSCOM_REFRESH_WINDOW_MS);
  if (chesscomRefreshTimes.length >= CHESSCOM_REFRESH_TRIGGER) {
    const hint = $("chesscom-hint");
    if (hint) hint.hidden = false;
  }
}

// Hide the rapid-refresh hint and reset its counter (on tab switch, or once new games arrive).
function clearChesscomHint() {
  chesscomRefreshTimes = [];
  const hint = $("chesscom-hint");
  if (hint) hint.hidden = true;
}

// ones history hasn't seen. Returns true when a sync batch was started (the board shows its first
// game). `quiet` suppresses the "nothing new" message (used on app launch).
async function syncChesscom(quiet) {
  if (!chesscomUsername) return false;
  if (!quiet) $("history-status").textContent = "Syncing chess.com games…";
  let res;
  try {
    res = await fetch("/api/sync/chesscom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).then((r) => r.json());
  } catch (_) {
    if (!quiet) $("history-status").textContent = "Could not reach Chess.com.";
    return false;
  }
  if (!res || res.error || !res.new_games) {
    if (!quiet)
      $("history-status").textContent =
        res && res.error ? res.error : "chess.com is up to date — no new games.";
    return false;
  }
  const n = res.new_games;
  clearChesscomHint(); // new games arrived — the "chess.com is slow" advice no longer applies
  batchInfo = { total: n, self_handle: res.self_handle, lastDone: -1 };
  $("history-status").textContent =
    `Syncing ${n} new chess.com game${n === 1 ? "" : "s"} → they'll appear in My games.`;
  beginProvisional(
    res.first_pgn,
    res.first_side,
    `Syncing ${n} new chess.com game${n === 1 ? "" : "s"}… you can step through this one now.`
  );
  startPolling();
  return true;
}

// Open the configured chess.com user's most recent game (autoload for chess.com-only users).
async function autoOpenLatestChesscom(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent chess.com game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await fetch("/api/chesscom/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("game-meta").textContent = "Could not reach Chess.com — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No chess.com games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  openGame(g.pgn, sideForUser(g, username));
}

// Persist both handles server-side (Lichess + chess.com) in one write and reflect them locally.
// Used by the first-run prompt, which offers both fields at once.
async function saveIdentity(lichess, chesscom) {
  appUsername = (lichess || "").trim();
  chesscomUsername = (chesscom || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: appUsername, chesscom_username: chesscomUsername }),
    });
  } catch (_) {}
}

// Persist the configured username server-side (unified identity) and reflect it locally.
async function saveUsername(username) {
  appUsername = (username || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: appUsername }),
    });
  } catch (_) {}
}

// Infer which side `who` played in a Lichess game (same rule as renderHistory's lichess branch).
function sideForUser(game, who) {
  const w = (game.white || "").toLowerCase();
  const b = (game.black || "").toLowerCase();
  const me = (who || "").toLowerCase();
  return me && w === me ? "white" : me && b === me ? "black" : "auto";
}

async function autoOpenLatest(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent Lichess game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await fetch("/api/lichess/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("game-meta").textContent = "Could not reach Lichess — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No Lichess games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  openGame(g.pgn, sideForUser(g, username));
}

function showFirstRun(defaultUsername) {
  const overlay = $("firstrun");
  if (!overlay) return;
  $("firstrun-user").value = defaultUsername || "";
  $("firstrun-chesscom-user").value = chesscomUsername || "";
  overlay.hidden = false;
  $("firstrun-user").focus();
}

// --- progressive open: navigate the PGN immediately, swap in engine analysis when ready ----
// Build a provisional timeline from a PGN entirely client-side (chess.js), so the board is
// steppable the instant a game is opened — no engine, no win%/classifications yet (those arrive
// in phase 2). Shape matches the server timeline's navigation fields. Throws on an unparseable PGN.
function buildProvisionalTimeline(pgn) {
  const c = new Chess();
  c.loadPgn(pgn);
  const moves = c.history({ verbose: true });
  if (!moves.length) throw new Error("no moves");
  const nodes = moves.map((mv, i) => ({
    node: i,
    fen: mv.before,
    win_white: null,
    color: mv.color === "w" ? "white" : "black",
    move_number: Math.floor(i / 2) + 1,
    ply: i + 1,
    move_san: mv.san,
    move_uci: mv.from + mv.to + (mv.promotion || ""),
    best_uci: null,
    best_san: null,
    classification: null,
    mistake_index: null,
  }));
  const last = moves[moves.length - 1];
  nodes.push({
    node: moves.length,
    fen: last.after,
    win_white: null,
    color: c.turn() === "w" ? "white" : "black",
    move_number: Math.floor(moves.length / 2) + 1,
  });
  return nodes;
}

function setAnalyzingUI(on) {
  $("best-toggle").disabled = on; // engine pool is busy with the sweep
  $("threat-toggle").disabled = on;
  if (on) {
    bestArrowOn = false;
    $("best-toggle").checked = false;
    threatArrowOn = false;
    $("threat-toggle").checked = false;
  }
  const box = $("analysis-progress");
  if (box) box.hidden = !on;
  if (on) renderProgress(null); // start indeterminate until the first status arrives
}

// Render the sweep progress bar over the win graph. `st` is the /analysis-status payload (or null
// for the initial indeterminate state). We show a measured fill + ETA once the job reports a stable
// per-ply rate (eta_seconds), and an indeterminate shimmer before that (engine pool warming up).
function renderProgress(st) {
  const fill = $("analysis-progress-fill");
  const label = $("analysis-progress-label");
  if (!fill || !label) return;
  // Multi-game batch: prefix the per-game bar with "Game k of N".
  const multi = st && (st.total_games || 1) > 1;
  const prefix = multi ? `Game ${st.current_game} of ${st.total_games} · ` : "";
  const done = st && st.total ? st.done : 0;
  const total = st && st.total ? st.total : 0;
  const eta = st ? st.eta_seconds : null;
  if (!total || eta == null) {
    fill.classList.add("indeterminate");
    label.textContent = `${prefix}Analyzing… estimating time`;
    return;
  }
  fill.classList.remove("indeterminate");
  const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
  fill.style.width = pct + "%";
  const secs = Math.max(1, Math.round(eta));
  label.textContent = `${prefix}Analyzing… ${pct}% · ~${secs}s left`;
}

// Set up the board to navigate a PGN immediately (provisional timeline, no engine yet) and reset
// per-game UI state. Shared by single-game opens and the first game of a batch upload.
function beginProvisional(pgn, side, metaText) {
  analyzing = true;
  currentMistake = -1;
  anchorNode = 0;
  mistakes = [];
  currentPgn = pgn; // enable "Review other side" immediately; names fill in at phase-2
  gameWhite = "";
  gameBlack = "";
  currentPrompt = "";
  $("comment").textContent = "";
  $("verdict").innerHTML = "";
  setAnalyzingUI(true);
  renderMistakeList();
  $("scoreboard").hidden = true; // stale until the new game's stats land in phase-2
  quickSummaryHasText = false;
  quickSummaryUserExpanded = false;
  coachAiReady = false;
  $("coach").hidden = true;
  $("coach-toggle").hidden = true;
  coachAiToken++; // invalidate any in-flight AI summary from the previous game
  setCoachAI("hidden");
  showCoachButton(false);
  // Clear the chat panel for the new game; its prior in-memory transcript (if any) is restored
  // in onAnalysisReady once the session is loaded. chatSession is reset so we don't thread the
  // previous game's conversation into this one. Bumping chatGen invalidates any in-flight
  // restoreChat from the game we're leaving, so a late response can't repopulate this panel.
  chatGen++;
  $("chat-messages").innerHTML = "";
  chatSession = null;

  let prov = null;
  try {
    prov = buildProvisionalTimeline(pgn);
  } catch (_) {
    prov = null; // unparseable PGN -> fall back to a blocking spinner (phase-2 still works)
  }
  // Pull names + the source-site URL straight from the PGN headers so the header line (names + ↗
  // link) is populated during analysis too, not only once phase-2 lands.
  const hdr = pgnHeaders(pgn);
  currentGameUrl = hdr.url || null;
  if (prov && prov.length >= 2) {
    timeline = prov;
    player = side === "white" || side === "black" ? side : "white";
    orient = player;
    applyEvalBarTheme();
    renderMoveList(); // provisional notation: navigable immediately, glyphs fill in later
    gotoNode(0);
    const tail = metaText || "analyzing… you can step through the moves now (← / →)";
    setGameMeta(hdr.white, hdr.black, currentGameUrl, ` — ${tail}`);
  } else {
    timeline = [];
    renderMoveList();
    $("game-meta").textContent = "Analyzing…";
  }
  updateFlipReviewButton(); // side is known now; names fill in at phase-2
}

// Show/label the "Review other side" button for the loaded game (hidden until a game is open).
// It re-analyses the SAME game from the opponent's perspective — a separate history record
// (keyed by reviewed_side, so it never collides with the side already analysed).
function updateFlipReviewButton() {
  const btn = $("flip-review");
  if (!btn) return;
  if (!currentPgn || !timeline.length) {
    btn.hidden = true;
    return;
  }
  const otherColor = player === "white" ? "black" : "white";
  const name = otherColor === "white" ? gameWhite : gameBlack;
  const label = name && name !== "?" ? name : otherColor === "white" ? "White" : "Black";
  btn.textContent = `↺ Review ${label}'s side`;
  btn.hidden = false;
}

// Re-open the current game reviewing the opponent (the wrong side may have been auto-picked).
function reviewOtherSide() {
  if (!currentPgn) return;
  openGame(currentPgn, player === "white" ? "black" : "white");
}

async function openGame(pgn, side) {
  batchInfo = null; // a single open is not a batch
  closeHistoryDrawer(); // on small screens the drawer covers the board — get out of the way
  beginProvisional(pgn, side);
  // AWAIT the POST: jobs.start switches the server's session/job synchronously, so by the time it
  // returns the status reflects THIS game. Polling before that could observe the *previous* game's
  // lingering "ready" and load the wrong session (showing its chat/board). On a cache hit the server
  // is already "ready", so we skip polling and render immediately.
  let st = null;
  try {
    st = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pgn, player: side || "auto" }),
    }).then((r) => r.json());
  } catch (_) {}
  if (st && st.status === "ready") { onAnalysisReady(); return; }
  if (st && st.status === "error") { onAnalysisError(st.error); return; }
  startPolling();
}

// Analyze a multi-game PGN (e.g. a Chess.com export): the backend splits + analyzes each game in
// the background and records them to "My games"; we show the first game while the rest run.
async function openBatch(pgnText, side, username) {
  let res;
  try {
    res = await fetch("/api/analyze-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pgn: pgnText, player: side || "auto", username: username || "" }),
    }).then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not start analysis.";
    return;
  }
  if (res.error || !res.total_games) {
    $("history-status").textContent = res.error || "No valid games found in that PGN.";
    return;
  }
  batchInfo = { total: res.total_games, self_handle: res.self_handle, lastDone: -1 };
  const who = res.self_handle ? ` as ${res.self_handle}` : "";
  $("history-status").textContent = `Analyzing ${res.total_games} games${who} → they'll appear in My games.`;
  closeHistoryDrawer(); // on small screens the drawer covers the board — get out of the way
  beginProvisional(res.first_pgn, res.first_side, `Analyzing game 1 of ${res.total_games}…`);
  startPolling();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    let st;
    try {
      st = await fetch("/api/analysis-status").then((r) => r.json());
    } catch (_) {
      return;
    }
    // Batch: surface each finished game in "My games" as it lands.
    if (batchInfo && st.done_games != null && st.done_games !== batchInfo.lastDone) {
      batchInfo.lastDone = st.done_games;
      if (historyMode === "normal") loadHistory();
    }
    if (st.status === "ready") {
      stopPolling();
      onAnalysisReady();
    } else if (st.status === "error" && (!batchInfo || (st.total_games || 1) === 1)) {
      // A single-game failure is terminal; in a batch we keep going (error is just the last note).
      stopPolling();
      onAnalysisError(st.error);
    } else {
      renderProgress(st); // pending: advance the bar / ETA
    }
  }, 800);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function onAnalysisReady() {
  // Where the user navigated during phase 1 — only meaningful if a provisional timeline existed
  // for THIS game (when the PGN couldn't be replayed client-side, `cur` is a stale index from the
  // previous game and honouring it would land on an arbitrary move with no mistake selected).
  const prevCur = timeline.length ? cur : 0;
  const session = await fetch("/api/session").then((r) => r.json());
  const tl = await fetch("/api/timeline").then((r) => r.json());
  if (session.empty) return; // superseded/cleared
  analyzing = false;
  setAnalyzingUI(false); // hides the progress bar
  applySession(session);
  applyTimeline(tl);
  // "Replay in full game" from a mistake puzzle wins: land on the exact position of the mistake.
  if (pendingGotoPly != null) {
    gotoNode(clamp(pendingGotoPly, 0, timeline.length - 1));
    pendingGotoPly = null;
  }
  // Keep the user where they were navigating; if they hadn't moved, jump to the first mistake.
  else if (prevCur === 0 && mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(clamp(prevCur, 0, timeline.length - 1));
  restoreChat(); // repopulate this game's in-memory Q&A (if we've chatted about it this session)
  prepareCoachAI(session); // timeline is set now → show saved summary, auto-generate, or offer button
  if (batchInfo) {
    // Whole upload done: surface every game in "My games".
    const n = batchInfo.total;
    const who = batchInfo.self_handle ? ` as ${batchInfo.self_handle}` : "";
    batchInfo = null;
    activateTab("normal");
    loadHistory(`Analyzed ${n} game${n === 1 ? "" : "s"}${who}. Showing the first below.`);
  } else if (historyMode === "normal") {
    loadHistory(); // the just-analyzed game now appears in the list
  }
}

function onAnalysisError(msg) {
  analyzing = false;
  setAnalyzingUI(false);
  renderGraph();
  $("history-status").textContent = `Analysis failed: ${msg || "unknown error"}`;
  $("game-meta").textContent = "Analysis failed — you can still step through the moves.";
}

// --- history / lichess panel ---------------------------------------------
// `resetPaging` collapses back to the first page + scrolls to top; callers pass it only on a
// genuine identity change. The default preserves how far the user paged/scrolled, so refreshing
// the list after opening/analyzing a game doesn't force them to press "Show more" and re-scroll
// to find an older game they were looking at.
async function loadHistory(doneMsg, { resetPaging = false } = {}) {
  $("history-status").textContent = "Loading…";
  let data;
  try {
    data = await fetch("/api/history").then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not load history.";
    return;
  }
  myPlayerId = data.player_id || myPlayerId;
  if (myPlayerId) $("lichess-user").placeholder = myPlayerId;
  historyGames = data.games || [];
  if (resetPaging) {
    historyCount = HISTORY_PAGE;
    $("history-list").scrollTop = 0;
  } else {
    // Keep the current page count (clamped to the list) so an expanded list stays expanded.
    historyCount = Math.min(Math.max(historyCount, HISTORY_PAGE), Math.max(historyGames.length, HISTORY_PAGE));
  }
  renderMyGames();
  $("history-status").textContent = historyGames.length ? doneMsg || "" : "No analyzed games yet.";
  loadInsights(); // the aggregate reflects whatever just landed in history
}

// Render the current page of "My games" into the (fixed-height, scrollable) list, with a
// "Show more" row when there are extra games beyond what's shown. No refetch — pages a cached list.
function renderMyGames() {
  const box = $("history-list");
  const prevScroll = box.scrollTop; // rebuilding the list resets scroll; restore it below
  renderHistory(historyGames.slice(0, historyCount), "normal");
  const remaining = historyGames.length - historyCount;
  if (remaining > 0) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = `Show more (${remaining})`;
    li.addEventListener("click", () => {
      historyCount += HISTORY_PAGE;
      renderMyGames();
    });
    box.appendChild(li);
  }
  box.scrollTop = prevScroll;
}

async function loadLichess(username) {
  lichessUser = username;
  $("history-status").textContent = "Fetching from Lichess…";
  const q = new URLSearchParams();
  if (username) q.set("username", username);
  q.set("max", String(lichessCount));
  let data;
  try {
    data = await fetch("/api/lichess/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not reach Lichess.";
    return;
  }
  if (data.error) {
    $("history-status").textContent = data.error;
    $("history-list").innerHTML = "";
    return;
  }
  const games = data.games || [];
  const who = (username || myPlayerId || "").toLowerCase();
  reflectSetAsMe(who); // is the looked-up account already "me"?
  renderHistory(games, "lichess", who);
  $("history-status").textContent = games.length ? "" : "No games found.";
  // While the server keeps returning a full page, there are probably more to fetch.
  if (games.length >= lichessCount) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = "Load more";
    li.addEventListener("click", () => {
      lichessCount += LICHESS_PAGE;
      loadLichess(lichessUser);
    });
    $("history-list").appendChild(li);
  }
}

async function loadChesscom(username) {
  chesscomUser = username;
  $("history-status").textContent = "Fetching from Chess.com…";
  const q = new URLSearchParams();
  if (username) q.set("username", username);
  q.set("max", String(chesscomCount));
  let data;
  try {
    data = await fetch("/api/chesscom/games?" + q.toString()).then((r) => r.json());
  } catch (_) {
    $("history-status").textContent = "Could not reach Chess.com.";
    return;
  }
  if (data.error) {
    $("history-status").textContent = data.error;
    $("history-list").innerHTML = "";
    return;
  }
  const games = data.games || [];
  const who = (username || chesscomUsername || "").toLowerCase();
  renderHistory(games, "lichess", who); // same remote-games rendering as the Lichess tab
  $("history-status").textContent = games.length ? "" : "No games found.";
  if (games.length >= chesscomCount) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = "Load more";
    li.addEventListener("click", () => {
      chesscomCount += LICHESS_PAGE;
      loadChesscom(chesscomUser);
    });
    $("history-list").appendChild(li);
  }
}

const resultClass = (r) => (r === "win" ? "win" : r === "loss" ? "loss" : r === "draw" ? "draw" : "");
const resultWord = (r) => ({ win: "Won", loss: "Lost", draw: "Drew" }[r] || "");

function renderHistory(games, mode, who) {
  const ol = $("history-list");
  ol.innerHTML = "";
  for (const g of games) {
    const li = document.createElement("li");
    let side, title, sub, blunders, disabled, cls;
    if (mode === "normal") {
      side = g.reviewed_side;
      const opp = side === "white" ? g.black : g.white;
      cls = resultClass(g.player_result);
      const acc = g.accuracy != null ? `${g.accuracy}%` : "?";
      title = `${resultWord(g.player_result) || "vs"} ${opp || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${acc} · ${g.speed}`;
      blunders = (g.counts && g.counts.blunder) || 0;
      disabled = !g.has_pgn;
      // Tint games recorded under the configured user ("you") so they stand out from games
      // analysed for someone else (e.g. an opponent-side review, or another account's PGN).
      // The backend computes `is_me` against the CURRENT identity (so a chess.com game recorded
      // before that handle was added to "me" still tints); fall back to the frozen id match.
      if (g.is_me || (myPlayerId && g.player_id && g.player_id === myPlayerId)) cls += " mine";
    } else {
      const w = (g.white || "").toLowerCase();
      const b = (g.black || "").toLowerCase();
      side = who && w === who ? "white" : who && b === who ? "black" : "auto";
      cls = "";
      title = `${g.white || "?"} vs ${g.black || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${g.speed} · ${g.result || ""}`;
      blunders = null;
      disabled = !g.pgn;
    }
    li.className = cls + (disabled ? " disabled" : "");
    li.innerHTML =
      `<div class="h-title"><span>${escapeHtml(title)}</span>` +
      (blunders ? `<span class="h-blunders">●${blunders}</span>` : "") +
      `</div><div class="h-sub">${escapeHtml(sub)}</div>`;
    if (disabled) {
      li.title =
        mode === "normal"
          ? "Can't reopen — this game was analyzed before PGNs were stored. Re-analyze it from Lichess."
          : "No PGN available for this game.";
    } else {
      li.addEventListener("click", () => openGame(g.pgn, side));
    }
    ol.appendChild(li);
  }
}

// Build a small "open this game on the source site" ↗ anchor (Lichess / Chess.com), or null if
// there's no usable URL. Opens in a new tab; stops click-propagation so it never triggers a
// surrounding row/handler.
function gameLink(url, className) {
  if (!url || !/^https?:\/\//i.test(url)) return null;
  let host = "the source site";
  try {
    host = new URL(url).hostname.replace(/^www\./, "");
  } catch (_) {}
  const a = document.createElement("a");
  a.className = className || "game-open";
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = "↗";
  a.title = `Open this game on ${host}`;
  a.setAttribute("aria-label", `Open this game on ${host}`);
  a.addEventListener("click", (e) => e.stopPropagation());
  return a;
}

// Render the board-header line: "White vs Black" + an optional ↗ source-site link + a details tail.
// Built via DOM (not innerHTML) so the URL/names can't break out of an attribute or inject markup.
function setGameMeta(white, black, url, tail) {
  const gm = $("game-meta");
  if (!gm) return;
  gm.textContent = "";
  const names = document.createElement("span");
  names.textContent = `${white || "White"} vs ${black || "Black"}`;
  gm.appendChild(names);
  const link = gameLink(url, "meta-open");
  if (link) gm.appendChild(link);
  if (tail) gm.appendChild(document.createTextNode(tail));
}

// The original game's URL from PGN Site/Link headers (mirrors history.game_url_from_headers).
function gameUrlFromHeaders(h) {
  for (const k of ["Site", "Link"]) {
    const v = (h && h[k] != null ? String(h[k]) : "").trim();
    if (/^https?:\/\//i.test(v)) return v;
  }
  return null;
}

// Parse just the White/Black/URL out of a PGN's headers (chess.js), best-effort.
function pgnHeaders(pgn) {
  try {
    const c = new Chess();
    c.loadPgn(pgn);
    const h = c.header();
    return { white: h.White, black: h.Black, url: gameUrlFromHeaders(h) };
  } catch (_) {
    return {};
  }
}

// --- insights panel --------------------------------------------------------
// Cross-game themes + stats for the configured user over a chosen time window, aggregated
// server-side from the analyzed-games history (GET /api/insights). Refreshed whenever the
// history list reloads (i.e. as new games are analyzed) and when the period changes.
let insightsDays = 30;

async function loadInsights() {
  const body = $("insights-body");
  if (!body) return;
  let data;
  try {
    data = await fetch(`/api/insights?days=${insightsDays}`).then((r) => r.json());
  } catch (_) {
    body.innerHTML = `<p class="muted">Could not load insights.</p>`;
    return;
  }
  renderInsights(data);
}

function renderInsights(d) {
  const body = $("insights-body");
  if (!d || d.error) {
    body.innerHTML = `<p class="muted">${escapeHtml((d && d.error) || "No data.")}</p>`;
    return;
  }
  if (!d.games) {
    body.innerHTML = `<p class="muted">No analyzed games in this period yet.</p>`;
    return;
  }
  const r = d.results || {};
  const mt = d.mistake_totals || {};
  const parts = [];
  parts.push(
    `<div class="ins-stat"><b>${d.games}</b> game${d.games === 1 ? "" : "s"} · ` +
      `${r.win || 0}W–${r.loss || 0}L–${r.draw || 0}D` +
      (d.avg_accuracy != null ? ` · <b>${d.avg_accuracy}%</b> avg accuracy` : "") +
      `</div>`
  );
  parts.push(
    `<div class="ins-stat muted">${mt.blunder || 0} blunders · ${mt.mistake || 0} mistakes · ` +
      `${mt.inaccuracy || 0} inaccuracies</div>`
  );
  const motifs = (d.top_motifs || []).slice(0, 5);
  if (motifs.length) {
    parts.push(
      `<h3>Recurring themes</h3><ul class="ins-list">` +
        motifs
          .map(
            (m) =>
              `<li>${escapeHtml(m.label || m.motif)} <span class="muted">×${m.count}</span></li>`
          )
          .join("") +
        `</ul>`
    );
  }
  if (d.weakest_phase) {
    parts.push(`<div class="ins-stat">Weakest phase: <b>${escapeHtml(d.weakest_phase)}</b></div>`);
  }
  const ops = (d.openings || []).slice(0, 3);
  if (ops.length) {
    parts.push(
      `<h3>Most played openings</h3><ul class="ins-list">` +
        ops
          .map(
            (o) =>
              `<li>${escapeHtml(o.opening)} <span class="muted">×${o.games}` +
              (o.avg_accuracy != null ? ` · ${o.avg_accuracy}%` : "") +
              `</span></li>`
          )
          .join("") +
        `</ul>`
    );
  }
  body.innerHTML = parts.join("");
}

// Just the tab chrome (active button + which form/list is shown), no data fetch.
function activateTab(mode) {
  historyMode = mode;
  clearChesscomHint(); // leaving/returning to a tab resets the rapid-refresh detector
  $("mode-normal").classList.toggle("active", mode === "normal");
  $("mode-lichess").classList.toggle("active", mode === "lichess");
  $("mode-chesscom").classList.toggle("active", mode === "chesscom");
  $("mode-paste").classList.toggle("active", mode === "paste");
  $("lichess-form").style.display = mode === "lichess" ? "flex" : "none";
  $("chesscom-form").style.display = mode === "chesscom" ? "flex" : "none";
  $("paste-form").style.display = mode === "paste" ? "flex" : "none";
  $("history-list").style.display = mode === "paste" ? "none" : "";
}

// Update the "Set as my account" button to reflect whether `who` (lowercased) is already you.
function reflectSetAsMe(who) {
  const btn = $("set-as-me");
  if (!btn) return;
  const isMe = !!appUsername && appUsername.toLowerCase() === who && !!who;
  btn.disabled = isMe;
  btn.textContent = isMe ? "✓ This is your account" : "Set as my account";
}

// --- settings panel ------------------------------------------------------
// "Coaching memory" is a friendly single-choice front for the two profile windows
// (profile_recent + profile_lifetime). Each preset sets both at once.
const PROFILE_PRESETS = {
  balanced: {
    recent: "100", lifetime: "all",
    hint: "Coaches on your last 100 games for current form, plus your whole history for long-term patterns.",
  },
  recent: {
    recent: "100", lifetime: "0",
    hint: "Focuses only on your last 100 games; older games are ignored.",
  },
  all: {
    recent: "0", lifetime: "all",
    hint: "Weighs every game you've played equally, recent or old.",
  },
};

// The two Advanced number fields are the source of truth; the dropdown is a convenience that
// fills them. Match the current field values to a preset (or "custom" for any other combination).
function profileModeFromFields() {
  const r = $("set-recent").value.trim();
  const l = $("set-lifetime").value.trim().toLowerCase() || "all"; // blank lifetime == "all"
  for (const [mode, p] of Object.entries(PROFILE_PRESETS)) {
    if (r === p.recent && l === p.lifetime) return mode;
  }
  return "custom";
}

// Picking a named preset writes its windows into the Advanced fields.
function applyProfilePreset() {
  const p = PROFILE_PRESETS[$("set-profile-mode").value];
  if (p) {
    $("set-recent").value = p.recent;
    $("set-lifetime").value = p.lifetime;
  }
  updateProfileHint();
}

// Editing a window by hand flips the dropdown to the matching preset (or "Custom").
function syncProfileModeFromFields() {
  $("set-profile-mode").value = profileModeFromFields();
  updateProfileHint();
}

function updateProfileHint() {
  const mode = $("set-profile-mode").value;
  $("set-profile-hint").textContent = PROFILE_PRESETS[mode]
    ? PROFILE_PRESETS[mode].hint
    : "Using your own window sizes from the fields below.";
}

// Mistake sensitivity: a checkbox auto-scales it to each game's PGN rating (default on). Unchecking
// reveals a slider whose value is a representative Elo (stored as `player_elo`; blank = Auto). The
// tier label tracks the same casual/intermediate/advanced/master bands the backend tunes against.
const SKILL_DEFAULT_ELO = "1200"; // where the slider starts when first switching to manual

// Named levels along the slider. Each entry is [minimum Elo, label]; the label shown is the highest
// tier whose minimum the value has reached. Anchors: Beginner 400, Intermediate 1200, Advanced 1800,
// Master 2300, with in-between levels so every slider stop reads as a recognisable strength.
const SKILL_TIERS = [
  [400, "Beginner"],
  [700, "Novice"],
  [1000, "Casual"],
  [1200, "Intermediate"],
  [1500, "Club player"],
  [1800, "Advanced"],
  [2100, "Expert"],
  [2300, "Master"],
];

function eloTier(elo) {
  let label = SKILL_TIERS[0][1];
  for (const [min, name] of SKILL_TIERS) {
    if (elo >= min) label = name;
  }
  return label;
}

// Show/hide the "how many recent games to check" field to match the auto-sync checkbox.
function updateChesscomSyncUI() {
  $("chesscom-sync-max-field").hidden = !$("set-chesscom-sync").checked;
}

// Show/hide the slider to match the checkbox and refresh the readout.
function updateSkillUI() {
  const auto = $("set-skill-auto").checked;
  $("skill-manual").hidden = auto;
  if (!auto) {
    const elo = parseInt($("set-elo").value, 10);
    $("set-elo-label").textContent = `${eloTier(elo)} · ~${elo} Elo`;
  }
}

async function openSettings() {
  $("settings-status").textContent = "";
  let data;
  try {
    data = await fetch("/api/settings").then((r) => r.json());
  } catch (_) {
    $("settings-status").textContent = "Could not load settings.";
    $("settings").hidden = false;
    return;
  }
  const s = data.settings || {};
  $("set-username").value = s.username || "";
  $("set-chesscom").value = s.chesscom_username || "";
  $("set-chesscom-sync").checked = s.chesscom_sync !== false; // auto-sync new games (default on)
  $("set-chesscom-sync-max").value = s.chesscom_sync_max || "5";
  updateChesscomSyncUI();
  $("set-aliases").value = s.aliases || "";
  $("set-token").value = s.lichess_token || "";
  // Coaching memory: load the raw windows into the Advanced fields, then point the
  // dropdown at whichever preset they match (or "Custom" for any other combination).
  $("set-recent").value = s.profile_recent || "";
  $("set-lifetime").value = s.profile_lifetime || "";
  syncProfileModeFromFields();
  // Mistake sensitivity: a stored rating means manual (slider shown); blank means auto-scale.
  const elo = (s.player_elo || "").trim();
  $("set-skill-auto").checked = !elo;
  $("set-elo").value = elo || SKILL_DEFAULT_ELO;
  updateSkillUI();
  $("set-stockfish").value = s.stockfish_path || "";
  $("set-local-llm-url").value = s.local_llm_base_url || "";
  $("set-local-llm-model").value = s.local_llm_model || "";
  $("set-ollama-status").textContent = "";
  $("set-ollama-pick-row").hidden = true; // picker only appears after a successful Detect
  $("set-coach-ai-auto").checked = !!s.coach_ai_auto; // auto-generate per game (default off)
  $("set-coach-ai-persist").checked = s.coach_ai_persist !== false; // remember summaries (default on)
  $("set-personalize").checked = s.personalize_history !== false; // personalize chat (default on)
  $("set-puzzle-animations").checked = s.puzzle_animations !== false; // solve animations (default on)
  $("set-puzzle-auto-advance").checked = s.puzzle_auto_advance === true; // auto-next after solve (default off)
  $("set-puzzle-interleave").checked = s.puzzle_mistake_interleave !== false; // mix in own-game mistakes (default on)
  $("set-sf-status").textContent = data.stockfish_ok
    ? "Stockfish engine found ✓"
    : "Stockfish not found — analysis won't run until this points at the engine.";
  // Open on the tab most relevant to what the user is doing (Puzzles while solving, else Account).
  activateSettingsTab(puzzleMode ? "puzzles" : "account");
  $("settings").hidden = false;
  if (!puzzleMode) $("set-username").focus();
}

// Switch the Settings panel to one category tab.
function activateSettingsTab(name) {
  document
    .querySelectorAll(".set-tab-btn")
    .forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document
    .querySelectorAll(".set-panel")
    .forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

// One-click Ollama setup: fill in the default URL if blank, ask the backend what models Ollama
// has pulled, populate the model picker, and auto-select the first one if none is chosen yet.
async function detectOllama() {
  const status = $("set-ollama-status");
  status.textContent = "Looking for Ollama…";
  const url = $("set-local-llm-url").value.trim();
  let data;
  try {
    const q = url ? `?url=${encodeURIComponent(url)}` : "";
    data = await fetch(`/api/ollama/models${q}`).then((r) => r.json());
  } catch (_) {
    status.textContent = "Could not reach the server.";
    return;
  }
  if (!data.ok) {
    status.textContent = data.error || "No Ollama found.";
    return;
  }
  if (!url) $("set-local-llm-url").value = data.base_url; // adopt the URL we found it at
  const sel = $("set-ollama-model-select");
  sel.innerHTML = "";
  for (const name of data.models) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  if (!data.models.length) {
    $("set-ollama-pick-row").hidden = true;
    status.textContent = "Ollama is running but has no models. Pull one: ollama pull qwen2.5-coder";
    return;
  }
  // Show the picker; keep the existing model if it's one Ollama has, else default to the first.
  $("set-ollama-pick-row").hidden = false;
  const current = $("set-local-llm-model").value.trim();
  const chosen = data.models.includes(current) ? current : data.models[0];
  sel.value = chosen;
  $("set-local-llm-model").value = chosen;
  status.textContent = `Found ${data.models.length} model${data.models.length === 1 ? "" : "s"} ✓ — pick one and Save.`;
}

async function saveSettings(e) {
  e.preventDefault();
  $("settings-status").textContent = "Saving…";
  const patch = {
    username: $("set-username").value.trim(),
    chesscom_username: $("set-chesscom").value.trim(),
    chesscom_sync: $("set-chesscom-sync").checked,
    chesscom_sync_max: $("set-chesscom-sync-max").value.trim(),
    aliases: $("set-aliases").value.trim(),
    lichess_token: $("set-token").value.trim(),
    stockfish_path: $("set-stockfish").value.trim(),
    local_llm_base_url: $("set-local-llm-url").value.trim(),
    local_llm_model: $("set-local-llm-model").value.trim(),
    coach_ai_auto: $("set-coach-ai-auto").checked,
    coach_ai_persist: $("set-coach-ai-persist").checked,
    personalize_history: $("set-personalize").checked,
    puzzle_animations: $("set-puzzle-animations").checked,
    puzzle_auto_advance: $("set-puzzle-auto-advance").checked,
    puzzle_mistake_interleave: $("set-puzzle-interleave").checked,
  };
  // The Advanced fields are the source of truth (the dropdowns just fill them).
  patch.profile_recent = $("set-recent").value.trim();
  patch.profile_lifetime = $("set-lifetime").value.trim();
  // Auto-scale on -> blank (read each game's Elo); off -> the slider's chosen Elo.
  patch.player_elo = $("set-skill-auto").checked ? "" : $("set-elo").value.trim();
  let res;
  try {
    res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }).then((r) => r.json());
  } catch (_) {
    $("settings-status").textContent = "Could not save settings.";
    return;
  }
  if (res.error) {
    $("settings-status").textContent = res.error;
    return;
  }
  appUsername = (res.settings && res.settings.username) || "";
  chesscomUsername = (res.settings && res.settings.chesscom_username) || "";
  if (appUsername) $("lichess-user").placeholder = appUsername;
  $("settings").hidden = true;
  // Apply the auto-summary preference without clobbering a summary that's already shown/pending:
  // only act when nothing's there yet (turn-on -> generate now; turn-off -> offer the button).
  coachAiAuto = !!(res.settings && res.settings.coach_ai_auto);
  personalizeHistory = !(res.settings && res.settings.personalize_history === false);
  puzzleAnimations = !(res.settings && res.settings.puzzle_animations === false);
  puzzleAutoAdvance = !!(res.settings && res.settings.puzzle_auto_advance === true);
  chesscomSync = !(res.settings && res.settings.chesscom_sync === false);
  chesscomSyncMax = Number(res.settings && res.settings.chesscom_sync_max) || 5;
  const card = $("coach-ai");
  const busy = card && !card.hidden && !card.classList.contains("err");
  if (timeline.length && !busy) {
    if (coachAiAuto) fetchCoachAI();
    else { setCoachAI("hidden"); showCoachButton(true); }
  } else if (!timeline.length) {
    setCoachAI("hidden");
    showCoachButton(false);
  }
  if (historyMode === "normal") loadHistory(); // identity may have changed -> refresh My games
}

function setMode(mode) {
  activateTab(mode);
  if (mode === "normal") {
    loadHistory();
  } else if (mode === "lichess") {
    lichessCount = LICHESS_PAGE; // fresh search starts at the first page
    loadLichess($("lichess-user").value.trim());
  } else if (mode === "chesscom") {
    chesscomCount = LICHESS_PAGE;
    loadChesscom($("chesscom-user").value.trim());
  } else {
    // paste: nothing to fetch; just a hint until they submit.
    updatePasteHint();
  }
}

// Count games in a PGN by its [Event headers (>=1: a header-less PGN is still one game).
const countGames = (pgn) => Math.max(1, (pgn.match(/^\s*\[Event\b/gm) || []).length);

function updatePasteHint() {
  if (historyMode !== "paste") return;
  const pgn = $("paste-pgn").value.trim();
  if (!pgn) {
    $("history-status").textContent = "Paste or upload a PGN (one or many games), then Analyze.";
    return;
  }
  const n = countGames(pgn);
  $("history-status").textContent =
    n > 1 ? `${n} games detected — all will be analyzed into My games.` : "1 game ready to analyze.";
}

// Kick off analysis of a PGN typed/uploaded/dropped into the Paste panel. Shared by the Analyze
// button, the file picker, and the drag-and-drop drop zone. Routes multi-game PGNs to openBatch.
function startPasteAnalysis(pgn, side, username) {
  $("firstrun").hidden = true; // in case the first-run prompt was still up
  $("history-status").textContent = "";
  if (countGames(pgn) > 1) openBatch(pgn, side, username);
  else openGame(pgn, side);
}

// Read a dropped/picked .pgn file, mirror it into the Paste textarea (so the user sees what loaded
// and can still pick a side), and — when `analyze` — start the sweep straight away. Dropping a file
// anywhere on the Games panel uses analyze=true so people don't have to click Upload then Analyze.
function loadPgnFile(file, analyze) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    setMode("paste"); // reveal the Paste panel so the loaded PGN is visible/editable
    $("paste-pgn").value = reader.result || "";
    updatePasteHint();
    if (!analyze) return;
    const pgn = ($("paste-pgn").value || "").trim();
    if (!pgn) {
      $("history-status").textContent = "That file had no PGN text.";
      return;
    }
    startPasteAnalysis(pgn, $("paste-side").value || "auto", ($("paste-username").value || "").trim());
  };
  reader.onerror = () => {
    $("history-status").textContent = "Could not read that file.";
  };
  reader.readAsText(file);
}

// True when a drag carries files (vs. selected text), so we only hijack file drags.
function dragHasFiles(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
}

// First dropped file that looks like a PGN (by extension — many sources set no MIME type).
function firstPgnFile(dataTransfer) {
  const files = dataTransfer && dataTransfer.files ? Array.from(dataTransfer.files) : [];
  return files.find((f) => /\.(pgn|txt)$/i.test(f.name || "")) || null;
}

// Drag-and-drop a .pgn onto the Games panel (any tab) to load + analyze it. dragenter/leave use a
// depth counter so the highlight doesn't flicker as the cursor crosses child elements.
function initPgnDrop() {
  const col = $("history-col");
  if (!col) return;
  let depth = 0;
  const clear = () => {
    depth = 0;
    col.classList.remove("drag-over");
  };
  col.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth++;
    col.classList.add("drag-over");
  });
  col.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  col.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    if (--depth <= 0) clear();
  });
  col.addEventListener("drop", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    clear();
    const file = firstPgnFile(e.dataTransfer);
    if (!file) {
      setMode("paste");
      $("history-status").textContent = "Drop a .pgn file to analyze it.";
      return;
    }
    loadPgnFile(file, true);
  });
  // Stop the browser from navigating away if a file is dropped outside the panel (only intercept
  // file drags, so dragging selected text into inputs/textarea still works normally).
  ["dragover", "drop"].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      if (dragHasFiles(e)) e.preventDefault();
    })
  );
}

// Below this width the Games panel is an off-canvas drawer (see styles.css) rather than a third
// column, so it must start closed and auto-close when a game is opened to reveal the board.
const HISTORY_DRAWER_MAX = 1400;
function historyIsDrawer() {
  return window.innerWidth <= HISTORY_DRAWER_MAX;
}
function closeHistoryDrawer() {
  if (historyIsDrawer()) document.body.classList.add("history-hidden");
}

function toggleHistory() {
  document.body.classList.toggle("history-hidden");
}

// Keep the drawer state sane when the window crosses the 1400px breakpoint. Without this, the
// `history-hidden` class is whatever it was last set to (e.g. never set, if the page loaded wide),
// so shrinking below 1400 can leave the panel stuck open as a fixed drawer overlaying the board —
// and the open drawer covers the ☰ Games button, so toggling it looks like nothing happens.
// Entering drawer mode → start closed (☰ Games opens it); back to wide → show the column.
let wasDrawer = historyIsDrawer();
window.addEventListener("resize", () => {
  const now = historyIsDrawer();
  if (now === wasDrawer) return;
  wasDrawer = now;
  document.body.classList.toggle("history-hidden", now);
});

// --- board resizing (the iPad-style drag handle, #col-resizer) ------------------------------
// Everything board-sized derives from the CSS var --board-size, which falls back to the
// responsive --board-default unless we set an explicit --board-user (a px override). We keep the
// user's chosen px in localStorage and re-clamp it on every apply, so it never overflows the row.
const BOARD_SIZE_KEY = "chessBoardSize";
let boardSizeUser = null; // px override, or null = use the responsive default

// Looser than the default's 660px / 48vw caps (the user asked for less restriction) but still
// leaves the analysis column (and the Games column, when it's not a drawer) enough room.
function boardSizeBounds() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const PAD = 40; // main's left+right padding
  const GAP = 24; // main's column gap
  const puzzle = document.body.classList.contains("puzzle-mode");
  const SIDE_MIN = 280; // keep the analysis column / puzzle rail usable
  const EVALBAR = puzzle ? 0 : 28; // eval bar + its gap (col-width = board-size + 28); hidden in puzzle mode
  // The Games column only exists in analysis mode above the drawer breakpoint.
  const historyCol = !puzzle && vw > HISTORY_DRAWER_MAX ? 280 + GAP : 0;
  const maxByWidth = vw - PAD - GAP - SIDE_MIN - historyCol - EVALBAR;
  const maxByHeight = Math.round(vh * 0.92);
  const min = 240;
  const max = Math.max(min, Math.min(maxByWidth, maxByHeight));
  return { min, max };
}

let boardRedrawPending = false;
function scheduleBoardRedraw() {
  // Chessground caches its bounds, so it needs a redraw to re-place pieces after the board's
  // rendered size changes (a resize, an override, or the panel scrollbar appearing/disappearing).
  // Debounced to one redraw per frame — a ResizeObserver can fire many times in a burst.
  if (!ground || boardRedrawPending) return;
  boardRedrawPending = true;
  requestAnimationFrame(() => {
    boardRedrawPending = false;
    if (ground) ground.redrawAll();
  });
}

// Anchor the fixed drag handle to the board panel's *measured* right edge (+ a small gap) instead
// of a CSS var that can't see the panel's scrollbar gutter. This keeps the separator and the board
// reading the same width, so the handle clears the scrollbar in every browser (Chrome overlay,
// Firefox reserved-gutter) and in puzzle mode (no eval bar) alike. See issue #5.
const RESIZER_GAP = 6;
function positionResizer() {
  const rez = $("col-resizer");
  const col = document.querySelector(".board-col");
  if (!rez || !col) return;
  // Hidden on narrow/stacked layouts (CSS display:none) — nothing to place.
  if (getComputedStyle(rez).display === "none") return;
  const right = col.getBoundingClientRect().right; // viewport coords == fixed-position origin
  rez.style.left = Math.round(right + RESIZER_GAP) + "px";
}

function applyBoardSize() {
  const root = document.documentElement;
  if (boardSizeUser == null) {
    root.style.removeProperty("--board-user");
  } else {
    const { min, max } = boardSizeBounds();
    boardSizeUser = Math.round(Math.max(min, Math.min(max, boardSizeUser)));
    root.style.setProperty("--board-user", boardSizeUser + "px");
  }
  scheduleBoardRedraw();
  positionResizer();
}

// Watch the board's rendered size (changes on window resize even with no user override, on an
// override drag, and when the panel scrollbar toggles) and keep chessground's bounds + the
// separator in sync. Without this, a responsive resize with no --board-user left the pieces
// mis-centered against stale bounds (the old window-resize handler only redrew when an override
// was set). See issue #5.
function observeBoardLayout() {
  const board = $("board");
  const col = document.querySelector(".board-col");
  if (typeof ResizeObserver === "undefined") return;
  if (board) {
    new ResizeObserver(() => {
      scheduleBoardRedraw();
      positionResizer();
    }).observe(board);
  }
  // The panel's own width changes independently of the board when its scrollbar gutter appears or
  // the eval bar is hidden (puzzle mode) — reposition the handle for those too.
  if (col) new ResizeObserver(() => positionResizer()).observe(col);
}

function restoreBoardSize() {
  try {
    const v = parseInt(localStorage.getItem(BOARD_SIZE_KEY) || "", 10);
    if (Number.isFinite(v) && v > 0) boardSizeUser = v;
  } catch (_) {}
  applyBoardSize();
}

function resetBoardSize() {
  boardSizeUser = null;
  try {
    localStorage.removeItem(BOARD_SIZE_KEY);
  } catch (_) {}
  applyBoardSize();
}

function persistBoardSize() {
  if (boardSizeUser == null) return;
  try {
    localStorage.setItem(BOARD_SIZE_KEY, String(boardSizeUser));
  } catch (_) {}
}

function initBoardResizer() {
  const rez = $("col-resizer");
  if (!rez) return;
  let startX = 0;
  let startSize = 0;
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    // Board is on the left, so dragging right (positive delta) grows it.
    boardSizeUser = startSize + (e.clientX - startX);
    applyBoardSize();
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    rez.classList.remove("dragging");
    document.body.style.userSelect = "";
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    persistBoardSize();
  };
  rez.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    // Start from whatever the board is actually rendered at (works whether or not an override is set).
    startSize = Math.round($("board").getBoundingClientRect().width);
    rez.classList.add("dragging");
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
  // Double-click resets to the responsive default.
  rez.addEventListener("dblclick", resetBoardSize);
  // Keyboard nudge for accessibility (handle is focusable).
  rez.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const base = boardSizeUser == null ? Math.round($("board").getBoundingClientRect().width) : boardSizeUser;
    boardSizeUser = base + (e.key === "ArrowRight" ? 24 : -24);
    applyBoardSize();
    persistBoardSize();
  });
  // Re-clamp a fixed board size when the window changes (so it can't overflow a now-smaller window).
  // With no override the responsive board still changes size, so always re-place the handle (and the
  // ResizeObserver redraws the pieces); with an override, re-clamp too.
  window.addEventListener("resize", () => {
    if (boardSizeUser != null) applyBoardSize();
    else positionResizer();
  });
}

// --- puzzle mode ---------------------------------------------------------

// 'e4' -> {left,top} as board-percent, respecting orientation (the board flips for Black).
function squareXY(square, orientation) {
  let f = square.charCodeAt(0) - 97; // file a..h -> 0..7
  let r = 8 - parseInt(square[1], 10); // rank 8..1 -> 0..7 (row from top)
  if (orientation === "black") {
    f = 7 - f;
    r = 7 - r;
  }
  return { left: f * 12.5 + "%", top: r * 12.5 + "%" };
}

// Pulse a square in the decoupled overlay (survives chessground redraws). kind: ok|part|bad.
// A solve (ok/part) also gets a soft expanding ripple ring, which reads cleaner than a scaling fill.
function blinkSquare(square, kind) {
  if (!puzzleAnimations) return; // animations off -> result shown by verdict text colour only
  const overlay = $("board-overlay");
  if (!overlay || !square) return;
  const xy = squareXY(square, orient);
  const cell = document.createElement("div");
  cell.className = "sq-blink " + kind;
  cell.style.left = xy.left;
  cell.style.top = xy.top;
  overlay.appendChild(cell);
  setTimeout(() => cell.remove(), kind === "bad" ? 520 : 900);
  if (kind === "ok" || kind === "part") {
    const ring = document.createElement("div");
    ring.className = "sq-ring " + kind;
    ring.style.left = xy.left;
    ring.style.top = xy.top;
    overlay.appendChild(ring);
    setTimeout(() => ring.remove(), 720);
  }
}

// A little confetti burst from the solved square. Multicoloured green/gold for a clean solve,
// a smaller warmer amber puff when the solve came after a miss (still a win, but calmer).
function spawnConfetti(square, kind) {
  if (!puzzleAnimations) return;
  const overlay = $("board-overlay");
  if (!overlay || !square) return;
  const xy = squareXY(square, orient);
  const cx = parseFloat(xy.left) + 6.25 + "%"; // square centre
  const cy = parseFloat(xy.top) + 6.25 + "%";
  const colors =
    kind === "part"
      ? ["#e08000", "#f0a640", "#f5c451", "#ffffff"]
      : ["#7bb434", "#a3d160", "#f5c451", "#ffffff", "#5a9216"];
  const n = kind === "part" ? 10 : 18;
  for (let i = 0; i < n; i++) {
    const p = document.createElement("div");
    p.className = "confetti";
    const angle = Math.random() * Math.PI * 2;
    const dist = 26 + Math.random() * 48;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist + 22; // gravity bias so pieces drift down as they fly
    p.style.left = cx;
    p.style.top = cy;
    p.style.background = colors[i % colors.length];
    p.style.setProperty("--dx", dx.toFixed(1) + "px");
    p.style.setProperty("--dy", dy.toFixed(1) + "px");
    p.style.setProperty("--rot", (Math.random() * 540 - 270).toFixed(0) + "deg");
    p.style.animationDelay = Math.floor(Math.random() * 70) + "ms";
    const s = 4 + Math.random() * 4;
    p.style.width = s.toFixed(1) + "px";
    p.style.height = (s * (0.55 + Math.random() * 0.7)).toFixed(1) + "px";
    overlay.appendChild(p);
    setTimeout(() => p.remove(), 1050);
  }
}

function shakeBoard() {
  if (!puzzleAnimations) return;
  const b = $("board");
  if (!b) return;
  b.classList.remove("shake");
  void b.offsetWidth; // restart the animation
  b.classList.add("shake");
  setTimeout(() => b.classList.remove("shake"), 400);
}

// Set the board to the current `chess` position for puzzle solving. `movable` gates whether the
// solver can move (locked while a forced reply animates or after the puzzle is done).
function renderPuzzleBoard(movable) {
  const color = turnColor();
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: color,
    check: chess.inCheck(),
    // Explicitly drive the yellow last-move highlight: because we set the FEN directly (rather than
    // via ground.move), chessground doesn't infer it, so without this it flickered on and off.
    lastMove: puzzleLastMove || undefined,
    movable: {
      color: movable ? color : undefined,
      dests: movable ? computeDests() : new Map(),
      free: false,
      showDests: true,
    },
    animation: { enabled: true },
  });
  drawArrows(); // draws puzzleShapes in puzzle mode
}

function pzStatus(msg) {
  $("pz-status").textContent = msg || "";
}

function updatePuzzleStats(rating, streak) {
  if (rating != null) $("pz-rating").textContent = "Puzzle · " + rating;
  if (streak != null) puzzleStreak = streak;
  renderPuzzlePips();
}

// Colour-coded pips for the last 5 outcomes THIS session (green = solved clean, red = missed),
// plus the running streak number.
function renderPuzzlePips() {
  const el = $("pz-streak");
  if (!el) return;
  const last = puzzleResults.slice(-5);
  let html = last.map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`).join("");
  if (puzzleStreak) html += `<span class="pz-streak-n">streak ${puzzleStreak}</span>`;
  el.innerHTML = html;
}

// Day-over-day practice streak, shown as a quiet flame chip (brightens past a day).
function renderDailyStreak(streak, best) {
  if (streak != null) puzzleDailyStreak = streak;
  if (best != null) puzzleBestDaily = best;
  const el = $("pz-daily");
  if (!el) return;
  const n = puzzleDailyStreak || 0;
  if (n < 1) { el.hidden = true; el.textContent = ""; return; }
  el.classList.toggle("hot", n >= 2);
  el.innerHTML = `🔥 <b>${n}</b> day${n === 1 ? "" : "s"}`;
  el.title = `Practised ${n} day${n === 1 ? "" : "s"} in a row` +
    (puzzleBestDaily > n ? ` · best ${puzzleBestDaily}` : "");
  el.hidden = false;
}

// The rating progress is COLLAPSED by default: the rail shows only "Rating" + your current number.
// Pressing the header expands a quiet white line curve (styled like the analysis win% plot) of your
// recent rated attempts — no colour-coding, just the trend line at the top of where the bars were.
let ratingCurveExpanded = false;

function renderRatingCurve(history) {
  const el = $("pz-progress");
  if (!el) return;
  const pts = (history || [])
    .filter((h) => h && h.rated && typeof h.rating_after === "number")
    .map((h) => h.rating_after);
  if (pts.length < 2) { el.hidden = true; el.innerHTML = ""; return; }
  const win = pts.slice(-24); // keep it compact
  const current = Math.round(win[win.length - 1]);
  const min = Math.min(...win), max = Math.max(...win);
  const span = Math.max(1, max - min);
  const net = Math.round(win[win.length - 1] - win[0]);
  const netCls = net > 0 ? "up" : net < 0 ? "down" : "flat";
  const netStr = (net > 0 ? "+" : "") + net;

  // White trend line (+ faint fill under it) over a dark panel — same visual language as renderGraph.
  const W = 260, H = 48, PAD = 4;
  const xAt = (i) => (i / (win.length - 1)) * W;
  const yAt = (r) => H - PAD - ((r - min) / span) * (H - 2 * PAD);
  const seq = win.map((r, i) => `${xAt(i).toFixed(1)},${yAt(r).toFixed(1)}`);
  const line = seq.map((p, i) => `${i === 0 ? "M" : "L"}${p}`).join(" ");
  const area = `M0,${H} L${seq.join(" L")} L${W},${H} Z`;
  const svg =
    `<svg class="pz-curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
    `<rect x="0" y="0" width="${W}" height="${H}" fill="#14130f"/>` +
    `<path d="${area}" fill="rgba(236,234,228,0.12)"/>` +
    `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>` +
    `</svg>`;

  el.classList.toggle("open", ratingCurveExpanded);
  el.innerHTML =
    `<button type="button" class="pz-progress-head" aria-expanded="${ratingCurveExpanded}">` +
    `<span class="pz-progress-title">Rating <b>${current}</b></span>` +
    `<span class="pz-progress-caret">▸</span></button>` +
    `<div class="pz-progress-body">` +
    `<div class="pz-progress-sub"><span class="pz-progress-net ${netCls}">${netStr} · last ${win.length}</span></div>` +
    svg +
    `</div>`;
  el.hidden = false;
  const head = el.querySelector(".pz-progress-head");
  if (head) head.onclick = () => {
    ratingCurveExpanded = !ratingCurveExpanded;
    el.classList.toggle("open", ratingCurveExpanded);
    head.setAttribute("aria-expanded", String(ratingCurveExpanded));
  };
}

function loadPuzzleResults() {
  try {
    puzzleResults = JSON.parse(sessionStorage.getItem("pzResults") || "[]");
  } catch (_) {
    puzzleResults = [];
  }
}

function recordPuzzleResult(ok) {
  puzzleResults.push(!!ok);
  if (puzzleResults.length > 20) puzzleResults = puzzleResults.slice(-20);
  try {
    sessionStorage.setItem("pzResults", JSON.stringify(puzzleResults));
  } catch (_) {}
  renderPuzzlePips();
}

async function setPuzzleMode(on, opts = {}) {
  if (on === puzzleMode) return;
  cancelAutoAdvance(); // don't let a queued advance fire after switching activities
  puzzleMode = on;
  document.body.classList.toggle("puzzle-mode", on);
  $("mode-analyze").classList.toggle("active", !on);
  $("mode-puzzles").classList.toggle("active", on);
  // Puzzle mode frees the eval-bar + Games-column width, so the two modes have different board-size
  // bounds. Re-clamp any user override to the mode we're entering (also redraws the board).
  if (boardSizeUser != null) applyBoardSize();
  // The mode swap hides/shows elements around the board, shifting its on-screen position; chessground
  // caches its bounding rect, so without a redraw grabs land offset from the cursor. Recompute bounds
  // after the layout settles (next frame).
  requestAnimationFrame(() => {
    if (ground) ground.redrawAll();
    positionResizer(); // eval bar hides/shows, so the panel's right edge (and the handle) moves
  });
  if (on) {
    lsSet(PZ_MODE_KEY, "1"); // remember across reloads AND full app restarts (any launcher)
    closeHistoryDrawer();
    evalShapes = [];
    bestArrows = [];
    loadPuzzleResults();
    if (!puzzleConfigCache) {
      puzzleConfigCache = await fetch("/api/puzzle/config").then((r) => r.json()).catch(() => null);
    }
    // "From your games" needs the engine (it validates moves live); disable the segment otherwise.
    const hasEngine = !!(puzzleConfigCache && puzzleConfigCache.has_engine);
    const mineBtn = $("pz-src-mine");
    mineBtn.disabled = !hasEngine;
    mineBtn.title = hasEngine
      ? "Practice positions from your own analysed games"
      : "Needs the chess engine (Stockfish) — unavailable";
    if (!hasEngine && puzzleSource === "your_games") puzzleSource = "lichess";
    syncSourceUI();
    if (puzzleConfigCache) {
      updatePuzzleStats(puzzleConfigCache.your_rating, puzzleConfigCache.streak);
      renderDailyStreak(puzzleConfigCache.daily_streak, puzzleConfigCache.best_daily_streak);
    }
    loadPuzzleStatCard(); // daily streak + discrete rating curve (quiet, under the rail)
    // On a reload, resume the in-progress puzzle at the same spot; otherwise start a fresh one.
    if (opts.resume && (await resumeCurrentPuzzle())) return;
    await loadNextPuzzle();
  } else {
    lsSet(PZ_MODE_KEY, "0"); // back in analyze mode -> next open lands on analyze
    clearSolutionPlayback(); // drop any solution step-through + hide its nav
    // Leaving puzzles entirely: bank + drop any storm run and reset the sub-mode to Solve.
    if (stormShown) {
      endStormRun({ abandon: true });
      stormShown = false;
      inStormReview = false;
      $("pz-next").textContent = "Next puzzle →";
      $("pz-solve").hidden = false;
      $("pz-storm").hidden = true;
      $("pz-mode-solve").classList.add("active");
      $("pz-mode-storm").classList.remove("active");
    }
    // Restore the analysis board to wherever the game cursor was.
    puzzleChatReset();
    puzzleData = null;
    puzzleShapes = [];
    if (timeline.length) gotoNode(cur);
    else {
      chess.reset();
      renderBoard();
    }
  }
}

// Resume the puzzle the server still has in progress (after a browser reload). Returns false if
// there's nothing to resume (e.g. the server restarted), so the caller loads a fresh puzzle.
async function resumeCurrentPuzzle() {
  const myGen = ++puzzleGen;
  let cur;
  try {
    cur = await fetch("/api/puzzle/current").then((r) => r.json());
  } catch (_) {
    return false;
  }
  if (myGen !== puzzleGen) return true; // superseded; caller should not load another
  if (!cur || !cur.active || cur.finished) return false;
  const isMine = cur.source === "your_games";
  puzzleData = {
    id: cur.id,
    source: cur.source || "lichess",
    side_to_move: cur.side_to_move,
    themes: cur.themes || [],
    rating: cur.rating,
    your_rating: cur.your_rating,
    game_id: cur.game_id,
    reviewed_side: cur.reviewed_side,
    ply: cur.ply,
    win_drop: cur.win_drop,
    badge: cur.badge,
    game_url: cur.game_url,
    fen: cur.fen,
    solve_fen: cur.fen,
    played_uci: cur.played_uci,
    played_san: cur.played_san,
  };
  puzzleSource = puzzleData.source;
  puzzleDone = false;
  puzzleFailed = !!cur.failed;
  puzzleHinted = !!cur.hinted;
  puzzleMissRating = null;
  puzzleBusy = false;
  puzzleChatReset();
  puzzleShapes = mistakePlayedShape(puzzleData); // grey "you played" arrow for mistake puzzles
  puzzleSolveColor = cur.side_to_move || "white";
  orient = puzzleSolveColor;
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-prompt").hidden = false;
  $("pz-ghosts").hidden = false;
  $("pz-prompt-line").textContent = (puzzleSolveColor === "white" ? "White" : "Black") + " to move";
  $("pz-prompt-sub").textContent = isMine ? "Find a better move than you played" : "Find the best move";
  renderPuzzleBadge(isMine ? puzzleData : null);
  syncSourceUI();
  updatePuzzleStats(cur.your_rating, puzzleStreak);
  chess.load(cur.fen);
  renderPuzzleBoard(true);
  pzStatus(puzzleFailed ? "Resumed — keep trying, or press Show solution." : "Resumed your puzzle.");
  return true;
}

async function loadNextPuzzle(opts = {}) {
  // Remember the puzzle we're leaving so "‹ Previous" can bring it back — but only if it was
  // finished (a solve or a shown solution is worth revisiting; a skipped, unsolved one isn't, and
  // can't be re-rendered in review mode anyway, so leaving it just drops any stale snapshot).
  if (puzzleDone) capturePrevPuzzle();
  else clearPrevPuzzle();
  cancelAutoAdvance(); // supersede any pending auto-advance (also covers manual Next/Skip)
  const myGen = ++puzzleGen; // cancel any in-flight handler from the puzzle we're leaving
  puzzleBusy = true; // lock the board until the new puzzle is in place
  pzStatus("Loading…");
  const q = new URLSearchParams();
  const src = opts.source !== undefined ? opts.source : puzzleSource;
  if (src && src !== "lichess") q.set("source", src);
  const diff = opts.difficulty !== undefined ? opts.difficulty : puzzleDifficulty;
  if (diff) q.set("difficulty", diff);
  if (puzzleWeakness && src === "lichess") q.set("weakness", "1");
  let p;
  try {
    p = await fetch("/api/puzzle/next?" + q.toString()).then((r) => r.json());
  } catch (_) {
    if (myGen === puzzleGen) puzzleBusy = false;
    pzStatus("Couldn't load a puzzle.");
    return;
  }
  if (myGen !== puzzleGen) return; // superseded by another load
  if (!p || p.error || !p.fen) {
    puzzleBusy = false;
    pzStatus((p && p.error) || "No puzzles available.");
    return;
  }
  applyPuzzle(p);
}

function applyPuzzle(p) {
  const myGen = ++puzzleGen; // this is now the current puzzle; older handlers are superseded
  puzzleData = p;
  puzzleDone = false;
  puzzleFailed = false;
  puzzleHinted = false;
  puzzleMissRating = null;
  puzzleBusy = true;
  // Mistake puzzles: seed with a grey "you played" arrow of the move the player actually made in
  // that game, so they can see what to improve on (curated tactics have no such move to show).
  puzzleShapes = mistakePlayedShape(p);
  puzzleLastMove = null; // no carry-over highlight from the previous puzzle
  puzzleChatReset(); // a new puzzle drops any follow-up chat thread
  clearSolutionPlayback(); // a new puzzle supersedes the previous puzzle's solution step-through
  puzzleSolveColor = p.side_to_move || "white";
  orient = puzzleSolveColor;
  pzStatus("");

  // Reset the result/prompt cards.
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-prompt").hidden = false;
  $("pz-ghosts").hidden = false;
  $("pz-explain").disabled = false;
  $("pz-prompt-line").textContent = (puzzleSolveColor === "white" ? "White" : "Black") + " to move";
  const isMine = p.source === "your_games";
  $("pz-prompt-sub").textContent = isMine ? "Find a better move than you played" : "Find the best move";
  renderPuzzleBadge(isMine ? p : null);
  if (puzzleConfigCache) updatePuzzleStats(p.your_rating, puzzleConfigCache.streak);
  else updatePuzzleStats(p.your_rating, null);

  // Show the position BEFORE the setup move, then play it in with a short animation.
  chess.load(p.fen);
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: turnColor(),
    check: chess.inCheck(),
    lastMove: undefined, // clear any highlight carried over from the previous puzzle
    movable: { color: undefined, dests: new Map() },
    animation: { enabled: true },
  });
  drawArrows();

  setTimeout(() => {
    if (myGen !== puzzleGen) return; // a newer puzzle loaded while we waited
    const su = p.setup_move;
    if (su) {
      chess.move({ from: su.slice(0, 2), to: su.slice(2, 4), promotion: su.slice(4) || undefined });
      puzzleLastMove = [su.slice(0, 2), su.slice(2, 4)];
    }
    renderPuzzleBoard(true);
    puzzleBusy = false;
  }, 430);
  updatePrevPuzzleButton(); // show "‹ Previous" if we just auto-advanced off a finished puzzle
}

// Grey "you played" arrow for a mistake puzzle: the move the player actually made in that game, so
// they can see what to beat. Curated tactics (or a puzzle missing the data) get no arrow -> [].
function mistakePlayedShape(p) {
  if (!p || p.source !== "your_games" || !p.played_uci) return [];
  const u = p.played_uci;
  return [{ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: "grey" }];
}

// --- puzzle follow-up chat (revealed only after "Explain why") -----------------------------------

// Drop any follow-up chat thread + hide the panel (called on each new/resumed puzzle).
function puzzleChatReset() {
  puzzleChatSession = null;
  puzzleChatFen = null;
  const box = $("pz-chat-messages");
  if (box) box.innerHTML = "";
  const chat = $("pz-chat");
  if (chat) chat.hidden = true;
  const input = $("pz-chat-input");
  if (input) input.value = "";
}

function pzChatMsg(cls, text) {
  const d = document.createElement("div");
  d.className = `chat-msg ${cls}`;
  if (cls === "bot") d.innerHTML = renderMarkdown(text); // only the final answer is markdown
  else d.textContent = text;
  const box = $("pz-chat-messages");
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

async function sendPuzzleChat(ev) {
  ev.preventDefault();
  if (puzzleChatBusy || !puzzleData) return;
  cancelAutoAdvance(); // engaging with the coach means stay on this puzzle
  const input = $("pz-chat-input");
  const q = input.value.trim() || "Can you explain that a bit more?";
  input.value = "";
  pzChatMsg("user", q);
  puzzleChatBusy = true;
  $("pz-chat-send").disabled = true;
  const pending = pzChatMsg("bot pending", "Snowie is sniffing around (thinking)");
  // The move to reason about: for a mistake puzzle it's the move played in the game; for a tactic
  // it's whatever wrong move they tried (if any). The chat grounds on the puzzle's solve position.
  const moveInQuestion =
    puzzleData.source === "your_games" ? puzzleData.played_uci : puzzleData._yourMove || null;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        fen: puzzleChatFen || puzzleData.solve_fen || puzzleData.fen || null,
        last_move: moveInQuestion,
        session_id: puzzleChatSession,
        use_profile: personalizeHistory,
      }),
    }).then((r) => r.json());
    pending.remove();
    if (res.error) {
      pzChatMsg("bot err", res.error);
    } else {
      pzChatMsg("bot", res.answer || "(no answer)");
      if (res.session_id) puzzleChatSession = res.session_id;
    }
  } catch (e) {
    pending.remove();
    pzChatMsg("bot err", "Request failed: " + e);
  } finally {
    puzzleChatBusy = false;
    $("pz-chat-send").disabled = false;
    input.focus();
  }
}

async function onPuzzleMove(orig, dest) {
  if (puzzleDone || puzzleBusy) {
    renderPuzzleBoard(!puzzleDone); // snap the piece back; chessground already moved it visually
    return;
  }
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");
  const mv = chess.move({ from: orig, to: dest, promotion: promo });
  if (!mv) {
    renderPuzzleBoard(true);
    return;
  }
  puzzleLastMove = [orig, dest];
  puzzleBusy = true;
  const myGen = puzzleGen;
  renderPuzzleBoard(false);

  let res;
  try {
    res = await fetch("/api/puzzle/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: puzzleData.id, uci }),
    }).then((r) => r.json());
  } catch (_) {
    if (myGen !== puzzleGen) return;
    chess.undo();
    renderPuzzleBoard(true);
    puzzleBusy = false;
    pzStatus("Move check failed — try again.");
    return;
  }
  if (myGen !== puzzleGen) return; // a new puzzle loaded while the server was validating

  if (res.error) {
    chess.undo();
    renderPuzzleBoard(true);
    puzzleBusy = false;
    pzStatus(res.error);
    return;
  }

  if (!res.correct) {
    // Wrong move: flash red and take it back, but DON'T reveal the solution — let them try again.
    // The first miss already cost the rating server-side; reflect that, but keep the board live.
    blinkSquare(dest, "bad");
    shakeBoard();
    await sleep(440);
    if (myGen !== puzzleGen) return;
    chess.undo();
    puzzleLastMove = null;
    puzzleFailed = true;
    // Mistake puzzles: show the engine's refutation of the move they tried, as red arrows.
    if (res.source === "your_games" && (res.refutation_uci || []).length) {
      puzzleShapes = res.refutation_uci
        .slice(0, 3)
        .map((u) => ({ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: "red" }));
    }
    renderPuzzleBoard(true);
    if (res.rating) {
      puzzleMissRating = res.rating; // remember the applied loss for the final card
      updatePuzzleStats(res.rating.rating_after, res.rating.streak);
    }
    pzStatus(
      res.source === "your_games"
        ? "Still drops too much — try another move, or press Show solution."
        : res.rating && res.rating.rated
        ? "Not quite — that cost some rating. Try again, or press Show solution."
        : "Not quite — try again, or press Show solution."
    );
    puzzleBusy = false;
    return;
  }

  if (res.is_complete) {
    // Celebratory green for a clean solve, calmer amber when it took a miss — matches the verdict
    // colour so the board and the text tell the same story, and a little confetti off the piece.
    const kind = puzzleFailed ? "part" : "ok";
    blinkSquare(dest, kind);
    spawnConfetti(dest, kind);
    puzzleDone = true;
    renderPuzzleBoard(false);
    const outcome = puzzleFailed || puzzleHinted ? "solved_with_hints" : "solved_first_try";
    finishPuzzle(outcome, res.rating, null);
    puzzleBusy = false;
    return;
  }

  // Correct, more to come: play the forced reply, then hand the move back.
  await sleep(280);
  if (myGen !== puzzleGen) return;
  const reply = res.opponent_reply_uci;
  if (reply) {
    chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply.slice(4) || undefined });
    puzzleLastMove = [reply.slice(0, 2), reply.slice(2, 4)];
  }
  renderPuzzleBoard(true);
  puzzleBusy = false;
}

// --- puzzle storm (timed rush) -----------------------------------------------------------------

// Switch the rail between the per-puzzle "Solve" trainer and the "Storm" scoreboard. Ending an
// in-progress run when we leave storm is handled by the caller (setStormMode(false)).
function setStormMode(on) {
  if (on === stormShown) return;
  cancelAutoAdvance(); // a solve-trainer auto-advance must not fire into the storm sub-mode
  inStormReview = false; // any open post-run review ends when we switch sub-mode
  clearSolutionPlayback();
  $("pz-next").textContent = "Next puzzle →"; // undo the "‹ Back to results" repurposing
  stormShown = on;
  $("pz-mode-solve").classList.toggle("active", !on);
  $("pz-mode-storm").classList.toggle("active", on);
  $("pz-solve").hidden = on;
  $("pz-storm").hidden = !on;
  if (on) {
    // Leaving the Solve trainer: cancel any in-flight puzzle handler + clear its board state.
    puzzleGen++;
    puzzleData = null;
    puzzleShapes = [];
    puzzleDone = false;
    pzStatus("");
    renderStormBests();
    stormResetBoard();
    stormShowStart(false);
  } else {
    endStormRun({ abandon: true });
    // Back to the Solve trainer: reload a puzzle (we cleared puzzleData on entering storm).
    if (puzzleMode) loadNextPuzzle();
  }
}

// A calm, empty board with the run stats reset — the between-runs resting state.
function stormResetBoard() {
  chess.reset();
  puzzleLastMove = null;
  puzzleShapes = [];
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    lastMove: undefined,
    movable: { color: undefined, dests: new Map() },
    animation: { enabled: false },
  });
  drawArrows();
}

function fmtClock(secs) {
  secs = Math.max(0, Math.ceil(secs));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m + ":" + String(s).padStart(2, "0");
}

// Show the "start / play again" button + the intro/game-over message (vs the live clock).
function stormShowStart(gameOver, view) {
  const btn = $("pz-storm-start");
  btn.hidden = false;
  btn.textContent = gameOver ? "↻ Play again" : "⚡ Start storm";
  setStormSide(null); // no live puzzle on the start / game-over screen
  const clockEl = $("pz-storm-clock");
  const cfg = puzzleConfigCache || {};
  const dur = (view && view.duration) || cfg.storm_duration || 180;
  clockEl.classList.remove("low");
  const msg = $("pz-storm-msg");
  if (gameOver && view) {
    clockEl.textContent = "0:00";
    $("pz-storm-score").textContent = view.score;
    const nh = view.new_high ? ` <span class="pz-storm-nh">new best!</span>` : "";
    msg.innerHTML =
      `Time! You solved <b>${view.score}</b>` +
      (view.misses ? ` · ${view.misses} missed` : "") +
      (view.best_combo ? ` · best combo ${view.best_combo}` : "") +
      nh;
  } else {
    clockEl.textContent = fmtClock(dur);
    $("pz-storm-score").textContent = "0";
    $("pz-storm-combo").hidden = true;
    $("pz-storm-results").innerHTML = "";
    msg.innerHTML =
      "Solve as many as you can before the clock runs out. A combo earns bonus time; a wrong move costs time.";
    $("pz-storm-review").hidden = true; // no run to review on the intro screen
  }
  renderStormBests();
}

function renderStormBests() {
  const el = $("pz-storm-bests");
  if (!el) return;
  const cfg = puzzleConfigCache || {};
  const high = cfg.storm_high || 0;
  const combo = cfg.storm_best_combo || 0;
  el.innerHTML = high || combo
    ? `Best: <b>${high}</b> solved` + (combo ? ` · combo ${combo}` : "")
    : "";
}

async function startStorm() {
  const btn = $("pz-storm-start");
  btn.hidden = true;
  inStormReview = false; // a new run supersedes any previous run's review
  clearSolutionPlayback();
  $("pz-storm-review").hidden = true;
  $("pz-next").textContent = "Next puzzle →";
  pzStatus("");
  let view;
  try {
    view = await fetch("/api/puzzle/storm/start", { method: "POST" }).then((r) => r.json());
  } catch (_) {
    pzStatus("Couldn't start storm.");
    btn.hidden = false;
    return;
  }
  if (!view || view.error || !view.puzzle) {
    pzStatus((view && view.error) || "No puzzles available.");
    btn.hidden = false;
    return;
  }
  stormRunning = true;
  stormScore = 0;
  stormCombo = 0;
  applyStormState(view);
  startStormClock();
  applyStormPuzzle(view.puzzle);
}

function startStormClock() {
  stopStormClock();
  stormTimerId = setInterval(() => {
    const remaining = (stormDeadline - Date.now()) / 1000;
    const el = $("pz-storm-clock");
    el.textContent = fmtClock(remaining);
    el.classList.toggle("low", remaining <= 10);
    if (remaining <= 0) {
      stopStormClock();
      stormTimeUp();
    }
  }, 250);
}

function stopStormClock() {
  if (stormTimerId) {
    clearInterval(stormTimerId);
    stormTimerId = null;
  }
}

// The client clock hit zero: ask the server for the final (it finishes a run whose time is up).
async function stormTimeUp() {
  if (!stormRunning) return;
  let view;
  try {
    view = await fetch("/api/puzzle/storm/next").then((r) => r.json());
  } catch (_) {
    view = { ended: true, score: stormScore };
  }
  finishStorm(view);
}

// Update the live scoreboard from a server view (authoritative remaining/score/combo).
function applyStormState(view) {
  if (typeof view.remaining === "number") stormDeadline = Date.now() + view.remaining * 1000;
  if (typeof view.score === "number") {
    stormScore = view.score;
    $("pz-storm-score").textContent = stormScore;
  }
  if (typeof view.combo === "number") {
    stormCombo = view.combo;
    const el = $("pz-storm-combo");
    if (stormCombo >= 2) {
      el.hidden = false;
      el.textContent = "🔥 " + stormCombo + " combo";
    } else {
      el.hidden = true;
    }
  }
  if (view.results) {
    $("pz-storm-results").innerHTML = view.results
      .slice(-16)
      .map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`)
      .join("");
  }
  const clockEl = $("pz-storm-clock");
  const remaining = (stormDeadline - Date.now()) / 1000;
  clockEl.textContent = fmtClock(remaining);
  clockEl.classList.toggle("low", remaining <= 10);
}

// Load a storm puzzle onto the board. Storm serves the solve position directly (no setup-move
// animation), so we just render it and hand the move to the solver.
function applyStormPuzzle(pz) {
  const myGen = ++stormGen;
  stormPuzzle = pz;
  stormBusy = false;
  puzzleShapes = [];
  puzzleLastMove = null;
  puzzleSolveColor = pz.side_to_move || "white";
  orient = puzzleSolveColor;
  chess.load(pz.fen);
  renderPuzzleBoard(true);
  setStormSide(puzzleSolveColor);
  return myGen;
}

// Show which side the solver plays for the current storm puzzle (the board also flips to it, but
// the explicit label removes any doubt in a fast-paced run). Hidden when no puzzle is on the board.
function setStormSide(color) {
  const el = $("pz-storm-side");
  if (!el) return;
  if (!color) {
    el.hidden = true;
    return;
  }
  const white = color === "white";
  el.innerHTML = `<span class="pz-storm-side-dot ${white ? "w" : "b"}"></span>You play ${white ? "White" : "Black"}`;
  el.hidden = false;
}

async function stormServeNext() {
  let view;
  try {
    view = await fetch("/api/puzzle/storm/next").then((r) => r.json());
  } catch (_) {
    pzStatus("Couldn't load the next puzzle.");
    return;
  }
  if (!stormRunning) return;
  if (view.ended || !view.puzzle) {
    finishStorm(view);
    return;
  }
  applyStormState(view);
  applyStormPuzzle(view.puzzle);
}

async function onStormMove(orig, dest) {
  if (stormBusy || !stormRunning) {
    renderPuzzleBoard(!stormBusy);
    return;
  }
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");
  const mv = chess.move({ from: orig, to: dest, promotion: promo });
  if (!mv) {
    renderPuzzleBoard(true);
    return;
  }
  puzzleLastMove = [orig, dest];
  stormBusy = true;
  const myGen = stormGen;
  renderPuzzleBoard(false);

  let res;
  try {
    res = await fetch("/api/puzzle/storm/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uci }),
    }).then((r) => r.json());
  } catch (_) {
    if (myGen !== stormGen) return;
    chess.undo();
    renderPuzzleBoard(true);
    stormBusy = false;
    return;
  }
  if (myGen !== stormGen || !stormRunning) return;

  if (res.error || res.ended) {
    finishStorm(res);
    return;
  }
  applyStormState(res);

  if (!res.correct) {
    // Wrong move: red flash, undo, and move straight on to the next puzzle (storm never retries).
    blinkSquare(dest, "bad");
    shakeBoard();
    await sleep(360);
    if (myGen !== stormGen || !stormRunning) return;
    chess.undo();
    puzzleLastMove = null;
    stormBusy = false;
    stormServeNext();
    return;
  }

  if (res.puzzle_done && res.solved) {
    // Storm is a race, so a clean solve jumps straight to the next puzzle — no green-tile blink or
    // pause (they'd bleed onto the puzzle that's already replaced them, looking janky). The clock
    // bonus still floats over the clock, which is off-board and doesn't delay anything.
    if (res.time_bonus) floatBonus("+" + Math.round(res.time_bonus) + "s");
    stormBusy = false;
    stormServeNext();
    return;
  }

  // Correct but more to come: play the forced reply, keep the same puzzle.
  await sleep(220);
  if (myGen !== stormGen || !stormRunning) return;
  const reply = res.opponent_reply_uci;
  if (reply) {
    chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply.slice(4) || undefined });
    puzzleLastMove = [reply.slice(0, 2), reply.slice(2, 4)];
  }
  renderPuzzleBoard(true);
  stormBusy = false;
}

// A brief floating "+5s" over the clock when a combo grants bonus time.
function floatBonus(text) {
  const clockEl = $("pz-storm-clock");
  if (!clockEl) return;
  const b = document.createElement("span");
  b.className = "pz-storm-bonus";
  b.textContent = text;
  clockEl.appendChild(b);
  setTimeout(() => b.remove(), 900);
}

// The run ended (time up or server-finished). Show the game-over card + refresh personal bests.
function finishStorm(view) {
  stormRunning = false;
  stormBusy = false;
  stopStormClock();
  stormResetBoard();
  if (view) {
    if (view.new_high || typeof view.high === "number") {
      if (puzzleConfigCache) {
        puzzleConfigCache.storm_high = Math.max(puzzleConfigCache.storm_high || 0, view.high || view.score || 0);
        if (view.best_combo) {
          puzzleConfigCache.storm_best_combo = Math.max(puzzleConfigCache.storm_best_combo || 0, view.best_combo);
        }
      }
    }
    stormShowStart(true, view);
  } else {
    stormShowStart(false);
  }
  // Turn the finished rush into study time: list every puzzle for AI review. The finish view
  // usually carries the log inline; fall back to a fetch (refresh-safe while the run lingers).
  if (view && Array.isArray(view.log)) {
    populateStormReview(view.log);
  } else {
    fetch("/api/puzzle/storm/review")
      .then((r) => r.json())
      .then((d) => populateStormReview((d && d.log) || []))
      .catch(() => populateStormReview([]));
  }
}

// --- post-run review: study the puzzles from a finished storm with the AI coach -----------------

// Show the review list on the game-over card (misses pinned first). `has_llm` gates the AI bits.
function populateStormReview(log) {
  stormReviewEntries = Array.isArray(log) ? log : [];
  const wrap = $("pz-storm-review");
  const list = $("pz-storm-review-list");
  const summaryBtn = $("pz-storm-summary-btn");
  $("pz-storm-summary-out").hidden = true;
  $("pz-storm-summary-out").innerHTML = "";
  if (!stormReviewEntries.length) {
    wrap.hidden = true;
    return;
  }
  const hasLlm = !!(puzzleConfigCache && puzzleConfigCache.has_llm);
  summaryBtn.hidden = !hasLlm;
  summaryBtn.disabled = false;
  // Misses first (that's where the learning is), then solves; stable within each group.
  const rows = stormReviewEntries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => (a.e.solved === b.e.solved ? a.i - b.i : a.e.solved ? 1 : -1));
  list.innerHTML = "";
  rows.forEach(({ e }) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "pz-storm-review-row " + (e.solved ? "ok" : "bad");
    const label = motifThemes(e.themes).slice(0, 2).join(", ") || "tactic";
    row.innerHTML =
      `<span class="pz-rr-mark">${e.solved ? "✓" : "✗"}</span>` +
      `<span class="pz-rr-theme">${escapeHtml(label)}</span>` +
      `<span class="pz-rr-rating">${e.rating || ""}</span>`;
    row.addEventListener("click", () => openStormReview(e));
    list.appendChild(row);
  });
  wrap.hidden = false;
}

// Open one finished storm puzzle on the board for review, reusing the Solve rail's result card so
// the existing "Explain why" + follow-up chat work unchanged (they ground on puzzleData.id + fen).
function openStormReview(entry) {
  inStormReview = true;
  ++puzzleGen; // become the current puzzle; supersede any in-flight Solve-trainer handler
  puzzleBusy = false;
  puzzleDone = true;
  puzzleSource = "lichess";
  puzzleData = {
    id: entry.id,
    fen: entry.fen,
    solve_fen: entry.fen,
    themes: entry.themes || [],
    source: "lichess",
    side_to_move: entry.side_to_move || "white",
    _outcome: entry.solved ? "solved_first_try" : "failed",
    _yourMove: entry.your_move || null,
  };
  puzzleFailed = !entry.solved;
  puzzleHinted = false;
  puzzleMissRating = null;
  puzzleChatReset();
  puzzleSolveColor = entry.side_to_move || "white";
  orient = puzzleSolveColor;
  // A miss shows the move the solver played, in red, so the review has a concrete starting point.
  puzzleShapes = !entry.solved && entry.your_move
    ? [{ orig: entry.your_move.slice(0, 2), dest: entry.your_move.slice(2, 4), brush: "red" }]
    : [];

  // Swap the storm scoreboard for the Solve result card (we stay in the storm sub-mode).
  $("pz-storm").hidden = true;
  $("pz-solve").hidden = false;
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-progress").hidden = true;
  $("pz-statcard").hidden = true;
  $("pz-source-badge").hidden = true;
  $("pz-result").hidden = false;

  const verdict = $("pz-verdict");
  verdict.className = "pz-verdict " + (entry.solved ? "ok" : "bad");
  verdict.textContent = entry.solved ? "✓ You solved this" : "✗ You missed this";
  const themes = motifThemes(entry.themes);
  $("pz-theme").innerHTML = themes.length ? "Theme: <b>" + escapeHtml(themes.slice(0, 3).join(", ")) + "</b>" : "";
  $("pz-replay").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-explain").hidden = !(puzzleConfigCache && puzzleConfigCache.has_llm);
  $("pz-explain").disabled = false;
  $("pz-chat").hidden = true;
  $("pz-chat-messages").innerHTML = "";
  $("pz-next").textContent = "‹ Back to results"; // repurposed while reviewing (routed in the handler)

  chess.load(entry.fen);
  renderPuzzleBoard(false); // locked review board; draws the played-move arrow at the solve position
  pzStatus("Reviewing a storm puzzle — step through the solution below the board, or press Explain.");
  // Fetch + animate the solution, then leave the step-nav for scrubbing (storm review starts at the
  // solve position and plays forward).
  startSolutionPlayback({
    id: entry.id, yourMove: entry.your_move, solved: entry.solved, animate: true,
  });
}

// Fetch a curated puzzle's solution line and set up the step-through nav below the board. Shared by
// Storm review and the normal Solve trainer. `animate` plays the line forward once (storm review);
// otherwise it rests at the final position (the Solve board has already played the moves out).
// Cancels cleanly (via `solutionGen`) if the user leaves or loads another puzzle mid-fetch.
async function startSolutionPlayback({ id, yourMove = null, solved = true, animate = false }) {
  const myGen = ++solutionGen;
  solutionPlay = null;
  $("pz-review-nav").hidden = true;
  if (!id) return;
  let sol;
  try {
    sol = await fetch("/api/puzzle/solution?id=" + encodeURIComponent(id)).then((r) => r.json());
  } catch (_) {
    return; // no solution available (e.g. a mistake puzzle) -> the card still works, just no playback
  }
  if (myGen !== solutionGen) return;
  const base = (sol && sol.solve_fen) || null;
  const ucis = (sol && sol.solution_uci) || [];
  const sans = (sol && sol.solution_san) || [];
  if (!base || !ucis.length) return;
  // Build the board position at each step of the solution, starting from the solve position.
  const fens = [base];
  const lastMoves = [null];
  const tmp = new Chess(base);
  for (const u of ucis) {
    const mv = tmp.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u.slice(4) || undefined });
    if (!mv) break; // defensive: a bad line just stops the playback where it is
    fens.push(tmp.fen());
    lastMoves.push([u.slice(0, 2), u.slice(2, 4)]);
  }
  solutionPlay = {
    fens, ucis: ucis.slice(0, fens.length - 1), sans, lastMoves,
    idx: 0, yourMove, solved,
  };
  if (animate) {
    solutionGotoStep(0);
    for (let i = 1; i < fens.length; i++) {
      await sleep(650);
      if (myGen !== solutionGen) return;
      solutionGotoStep(i);
    }
  } else {
    // Rest at the final position (the Solve board is already there); the user scrubs backward.
    solutionGotoStep(fens.length - 1);
  }
}

// Show a specific step of the solution on the board (idx 0 = the solve position).
function solutionGotoStep(i) {
  const p = solutionPlay;
  if (!p) return;
  p.idx = Math.max(0, Math.min(p.fens.length - 1, i));
  chess.load(p.fens[p.idx]);
  puzzleLastMove = p.lastMoves[p.idx] || null;
  if (p.idx === 0) {
    // At the start, a miss shows the move the solver actually played (red); a clean solve shows nothing.
    puzzleShapes = !p.solved && p.yourMove
      ? [{ orig: p.yourMove.slice(0, 2), dest: p.yourMove.slice(2, 4), brush: "red" }]
      : [];
  } else {
    const u = p.ucis[p.idx - 1];
    puzzleShapes = [{ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: "green" }];
  }
  renderPuzzleBoard(false);
  updateSolutionNav();
}

// Manual scrub: cancel any running auto-animation, then step one move.
function solutionStep(delta) {
  if (!solutionPlay) return;
  ++solutionGen;
  solutionGotoStep(solutionPlay.idx + delta);
}

function updateSolutionNav() {
  const nav = $("pz-review-nav");
  const p = solutionPlay;
  if (!p) {
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  $("pz-review-prev").disabled = p.idx <= 0;
  $("pz-review-next").disabled = p.idx >= p.fens.length - 1;
  const total = p.fens.length - 1;
  $("pz-review-label").textContent =
    p.idx === 0 ? "Start position" : `Move ${p.idx} / ${total}: ${p.sans[p.idx - 1] || ""}`;
}

// Tear down any active solution playback + hide the step nav (new puzzle, leaving, mode switch).
function clearSolutionPlayback() {
  ++solutionGen;
  solutionPlay = null;
  const nav = $("pz-review-nav");
  if (nav) nav.hidden = true;
}

// Leave the single-puzzle review and return to the game-over card + review list.
function closeStormReview() {
  inStormReview = false;
  clearSolutionPlayback();
  puzzleChatReset();
  $("pz-next").textContent = "Next puzzle →";
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-solve").hidden = true;
  $("pz-storm").hidden = false;
}

async function summarizeStormRun() {
  const btn = $("pz-storm-summary-btn");
  const out = $("pz-storm-summary-out");
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = '<p class="muted">Snowie is reviewing your run (thinking)</p>';
  let res;
  try {
    res = await fetch("/api/puzzle/storm/summary", { method: "POST" }).then((r) => r.json());
  } catch (_) {
    out.innerHTML = '<p class="muted">Summary failed — try again.</p>';
    btn.disabled = false;
    return;
  }
  out.innerHTML = renderMarkdown(res.error || res.answer || "");
  btn.disabled = false;
}

// End the run when leaving storm mode entirely (tell the server so the highscore is banked).
function endStormRun({ abandon } = {}) {
  stopStormClock();
  if (stormRunning && abandon) {
    fetch("/api/puzzle/storm/end", { method: "POST" }).catch(() => {});
  }
  stormRunning = false;
  stormBusy = false;
  stormPuzzle = null;
}

// The amber "From your game" badge for a mistake puzzle (null hides it for curated tactics).
function renderPuzzleBadge(p) {
  const el = $("pz-source-badge");
  if (!p) { el.hidden = true; el.textContent = ""; return; }
  const b = p.badge || {};
  const opp = p.reviewed_side === "white" ? b.black : b.white;
  const bits = ["From your game"];
  if (opp) bits.push("vs " + opp);
  if (b.speed && b.speed !== "unknown") bits.push(b.speed);
  if (b.date) bits.push(b.date);
  // How much win% the original move threw away — small context on the cost of the miss.
  if (p.win_drop != null && p.win_drop >= 1) bits.push("cost −" + Math.round(p.win_drop) + "% win chance");
  el.textContent = bits.join(" · ");
  el.hidden = false;
}

// "Replay in full game": leave puzzle mode and open the source game at the mistake position. Needs
// the game's PGN, which the history list carries; falls back to the external game link if absent.
async function replayMistake(p) {
  let row = null;
  try {
    const rows = await fetch("/api/history").then((r) => r.json());
    row = (rows || []).find(
      (r) => r.game_id === p.game_id && r.reviewed_side === p.reviewed_side && r.has_pgn && r.pgn
    );
  } catch (_) {}
  if (row) {
    pendingGotoPly = p.ply != null ? p.ply - 1 : null; // land on the position BEFORE the played move
    await setPuzzleMode(false);
    openGame(row.pgn, row.reviewed_side);
  } else if (p.game_url) {
    window.open(p.game_url, "_blank", "noopener");
  }
}

// Cancel a pending auto-advance (manual nav, leaving puzzle mode, or the user chose to Explain/chat
// about this puzzle — in which case yanking them to the next one would be rude).
function cancelAutoAdvance() {
  if (puzzleAdvanceTimer !== null) {
    clearTimeout(puzzleAdvanceTimer);
    puzzleAdvanceTimer = null;
  }
}

// Show/hide the "‹ Previous" ghost button (only meaningful while a restorable snapshot exists).
function updatePrevPuzzleButton() {
  const btn = $("pz-prev");
  if (btn) btn.hidden = !prevPuzzleSnapshot;
}

function clearPrevPuzzle() {
  prevPuzzleSnapshot = null;
  updatePrevPuzzleButton();
}

// Snapshot the current FINISHED puzzle (board + fully-rendered result card, incl. any Explain text
// and follow-up chat) so restorePrevPuzzle can bring it back verbatim for review. Called as we leave
// a done puzzle. Restoring is review-only (board stays locked), so we don't need server puzzle state;
// Explain/chat re-ground on puzzleData.id + the stored fen.
function capturePrevPuzzle() {
  if (!puzzleDone || !puzzleData) return;
  prevPuzzleSnapshot = {
    data: puzzleData,
    shapes: puzzleShapes,
    failed: puzzleFailed,
    hinted: puzzleHinted,
    solveColor: puzzleSolveColor,
    missRating: puzzleMissRating,
    fen: chess.fen(),
    lastMove: puzzleLastMove,
    chatSession: puzzleChatSession,
    chatFen: puzzleChatFen,
    // Rendered result-card DOM so the verdict / theme / explanation / chat all survive intact.
    verdictText: $("pz-verdict").textContent,
    verdictClass: $("pz-verdict").className,
    themeHTML: $("pz-theme").innerHTML,
    replayHidden: $("pz-replay").hidden,
    explainHidden: $("pz-explain").hidden,
    explainDisabled: $("pz-explain").disabled,
    explainOutHTML: $("pz-explain-out").innerHTML,
    explainOutHidden: $("pz-explain-out").hidden,
    chatHTML: $("pz-chat-messages").innerHTML,
    chatHidden: $("pz-chat").hidden,
  };
  updatePrevPuzzleButton();
}

// Bring back the last finished puzzle in review mode: the board (locked) + its result card, so the
// player can still press Explain / ask a follow-up about a puzzle auto-advance already moved past.
function restorePrevPuzzle() {
  const s = prevPuzzleSnapshot;
  if (!s) return;
  cancelAutoAdvance();
  clearSolutionPlayback(); // restored review shows the snapshot board, not a live step-through
  ++puzzleGen; // become the current puzzle; supersede any in-flight handler
  prevPuzzleSnapshot = null; // single-level back; a later Next re-captures this one
  puzzleBusy = false;
  puzzleDone = true;
  puzzleData = s.data;
  puzzleShapes = s.shapes;
  puzzleFailed = s.failed;
  puzzleHinted = s.hinted;
  puzzleSolveColor = s.solveColor;
  puzzleMissRating = s.missRating;
  puzzleLastMove = s.lastMove;
  puzzleChatSession = s.chatSession;
  puzzleChatFen = s.chatFen;
  orient = puzzleSolveColor;
  chess.load(s.fen);

  // Restore the result card verbatim; hide the prompt/ghosts (it's a finished puzzle).
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-result").hidden = false;
  $("pz-verdict").textContent = s.verdictText;
  $("pz-verdict").className = s.verdictClass;
  $("pz-theme").innerHTML = s.themeHTML;
  $("pz-replay").hidden = s.replayHidden;
  if (!s.replayHidden) $("pz-replay").onclick = (e) => { e.preventDefault(); replayMistake(puzzleData); };
  $("pz-explain").hidden = s.explainHidden;
  $("pz-explain").disabled = s.explainDisabled;
  $("pz-explain-out").hidden = s.explainOutHidden;
  $("pz-explain-out").innerHTML = s.explainOutHTML;
  $("pz-chat").hidden = s.chatHidden;
  $("pz-chat-messages").innerHTML = s.chatHTML;

  renderPuzzleBoard(false); // locked (done): draws the stored solution/refutation arrows
  pzStatus("Reviewing your previous puzzle — press Explain or ask below, then Next to continue.");
  updatePrevPuzzleButton();
}

// After a solve, queue the next puzzle — but only once the solve animation has had time to play out,
// per the §7A "let the green pulse be the reward" feel. Delay comfortably exceeds the confetti/ripple
// (~1.1s); with animations off, a shorter beat so the result text is still readable first.
function scheduleAutoAdvance() {
  cancelAutoAdvance();
  if (!puzzleAutoAdvance) return;
  const myGen = puzzleGen;
  const delay = puzzleAnimations ? 1900 : 1000;
  puzzleAdvanceTimer = setTimeout(() => {
    puzzleAdvanceTimer = null;
    if (!puzzleMode || stormShown) return; // left the trainer while waiting
    if (myGen !== puzzleGen) return; // a newer puzzle/nav superseded this one
    if (!puzzleDone) return; // defensive: only advance from a finished puzzle
    loadNextPuzzle();
  }, delay);
}

function finishPuzzle(outcome, ratingSummary, yourMove) {
  puzzleDone = true;
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-result").hidden = false;

  const solved = outcome !== "failed";
  const clean = solved && !puzzleFailed; // green only when solved with no wrong move
  const isMine = puzzleData.source === "your_games";
  const verdict = $("pz-verdict");
  // Three states, so a correct final attempt never wears the same red as a genuine failure:
  //   green  = solved first try, orange = solved after a miss (still a win!), red = solution shown.
  verdict.className = "pz-verdict " + (!solved ? "bad" : clean ? "ok" : "part");
  let head;
  if (isMine) {
    // Coaching tone: these are the player's own past positions, not pass/fail tactics, and unrated.
    head = solved
      ? clean ? "✓ Better than your game move!" : "✓ Better move found (after a try)"
      : "Solution shown";
  } else {
    head = solved ? (clean ? "✓ Solved!" : "✓ Solved (after a miss)") : "Solution shown";
    // The rating may have been applied on the FIRST wrong move (Lichess-style), so the completion
    // itself returns no fresh summary. Fall back to that stored miss summary so a solve-after-miss
    // still shows the rating already moved — otherwise it looks like the miss cost nothing.
    const applied =
      ratingSummary && ratingSummary.rated
        ? ratingSummary
        : puzzleMissRating && puzzleMissRating.rated
        ? puzzleMissRating
        : null;
    if (applied) {
      const sign = applied.delta >= 0 ? "+" : "";
      head += `  ${applied.rating_before} → ${applied.rating_after} (${sign}${applied.delta})`;
      updatePuzzleStats(applied.rating_after, applied.streak);
    } else if (ratingSummary || puzzleMissRating) {
      const su = ratingSummary || puzzleMissRating;
      head += "  (unrated)";
      updatePuzzleStats(su.rating_after, su.streak);
    }
  }
  verdict.textContent = head;

  // Session pips track curated-tactic outcomes only (mistake puzzles are unrated practice).
  if (!isMine) recordPuzzleResult(clean);

  if (isMine) {
    $("pz-theme").innerHTML = "";
    const replay = $("pz-replay");
    replay.hidden = false;
    replay.onclick = (e) => { e.preventDefault(); replayMistake(puzzleData); };
  } else {
    const themes = (puzzleData.themes || []).filter((t) => !/^mateIn\d|^oneMove$|^short$|^long$/.test(t));
    $("pz-theme").innerHTML = themes.length ? "Theme: <b>" + themes.slice(0, 3).join(", ") + "</b>" : "";
    $("pz-replay").hidden = true;
  }

  // Stash for the Explain call.
  puzzleData._outcome = outcome;
  puzzleData._yourMove = yourMove || null;
  $("pz-explain").hidden = !(puzzleConfigCache && puzzleConfigCache.has_llm);
  loadPuzzleStatCard();

  // Let the player walk the solution move-by-move with the step-nav below the board. Curated tactics
  // only — "from your games" puzzles have no forced line to replay (they use "replay in full game").
  // The board is already at the end of the line, so we rest there and let them scrub backward.
  if (!isMine) {
    startSolutionPlayback({ id: puzzleData.id, solved, animate: false });
  } else {
    clearSolutionPlayback();
  }

  // Flow-state grinding: on a solve, roll straight into the next puzzle after the animation plays
  // (opt-out in Settings). Never on "Show solution" (outcome "failed") — that's a study moment.
  if (solved) scheduleAutoAdvance();
}

// Weakest-theme stats card, from /api/puzzle/state (quiet; hidden when there's nothing to show).
async function loadPuzzleStatCard() {
  const card = $("pz-statcard");
  if (!card) return;
  let st;
  try {
    st = await fetch("/api/puzzle/state").then((r) => r.json());
  } catch (_) {
    return;
  }
  // Daily streak + the discrete rating curve ride on the same state fetch.
  renderDailyStreak(st && st.daily_streak, st && st.best_daily_streak);
  renderRatingCurve(st && st.history);
  // The server already filters to trainable motifs (no metadata tags like master/oneMove) and
  // ranks them worst-first, so the card just renders what it's given.
  const weak = (st && st.weak_themes) || [];
  if (!weak.length) { card.hidden = true; card.innerHTML = ""; return; }
  // Collapsed by default to keep the rail quiet: just a "Work on" toggle. Click reveals the themes
  // (the open/closed choice is remembered).
  const open = lsGet("pzWorkOnOpen") === "1";
  card.innerHTML =
    `<button type="button" class="pz-statcard-toggle" aria-expanded="${open}">` +
      `Work on <span class="pz-statcard-caret">${open ? "▾" : "▸"}</span></button>` +
    `<div class="pz-statcard-body"${open ? "" : " hidden"}>` +
      weak
        .map((x) => `<span class="pz-weak-theme">${x.theme} <b>${Math.round(x.rate * 100)}%</b></span>`)
        .join("") +
    `</div>`;
  card.hidden = false;
  card.querySelector(".pz-statcard-toggle").addEventListener("click", () => {
    const body = card.querySelector(".pz-statcard-body");
    const nowOpen = body.hidden; // about to open
    body.hidden = !nowOpen;
    card.querySelector(".pz-statcard-caret").textContent = nowOpen ? "▾" : "▸";
    card.querySelector(".pz-statcard-toggle").setAttribute("aria-expanded", String(nowOpen));
    lsSet("pzWorkOnOpen", nowOpen ? "1" : "0");
  });
}

// "Show solution": reveal + play out the remaining solution line, then end the puzzle.
async function puzzleShowSolution() {
  if (!puzzleData || puzzleDone || puzzleBusy) return;
  const myGen = puzzleGen;
  puzzleBusy = true;
  renderPuzzleBoard(false);
  let res;
  try {
    res = await fetch("/api/puzzle/giveup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: puzzleData.id }),
    }).then((r) => r.json());
  } catch (_) {
    if (myGen !== puzzleGen) return;
    puzzleBusy = false;
    renderPuzzleBoard(true);
    return;
  }
  if (myGen !== puzzleGen) return;
  const line = (res && res.solution_uci) || [];
  // Play the solution out move by move so the user sees the idea.
  for (const uci of line) {
    chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci.slice(4) || undefined });
    puzzleLastMove = [uci.slice(0, 2), uci.slice(2, 4)];
    renderPuzzleBoard(false);
    await sleep(480);
    if (myGen !== puzzleGen) return;
  }
  puzzleBusy = false;
  finishPuzzle("failed", null, null);
}

async function puzzleExplain() {
  cancelAutoAdvance(); // the user wants to study this one — don't yank them to the next puzzle
  const btn = $("pz-explain");
  const out = $("pz-explain-out");
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = '<p class="muted">Snowie is sniffing around (thinking)</p>';
  let res;
  try {
    res = await fetch("/api/puzzle/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: puzzleData.id,
        outcome: puzzleData._outcome,
        your_move: puzzleData._yourMove,
      }),
    }).then((r) => r.json());
  } catch (_) {
    out.innerHTML = '<p class="muted">Explanation failed — try again.</p>';
    btn.disabled = false;
    return;
  }
  if (res.error) {
    out.innerHTML = renderMarkdown(res.error);
  } else {
    out.innerHTML = renderMarkdown(res.answer || "");
    // Reveal the follow-up chat, threaded onto this explanation so questions have its context.
    puzzleChatSession = res.session_id || null;
    puzzleChatFen = res.chat_fen || puzzleData.solve_fen || puzzleData.fen || null;
    $("pz-chat").hidden = false;
  }
  btn.disabled = false;
}

async function puzzleHint() {
  if (!puzzleData || puzzleDone) return;
  let res;
  try {
    res = await fetch("/api/puzzle/hint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: puzzleData.id }),
    }).then((r) => r.json());
  } catch (_) {
    return;
  }
  if (res && res.from_square) {
    puzzleHinted = true;
    puzzleShapes = [{ orig: res.from_square, brush: "blue" }];
    drawArrows();
    pzStatus("Hint: move the piece on " + res.from_square + " (this attempt is now unrated).");
  }
}

// Reflect the current source in the segmented control + show difficulty/weakness only for tactics.
function syncSourceUI() {
  const mine = puzzleSource === "your_games";
  $("pz-src-tactics").classList.toggle("active", !mine);
  $("pz-src-mine").classList.toggle("active", mine);
  $("pz-controls").style.display = mine ? "none" : "";
}

function setPuzzleSource(src) {
  if (src === puzzleSource) return;
  if (src === "your_games" && !(puzzleConfigCache && puzzleConfigCache.has_engine)) return;
  puzzleSource = src;
  lsSet(PZ_SOURCE_KEY, src); // remember the sub-tab across reloads + full app restarts
  syncSourceUI();
  loadNextPuzzle();
}

function setPuzzleDifficulty(which) {
  puzzleDifficulty = puzzleDifficulty === which ? null : which; // click again to clear
  $("pz-easier").classList.toggle("active", puzzleDifficulty === "easier");
  $("pz-harder").classList.toggle("active", puzzleDifficulty === "harder");
  loadNextPuzzle();
}

function init() {
  // On small screens the Games panel is a drawer that overlays the board, so start it closed.
  closeHistoryDrawer();
  ground = Chessground($("board"), {
    fen: chess.fen(),
    orientation: orient,
    movable: { free: false, color: "white", dests: computeDests(), showDests: true },
    events: { move: onUserMove },
    drawable: { enabled: true },
  });
  // Add a neutral grey brush for the "move you played" arrow (it marks what you did, not a
  // judgement, so grey reads more intuitively than blue). Keeps all default brushes intact.
  ground.state.drawable.brushes.grey = {
    key: "grey",
    color: "#7c7c7c",
    opacity: 0.9,
    lineWidth: 10,
  };

  // Board-resize handle: apply any saved board size now that Chessground exists, then wire drag.
  restoreBoardSize();
  initBoardResizer();
  observeBoardLayout();
  positionResizer();

  $("back").addEventListener("click", stepBack);
  $("fwd").addEventListener("click", stepForward);
  $("prev-mistake").addEventListener("click", () => currentMistake > 0 && selectMistake(currentMistake - 1));
  $("next-mistake").addEventListener(
    "click",
    () => currentMistake < mistakes.length - 1 && selectMistake(currentMistake + 1)
  );
  $("reset").addEventListener("click", returnToReview);
  $("flip-review").addEventListener("click", reviewOtherSide);
  $("best-toggle").addEventListener("change", (e) => {
    bestArrowOn = e.target.checked;
    refreshBestMoves(); // starts the live search when on, clears arrows when off
  });
  $("threat-toggle").addEventListener("change", (e) => {
    threatArrowOn = e.target.checked;
    refreshBestMoves();
  });
  $("graph").addEventListener("click", onGraphClick);
  $("movelist-expand").addEventListener("click", toggleMoveList);
  $("coach-ai-btn").addEventListener("click", fetchCoachAI);
  $("coach-toggle").addEventListener("click", () => {
    quickSummaryUserExpanded = !quickSummaryUserExpanded;
    syncQuickSummary();
  });
  $("chat-form").addEventListener("submit", sendChat);

  // Games panel: collapse toggle, mode slider, lichess lookup.
  $("history-toggle").addEventListener("click", toggleHistory);
  $("history-collapse").addEventListener("click", toggleHistory);
  $("mode-normal").addEventListener("click", () => setMode("normal"));
  $("mode-lichess").addEventListener("click", () => setMode("lichess"));
  $("mode-chesscom").addEventListener("click", () => setMode("chesscom"));
  $("mode-paste").addEventListener("click", () => setMode("paste"));
  // Paste-PGN: analyze any PGN (e.g. Chess.com), single or multi-game, without the Lichess fetch.
  $("paste-upload").addEventListener("click", () => $("paste-file").click());
  $("paste-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    loadPgnFile(file, false); // picker just loads into the textarea; user presses Analyze
    e.target.value = ""; // allow re-selecting the same file later
  });
  initPgnDrop(); // drag a .pgn onto the Games panel to load + analyze it
  $("paste-pgn").addEventListener("input", updatePasteHint);
  $("paste-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const pgn = $("paste-pgn").value.trim();
    if (!pgn) {
      $("history-status").textContent = "Paste or upload a PGN first.";
      return;
    }
    startPasteAnalysis(pgn, $("paste-side").value || "auto", ($("paste-username").value || "").trim());
  });
  $("lichess-form").addEventListener("submit", (e) => {
    e.preventDefault();
    lichessCount = LICHESS_PAGE; // a new lookup starts fresh
    loadLichess($("lichess-user").value.trim());
  });
  $("chesscom-form").addEventListener("submit", (e) => {
    e.preventDefault();
    noteChesscomRefresh();
    chesscomCount = LICHESS_PAGE;
    loadChesscom($("chesscom-user").value.trim());
  });
  // Manual "Sync new games": fetch + analyze anything history hasn't seen for the configured user.
  $("chesscom-sync").addEventListener("click", () => {
    if (!chesscomUsername) {
      $("history-status").textContent = "Set your chess.com username in ⚙ Settings first.";
      return;
    }
    noteChesscomRefresh();
    syncChesscom(false);
  });
  // Hint shortcut: jump straight to the Paste PGN tab.
  $("chesscom-hint-paste").addEventListener("click", () => setMode("paste"));
  // "Set as my account": make the looked-up Lichess handle your unified identity.
  $("set-as-me").addEventListener("click", async () => {
    const u = ($("lichess-user").value.trim() || lichessUser || "").trim();
    if (!u) return;
    await saveUsername(u);
    reflectSetAsMe((u || "").toLowerCase());
    loadHistory(undefined, { resetPaging: true }); // "My games" now resolves to this account
  });
  // First-run prompt (no configured account): two fields, fill in either. Save both, then sync +
  // open the user's latest game (chess.com sync first, else the Lichess/chess.com autoload).
  $("firstrun-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const lichess = $("firstrun-user").value.trim();
    const chesscom = $("firstrun-chesscom-user").value.trim();
    if (!lichess && !chesscom) return;
    await saveIdentity(lichess, chesscom);
    $("firstrun").hidden = true;
    maybeAutoload();
  });
  // Insights panel: re-aggregate when the time period changes.
  $("insights-period").addEventListener("change", (e) => {
    insightsDays = Number(e.target.value) || 0;
    loadInsights();
  });
  // Puzzle mode: top-level Analyze/Puzzles switch + the rail controls.
  $("mode-analyze").addEventListener("click", () => setPuzzleMode(false));
  $("mode-puzzles").addEventListener("click", () => setPuzzleMode(true));
  $("pz-next").addEventListener("click", () => (inStormReview ? closeStormReview() : loadNextPuzzle()));
  $("pz-skip").addEventListener("click", () => loadNextPuzzle());
  $("pz-prev").addEventListener("click", restorePrevPuzzle);
  $("pz-hint").addEventListener("click", puzzleHint);
  $("pz-solution").addEventListener("click", puzzleShowSolution);
  $("pz-explain").addEventListener("click", puzzleExplain);
  $("pz-chat-form").addEventListener("submit", sendPuzzleChat);
  $("pz-src-tactics").addEventListener("click", () => setPuzzleSource("lichess"));
  $("pz-src-mine").addEventListener("click", () => setPuzzleSource("your_games"));
  $("pz-weakness").addEventListener("change", (e) => { puzzleWeakness = e.target.checked; loadNextPuzzle(); });
  $("pz-easier").addEventListener("click", () => setPuzzleDifficulty("easier"));
  $("pz-harder").addEventListener("click", () => setPuzzleDifficulty("harder"));
  $("pz-mode-solve").addEventListener("click", () => setStormMode(false));
  $("pz-mode-storm").addEventListener("click", () => setStormMode(true));
  $("pz-storm-start").addEventListener("click", startStorm);
  $("pz-storm-summary-btn").addEventListener("click", summarizeStormRun);
  $("pz-review-prev").addEventListener("click", () => solutionStep(-1));
  $("pz-review-next").addEventListener("click", () => solutionStep(1));

  // Settings panel.
  $("settings-toggle").addEventListener("click", openSettings);
  $("settings-cancel").addEventListener("click", () => ($("settings").hidden = true));
  $("settings-form").addEventListener("submit", saveSettings);
  document
    .querySelectorAll(".set-tab-btn")
    .forEach((b) => b.addEventListener("click", () => activateSettingsTab(b.dataset.tab)));
  $("set-profile-mode").addEventListener("change", applyProfilePreset);
  $("set-recent").addEventListener("input", syncProfileModeFromFields);
  $("set-lifetime").addEventListener("input", syncProfileModeFromFields);
  $("set-skill-auto").addEventListener("change", updateSkillUI);
  $("set-chesscom-sync").addEventListener("change", updateChesscomSyncUI);
  $("set-elo").addEventListener("input", updateSkillUI);
  $("set-ollama-detect").addEventListener("click", detectOllama);
  $("set-ollama-model-select").addEventListener("change", (e) => {
    $("set-local-llm-model").value = e.target.value; // picking a detected model fills the saved field
  });
  window.addEventListener("keydown", (e) => {
    // Escape blurs the chat box (or any field) so board hotkeys work again.
    if (e.key === "Escape" && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) {
      e.target.blur();
      return;
    }
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (solutionPlay) {
      // Scrub the solution with the arrow keys, mirroring the ‹ › buttons (Storm review + Solve).
      if (e.key === "ArrowLeft") { e.preventDefault(); solutionStep(-1); return; }
      if (e.key === "ArrowRight") { e.preventDefault(); solutionStep(1); return; }
    }
    if (puzzleMode) return; // puzzle mode has no game-navigation hotkeys
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepBack();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepForward();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      gotoNode(0); // jump to the start of the game (Lichess: ↑)
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      gotoNode(timeline.length - 1); // jump to the end of the game (Lichess: ↓)
    } else if (e.key === " ") {
      e.preventDefault();
      stepForward(); // space = next move (Lichess)
    } else if (e.key === "f" || e.key === "F") {
      e.preventDefault();
      flipBoard(); // f = flip board (Lichess)
    } else if (e.key === "l" || e.key === "L") {
      e.preventDefault();
      toggleBestArrows(); // l = toggle best-move arrows (Lichess: local engine)
    } else if (e.key === "t" || e.key === "T") {
      e.preventDefault();
      toggleThreatArrows(); // t = toggle threat arrows
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      if (currentMistake < mistakes.length - 1) selectMistake(currentMistake + 1); // next mistake
    } else if (e.key === "p" || e.key === "P") {
      e.preventDefault();
      if (currentMistake > 0) selectMistake(currentMistake - 1); // previous mistake
    }
  });

  loadAll();
  loadHistory(); // populate the games panel regardless of whether a game is loaded
}

init();
