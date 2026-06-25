import { Chessground } from "https://esm.sh/chessground@9";
import { Chess } from "https://esm.sh/chess.js@1";

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

let cur = 0; // current timeline node (valid when !exploring)
let anchorNode = 0; // the review (mistake) node we started from
let currentMistake = -1;
let currentPrompt = "";

let exploring = false; // off the game line, free-playing variations
let exploreBaseNode = 0; // node we left the timeline from

let bestArrowOn = false;
// Live best-move arrows: progressively deepen and refine while you sit on a position,
// cancelled the moment the position changes, with a hard time cap so it never runs forever.
let bestArrows = [];
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

// History panel + progressive (navigate-while-analyzing) open.
let analyzing = false; // true during phase 1: provisional PGN timeline, no engine evals yet
let historyMode = "normal"; // "normal" (local games) | "lichess"
let myPlayerId = ""; // configured user's id, for inferring side on lichess lookups
let pollTimer = null; // analysis-status poller
let batchInfo = null; // {total, self_handle, lastDone} while a multi-game upload is analyzing
let lichessCount = 5; // how many recent lichess games to show ("Load more" grows it)
let lichessUser = ""; // the handle currently shown in lichess mode (for "Load more")
const LICHESS_PAGE = 5; // initial count + how many more each "Load more"
// "My games": /api/history returns every analysed game; we render them in pages (inside the
// fixed-height scroll box) so the list starts short and grows on "Show more", not the page.
let historyGames = []; // all rows from the last /api/history fetch
let historyCount = 10; // how many to show now ("Show more" grows it)
const HISTORY_PAGE = 10; // initial count + how many more each "Show more"

// App mode (double-click launcher): on open, auto-load the user's most recent game.
// `appUsername` is the Lichess handle (config.LICHESS_USERNAME) — the only autoloadable identity, so
// it drives "open my latest game" and the Lichess panel placeholder. `chesscomUsername` is the
// configured chess.com handle; chess.com has no public fetch API, so it can't autoload — it only
// tells the first-run flow not to re-prompt and routes the user to Paste / Upload PGN instead.
let appMode = false;
let appUsername = "";
let chesscomUsername = "";
// On-demand Claude-written end-of-game summary: generated when the user presses the button, or
// automatically per game when `coachAiAuto` is on (a Settings option). `coachAiToken` invalidates
// an in-flight request when a new game is opened so a stale summary never lands on the wrong game.
let coachAiAuto = false;
let coachAiToken = 0;
// Whether chat questions inject the cross-game coaching profile (a Settings option, default on).
let personalizeHistory = true;

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
function drawArrows() {
  const shapes = [];
  // The move you actually played in-game — only at the review position. Grey = neutral
  // "here's what you did", not a colour-coded judgement.
  if (!exploring && !analyzing && cur === anchorNode && timeline[cur] && timeline[cur].move_uci) {
    shapes.push(arrowShape(timeline[cur].move_uci, "grey"));
  }
  if (bestArrowOn) for (const a of bestArrows) shapes.push(a);
  for (const s of evalShapes) shapes.push(s);
  // autoShapes (not setShapes): app-managed annotations that survive piece press/drag and
  // only change when we redraw — so the played-move arrow stays until you actually move.
  ground.setAutoShapes(shapes);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Map top engine moves → green arrows. The best move is a bold arrow; alternatives are
// clearly thinner (with proportionally smaller heads, since chessground scales the arrowhead
// with stroke width) so the recommendation stands out at a glance.
function movesToArrows(moves) {
  if (!moves.length) return [];
  const best = moves[0].win_percent;
  const out = [];
  for (let i = 0; i < moves.length; i++) {
    const delta = best - moves[i].win_percent;
    if (i > 0 && delta > 12) break; // only surface genuinely good alternatives
    // best = bold (13); alternatives start much thinner (≤7) and taper with how much worse.
    const lineWidth = i === 0 ? 13 : Math.max(4, 7 - delta);
    out.push({
      orig: moves[i].uci.slice(0, 2),
      dest: moves[i].uci.slice(2, 4),
      brush: "green",
      modifiers: { lineWidth },
    });
  }
  return out;
}

// Run an escalating-depth search for the current position; cancels itself on any
// position change (searchGen) and stops after SEARCH_MAX_MS. Only active when the toggle is on.
async function refreshBestMoves() {
  searchGen += 1; // cancel any in-flight search
  bestArrows = [];
  drawArrows();
  if (!bestArrowOn) return;
  const myGen = searchGen;
  const fen = chess.fen();
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

function updateStatus() {
  const el = $("status");
  if (exploring) {
    el.className = "status away";
    el.innerHTML = `🔍 Exploring a variation. <button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else if (cur !== anchorNode) {
    el.className = "status away";
    el.innerHTML = `Viewing ${nodeLabel(cur)} — not the review move. <button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else {
    el.className = "status";
    const g = timeline[cur] ? classGlyph(timeline[cur].classification) : "";
    el.innerHTML = g + escapeHtml(currentPrompt || nodeLabel(cur));
  }
}

// --- navigation ----------------------------------------------------------
function gotoNode(n) {
  exploring = false;
  cur = clamp(n, 0, timeline.length - 1);
  evalShapes = [];
  // chat context: the game node's own position (before its move) + the move played there
  chatFen = timeline[cur] ? timeline[cur].fen : null;
  chatMove = timeline[cur] ? timeline[cur].move_san || null : null;
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
  renderBoard();
  updateStatus();
  renderGraph();
  refreshBestMoves(); // live best-move arrows for the new position

  $("verdict").innerHTML = `<span class="line">Evaluating…</span>`;
  const res = await fetch("/api/evaluate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fen: fenBefore, move: uci }),
  }).then((r) => r.json());
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

  const mistakeDots = timeline
    .filter((nd) => nd.mistake_index != null)
    .map(
      (nd) =>
        `<circle cx="${x(nd.node).toFixed(1)}" cy="${y(val(nd)).toFixed(1)}" r="3" ` +
        `fill="${classColor(nd.classification)}" vector-effect="non-scaling-stroke"/>`
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
    analyzingNote;
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

function onGraphClick(ev) {
  const n = timeline.length;
  if (n < 2) return;
  const rect = $("graph").getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  gotoNode(Math.round(frac * (n - 1)));
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
  const pos = await fetch(`/api/position/${i}`).then((r) => r.json());
  currentMistake = i;
  currentPrompt = pos.error ? "" : pos.prompt;
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
  else gotoNode(i);
}

// Highlight the move at the current node and keep it scrolled into view (works in compact mode).
function highlightCurrentMove() {
  const ol = $("movelist");
  if (!ol) return;
  let active = null;
  ol.querySelectorAll(".ply[data-node]").forEach((el) => {
    const on = Number(el.dataset.node) === cur;
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
  $("game-meta").textContent =
    `${session.white} vs ${session.black} — ${session.result} · reviewing ${session.player} ` +
    `(acc W ${session.accuracy_white} / B ${session.accuracy_black}) · ${session.num_mistakes} mistakes${sens}`;
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
function renderCoach(session) {
  const el = $("coach");
  if (!el) return;
  const text = session && session.coach_summary;
  el.textContent = text || "";
  el.hidden = !text;
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
    el.textContent = "Snowie is barking up a full game summary…";
  } else if (state === "error") {
    el.hidden = false;
    el.className = "coach-ai err";
    el.textContent = text || "Couldn't generate the summary.";
  } else {
    el.hidden = false;
    el.className = "coach-ai";
    // Render bold / italics / lists / paragraphs (same safe markdown as the chat answers).
    el.innerHTML = `<span class="coach-ai-tag">AI coach (Snowie)</span>${renderMarkdown(text)}`;
  }
}

// Set up the AI-summary UI for the freshly-loaded game: auto-generate, or just offer the button.
function prepareCoachAI() {
  coachAiToken++; // any earlier in-flight request is now stale
  setCoachAI("hidden");
  if (!timeline.length) { showCoachButton(false); return; }
  if (coachAiAuto) fetchCoachAI();
  else showCoachButton(true);
}

// Actually request the summary (button press, or the auto path). Always allowed — it only ever
// runs from an explicit user choice, so it spends Claude only when asked.
async function fetchCoachAI() {
  const tok = ++coachAiToken;
  showCoachButton(false);
  setCoachAI("pending");
  let res;
  try {
    res = await fetch("/api/coach", { method: "POST" }).then((r) => r.json());
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
  // Identity + app-mode come from the server (settings-backed), so one source of truth.
  try {
    const cfg = await fetch("/api/app-config").then((r) => r.json());
    appMode = !!cfg.app_mode;
    appUsername = (cfg.lichess_username || "").trim();
    chesscomUsername = (cfg.chesscom_username || "").trim();
    coachAiAuto = !!cfg.coach_ai_auto;
    personalizeHistory = cfg.personalize_history !== false; // default on
  } catch (_) {}
  if (appUsername) $("lichess-user").placeholder = appUsername;
  if (appMode) startHeartbeat(); // so closing this tab quits the standalone app
  checkSetup(); // surface a banner if Stockfish / the claude CLI is missing (fire-and-forget)

  const session = await fetch("/api/session").then((r) => r.json());
  if (session.empty) {
    // App mode: try to auto-load the user's most recent Lichess game instead of an empty board.
    if (appMode && (await maybeAutoload())) return;
    $("game-meta").textContent =
      "No game analysed yet — pick one from the Games panel, or run analyze_game.";
    return;
  }
  applySession(session);
  const tl = await fetch("/api/timeline").then((r) => r.json());
  applyTimeline(tl);
  if (mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(0);
  prepareCoachAI(); // timeline is set now → offer the button (or auto-generate)
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

  if (!sf.ok) {
    showSetupBanner(
      banner,
      true,
      `<b>Stockfish engine not found.</b> ${escapeHtml(sf.hint || "Install Stockfish to analyze games.")}`,
      null
    );
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

// App-mode empty board: auto-load the configured Lichess user's latest game; for a chess.com-only
// user (no autoloadable handle) skip straight to Paste / Upload; otherwise prompt for a username.
async function maybeAutoload() {
  if (appUsername) await autoOpenLatest(appUsername);
  else if (chesscomUsername) openPastePanel(`Paste or upload ${chesscomUsername}'s games to analyze them.`);
  else showFirstRun("");
  return true;
}

// Reveal the Games panel on the Paste-PGN tab (the entry point for chess.com / any-source games).
function openPastePanel(metaMsg) {
  document.body.classList.remove("history-hidden"); // make sure the panel is visible
  setMode("paste");
  $("paste-pgn").focus();
  if (metaMsg) $("game-meta").textContent = metaMsg;
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
  if (on) {
    bestArrowOn = false;
    $("best-toggle").checked = false;
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
  $("coach").hidden = true;
  coachAiToken++; // invalidate any in-flight AI summary from the previous game
  setCoachAI("hidden");
  showCoachButton(false);

  let prov = null;
  try {
    prov = buildProvisionalTimeline(pgn);
  } catch (_) {
    prov = null; // unparseable PGN -> fall back to a blocking spinner (phase-2 still works)
  }
  if (prov && prov.length >= 2) {
    timeline = prov;
    player = side === "white" || side === "black" ? side : "white";
    orient = player;
    applyEvalBarTheme();
    renderMoveList(); // provisional notation: navigable immediately, glyphs fill in later
    gotoNode(0);
    $("game-meta").textContent = metaText || "Analyzing… you can step through the moves now (← / →).";
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

function openGame(pgn, side) {
  batchInfo = null; // a single open is not a batch
  closeHistoryDrawer(); // on small screens the drawer covers the board — get out of the way
  beginProvisional(pgn, side);
  fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pgn, player: side || "auto" }),
  }).catch(() => {});
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
  const prevCur = cur;
  const session = await fetch("/api/session").then((r) => r.json());
  const tl = await fetch("/api/timeline").then((r) => r.json());
  if (session.empty) return; // superseded/cleared
  analyzing = false;
  setAnalyzingUI(false); // hides the progress bar
  applySession(session);
  applyTimeline(tl);
  // Keep the user where they were navigating; if they hadn't moved, jump to the first mistake.
  if (prevCur === 0 && mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(clamp(prevCur, 0, timeline.length - 1));
  prepareCoachAI(); // timeline is set now → offer the button (or auto-generate)
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
async function loadHistory(doneMsg) {
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
  historyCount = HISTORY_PAGE; // a fresh fetch resets paging back to the first page
  renderMyGames();
  $("history-status").textContent = historyGames.length ? doneMsg || "" : "No analyzed games yet.";
}

// Render the current page of "My games" into the (fixed-height, scrollable) list, with a
// "Show more" row when there are extra games beyond what's shown. No refetch — pages a cached list.
function renderMyGames() {
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
    $("history-list").appendChild(li);
  }
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

// Just the tab chrome (active button + which form/list is shown), no data fetch.
function activateTab(mode) {
  historyMode = mode;
  $("mode-normal").classList.toggle("active", mode === "normal");
  $("mode-lichess").classList.toggle("active", mode === "lichess");
  $("mode-paste").classList.toggle("active", mode === "paste");
  $("lichess-form").style.display = mode === "lichess" ? "flex" : "none";
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
    : "Using your own window sizes from Advanced below.";
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
  $("set-personalize").checked = s.personalize_history !== false; // personalize chat (default on)
  $("set-sf-status").textContent = data.stockfish_ok
    ? "Stockfish engine found ✓"
    : "Stockfish not found — analysis won't run until this points at the engine.";
  $("settings").hidden = false;
  $("set-username").focus();
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
    aliases: $("set-aliases").value.trim(),
    lichess_token: $("set-token").value.trim(),
    stockfish_path: $("set-stockfish").value.trim(),
    local_llm_base_url: $("set-local-llm-url").value.trim(),
    local_llm_model: $("set-local-llm-model").value.trim(),
    coach_ai_auto: $("set-coach-ai-auto").checked,
    personalize_history: $("set-personalize").checked,
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
  $("graph").addEventListener("click", onGraphClick);
  $("movelist-expand").addEventListener("click", toggleMoveList);
  $("coach-ai-btn").addEventListener("click", fetchCoachAI);
  $("chat-form").addEventListener("submit", sendChat);

  // Games panel: collapse toggle, mode slider, lichess lookup.
  $("history-toggle").addEventListener("click", toggleHistory);
  $("history-collapse").addEventListener("click", toggleHistory);
  $("mode-normal").addEventListener("click", () => setMode("normal"));
  $("mode-lichess").addEventListener("click", () => setMode("lichess"));
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
  // "Set as my account": make the looked-up Lichess handle your unified identity.
  $("set-as-me").addEventListener("click", async () => {
    const u = ($("lichess-user").value.trim() || lichessUser || "").trim();
    if (!u) return;
    await saveUsername(u);
    reflectSetAsMe((u || "").toLowerCase());
    loadHistory(); // "My games" now resolves to this account
  });
  // First-run prompt (no configured account): two fields, fill in either. Save both, then open the
  // Lichess user's latest game if given, else route the chess.com user to Paste / Upload PGN.
  $("firstrun-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const lichess = $("firstrun-user").value.trim();
    const chesscom = $("firstrun-chesscom-user").value.trim();
    if (!lichess && !chesscom) return;
    await saveIdentity(lichess, chesscom);
    $("firstrun").hidden = true;
    if (lichess) autoOpenLatest(lichess);
    else openPastePanel(`Paste or upload ${chesscom}'s games to analyze them.`);
  });
  // Settings panel.
  $("settings-toggle").addEventListener("click", openSettings);
  $("settings-cancel").addEventListener("click", () => ($("settings").hidden = true));
  $("settings-form").addEventListener("submit", saveSettings);
  $("set-profile-mode").addEventListener("change", applyProfilePreset);
  $("set-recent").addEventListener("input", syncProfileModeFromFields);
  $("set-lifetime").addEventListener("input", syncProfileModeFromFields);
  $("set-skill-auto").addEventListener("change", updateSkillUI);
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
