"use strict";

const symbols = {
  wk: "♔", wq: "♕", wr: "♖", wb: "♗", wn: "♘", wp: "♙",
  bk: "♚", bq: "♛", br: "♜", bb: "♝", bn: "♞", bp: "♟",
};
const gradeColors = {
  "Missed win": "#a8433d", "Grave error": "#752c2a", Blunder: "#a8433d", Mistake: "#bb633f",
  Inaccuracy: "#9a713d", Good: "#66758a", Excellent: "#4d6687",
  Best: "#354e70", Great: "#765b83", Brilliant: "#765b3c",
};
const gradeIcons = {
  Brilliant: "✦", Best: "★", Great: "◆", Excellent: "✓", Good: "✓",
  Inaccuracy: "?!", Mistake: "!", Blunder: "⚠", "Grave error": "☠",
  "Missed win": "×",
};
const pieceValues = { p: 1, n: 3, b: 3, r: 5, q: 9 };
const boardElement = document.querySelector("#board");
const toastElement = document.querySelector("#toast");
const trySkinMatch = new URLSearchParams(window.location.search)
  .get("try")?.match(/^(board|piece):([a-z0-9-]+)$/);
if (trySkinMatch) {
  const cleanUrl = new URL(window.location.href);
  cleanUrl.searchParams.delete("try");
  window.history.replaceState(null, "", cleanUrl);
}
const state = {
  game: null,
  trySkin: trySkinMatch ? { type: trySkinMatch[1], id: trySkinMatch[2] } : null,
  nebulaDetailSquares: null,
  roomCode: new URLSearchParams(window.location.search).get("room")?.toUpperCase() || "",
  isSpectator: new URLSearchParams(window.location.search).get("spectate") === "1",
  selected: null,
  premoves: [],
  drag: null,
  suppressClick: false,
  requiresAcceptance: false,
  flipped: false,
  orientationInitialized: false,
  clockSyncAt: Date.now(),
  engineReady: null,
  enginePending: null,
  busy: false,
  botThinking: false,
  startedAt: Date.now(),
  modalShown: false,
  reviewPly: null,
  reviewTimer: null,
  reviewPlaying: false,
  reviewAnalysisPly: null,
  reviewCandidates: [],
  reviewSelectedCandidate: null,
  reviewTopMoveSan: null,
  reviewAnalysisStatus: "",
  toastTimer: null,
  gradePopupTimer: null,
  animatedPly: 0,
  gradeAnimationPlayedPly: 0,
  lowTimeWarningActive: false,
  chatMessages: [],
  chatTyping: {},
  chatPlayerSide: null,
  chatBubbleKey: { w: "", b: "" },
  chatMessageKey: "",
  chatPollTimer: null,
  lastTypingSentAt: 0,
};
const squares = new Map();
const gridSquares = new Map();
const squareParts = new Map();
let stockfish = null;
const gradePopupNames = new Set([
  "Brilliant", "Great", "Best", "Blunder", "Grave error", "Missed win",
]);

function initializeStockfish() {
  if (state.engineReady) return state.engineReady;
  state.engineReady = new Promise((resolve, reject) => {
    const worker = new Worker("/engine/stockfish-19-lite-single.js");
    stockfish = worker;
    const timeout = window.setTimeout(() => {
      worker.terminate();
      reject(new Error("Stockfish took too long to start."));
    }, 15000);
    worker.onerror = () => {
      window.clearTimeout(timeout);
      worker.terminate();
      reject(new Error("The Stockfish analysis engine could not be loaded."));
    };
    worker.onmessage = (event) => {
      const line = String(event.data);
      if (line === "uciok") {
        worker.postMessage("setoption name Threads value 1");
        worker.postMessage("setoption name Hash value 16");
        worker.postMessage("isready");
      } else if (line === "readyok") {
        window.clearTimeout(timeout);
        resolve();
      }
      const pending = state.enginePending;
      if (!pending) return;
      const score = line.match(/\bscore (cp|mate) (-?\d+)/);
      if (pending.kind === "candidates" && score) {
        const variation = line.match(/\bmultipv (\d+)\b.*?\bpv ((?:[a-h][1-8][a-h][1-8][qrbn]?\s*)+)/);
        if (variation) {
          pending.candidates.set(Number(variation[1]), {
            uci: variation[2].trim().split(/\s+/)[0],
            rank: Number(variation[1]),
            value: score[1] === "mate"
              ? (Number(score[2]) > 0 ? 10000 : -10000)
              : Number(score[2]),
            mate: score[1] === "mate" ? Number(score[2]) : null,
          });
        }
      }
      if (score) {
        const mate = score[1] === "mate" ? Number(score[2]) : null;
        pending.score = {
          value: mate === null ? Number(score[2]) : (mate > 0 ? 10000 : -10000),
          mate,
        };
      }
      if (line.startsWith("bestmove")) {
        window.clearTimeout(pending.timeout);
        state.enginePending = null;
        if (pending.kind === "move") {
          const bestMove = line.match(/^bestmove\s+([a-h][1-8][a-h][1-8][qrbn]?)/);
          if (bestMove) pending.resolve(bestMove[1]);
          else pending.reject(new Error("Stockfish did not return a legal move."));
        } else if (pending.kind === "candidates") {
          const candidates = [...pending.candidates.values()]
            .sort((left, right) => left.rank - right.rank);
          if (candidates.length) pending.resolve(candidates);
          else pending.reject(new Error("Stockfish did not return review variations."));
        } else if (pending.score === null) {
          pending.reject(new Error("Stockfish did not return an evaluation."));
        } else {
          pending.resolve(pending.score);
        }
      }
    };
    worker.postMessage("uci");
  });
  return state.engineReady;
}

let engineQueue = Promise.resolve();

function stockfishScore(fen) {
  const request = engineQueue.then(async () => {
    await initializeStockfish();
    return new Promise((resolve, reject) => {
      const pending = {
        kind: "score",
        score: null,
        resolve,
        reject,
        timeout: window.setTimeout(() => {
          state.enginePending = null;
          reject(new Error("Stockfish analysis timed out."));
        }, 12000),
      };
      state.enginePending = pending;
      stockfish.postMessage("setoption name UCI_LimitStrength value false");
      stockfish.postMessage("setoption name MultiPV value 1");
      stockfish.postMessage("setoption name Skill Level value 20");
      stockfish.postMessage(`position fen ${fen}`);
      stockfish.postMessage("go movetime 80");
    });
  });
  engineQueue = request.catch(() => {});
  return request;
}

function stockfishBestMove(fen, elo, skillLevel = BOT_SKILL_LEVELS[elo]) {
  const request = engineQueue.then(async () => {
    await initializeStockfish();
    return new Promise((resolve, reject) => {
      const pending = {
        kind: "move",
        resolve,
        reject,
        timeout: window.setTimeout(() => {
          state.enginePending = null;
          reject(new Error("Stockfish bot search timed out."));
        }, 12000),
      };
      state.enginePending = pending;
      stockfish.postMessage("setoption name UCI_LimitStrength value false");
      stockfish.postMessage("setoption name MultiPV value 1");
      stockfish.postMessage(`setoption name Skill Level value ${skillLevel}`);
      stockfish.postMessage(`position fen ${fen}`);
      stockfish.postMessage("go movetime 350");
    });
  });
  engineQueue = request.catch(() => {});
  return request;
}

function stockfishCandidates(fen, count = 4) {
  const request = engineQueue.then(async () => {
    await initializeStockfish();
    return new Promise((resolve, reject) => {
      const pending = {
        kind: "candidates",
        candidates: new Map(),
        resolve,
        reject,
        timeout: window.setTimeout(() => {
          state.enginePending = null;
          reject(new Error("Stockfish review analysis timed out."));
        }, 15000),
      };
      state.enginePending = pending;
      stockfish.postMessage("setoption name UCI_LimitStrength value false");
      stockfish.postMessage(`setoption name MultiPV value ${count}`);
      stockfish.postMessage("setoption name Skill Level value 20");
      stockfish.postMessage(`position fen ${fen}`);
      stockfish.postMessage("go movetime 650");
    });
  });
  engineQueue = request.catch(() => {});
  return request;
}

const BOT_SKILL_LEVELS = { 200: 0, 1000: 2, 1600: 5 };

async function analyzeLastMove(beforeFen, next, ply) {
  const bestBefore = await stockfishScore(beforeFen);
  const afterScore = await stockfishScore(next.fen);
  let bestMove = null;
  try {
    bestMove = await stockfishBestMove(beforeFen, 1600, 20);
  } catch (error) {
    showToast(`Stockfish could not find a recommended move: ${error.message}`);
  }
  const missedWin = bestBefore.mate !== null && bestBefore.mate > 0 &&
    !(afterScore.mate !== null && afterScore.mate < 0);
  const centipawnLoss = missedWin
    ? 3000
    : Math.max(0, Math.min(3000, Math.round(bestBefore.value + afterScore.value)));
  const whiteEvaluation = Math.max(
    -10000,
    Math.min(10000, Math.round(next.turn === "w" ? afterScore.value : -afterScore.value)),
  );
  const analyzed = await api(roomApiPath("/analysis"), {
    method: "POST",
    body: JSON.stringify({
      ply,
      centipawnLoss,
      evaluation: whiteEvaluation,
      missedWin,
      bestMove,
    }),
  });
  await updateGame(analyzed);
}

async function playBotMove() {
  if (state.botThinking || state.busy || !state.game ||
      state.game.botElo === null || state.game.botElo === undefined ||
      state.game.status !== "active" || state.game.over ||
      state.game.playerSide !== "w" || state.game.turn !== "b") return;
  state.botThinking = true;
  const fen = state.game.fen;
  const elo = state.game.botElo;
  try {
    const bestMove = await stockfishBestMove(fen, elo);
    if (state.game.fen !== fen || state.game.over) return;
    const move = elo === 200 && state.game.legalMoves.length > 1 && Math.random() < 0.75
      ? state.game.legalMoves[Math.floor(Math.random() * state.game.legalMoves.length)]
      : {
        from: bestMove.slice(0, 2),
        to: bestMove.slice(2, 4),
        promotion: bestMove[4],
      };
    const beforeFen = state.game.fen;
    const next = await api(roomApiPath("/bot-move"), {
      method: "POST",
      body: JSON.stringify({
        from: move.from,
        to: move.to,
        ...(move.promotion ? { promotion: move.promotion } : {}),
      }),
    });
    await updateGame(next, true);
    void analyzeLastMove(beforeFen, next, next.history.length).catch((error) => {
      if (error.status !== 409) showToast(error.message);
    });
  } catch (error) {
    showToast(error.message);
  } finally {
    state.botThinking = false;
    if (state.game?.fen !== fen) void playBotMove();
  }
}

function squareName(row, col) {
  return `${"abcdefgh"[col]}${8 - row}`;
}

function pieceSkinClass(color) {
  const side = color === "w" ? "white" : "black";
  const skin = color === "w" && state.trySkin?.type === "piece"
    ? state.trySkin.id
    : state.game?.[`${side}PieceSkin`] || "classic";
  return skin === "classic" ? "" : `skin-${side}-${skin}`;
}

function nebulaDetailSquares() {
  if (state.nebulaDetailSquares) return state.nebulaDetailSquares;
  const seed = state.roomCode;
  const rankedBySide = [[], []];
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const name = squareName(row, col);
      let hash = 2166136261;
      for (const character of `${seed}:${name}`) {
        hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0;
      }
      rankedBySide[row >= 4 ? 1 : 0].push({ name, hash });
    }
  }
  state.nebulaDetailSquares = new Set();
  for (const rankedSquares of rankedBySide) {
    rankedSquares.sort((a, b) => a.hash - b.hash || a.name.localeCompare(b.name));
    for (const { name } of rankedSquares.slice(0, 16)) {
      state.nebulaDetailSquares.add(name);
    }
  }
  return state.nebulaDetailSquares;
}

function squareCoords(name) {
  return { row: 8 - Number(name[1]), col: name.charCodeAt(0) - 97 };
}

function showToast(message) {
  toastElement.textContent = message;
  toastElement.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toastElement.classList.remove("show"), 2200);
}

function showGradePopup(move) {
  document.querySelector(".move-grade-popup")?.remove();
  clearTimeout(state.gradePopupTimer);
  const popup = document.createElement("div");
  popup.className = "move-grade-popup";
  popup.dataset.grade = move.grade.toLowerCase().replaceAll(" ", "-");
  popup.setAttribute("role", "status");
  popup.setAttribute("aria-live", "polite");
  const card = document.createElement("div");
  card.className = "move-grade-popup-card";
  const icon = gradeIcon(move.grade, "move-grade-popup-icon");
  const piece = document.createElement("span");
  piece.className = `move-grade-piece ${move.color === "w" ? "white-piece" : "black-piece"}`;
  piece.textContent = symbols[`${move.color}${move.piece}`] || "";
  piece.setAttribute("aria-label", `${move.color === "w" ? "White" : "Black"} piece`);
  const grade = document.createElement("strong");
  grade.textContent = move.grade.toUpperCase();
  const san = document.createElement("span");
  san.textContent = move.san;
  card.append(icon, piece, grade, san);
  popup.append(card);
  document.body.append(popup);
  state.gradePopupTimer = window.setTimeout(
    () => popup.remove(),
    move.grade === "Best" ? 900 : 1800,
  );
}

function roomApiPath(action = "") {
  return `/api/rooms/${encodeURIComponent(state.roomCode)}${action}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "The request failed.");
    error.status = response.status;
    throw error;
  }
  return result;
}

function startPieceDrag(drag, x, y) {
  if (drag.started) return;
  drag.started = true;
  const pieceCode = state.game.board[squareCoords(drag.from).row][squareCoords(drag.from).col];
  const ghost = document.createElement("span");
  const color = pieceCode[0] === "w" ? "white" : "black";
  ghost.className = `drag-piece ${color} ${pieceSkinClass(pieceCode[0])}`;
  ghost.dataset.piece = pieceCode;
  ghost.textContent = symbols[pieceCode];
  document.body.append(ghost);
  drag.ghost = ghost;
  movePieceGhost(drag, x, y);
  renderBoard();
}

function movePieceGhost(drag, x, y) {
  if (!drag.ghost) return;
  drag.ghost.style.left = `${x}px`;
  drag.ghost.style.top = `${y}px`;
}

function beginPiecePointer(event) {
  if (event.button !== 0 || !state.game || state.busy || state.isSpectator ||
      state.reviewPly !== null || state.game.over || state.game.status !== "active") return;
  const square = event.target.closest(".square");
  if (!square) return;
  const from = square.dataset.square;
  const piece = state.game.board[squareCoords(from).row][squareCoords(from).col];
  if (!piece || piece[0] !== state.game.playerSide) return;
  const drag = {
    pointerId: event.pointerId,
    from,
    startX: event.clientX,
    startY: event.clientY,
    target: from,
    started: false,
    timer: null,
    ghost: null,
  };
  state.drag = drag;
  square.setPointerCapture(event.pointerId);
  drag.timer = window.setTimeout(
    () => startPieceDrag(drag, drag.startX, drag.startY),
    140,
  );
  square.classList.add("piece-pressed");
}

function movePiecePointer(event) {
  const drag = state.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
  if (!drag.started && distance < 5) return;
  window.clearTimeout(drag.timer);
  startPieceDrag(drag, event.clientX, event.clientY);
  event.preventDefault();
  movePieceGhost(drag, event.clientX, event.clientY);
  const hovered = document.elementFromPoint(event.clientX, event.clientY)?.closest(".square");
  const nextTarget = hovered?.dataset.square || null;
  if (drag.target !== nextTarget) {
    drag.target = nextTarget;
    renderBoard();
  }
}

function clearPiecePointer(drag) {
  window.clearTimeout(drag.timer);
  drag.ghost?.remove();
  squares.get(drag.from)?.classList.remove("piece-pressed");
  state.drag = null;
  renderBoard();
}

function endPiecePointer(event) {
  const drag = state.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const moved = drag.started;
  const hovered = moved
    ? document.elementFromPoint(event.clientX, event.clientY)?.closest(".square")
    : null;
  const destination = hovered?.dataset.square || drag.from;
  clearPiecePointer(drag);
  if (!moved) return;
  state.suppressClick = true;
  window.setTimeout(() => { state.suppressClick = false; }, 0);
  state.selected = drag.from;
  void onSquareClick(destination);
}

function cancelPiecePointer(event) {
  const drag = state.drag;
  if (drag && drag.pointerId === event.pointerId) clearPiecePointer(drag);
}

function makeBoard() {
  for (let displayRow = 0; displayRow < 8; displayRow += 1) {
    for (let displayCol = 0; displayCol < 8; displayCol += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "square";
      button.setAttribute("role", "gridcell");
      button.addEventListener("click", () => {
        if (state.suppressClick) {
          state.suppressClick = false;
          return;
        }
        onSquareClick(button.dataset.square);
      });
      const rank = document.createElement("span");
      rank.className = "coordinate rank";
      const file = document.createElement("span");
      file.className = "coordinate file";
      const dot = document.createElement("span");
      dot.className = "legal-dot";
      const piece = document.createElement("span");
      piece.className = "piece";
      const premoveNumber = document.createElement("span");
      premoveNumber.className = "premove-number hidden";
      button.append(rank, file, dot, piece, premoveNumber);
      gridSquares.set(`${displayRow},${displayCol}`, button);
      squareParts.set(button, { rank, file, dot, piece, premoveNumber });
      boardElement.append(button);
    }
  }
  const overlay = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  overlay.id = "review-arrow-overlay";
  overlay.setAttribute("viewBox", "0 0 1000 1000");
  overlay.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
  marker.id = "review-arrow-head";
  marker.setAttribute("viewBox", "0 0 12 12");
  marker.setAttribute("refX", "10");
  marker.setAttribute("refY", "6");
  marker.setAttribute("markerWidth", "7");
  marker.setAttribute("markerHeight", "7");
  marker.setAttribute("markerUnits", "userSpaceOnUse");
  marker.setAttribute("orient", "auto");
  const arrowHead = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrowHead.setAttribute("d", "M 1 1 L 11 6 L 1 11 L 3.5 6 Z");
  arrowHead.classList.add("review-arrow-head");
  marker.append(arrowHead);
  defs.append(marker);
  const arrowHalo = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrowHalo.id = "review-move-arrow-halo";
  arrowHalo.classList.add("review-move-arrow-halo");
  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrow.id = "review-move-arrow";
  arrow.setAttribute("marker-end", "url(#review-arrow-head)");
  arrow.classList.add("review-move-arrow");
  overlay.append(defs, arrowHalo, arrow);
  boardElement.append(overlay);
  boardElement.addEventListener("pointerdown", beginPiecePointer);
  boardElement.addEventListener("pointermove", movePiecePointer, { passive: false });
  boardElement.addEventListener("pointerup", endPiecePointer);
  boardElement.addEventListener("pointercancel", cancelPiecePointer);
}

function setBoardOrientation() {
  const isReviewing = state.reviewPly !== null;
  const positionBoard = isReviewing
    ? reviewPositionBoard(Math.max(0, state.reviewPly - 1))
    : state.game.board;
  const reviewMove = isReviewing && state.reviewPly > 0
    ? state.game.history[state.reviewPly - 1]
    : null;
  const positionTurn = reviewPlyTurn(Math.max(0, state.reviewPly - 1));
  const positionCheck = isReviewing ? false : state.game.check;
  const selectedCandidate = state.reviewCandidates.find(
    (candidate) => candidate.uci === state.reviewSelectedCandidate,
  ) || state.reviewCandidates[0];
  const selectedThreat = selectedCandidate?.attackedTarget?.square;
  const lastMove = isReviewing ? null : state.game.lastMove;
  const legalDestinations = !isReviewing && state.selected
    ? new Set(
      state.game.legalMoves
        .filter((move) => move.from === state.selected)
        .map((move) => move.to),
    )
    : new Set();
  const currentKing = positionCheck
    ? positionBoard.flatMap((row, r) => row.map((piece, c) => ({
      piece, square: squareName(r, c),
    }))).find(({ piece }) => piece === `${positionTurn}k`)?.square
    : null;
  squares.clear();

  for (let displayRow = 0; displayRow < 8; displayRow += 1) {
    for (let displayCol = 0; displayCol < 8; displayCol += 1) {
      const row = state.flipped ? 7 - displayRow : displayRow;
      const col = state.flipped ? 7 - displayCol : displayCol;
      const name = squareName(row, col);
      const pieceCode = positionBoard[row][col];
      const square = gridSquares.get(`${displayRow},${displayCol}`);
      const parts = squareParts.get(square);
      square.style.gridRow = String(displayRow + 1);
      square.style.gridColumn = String(displayCol + 1);
      square.className = `square ${(row + col) % 2 === 0 ? "light" : "dark"}`;
      const boardSide = row >= 4 ? "white" : "black";
      const boardSkin = boardSide === "white" && state.trySkin?.type === "board"
        ? state.trySkin.id
        : state.game[`${boardSide}BoardSkin`] || "classic";
      if (boardSkin !== "classic") {
        square.classList.add(`board-skin-${boardSide}-${boardSkin}`);
      }
      if (boardSkin === "nebula" && nebulaDetailSquares().has(name)) {
        square.classList.add("nebula-detail");
      }
      square.dataset.square = name;
      squares.set(name, square);
      square.setAttribute("aria-label", `${pieceCode ? `${pieceCode[0] === "w" ? "White" : "Black"} ${pieceCode[1]}` : "Empty"} ${name}`);
      if (lastMove && [lastMove.from, lastMove.to].includes(name)) {
        square.classList.add("last-move");
      }
      if (isReviewing && reviewMove &&
          [reviewMove.from, reviewMove.to].includes(name)) {
        square.classList.add("review-played-square");
      }
      if (isReviewing && selectedCandidate &&
          selectedCandidate.from === name) {
        square.classList.add("review-candidate-from");
      }
      if (isReviewing && selectedCandidate &&
          selectedCandidate.to === name) {
        square.classList.add("review-candidate-to");
      }
      if (isReviewing && selectedThreat === name) {
        square.classList.add("review-threat-target");
      }
      if (state.selected === name) square.classList.add("selected");
      if (state.drag?.from === name && !state.drag.started) {
        square.classList.add("piece-pressed");
      }
      const premoveIndices = state.premoves
        .map((move, index) => move.from === name ? index + 1 : null)
        .filter((index) => index !== null);
      if (premoveIndices.length) {
        square.classList.add("premove-from");
        square.setAttribute(
          "aria-label",
          `${pieceCode ? `${pieceCode[0] === "w" ? "White" : "Black"} ${pieceCode[1]}` : "Empty"} ${name}, premove ${premoveIndices.join(", ")}`,
        );
        parts.premoveNumber.textContent = String(premoveIndices[0]);
        parts.premoveNumber.title = `Premove ${premoveIndices.join(", ")}`;
        parts.premoveNumber.classList.remove("hidden");
      } else {
        parts.premoveNumber.textContent = "";
        parts.premoveNumber.title = "";
        parts.premoveNumber.classList.add("hidden");
      }
      if (state.premoves.some((move) => move.to === name)) square.classList.add("premove-to");
      if (state.drag?.from === name) square.classList.add("drag-origin");
      if (state.drag?.target === name && state.drag.from !== name) square.classList.add("drag-target");
      if (currentKing === name) square.classList.add("check");
      if (state.game.legalMoves.some((move) => move.from === state.selected && move.to === name)) {
        square.classList.add("capture-target");
      }

      parts.rank.textContent = String(8 - row);
      parts.rank.hidden = displayCol !== 0;
      parts.file.textContent = "abcdefgh"[col];
      parts.file.hidden = displayRow !== 7;
      parts.dot.hidden = !legalDestinations.has(name) || Boolean(pieceCode);
      parts.piece.textContent = pieceCode ? symbols[pieceCode] : "";
      parts.piece.dataset.piece = pieceCode || "";
      parts.piece.className = pieceCode
        ? `piece ${pieceCode[0] === "w" ? "white" : "black"} ${pieceSkinClass(pieceCode[0])}`
        : "piece";
      if (isReviewing && selectedCandidate?.from === name) {
        parts.piece.classList.add("review-candidate-piece");
      }
      parts.piece.draggable = false;
    }
  }
  renderReviewArrow(selectedCandidate);
}

function renderReviewArrow(candidate) {
  const arrow = document.querySelector("#review-move-arrow");
  const halo = document.querySelector("#review-move-arrow-halo");
  if (!arrow) return;
  if (!candidate || state.reviewPly === null) {
    arrow.setAttribute("d", "");
    halo?.setAttribute("d", "");
    return;
  }
  const from = squareCoords(candidate.from);
  const to = squareCoords(candidate.to);
  const fromRow = state.flipped ? 7 - from.row : from.row;
  const fromCol = state.flipped ? 7 - from.col : from.col;
  const toRow = state.flipped ? 7 - to.row : to.row;
  const toCol = state.flipped ? 7 - to.col : to.col;
  let x1 = (fromCol + 0.5) * 125;
  let y1 = (fromRow + 0.5) * 125;
  let x2 = (toCol + 0.5) * 125;
  let y2 = (toRow + 0.5) * 125;
  const length = Math.hypot(x2 - x1, y2 - y1);
  const inset = Math.min(46, length * 0.28);
  const unitX = (x2 - x1) / length;
  const unitY = (y2 - y1) / length;
  x1 += unitX * inset;
  y1 += unitY * inset;
  x2 -= unitX * inset;
  y2 -= unitY * inset;
  const path = `M ${x1} ${y1} L ${x2} ${y2}`;
  arrow.setAttribute("d", path);
  halo?.setAttribute("d", path);
}

function reviewPositionBoard(ply) {
  const placement = ply === 0
    ? "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    : state.game.history[ply - 1]?.fen?.split(" ")[0];
  if (!placement) return state.game.board;
  return placement.split("/").map((rank) => {
    const row = [];
    for (const token of rank) {
      if (/\d/.test(token)) {
        row.push(...Array(Number(token)).fill(null));
      } else {
        row.push(`${token === token.toUpperCase() ? "w" : "b"}${token.toLowerCase()}`);
      }
    }
    return row;
  });
}

function reviewPlyTurn(ply) {
  if (ply === null) return state.game.turn;
  return ply === 0 ? "w" : state.game.history[ply - 1]?.fen?.split(" ")[1] || "w";
}

function renderBoard() {
  if (state.game) setBoardOrientation();
}

function gradeIcon(grade, className) {
  const icon = document.createElement("span");
  icon.className = className;
  icon.textContent = gradeIcons[grade] || "·";
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

function historyMove(move, index) {
  const wrapper = document.createElement("button");
  wrapper.type = "button";
  wrapper.className = "history-move";
  wrapper.dataset.ply = String(index + 1);
  if (index === state.game.history.length - 1) wrapper.classList.add("latest");
  if (state.reviewPly === index + 1) wrapper.classList.add("review-selected");
  wrapper.disabled = !state.game.over || state.isSpectator;
  wrapper.setAttribute("aria-label", `Review move ${Math.floor(index / 2) + 1}${index % 2 ? "... " : ". "}${move.san}`);
  wrapper.addEventListener("click", () => {
    if (state.game.over && !state.isSpectator) openGameReview(index + 1);
  });
  const san = document.createElement("span");
  san.textContent = move.san;
  wrapper.append(san);
  if (move.grade) {
    const grade = document.createElement("span");
    grade.className = `history-grade${move.grade === "Brilliant" ? " brilliant-grade" : ""}`;
    grade.style.color = gradeColors[move.grade] || "var(--cyan)";
    grade.append(gradeIcon(move.grade, "history-grade-icon"));
    const label = document.createElement("span");
    label.textContent = move.grade.toUpperCase();
    grade.append(label);
    wrapper.append(grade);
  }
  return wrapper;
}

function newlyHighlightedGrade(previousMoves, nextMoves) {
  for (let index = nextMoves.length - 1; index >= 0; index -= 1) {
    const move = nextMoves[index];
    if (gradePopupNames.has(move.grade) && move.grade !== previousMoves[index]?.grade) {
      return move;
    }
  }
  return null;
}

function accuracy(moves) {
  const analyzedMoves = moves.filter((move) => Number.isFinite(move.centipawnLoss));
  if (!analyzedMoves.length) return "—";
  const sum = analyzedMoves.reduce(
    (total, move) => total + Math.exp(-Math.min(move.centipawnLoss || 0, 1000) / 300) * 100,
    0,
  );
  return Math.round(sum / analyzedMoves.length);
}

function renderHistory() {
  const list = document.querySelector("#move-list");
  list.replaceChildren();
  const moves = state.game.history;
  if (!moves.length) {
    const empty = document.createElement("div");
    empty.className = "empty-history";
    empty.textContent = "No moves yet.";
    list.append(empty);
  } else {
    for (let index = 0; index < moves.length; index += 2) {
      const row = document.createElement("div");
      row.className = "move-row";
      const number = document.createElement("span");
      number.className = "move-number";
      number.textContent = `${Math.floor(index / 2) + 1}.`;
      row.append(number, historyMove(moves[index], index));
      if (moves[index + 1]) row.append(historyMove(moves[index + 1], index + 1));
      list.append(row);
    }
    list.scrollTop = list.scrollHeight;
  }
  document.querySelector("#move-total").textContent = `${moves.length} PLY${moves.length === 1 ? "" : "S"}`;
  for (const [color, selector] of [["w", "#white-accuracy"], ["b", "#black-accuracy"]]) {
    const value = accuracy(moves.filter((move) => move.color === color));
    document.querySelector(selector).textContent = value === "—" ? value : `${value}%`;
  }

  const last = moves.at(-1);
  document.querySelector("#last-move").textContent = last ? `${last.san}  ·  ${last.from} → ${last.to}` : "—";
  const chip = document.querySelector("#last-grade");
  chip.replaceChildren();
  if (last?.grade) {
    const animate = state.animatedPly === moves.length &&
      state.gradeAnimationPlayedPly !== moves.length;
    const icon = gradeIcon(last.grade, `grade-icon${animate ? " grade-icon-pop" : ""}`);
    const label = document.createElement("span");
    label.textContent = last.grade.toUpperCase();
    chip.append(icon, label);
    if (animate) state.gradeAnimationPlayedPly = moves.length;
  } else {
    chip.textContent = last ? "ANALYZING" : "—";
  }
  chip.className = `grade-chip ${last?.grade ? last.grade.toLowerCase().replaceAll(" ", "-") : last ? "analyzing" : "idle"}`;
  const explanation = last
    ? (last.centipawnLoss === undefined
      ? "Stockfish is analyzing this move."
      : `${last.centipawnLoss} centipawn loss · Stockfish 19 Lite`)
    : "Move details appear here.";
  document.querySelector("#last-explanation").textContent = explanation;
  renderGameReview();
}

function reviewExplanation(ply) {
  if (ply === 0) return "Starting position. Step through the moves to see Stockfish’s assessment and best line.";
  const move = state.game.history[ply - 1];
  if (!move?.grade) {
    return "Stockfish is still analyzing this move. The replay is available while its analysis catches up.";
  }
  const loss = Number.isFinite(move.centipawnLoss)
    ? move.centipawnLoss
    : null;
  let explanation;
  if (loss === null) {
    explanation = `Stockfish rates ${move.san} as ${move.grade.toLowerCase()}.`;
  } else if (loss <= 10) {
    explanation = `Stockfish rates ${move.san} as ${move.grade.toLowerCase()}: it keeps the position within 0.1 pawns of its preferred evaluation.`;
  } else {
    explanation = `Stockfish rates ${move.san} as ${move.grade.toLowerCase()}: it gives up about ${(loss / 100).toFixed(1)} pawns of evaluation compared with the best line.`;
  }
  const bestMoveSan = state.reviewTopMoveSan || move.bestMoveSan;
  if (bestMoveSan) {
    explanation += bestMoveSan === move.san
      ? " This was also Stockfish’s top move."
      : ` Stockfish preferred ${bestMoveSan}.`;
  }
  return explanation;
}

function renderGameReview() {
  const available = state.game.over && !state.isSpectator &&
    state.game.history.length > 0;
  const panel = document.querySelector("#game-review-panel");
  panel.classList.toggle("hidden", !available);
  document.querySelector("#open-game-review").classList.toggle("hidden", !available);
  document.querySelector(".app-shell").classList.toggle(
    "review-mode",
    available && state.reviewPly !== null,
  );
  if (!available) return;
  const reviewing = state.reviewPly !== null;
  document.querySelector("#start-game-review").classList.toggle("hidden", reviewing);
  document.querySelector("#review-controls").classList.toggle("hidden", !reviewing);
  if (!reviewing) return;
  const total = state.game.history.length;
  const move = state.game.history[state.reviewPly - 1];
  document.querySelector("#review-move-title").textContent =
    `${Math.floor((state.reviewPly + 1) / 2)}${state.reviewPly % 2 ? "." : "..."} ${move.san} · ${move.color === "w" ? "WHITE" : "BLACK"}`;
  document.querySelector("#review-position").textContent =
    `MOVE ${state.reviewPly} OF ${total}`;
  document.querySelector("#review-explanation").textContent =
    reviewExplanation(state.reviewPly);
  document.querySelector("#review-previous").disabled = state.reviewPly <= 1;
  document.querySelector("#review-next").disabled = state.reviewPly === total;
  document.querySelector("#review-play").disabled = total === 0;
  document.querySelector("#review-play").textContent =
    state.reviewPlaying ? "Pause" : "Play";
  renderReviewAlternatives();
}

function openGameReview(ply = 0) {
  if (!state.game?.over || state.isSpectator) return;
  stopReviewPlayback();
  state.reviewPly = Math.max(
    state.game.history.length ? 1 : 0,
    Math.min(state.game.history.length, ply),
  );
  state.selected = null;
  render();
  if (state.reviewPly > 0) void loadReviewAlternatives(state.reviewPly);
}

function stopReviewPlayback() {
  if (state.reviewTimer !== null) window.clearInterval(state.reviewTimer);
  state.reviewTimer = null;
  state.reviewPlaying = false;
}

function startReviewPlayback() {
  if (!state.game?.history.length) return;
  if (state.reviewPly >= state.game.history.length) state.reviewPly = 1;
  state.reviewPlaying = true;
  state.reviewTimer = window.setInterval(() => {
    if (state.reviewPly >= state.game.history.length) {
      stopReviewPlayback();
      render();
      return;
    }
    state.reviewPly += 1;
    render();
    void loadReviewAlternatives(state.reviewPly);
  }, 1100);
  render();
  void loadReviewAlternatives(state.reviewPly);
}

function reviewPositionFen(ply) {
  return ply <= 1
    ? "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    : state.game.history[ply - 2].fen;
}

function candidateExplanation(candidate, index, evaluation) {
  const sideToMove = reviewPositionFen(state.reviewAnalysisPly).split(" ")[1];
  const whiteEvaluation = sideToMove === "w" ? evaluation : -evaluation;
  const evaluationText = whiteEvaluation === 10000 || whiteEvaluation === -10000
    ? `Stockfish sees a forced mate for ${whiteEvaluation > 0 ? "White" : "Black"}.`
    : `Stockfish evaluates the position at ${whiteEvaluation >= 0 ? "+" : ""}${(whiteEvaluation / 100).toFixed(1)} for White.`;
  const feature = candidate.captured
    ? `It captures the ${candidate.captured} on ${candidate.to}.`
    : candidate.piece === "knight" || candidate.piece === "bishop"
      ? `It develops the ${candidate.piece} to ${candidate.to}, helping control the center.`
      : candidate.piece === "pawn" && ["d4", "e4", "d5", "e5"].includes(candidate.to)
        ? `It advances a central pawn to ${candidate.to}, contesting the center.`
        : `It places the ${candidate.piece} on ${candidate.to}, improving its activity.`;
  const attack = candidate.attackedTarget
    ? ` From there, it attacks the ${candidate.attackedTarget.color} ${candidate.attackedTarget.piece} on ${candidate.attackedTarget.square}.`
    : "";
  return `Stockfish ranks this as its #${candidate.rank} line. ${feature}${attack} ${evaluationText}`;
}

function renderReviewAlternatives() {
  const list = document.querySelector("#review-alternatives");
  list.replaceChildren();
  const status = document.querySelector("#review-alternatives-status");
  if (state.reviewAnalysisStatus) {
    status.textContent = state.reviewAnalysisStatus;
    return;
  }
  status.textContent = state.reviewCandidates.length
    ? "Choose an alternative to see its line on the board."
    : "No additional Stockfish alternatives were returned for this position.";
  for (const [index, candidate] of state.reviewCandidates.entries()) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "review-alternative";
    option.classList.toggle(
      "selected",
      candidate.uci === (state.reviewSelectedCandidate || state.reviewCandidates[0].uci),
    );
    const heading = document.createElement("strong");
    heading.textContent = `${candidate.san} · ${candidate.piece}`;
    const detail = document.createElement("span");
    detail.textContent = candidateExplanation(candidate, index, candidate.value);
    option.append(heading, detail);
    option.addEventListener("click", () => {
      state.reviewSelectedCandidate = candidate.uci;
      render();
    });
    list.append(option);
  }
}

async function loadReviewAlternatives(ply) {
  if (!state.game?.over || ply < 1 || ply > state.game.history.length) return;
  state.reviewAnalysisPly = ply;
  state.reviewCandidates = [];
  state.reviewSelectedCandidate = null;
  state.reviewTopMoveSan = null;
  state.reviewAnalysisStatus = "Stockfish is analyzing alternative moves…";
  render();
  try {
    const lines = await stockfishCandidates(reviewPositionFen(ply));
    if (state.reviewPly !== ply) return;
    const actualMove = state.game.history[ply - 1];
    const uci = `${actualMove.from}${actualMove.to}${actualMove.promotion || ""}`;
    if (!lines.length) {
      state.reviewAnalysisStatus = "";
      render();
      return;
    }
    const result = await api(
      roomApiPath(`/review/${ply}/alternatives`),
      {
        method: "POST",
        body: JSON.stringify({ moves: lines.map((line) => line.uci) }),
      },
    );
    if (state.reviewPly !== ply) return;
    const reviewedLines = result.alternatives.map((alternative) => {
      const line = lines.find((candidate) => candidate.uci === alternative.uci);
      return {
        ...alternative,
        value: line.value,
        mate: line.mate,
        rank: line.rank,
      };
    });
    state.reviewTopMoveSan =
      reviewedLines.find((line) => line.rank === 1)?.san || null;
    state.reviewCandidates = reviewedLines
      .filter((line) => line.uci !== uci)
      .slice(0, 3);
    state.reviewAnalysisStatus = "";
    state.reviewSelectedCandidate = state.reviewCandidates[0]?.uci || null;
    render();
  } catch (error) {
    if (state.reviewPly !== ply) return;
    state.reviewAnalysisStatus = `Stockfish alternatives could not be loaded: ${error.message}`;
    render();
  }
}

function renderCaptured() {
  const captured = { w: [], b: [] };
  const totals = { w: 0, b: 0 };
  for (const move of state.game.history) {
    if (!move.captured) continue;
    const capturer = move.color;
    const capturedColor = capturer === "w" ? "b" : "w";
    captured[capturer].push(symbols[`${capturedColor}${move.captured}`]);
    totals[capturer] += (pieceValues[move.captured] || 0) -
      (move.promotion ? (pieceValues[move.promotion] || 0) - 1 : 0);
  }
  for (const color of ["w", "b"]) {
    const suffix = color === "w" ? "white" : "black";
    document.querySelector(`#captured-${suffix}`).textContent = captured[color].join("");
    const difference = totals[color] - totals[color === "w" ? "b" : "w"];
    document.querySelector(`#${suffix}-material`).textContent =
      difference > 0 ? `+${difference}` : "";
  }
}

function renderEvaluation() {
  const score = state.game.evaluation;
  const clamped = Math.max(-8, Math.min(8, score));
  const whiteShare = 1 / (1 + Math.exp(-clamped));
  const percent = whiteShare * 100;
  const whiteWinChance = Math.round(percent);
  document.querySelector("#eval-score").textContent = `${score >= 0 ? "+" : ""}${score.toFixed(1)}`;
  document.querySelector("#eval-white").style.width = `${percent}%`;
  document.querySelector("#eval-marker").style.left = `${percent}%`;
  document.querySelector("#white-win-chance").textContent = `${whiteWinChance}%`;
  document.querySelector("#black-win-chance").textContent = `${100 - whiteWinChance}%`;
}

function renderTurn() {
  const white = state.game.turn === "w";
  const boardColumn = document.querySelector(".board-column");
  const boardFrame = document.querySelector(".board-frame");
  const whiteStrip = document.querySelector("#white-name").closest(".player-strip");
  const blackStrip = document.querySelector("#black-name").closest(".player-strip");
  const currentStrip = state.game.playerSide === "w" ? whiteStrip : blackStrip;
  const opponentStrip = state.game.playerSide === "w" ? blackStrip : whiteStrip;
  boardColumn.insertBefore(opponentStrip, boardFrame);
  boardFrame.after(currentStrip);
  currentStrip.classList.add("current-strip");
  currentStrip.classList.remove("opponent-strip");
  opponentStrip.classList.add("opponent-strip");
  opponentStrip.classList.remove("current-strip");
  currentStrip.querySelector(".player-meta small").textContent =
    state.isSpectator ? "WHITE" : "YOU";
  opponentStrip.querySelector(".player-meta small").textContent =
    state.isSpectator ? "BLACK" : "OPPONENT";
  document.querySelector("#white-name").textContent = state.game.whiteName.toUpperCase();
  document.querySelector("#black-name").textContent = state.game.blackName.toUpperCase();
  document.querySelector("#white-rating").textContent = `${state.game.whiteRating} ELO`;
  document.querySelector("#black-rating").textContent = `${state.game.blackRating} ELO`;
  document.querySelector("#room-label").textContent =
    `${state.isSpectator ? "SPECTATING" : "ROOM"} ${state.game.roomCode}`;
  document.querySelector("#time-control-label").textContent = state.game.timeControl;
  renderClocks();
  document.querySelector("#turn-name").textContent =
    state.game.over ? "MATCH COMPLETE" : `${white ? "WHITE" : "BLACK"} TO MOVE`;
  document.querySelector("#turn-subtitle").textContent =
    state.game.result || (state.isSpectator
      ? "You are watching this live match."
      : state.game.status === "waiting"
      ? "Waiting for your opponent to join."
      : state.game.status === "active" && state.game.clockStartedAt === null
        ? state.game.history.length === 0
          ? "White's clock starts when White makes the first move."
          : "Clock paused until both players are in the game."
        : state.game.botElo !== null && state.game.botElo !== undefined &&
          state.game.turn === "b"
          ? "Stockfish is thinking."
        : state.game.playerSide !== state.game.turn
          ? "Waiting for your opponent. Select a piece and destination to premove."
          : state.game.check ? "King is in check." : "Select a piece to move.");
  document.querySelector("#turn-piece").textContent = white ? "♙" : "♟";
  document.querySelector("#check-banner").classList.toggle("hidden", !state.game.check || state.game.over);
}

function renderChatBubbles() {
  for (const side of ["w", "b"]) {
    const bubble = document.querySelector(`#${side === "w" ? "white" : "black"}-chat-bubble`);
    const typing = Boolean(state.chatTyping[side]);
    const latest = [...state.chatMessages].reverse().find((entry) => entry.side === side);
    const key = typing ? `typing:${side}` : latest ? `message:${latest.id}` : "";
    if (state.chatBubbleKey[side] === key) continue;
    state.chatBubbleKey[side] = key;
    bubble.classList.remove("typing");
    bubble.replaceChildren();
    if (typing) {
      bubble.classList.add("typing");
      bubble.setAttribute("aria-label", `${side === (state.chatPlayerSide || state.game.playerSide) ? "You are" : "Your opponent is"} typing`);
      const label = document.createElement("span");
      label.textContent = "Typing";
      const dots = document.createElement("span");
      dots.className = "typing-dots";
      dots.setAttribute("aria-hidden", "true");
      for (let index = 0; index < 3; index += 1) dots.append(document.createElement("i"));
      bubble.append(label, dots);
      bubble.classList.remove("hidden");
    } else if (latest) {
      bubble.textContent = latest.message;
      bubble.title = latest.authorName;
      bubble.setAttribute("aria-label", `${latest.authorName} said: ${latest.message}`);
      bubble.classList.remove("hidden");
    } else {
      bubble.classList.add("hidden");
    }
  }
}

function renderChatLog() {
  const key = state.chatMessages.map((entry) => entry.id).join(",");
  if (state.chatMessageKey === key) return;
  state.chatMessageKey = key;
  const container = document.querySelector("#chat-messages");
  const wasNearBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight < 48;
  container.replaceChildren();
  if (!state.chatMessages.length) {
    const empty = document.createElement("p");
    empty.className = "empty-history";
    empty.textContent = "Say hello to your opponent.";
    container.append(empty);
    return;
  }
  for (const entry of state.chatMessages) {
    const message = document.createElement("article");
    const isOwnMessage = entry.side === (state.chatPlayerSide || state.game.playerSide);
    message.className = `chat-entry${isOwnMessage ? " own" : ""}`;
    const author = document.createElement("strong");
    author.textContent = isOwnMessage ? "You" : entry.authorName;
    const body = document.createElement("p");
    body.textContent = entry.message;
    const sentAt = document.createElement("time");
    sentAt.dateTime = new Date(entry.createdAt).toISOString();
    sentAt.textContent = new Date(entry.createdAt).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
    message.append(author, body, sentAt);
    container.append(message);
  }
  if (wasNearBottom) container.scrollTop = container.scrollHeight;
}

async function pollRoomChat() {
  if (!state.roomCode || state.isSpectator) return;
  try {
    const result = await api(roomApiPath("/chat"));
    state.chatMessages = result.messages;
    state.chatTyping = result.typing;
    state.chatPlayerSide = result.playerSide;
    renderChatLog();
    renderChatBubbles();
  } catch (error) {
    showToast(error.message);
  } finally {
    if (state.chatPollTimer) window.clearTimeout(state.chatPollTimer);
    if (!state.isSpectator && state.roomCode) {
      state.chatPollTimer = window.setTimeout(pollRoomChat, 1000);
    }
  }
}

async function setRoomTyping(isTyping) {
  try {
    await api(roomApiPath("/chat/typing"), {
      method: "POST",
      body: JSON.stringify({ isTyping }),
    });
  } catch (error) {
    showToast(error.message);
  }
}

function render() {
  if (!state.game) return;
  document.querySelector(".app-shell").classList.toggle(
    "review-mode",
    state.reviewPly !== null && state.game.over && !state.isSpectator,
  );
  document.querySelector("#spectator-banner").classList.toggle("hidden", !state.isSpectator);
  document.querySelector("#chat-panel").classList.toggle("hidden", state.isSpectator);
  const quickChatButton = document.querySelector("#open-quick-chat");
  quickChatButton.classList.toggle("hidden", state.isSpectator);
  if (state.isSpectator) {
    document.querySelector("#chat-panel").classList.remove("mobile-chat-open");
    quickChatButton.setAttribute("aria-expanded", "false");
    quickChatButton.innerHTML = '<span aria-hidden="true">▰</span> Chat';
  }
  document.querySelector(".action-row").classList.toggle("hidden", state.isSpectator);
  renderBoard();
  renderTurn();
  renderChatBubbles();
  renderHistory();
  renderCaptured();
  renderEvaluation();
  document.querySelector("#forfeit-game").disabled =
    state.game.over || state.game.status !== "active" || state.busy;
  const closeRoomButton = document.querySelector("#close-room");
  closeRoomButton.disabled = state.game.over || state.busy ||
    (state.game.status === "waiting" && !state.game.canCloseWaitingRoom);
  document.querySelector("#new-game").disabled = !state.game.over || state.busy;
  const roomNotice = document.querySelector("#room-notice");
  const invitedPlayerWaiting = state.game.status === "waiting" &&
    state.requiresAcceptance;
  const creatorWaiting = state.game.status === "waiting" &&
    state.game.canCloseWaitingRoom;
  const showRoomNotice = !state.isSpectator &&
    (invitedPlayerWaiting || creatorWaiting);
  roomNotice.classList.toggle("hidden", !showRoomNotice);
  roomNotice.classList.toggle(
    "opponent-present",
    showRoomNotice && state.game.opponentPresent,
  );
  const noticeText = document.querySelector("#room-notice-text");
  const noticeAction = document.querySelector("#room-notice-action");
  if (invitedPlayerWaiting) {
    const inviterName = state.game.playerSide === "w"
      ? state.game.blackName : state.game.whiteName;
    noticeText.textContent = state.game.opponentPresent
      ? `${inviterName} is here. Join their game when you’re ready.`
      : `${inviterName} invited you to play. Join when you’re ready.`;
    noticeAction.textContent = "Join game";
  } else if (creatorWaiting) {
    noticeText.textContent = state.game.opponentPresent
      ? "Your opponent is here and ready to join."
      : `Waiting for your opponent to join room ${state.game.roomCode}.`;
    noticeAction.textContent = "Back to lobby";
  }
  const rematchModal = document.querySelector("#rematch-modal");
  const rematchRequestedBy = state.game.rematchRequestedBy;
  const isRematchRequester = rematchRequestedBy === state.game.playerSide;
  rematchModal.classList.toggle("hidden", state.isSpectator || !rematchRequestedBy);
  document.querySelector("#rematch-title").textContent = isRematchRequester
    ? "Rematch requested"
    : "Your opponent wants a rematch";
  document.querySelector("#rematch-subtitle").textContent = isRematchRequester
    ? "Waiting for your opponent to accept. The clocks will stay paused."
    : "Accept to start a new game with the same time control.";
  document.querySelector("#accept-rematch").classList.toggle("hidden", isRematchRequester);
  document.querySelector("#decline-rematch").textContent =
    isRematchRequester ? "Cancel request" : "Decline";
  if (!state.isSpectator && state.game.over && !state.modalShown) {
    state.modalShown = true;
    openEloModal(state.game.result);
  }
}

function animateCapture() {
  const last = state.game.lastMove;
  if (!last?.capturedAt) return;
  const target = squares.get(last.capturedAt);
  if (!target) return;
  target.classList.remove("capture-impact");
  void target.offsetWidth;
  target.classList.add("capture-impact");
  const effects = document.querySelector("#capture-effects");
  const boardRect = boardElement.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const burst = document.createElement("div");
  burst.className = "capture-burst";
  burst.style.left = `${targetRect.left - boardRect.left + targetRect.width / 2}px`;
  burst.style.top = `${targetRect.top - boardRect.top + targetRect.height / 2}px`;
  burst.innerHTML = '<i class="burst-ring"></i><i class="burst-ring second"></i>';
  for (let index = 0; index < 12; index += 1) {
    const spark = document.createElement("i");
    spark.className = "burst-spark";
    spark.style.setProperty("--angle", `${index * 30}deg`);
    spark.style.animationDelay = `${(index % 3) * 12}ms`;
    burst.append(spark);
  }
  effects.append(burst);
  window.setTimeout(() => burst.remove(), 650);
  window.setTimeout(() => target.classList.remove("capture-impact"), 500);
}

async function updateGame(next, animate = false) {
  const oldLastMove = state.game?.lastMove;
  const previousGameOver = state.game?.over || false;
  const previousHistory = state.game?.history || [];
  const previousPly = state.game?.history.length || 0;
  if (next.over || next.status !== "active" ||
      next.history.length < previousHistory.length) {
    state.premoves = [];
    state.selected = null;
  }
  const highlightedGrade = state.game
    ? newlyHighlightedGrade(previousHistory, next.history)
    : null;
  state.game = next;
  state.requiresAcceptance = Boolean(next.requiresAcceptance);
  state.clockSyncAt = Date.now();
  if (animate && next.history.length > previousPly) {
    state.animatedPly = next.history.length;
  }
  if (!state.orientationInitialized) {
    state.flipped = next.playerSide === "b";
    state.orientationInitialized = true;
  }
  if (previousGameOver && !next.over) state.modalShown = false;
  if (previousGameOver && !next.over) {
    stopReviewPlayback();
    state.reviewPly = null;
  }
  render();
  if (!previousGameOver && next.over && next.coinsEarned > 0) {
    showToast(`You earned ${next.coinsEarned} coins for this win!`);
  }
  if (highlightedGrade) showGradePopup(highlightedGrade);
  if (animate && next.lastMove?.captured && next.lastMove !== oldLastMove) animateCapture();
  if (state.premoves.length && next.status === "active" && !next.over &&
      next.playerSide === next.turn) {
    const premove = state.premoves.shift();
    state.selected = null;
    renderBoard();
    void sendMove(premove.from, premove.to, premove.promotion, true);
  }
  void playBotMove();
}

async function sendMove(from, to, promotion, isPremove = false) {
  if (state.busy) return;
  state.busy = true;
  state.selected = null;
  const beforeFen = state.game.fen;
  try {
    const next = await api(roomApiPath("/move"), {
      method: "POST",
      body: JSON.stringify({ from, to, ...(promotion ? { promotion } : {}) }),
    });
    await updateGame(next, true);
    void analyzeLastMove(beforeFen, next, next.history.length).catch((error) => {
      if (error.status !== 409) showToast(error.message);
    });
    if (next.check && !next.over) showToast("Check! Protect your king.");
  } catch (error) {
    showToast(isPremove ? `Premove cancelled: ${error.message}` : error.message);
  } finally {
    state.busy = false;
    renderBoard();
    document.querySelector("#forfeit-game").disabled = state.game.over;
    void playBotMove();
  }
}

function queuePremove(from, to) {
  if (state.game.botElo !== null && state.game.botElo !== undefined) return false;
  if (from === to) return false;
  const fromCoords = squareCoords(from);
  const toCoords = squareCoords(to);
  const piece = state.game.board[fromCoords.row][fromCoords.col];
  const target = state.game.board[toCoords.row][toCoords.col];
  if (piece?.[0] !== state.game.playerSide || target?.[0] === state.game.playerSide) {
    return false;
  }
  const promotionRank = state.game.playerSide === "w" ? "8" : "1";
  const promotion = piece[1] === "p" && to[1] === promotionRank ? "q" : undefined;
  state.premoves.push({ from, to, promotion });
  state.selected = null;
  showToast(`Premove ${state.premoves.length} queued. Select a numbered piece to cancel it.`);
  renderBoard();
  return true;
}

async function onSquareClick(name) {
  if (state.isSpectator || state.reviewPly !== null || !name || !state.game || state.game.over || state.busy) return;
  if (state.game.status !== "active") return;
  const isOurTurn = state.game.playerSide === state.game.turn;
  if (!isOurTurn) {
    const piece = state.game.board[squareCoords(name).row][squareCoords(name).col];
    const queuedIndex = state.premoves.findIndex((move) => move.from === name);
    if (queuedIndex !== -1) {
      state.premoves.splice(queuedIndex, 1);
      state.selected = null;
      showToast(`Premove cancelled. ${state.premoves.length} remain queued.`);
      renderBoard();
      return;
    }
    if (piece?.[0] === state.game.playerSide) {
      state.selected = name;
      renderBoard();
      return;
    }
    if (state.selected) {
      if (queuePremove(state.selected, name)) {
        return;
      }
    }
    return;
  }
  const piece = state.game.board[squareCoords(name).row][squareCoords(name).col];
  if (!state.selected) {
    if (piece?.[0] === state.game.turn) {
      state.selected = name;
      renderBoard();
    }
    return;
  }
  if (piece?.[0] === state.game.turn) {
    state.selected = name;
    renderBoard();
    return;
  }
  const moves = state.game.legalMoves.filter((move) => move.from === state.selected && move.to === name);
  if (!moves.length) {
    state.selected = null;
    renderBoard();
    return;
  }
  let promotion;
  if (moves.some((move) => move.promotion)) promotion = await choosePromotion(state.game.turn);
  if (!promotion && moves.some((move) => move.promotion)) return;
  await sendMove(state.selected, name, promotion);
}

function choosePromotion(color) {
  const modal = document.querySelector("#promotion-modal");
  const options = modal.querySelectorAll("[data-promotion]");
  for (const button of options) {
    button.textContent = symbols[`${color}${button.dataset.promotion}`];
  }
  modal.classList.remove("hidden");
  return new Promise((resolve) => {
    const finish = (choice) => {
      modal.classList.add("hidden");
      for (const button of options) button.removeEventListener("click", onChoice);
      resolve(choice);
    };
    const onChoice = (event) => finish(event.currentTarget.dataset.promotion);
    for (const button of options) button.addEventListener("click", onChoice, { once: true });
  });
}

function openEloModal(result) {
  const modal = document.querySelector("#elo-modal");
  document.querySelector("#result-title").textContent = result;
  const changes = state.game.ratingChanges;
  if (changes?.white && changes?.black) {
    const describeChange = (color, name) => {
      const change = changes[color].change;
      return `${name}: ${change >= 0 ? "+" : ""}${change} (${changes[color].before} → ${changes[color].after})`;
    };
    document.querySelector("#elo-result").textContent =
      `${describeChange("white", state.game.whiteName)}\n${describeChange("black", state.game.blackName)}`;
  } else {
    document.querySelector("#elo-result").textContent =
      "No rating change. Ratings update for matches between two registered accounts.";
  }
  modal.classList.remove("hidden");
}

async function newGame() {
  if (!state.game?.over) return;
  try {
    if (state.game.botElo !== null && state.game.botElo !== undefined) {
      const next = await api("/api/bot-games", {
        method: "POST",
        body: JSON.stringify({
          botElo: state.game.botElo,
          timeControl: state.game.timeControlKey,
        }),
      });
      window.location.href = `/play?room=${encodeURIComponent(next.roomCode)}`;
      return;
    }
    const next = await api(roomApiPath("/new-game"), { method: "POST", body: "{}" });
    document.querySelector("#elo-modal").classList.add("hidden");
    await updateGame(next);
    showToast("Rematch request sent.");
  } catch (error) {
    showToast(error.message);
  }
}

async function respondToRematch(accept) {
  try {
    const next = await api(roomApiPath("/new-game/respond"), {
      method: "POST",
      body: JSON.stringify({ accept }),
    });
    if (accept) {
      state.selected = null;
      state.startedAt = Date.now();
      state.animatedPly = 0;
      state.gradeAnimationPlayedPly = 0;
      document.querySelector("#elo-modal").classList.add("hidden");
      showToast("Rematch accepted. The game is ready.");
    } else {
      showToast(next.rematchRequestedBy
        ? "Rematch request cancelled."
        : "Rematch declined.");
    }
    await updateGame(next);
  } catch (error) {
    showToast(error.message);
  }
}

async function forfeitGame() {
  if (state.game?.over || state.busy ||
      !window.confirm("Forfeit this game? The other player will win.")) return;
  state.busy = true;
  state.selected = null;
  try {
    const next = await api(roomApiPath("/forfeit"), { method: "POST", body: "{}" });
    await updateGame(next);
  } catch (error) {
    showToast(error.message);
  } finally {
    state.busy = false;
    render();
  }
}

async function closeRoom() {
  if (!state.game || state.game.over || state.busy) return;
  const confirmation = state.game.status === "active"
    ? "Close this active game? This will forfeit the match and may change both players’ ratings."
    : "Close this waiting room? Your opponent will no longer be able to join.";
  if (!window.confirm(confirmation)) return;
  state.busy = true;
  document.querySelector("#close-room").disabled = true;
  try {
    await api(roomApiPath("/close"), { method: "POST", body: "{}" });
    window.location.replace("/");
  } catch (error) {
    showToast(error.message);
    state.busy = false;
    render();
  }
}

function updateClock() {
  renderClocks();
}

function formatClock(milliseconds) {
  const seconds = Math.ceil(Math.max(0, milliseconds) / 1000);
  const minutes = Math.floor(seconds / 60);
  const remainder = String(seconds % 60).padStart(2, "0");
  return `${minutes}:${remainder}`;
}

function renderClocks() {
  if (!state.game) return;
  const elapsed = state.game.status === "active" && !state.game.over &&
    state.game.clockStartedAt !== null
    ? Date.now() - state.clockSyncAt : 0;
  for (const [color, side] of [["w", "white"], ["b", "black"]]) {
    const isRunning = state.game.turn === color && state.game.status === "active" &&
      state.game.clockStartedAt !== null;
    const remaining = state.game[`${side}TimeMs`] - (isRunning ? elapsed : 0);
    const clock = document.querySelector(`#${side}-clock`);
    clock.textContent = formatClock(remaining);
    clock.classList.toggle("active", isRunning && !state.game.over);
    clock.classList.toggle("low", remaining <= 20000);
  }
  const playerClock = state.game.playerSide === "w"
    ? state.game.whiteTimeMs - (state.game.turn === "w" ? elapsed : 0)
    : state.game.blackTimeMs - (state.game.turn === "b" ? elapsed : 0);
  const oneMinuteWarning = !state.isSpectator &&
    state.game.status === "active" && state.game.clockStartedAt !== null &&
    !state.game.over &&
    playerClock <= 60000 && playerClock > 20000;
  const minuteWarning = document.querySelector("#time-warning");
  minuteWarning.classList.toggle("hidden", !oneMinuteWarning);
  minuteWarning.setAttribute("aria-hidden", String(!oneMinuteWarning));
  const warningActive = !state.isSpectator &&
    state.game.status === "active" && state.game.clockStartedAt !== null &&
    !state.game.over &&
    playerClock <= 20000;
  const warning = document.querySelector("#low-time-warning");
  warning.classList.toggle("hidden", !warningActive);
  warning.setAttribute("aria-hidden", String(!warningActive));
  document.querySelector("#low-time-countdown").textContent = formatClock(playerClock);
  if (warningActive !== state.lowTimeWarningActive) {
    document.querySelector("#low-time-announcement").textContent = warningActive
      ? "Low time. You have 20 seconds or less remaining."
      : "";
    state.lowTimeWarningActive = warningActive;
  }
}

document.querySelector("#new-game").addEventListener("click", newGame);
document.querySelector("#open-game-review").addEventListener("click", () => {
  document.querySelector("#elo-modal").classList.add("hidden");
  openGameReview();
});
document.querySelector("#start-game-review").addEventListener("click", () => openGameReview());
document.querySelector("#review-previous").addEventListener("click", () => {
  if (state.reviewPly > 1) openGameReview(state.reviewPly - 1);
});
document.querySelector("#review-next").addEventListener("click", () => {
  if (state.reviewPly < state.game.history.length) openGameReview(state.reviewPly + 1);
});
document.querySelector("#review-play").addEventListener("click", () => {
  if (state.reviewPlaying) {
    stopReviewPlayback();
    render();
  } else {
    startReviewPlayback();
  }
});
document.querySelector("#review-exit").addEventListener("click", () => {
  stopReviewPlayback();
  state.reviewPly = null;
  state.reviewCandidates = [];
  state.reviewSelectedCandidate = null;
  render();
});
document.querySelector("#accept-rematch").addEventListener("click", () => {
  void respondToRematch(true);
});
document.querySelector("#decline-rematch").addEventListener("click", () => {
  void respondToRematch(false);
});
document.querySelector("#forfeit-game").addEventListener("click", forfeitGame);
document.querySelector("#close-room").addEventListener("click", closeRoom);
document.querySelector("#room-notice-action").addEventListener("click", async () => {
  const action = document.querySelector("#room-notice-action");
  if (!state.game) return;
  if (!state.requiresAcceptance) {
    window.location.href = "/";
    return;
  }
  action.disabled = true;
  try {
    const joined = await api(roomApiPath("/join"), {
      method: "POST",
      body: "{}",
    });
    await updateGame(joined);
  } catch (error) {
    showToast(error.message);
    action.disabled = false;
  }
});
document.querySelector("#flip-board").addEventListener("click", () => {
  state.flipped = !state.flipped;
  renderBoard();
});
document.querySelector("#share-room").addEventListener("click", async () => {
  const inviteUrl = new URL(`/play?room=${encodeURIComponent(state.roomCode)}`, window.location.origin).href;
  try {
    await navigator.clipboard.writeText(inviteUrl);
    showToast("Invite link copied.");
  } catch {
    window.prompt("Copy this invite link to share with your opponent:", inviteUrl);
  }
});
document.querySelector("#close-modal").addEventListener("click", () => {
  document.querySelector("#elo-modal").classList.add("hidden");
});

async function loadInitialGame() {
  let initial;
  if (state.isSpectator) {
    initial = await api(roomApiPath("/spectate"));
    state.startedAt = Date.now();
    await updateGame(initial);
    return;
  }
  try {
    initial = await api(roomApiPath());
  } catch (error) {
    if (error.status !== 403) throw error;
    initial = await api(roomApiPath("/join"), { method: "POST", body: "{}" });
  }
  state.startedAt = Date.now();
  await updateGame(initial);
  if (!state.isSpectator) void pollRoomChat();
}

document.querySelector("#elo-modal").addEventListener("click", (event) => {
  if (event.target.id === "elo-modal") event.currentTarget.classList.add("hidden");
});
document.querySelector("#chat-input").addEventListener("input", (event) => {
  const input = event.currentTarget;
  if (!input.value.trim()) return;
  const now = Date.now();
  if (now - state.lastTypingSentAt < 1000) return;
  state.lastTypingSentAt = now;
  void setRoomTyping(true);
});
document.querySelector("#chat-input").addEventListener("blur", () => {
  void setRoomTyping(false);
  state.lastTypingSentAt = 0;
});
async function sendRoomChatMessage(message) {
  const input = document.querySelector("#chat-input");
  const sendButton = document.querySelector("#chat-send");
  const trimmedMessage = message.trim();
  if (!trimmedMessage) return;
  sendButton.disabled = true;
  try {
    await api(roomApiPath("/chat"), {
      method: "POST",
      body: JSON.stringify({ message: trimmedMessage }),
    });
    if (input.value.trim() === trimmedMessage) input.value = "";
    state.lastTypingSentAt = 0;
    void setRoomTyping(false);
    await pollRoomChat();
  } catch (error) {
    showToast(error.message);
  } finally {
    sendButton.disabled = false;
  }
}
document.querySelector("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendRoomChatMessage(document.querySelector("#chat-input").value);
});
document.querySelector("#open-quick-chat").addEventListener("click", (event) => {
  const panel = document.querySelector("#chat-panel");
  const isOpening = panel.classList.toggle("mobile-chat-open");
  event.currentTarget.setAttribute("aria-expanded", String(isOpening));
  event.currentTarget.innerHTML = isOpening
    ? '<span aria-hidden="true">×</span> Close'
    : '<span aria-hidden="true">▰</span> Chat';
  if (isOpening) document.querySelector("#chat-input").focus();
});
document.querySelector("#close-quick-chat").addEventListener("click", () => {
  document.querySelector("#chat-panel").classList.remove("mobile-chat-open");
  const button = document.querySelector("#open-quick-chat");
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = '<span aria-hidden="true">▰</span> Chat';
});
for (const quickMessage of document.querySelectorAll(".quick-message-button")) {
  quickMessage.addEventListener("click", () => {
    void sendRoomChatMessage(quickMessage.dataset.message || "");
  });
}
window.addEventListener("pagehide", () => {
  if (state.isSpectator || !state.roomCode || !state.game || state.game.status === "completed") return;
  const body = new Blob(["{}"], { type: "application/json" });
  navigator.sendBeacon(roomApiPath("/leave"), body);
});
makeBoard();
if (!state.roomCode) {
  window.location.replace("/");
} else {
  loadInitialGame()
    .catch((error) => {
      showToast(error.message);
      window.setTimeout(() => window.location.replace("/"), 1800);
    });
}
async function pollGameState() {
  if (!state.roomCode) return;
  try {
    if (state.game && !state.busy) {
      const next = await api(roomApiPath(state.isSpectator ? "/spectate" : ""));
      if (next.history.length !== state.game.history.length ||
          next.status !== state.game.status || next.result !== state.game.result ||
          next.opponentPresent !== state.game.opponentPresent ||
          Boolean(next.requiresAcceptance) !== state.requiresAcceptance ||
          next.clockStartedAt !== state.game.clockStartedAt ||
          next.rematchRequestedBy !== state.game.rematchRequestedBy ||
          JSON.stringify(next.ratingChanges) !== JSON.stringify(state.game.ratingChanges) ||
          next.history.some((move, index) =>
            move.grade !== state.game.history[index]?.grade ||
            move.bestMoveSan !== state.game.history[index]?.bestMoveSan)) {
        updateGame(next, next.history.length > state.game.history.length);
      } else {
        state.game.whiteTimeMs = next.whiteTimeMs;
        state.game.blackTimeMs = next.blackTimeMs;
        state.game.clockStartedAt = next.clockStartedAt;
        state.clockSyncAt = Date.now();
        renderClocks();
      }
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    const pollDelay = state.premoves.length ? 400 : 1000;
    window.setTimeout(pollGameState, pollDelay);
  }
}
window.setTimeout(pollGameState, 1000);
window.setInterval(updateClock, 100);
