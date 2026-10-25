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
const state = {
  game: null,
  roomCode: new URLSearchParams(window.location.search).get("room")?.toUpperCase() || "",
  selected: null,
  flipped: false,
  orientationInitialized: false,
  clockSyncAt: Date.now(),
  engineReady: null,
  enginePending: null,
  busy: false,
  startedAt: Date.now(),
  modalShown: false,
  toastTimer: null,
  gradePopupTimer: null,
  animatedPly: 0,
  gradeAnimationPlayedPly: 0,
};
const squares = new Map();
const gridSquares = new Map();
const squareParts = new Map();
const stockfish = new Worker("/engine/stockfish-19-lite-single.js");
const gradePopupNames = new Set(["Brilliant", "Excellent", "Blunder", "Grave error"]);

state.engineReady = new Promise((resolve, reject) => {
  const timeout = window.setTimeout(
    () => reject(new Error("Stockfish took too long to start.")),
    15000,
  );
  stockfish.onerror = () => {
    window.clearTimeout(timeout);
    reject(new Error("The Stockfish analysis engine could not be loaded."));
  };
  stockfish.onmessage = (event) => {
    const line = String(event.data);
    if (line === "uciok") {
      stockfish.postMessage("setoption name Threads value 1");
      stockfish.postMessage("setoption name Hash value 16");
      stockfish.postMessage("isready");
    } else if (line === "readyok") {
      window.clearTimeout(timeout);
      resolve();
    }
    const pending = state.enginePending;
    if (!pending) return;
    const score = line.match(/\bscore (cp|mate) (-?\d+)/);
    if (score) {
      pending.score = score[1] === "mate"
        ? (Number(score[2]) > 0 ? 10000 : -10000)
        : Number(score[2]);
    }
    if (line.startsWith("bestmove")) {
      window.clearTimeout(pending.timeout);
      state.enginePending = null;
      if (pending.score === null) {
        pending.reject(new Error("Stockfish did not return an evaluation."));
      } else {
        pending.resolve(pending.score);
      }
    }
  };
  stockfish.postMessage("uci");
});

let engineQueue = Promise.resolve();

function stockfishScore(fen) {
  const request = engineQueue.then(async () => {
    await state.engineReady;
    return new Promise((resolve, reject) => {
      const pending = {
        score: null,
        resolve,
        reject,
        timeout: window.setTimeout(() => {
          state.enginePending = null;
          reject(new Error("Stockfish analysis timed out."));
        }, 12000),
      };
      state.enginePending = pending;
      stockfish.postMessage(`position fen ${fen}`);
      stockfish.postMessage("go movetime 180");
    });
  });
  engineQueue = request.catch(() => {});
  return request;
}

async function analyzeLastMove(beforeFen, next, ply) {
  const bestBefore = await stockfishScore(beforeFen);
  const afterScore = await stockfishScore(next.fen);
  const centipawnLoss = Math.max(0, Math.min(3000, Math.round(bestBefore + afterScore)));
  const whiteEvaluation = Math.max(
    -10000,
    Math.min(10000, Math.round(next.turn === "w" ? afterScore : -afterScore)),
  );
  const analyzed = await api(roomApiPath("/analysis"), {
    method: "POST",
    body: JSON.stringify({
      ply,
      centipawnLoss,
      evaluation: whiteEvaluation,
    }),
  });
  await updateGame(analyzed);
}

function squareName(row, col) {
  return `${"abcdefgh"[col]}${8 - row}`;
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
  state.gradePopupTimer = window.setTimeout(() => popup.remove(), 1800);
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

function makeBoard() {
  for (let displayRow = 0; displayRow < 8; displayRow += 1) {
    for (let displayCol = 0; displayCol < 8; displayCol += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "square";
      button.setAttribute("role", "gridcell");
      button.addEventListener("click", () => onSquareClick(button.dataset.square));
      button.addEventListener("dragover", (event) => event.preventDefault());
      button.addEventListener("drop", onPieceDrop);
      const rank = document.createElement("span");
      rank.className = "coordinate rank";
      const file = document.createElement("span");
      file.className = "coordinate file";
      const dot = document.createElement("span");
      dot.className = "legal-dot";
      const piece = document.createElement("span");
      piece.className = "piece";
      piece.addEventListener("dragstart", (event) => {
        const name = button.dataset.square;
        state.selected = name;
        event.dataTransfer.setData("text/plain", name);
        renderBoard();
      });
      button.append(rank, file, dot, piece);
      gridSquares.set(`${displayRow},${displayCol}`, button);
      squareParts.set(button, { rank, file, dot, piece });
      boardElement.append(button);
    }
  }
}

function setBoardOrientation() {
  const legalDestinations = state.selected
    ? new Set(
      state.game.legalMoves
        .filter((move) => move.from === state.selected)
        .map((move) => move.to),
    )
    : new Set();
  const currentKing = state.game.check
    ? state.game.board.flatMap((row, r) => row.map((piece, c) => ({
      piece, square: squareName(r, c),
    }))).find(({ piece }) => piece === `${state.game.turn}k`)?.square
    : null;
  squares.clear();

  for (let displayRow = 0; displayRow < 8; displayRow += 1) {
    for (let displayCol = 0; displayCol < 8; displayCol += 1) {
      const row = state.flipped ? 7 - displayRow : displayRow;
      const col = state.flipped ? 7 - displayCol : displayCol;
      const name = squareName(row, col);
      const pieceCode = state.game.board[row][col];
      const square = gridSquares.get(`${displayRow},${displayCol}`);
      const parts = squareParts.get(square);
      square.style.gridRow = String(displayRow + 1);
      square.style.gridColumn = String(displayCol + 1);
      square.className = `square ${(row + col) % 2 === 0 ? "light" : "dark"}`;
      square.dataset.square = name;
      squares.set(name, square);
      square.setAttribute("aria-label", `${pieceCode ? `${pieceCode[0] === "w" ? "White" : "Black"} ${pieceCode[1]}` : "Empty"} ${name}`);
      if (state.game.lastMove && [state.game.lastMove.from, state.game.lastMove.to].includes(name)) {
        square.classList.add("last-move");
      }
      if (state.selected === name) square.classList.add("selected");
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
      parts.piece.className = pieceCode
        ? `piece ${pieceCode[0] === "w" ? "white" : "black"}`
        : "piece";
      parts.piece.draggable = Boolean(pieceCode) && !state.busy &&
        !state.game.over && state.game.status === "active" &&
        state.game.playerSide === state.game.turn && pieceCode[0] === state.game.turn;
    }
  }
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
  const wrapper = document.createElement("div");
  wrapper.className = "history-move";
  if (index === state.game.history.length - 1) wrapper.classList.add("latest");
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
  currentStrip.querySelector(".player-meta small").textContent = "YOU";
  opponentStrip.querySelector(".player-meta small").textContent = "OPPONENT";
  document.querySelector("#white-name").textContent = state.game.whiteName.toUpperCase();
  document.querySelector("#black-name").textContent = state.game.blackName.toUpperCase();
  document.querySelector("#white-rating").textContent = `${state.game.whiteRating} ELO`;
  document.querySelector("#black-rating").textContent = `${state.game.blackRating} ELO`;
  document.querySelector("#room-label").textContent = `ROOM ${state.game.roomCode}`;
  document.querySelector("#time-control-label").textContent = state.game.timeControl;
  renderClocks();
  document.querySelector("#turn-name").textContent =
    state.game.over ? "MATCH COMPLETE" : `${white ? "WHITE" : "BLACK"} TO MOVE`;
  document.querySelector("#turn-subtitle").textContent =
    state.game.result || (state.game.status === "waiting"
      ? "Waiting for your opponent to join."
      : state.game.status === "active" && state.game.clockStartedAt === null
        ? "Clock paused until both players are in the game."
        : state.game.playerSide !== state.game.turn
          ? "Waiting for your opponent's move."
          : state.game.check ? "King is in check." : "Select a piece to move.");
  document.querySelector("#turn-piece").textContent = white ? "♙" : "♟";
  document.querySelector("#check-banner").classList.toggle("hidden", !state.game.check || state.game.over);
}

function render() {
  if (!state.game) return;
  renderBoard();
  renderTurn();
  renderHistory();
  renderCaptured();
  renderEvaluation();
  document.querySelector("#forfeit-game").disabled =
    state.game.over || state.game.status !== "active" || state.busy;
  document.querySelector("#new-game").disabled = !state.game.over || state.busy;
  const waitingModal = document.querySelector("#waiting-modal");
  waitingModal.classList.toggle(
    "hidden",
    state.game.status !== "waiting" || state.game.playerSide !== "w",
  );
  document.querySelector("#waiting-room-code").textContent = state.game.roomCode;
  const rematchModal = document.querySelector("#rematch-modal");
  const rematchRequestedBy = state.game.rematchRequestedBy;
  const isRematchRequester = rematchRequestedBy === state.game.playerSide;
  rematchModal.classList.toggle("hidden", !rematchRequestedBy);
  document.querySelector("#rematch-title").textContent = isRematchRequester
    ? "Rematch requested"
    : "Your opponent wants a rematch";
  document.querySelector("#rematch-subtitle").textContent = isRematchRequester
    ? "Waiting for your opponent to accept. The clocks will stay paused."
    : "Accept to start a new game with the same time control.";
  document.querySelector("#accept-rematch").classList.toggle("hidden", isRematchRequester);
  document.querySelector("#decline-rematch").textContent =
    isRematchRequester ? "Cancel request" : "Decline";
  if (state.game.over && !state.modalShown) {
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
  const highlightedGrade = state.game
    ? newlyHighlightedGrade(previousHistory, next.history)
    : null;
  state.game = next;
  state.clockSyncAt = Date.now();
  if (animate && next.history.length > previousPly) {
    state.animatedPly = next.history.length;
  }
  if (!state.orientationInitialized) {
    state.flipped = next.playerSide === "b";
    state.orientationInitialized = true;
  }
  if (previousGameOver && !next.over) state.modalShown = false;
  render();
  if (highlightedGrade) showGradePopup(highlightedGrade);
  if (animate && next.lastMove?.captured && next.lastMove !== oldLastMove) animateCapture();
}

async function sendMove(from, to, promotion) {
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
    showToast(error.message);
  } finally {
    state.busy = false;
    renderBoard();
    document.querySelector("#forfeit-game").disabled = state.game.over;
  }
}

async function onSquareClick(name) {
  if (!name || !state.game || state.game.over || state.busy) return;
  if (state.game.status !== "active" || state.game.playerSide !== state.game.turn) return;
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

function onPieceDrop(event) {
  event.preventDefault();
  const from = event.dataTransfer.getData("text/plain");
  const to = event.currentTarget.dataset.square;
  if (from && to) {
    state.selected = from;
    onSquareClick(to);
  }
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
}

document.querySelector("#new-game").addEventListener("click", newGame);
document.querySelector("#accept-rematch").addEventListener("click", () => {
  void respondToRematch(true);
});
document.querySelector("#decline-rematch").addEventListener("click", () => {
  void respondToRematch(false);
});
document.querySelector("#forfeit-game").addEventListener("click", forfeitGame);
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

function confirmInvitation() {
  const modal = document.querySelector("#invitation-modal");
  modal.classList.remove("hidden");
  return new Promise((resolve) => {
    document.querySelector("#accept-invitation").addEventListener("click", () => {
      modal.classList.add("hidden");
      resolve(true);
    }, { once: true });
  });
}

async function loadInitialGame() {
  let initial;
  try {
    initial = await api(roomApiPath());
  } catch (error) {
    if (error.status !== 403) throw error;
    initial = await api(roomApiPath("/join"), { method: "POST", body: "{}" });
  }
  if (initial.requiresAcceptance) {
    if (!await confirmInvitation()) return;
    initial = await api(roomApiPath("/join"), { method: "POST", body: "{}" });
  }
  state.startedAt = Date.now();
  await updateGame(initial);
}

document.querySelector("#elo-modal").addEventListener("click", (event) => {
  if (event.target.id === "elo-modal") event.currentTarget.classList.add("hidden");
});
window.addEventListener("pagehide", () => {
  if (!state.roomCode || !state.game || state.game.status === "completed") return;
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
window.setInterval(async () => {
  if (!state.roomCode || !state.game || state.busy) return;
  try {
    const next = await api(roomApiPath());
    if (next.history.length !== state.game.history.length ||
        next.status !== state.game.status || next.result !== state.game.result ||
        next.clockStartedAt !== state.game.clockStartedAt ||
        next.rematchRequestedBy !== state.game.rematchRequestedBy ||
        JSON.stringify(next.ratingChanges) !== JSON.stringify(state.game.ratingChanges) ||
        next.history.at(-1)?.grade !== state.game.history.at(-1)?.grade) {
      updateGame(next, next.history.length > state.game.history.length);
    } else {
      state.game.whiteTimeMs = next.whiteTimeMs;
      state.game.blackTimeMs = next.blackTimeMs;
      state.game.clockStartedAt = next.clockStartedAt;
      state.clockSyncAt = Date.now();
      renderClocks();
    }
  } catch (error) {
    showToast(error.message);
  }
}, 1000);
window.setInterval(updateClock, 100);
