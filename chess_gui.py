"""Responsive graphical interface for the two-player chess game.

Run with ``python chess_gui.py``. Click a piece, then click its destination.
"""

import tkinter as tk
from tkinter import messagebox
import math
import time

from two_player_chess import ChessGame, Move, Square


BACKGROUND = "#121a24"
PANEL = "#1c2734"
PANEL_LIGHT = "#263545"
TEXT = "#f1f5f9"
MUTED = "#a6b4c3"
ACCENT = "#80cbc4"
BOARD_LIGHT = "#e8dfca"
BOARD_DARK = "#78917e"
PIECE_SYMBOLS = {
    "K": "\u2654", "Q": "\u2655", "R": "\u2656",
    "B": "\u2657", "N": "\u2658", "P": "\u2659",
    "k": "\u265a", "q": "\u265b", "r": "\u265c",
    "b": "\u265d", "n": "\u265e", "p": "\u265f",
}
PIECE_VALUES = {"p": 1.0, "n": 3.2, "b": 3.3, "r": 5.0, "q": 9.0}
MATE_SCORE = 1000.0
RATING_COLORS = {
    "Missed win": "#ec6a76",
    "Blunder": "#ec6a76",
    "Mistake": "#f28c45",
    "Inaccuracy": "#f3c969",
    "Good": "#9bd46a",
    "Excellent": "#52c7a5",
    "Best": "#55d6e8",
    "Great": "#ba8cff",
    "Brilliant": "#ba8cff",
}


def evaluate_position(game: ChessGame, perspective: str) -> float:
    """Estimate a position in pawn units from material and piece activity."""
    score = 0.0
    for row, board_row in enumerate(game.board):
        for col, piece in enumerate(board_row):
            if piece == ".":
                continue
            kind = piece.lower()
            if kind == "k":
                continue
            sign = 1 if game.color_of(piece) == perspective else -1
            score += sign * PIECE_VALUES[kind]

            center_distance = abs(3.5 - row) + abs(3.5 - col)
            if kind in ("n", "b", "q"):
                score += sign * max(0.0, 3.5 - center_distance / 2) * 0.035
            elif kind == "p":
                advance = 6 - row if piece.isupper() else row - 1
                score += sign * max(0, advance) * 0.025

    enemy = "b" if perspective == "w" else "w"
    if game.in_check(enemy):
        score += 0.08
    if game.in_check(perspective):
        score -= 0.08
    return score


def _score_after_best_reply(game: ChessGame, move: Move, perspective: str) -> float:
    position = game._copy_for_analysis()
    position._apply_unchecked(move)
    replies = position.legal_moves()
    if not replies:
        if position.in_check(position.turn):
            return MATE_SCORE
        return 0.0

    worst_score = float("inf")
    for reply in replies:
        response = position._copy_for_analysis()
        response._apply_unchecked(reply)
        next_moves = response.legal_moves()
        if not next_moves and response.in_check(response.turn):
            score = -MATE_SCORE
        elif not next_moves:
            score = 0.0
        else:
            score = evaluate_position(response, perspective)
        worst_score = min(worst_score, score)
    return worst_score


def analyze_move(game: ChessGame, move: Move) -> tuple[str, int]:
    """Grade a move by its centipawn loss after the opponent's best reply."""
    mover = game.turn
    moves = game.legal_moves()
    if move not in moves:
        raise ValueError("Cannot analyze a move that is not legal in this position.")
    scores = [(candidate, _score_after_best_reply(game, candidate, mover)) for candidate in moves]
    best_score = max(score for _, score in scores)
    chosen_score = next(score for candidate, score in scores if candidate == move)

    if best_score >= MATE_SCORE / 2 and chosen_score < MATE_SCORE / 2:
        return "Missed win", 10000
    if chosen_score <= -MATE_SCORE / 2:
        return "Blunder", 10000

    loss = max(0, round((best_score - chosen_score) * 100))
    if loss <= 10:
        rating = "Best"
    elif loss <= 15:
        rating = "Great"
    elif loss <= 30:
        rating = "Excellent"
    elif loss <= 75:
        rating = "Good"
    elif loss <= 150:
        rating = "Inaccuracy"
    elif loss <= 300:
        rating = "Mistake"
    else:
        rating = "Blunder"
    return rating, loss


def rate_move(game: ChessGame, move: Move) -> str:
    """Return a chess-style category from lightweight two-ply analysis."""
    return analyze_move(game, move)[0]


def calculate_elo(
    white_rating: int, black_rating: int, white_score: float, k_factor: int = 32
) -> tuple[int, int]:
    """Return updated Elo ratings for a white-perspective score of 0, 0.5, or 1."""
    if white_rating < 0 or black_rating < 0:
        raise ValueError("Ratings cannot be negative.")
    if white_score not in (0.0, 0.5, 1.0):
        raise ValueError("Game score must be 0, 0.5, or 1.")
    if k_factor <= 0:
        raise ValueError("K-factor must be positive.")
    expected_white = 1 / (1 + 10 ** ((black_rating - white_rating) / 400))
    white_change = round(k_factor * (white_score - expected_white))
    return white_rating + white_change, black_rating - white_change


class ChessWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Royal Chess")
        self.root.configure(bg=BACKGROUND)
        self.root.resizable(True, True)
        self._set_initial_window_size()

        self.game = ChessGame()
        self.selected: Square | None = None
        self.legal_moves: list[Move] = []
        self.last_move: Move | None = None
        self.game_finished = False
        self.flipped = False
        self.move_history: list[tuple[str, str | None]] = []
        self.move_ratings: list[str] = []
        self.move_cp_losses: list[int] = []
        self.white_rating = 1200
        self.black_rating = 1200
        self.board_bounds = (0.0, 0.0, 0.0, 0.0)
        self.animation_counter = 0
        self.capture_animations: list[tuple[float, Square, str, int]] = []
        self.animation_job: str | None = None

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        content = tk.Frame(self.root, bg=BACKGROUND)
        content.grid(row=0, column=0, sticky="nsew", padx=16, pady=14)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(1, minsize=220)

        self._build_header(content)
        self._build_board(content)
        self._build_sidebar(content)
        self.refresh()

    def _set_initial_window_size(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(900, max(560, screen_width - 80))
        height = min(680, max(460, screen_height - 100))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(560, 460)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=BACKGROUND)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        tk.Label(
            header,
            text="♜",
            font=("Segoe UI Symbol", 23),
            fg=ACCENT,
            bg=BACKGROUND,
        ).pack(side="left", padx=(0, 9))
        brand = tk.Frame(header, bg=BACKGROUND)
        brand.pack(side="left")
        tk.Label(
            brand, text="ROYAL CHESS", font=("Segoe UI", 15, "bold"),
            fg=TEXT, bg=BACKGROUND,
        ).pack(anchor="w")
        tk.Label(
            brand, text="LOCAL TWO-PLAYER", font=("Segoe UI", 8, "bold"),
            fg=MUTED, bg=BACKGROUND,
        ).pack(anchor="w")
        self.turn_badge = tk.Label(
            header,
            text="",
            font=("Segoe UI", 10, "bold"),
            fg=TEXT,
            bg=PANEL_LIGHT,
            padx=12,
            pady=7,
        )
        self.turn_badge.pack(side="right", padx=(8, 0))

    def _build_board(self, parent: tk.Frame) -> None:
        self.canvas = tk.Canvas(
            parent,
            bg=BACKGROUND,
            highlightthickness=0,
            width=540,
            height=540,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        self.canvas.bind("<Configure>", lambda _event: self.draw_board())
        self.canvas.bind("<Button-1>", self.on_board_click)

    def _build_sidebar(self, parent: tk.Frame) -> None:
        sidebar = tk.Frame(parent, bg=BACKGROUND, width=220)
        sidebar.grid(row=1, column=1, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(4, weight=1)

        self.player_card = tk.Frame(sidebar, bg=PANEL, padx=12, pady=10)
        self.player_card.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        tk.Label(
            self.player_card, text="TO MOVE", font=("Segoe UI", 8, "bold"),
            fg=MUTED, bg=PANEL,
        ).pack(anchor="w")
        self.player_label = tk.Label(
            self.player_card, text="", font=("Segoe UI", 15, "bold"),
            fg=TEXT, bg=PANEL,
        )
        self.player_label.pack(anchor="w", pady=(2, 0))

        captured_card = tk.Frame(sidebar, bg=PANEL, padx=12, pady=9)
        captured_card.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        tk.Label(
            captured_card, text="CAPTURED PIECES", font=("Segoe UI", 8, "bold"),
            fg=MUTED, bg=PANEL,
        ).pack(anchor="w")
        self.captured_white = tk.Label(
            captured_card, text="White  —", font=("Segoe UI", 11),
            fg=TEXT, bg=PANEL, anchor="w",
        )
        self.captured_white.pack(fill="x", pady=(5, 1))
        self.captured_black = tk.Label(
            captured_card, text="Black  —", font=("Segoe UI", 11),
            fg=TEXT, bg=PANEL, anchor="w",
        )
        self.captured_black.pack(fill="x")

        analysis_card = tk.Frame(sidebar, bg=PANEL, padx=12, pady=9)
        analysis_card.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        tk.Label(
            analysis_card, text="LIVE EVALUATION", font=("Segoe UI", 8, "bold"),
            fg=MUTED, bg=PANEL,
        ).pack(anchor="w")
        self.eval_label = tk.Label(
            analysis_card, text="0.0", font=("Consolas", 14, "bold"),
            fg=ACCENT, bg=PANEL,
        )
        self.eval_label.pack(anchor="w", pady=(3, 4))
        self.eval_bar = tk.Canvas(
            analysis_card, height=10, bg="#101820", highlightthickness=0
        )
        self.eval_bar.pack(fill="x")
        self.eval_bar.bind("<Configure>", lambda _event: self._draw_eval_bar())
        self.accuracy_label = tk.Label(
            analysis_card, text="Accuracy  W 100%  ·  B 100%",
            font=("Segoe UI", 8, "bold"), fg=ACCENT, bg=PANEL,
        )
        self.accuracy_label.pack(anchor="w", pady=(5, 0))

        history_header = tk.Frame(sidebar, bg=BACKGROUND)
        history_header.grid(row=3, column=0, sticky="ew", pady=(1, 5))
        tk.Label(
            history_header, text="MOVE HISTORY",
            font=("Segoe UI", 8, "bold"),
            fg=MUTED, bg=BACKGROUND,
        ).pack(side="left")
        self.move_count = tk.Label(
            history_header, text="0 moves", font=("Segoe UI", 8),
            fg=MUTED, bg=BACKGROUND,
        )
        self.move_count.pack(side="right")

        history_frame = tk.Frame(sidebar, bg=PANEL)
        history_frame.grid(row=4, column=0, sticky="nsew")
        history_frame.grid_rowconfigure(0, weight=1)
        history_frame.grid_columnconfigure(0, weight=1)
        self.history_text = tk.Text(
            history_frame,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=PANEL_LIGHT,
            font=("Consolas", 10),
            relief="flat",
            padx=10,
            pady=8,
            state="disabled",
            wrap="none",
        )
        self.history_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            history_frame, command=self.history_text.yview, orient="vertical"
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.history_text.configure(yscrollcommand=scrollbar.set)

        tk.Label(
            sidebar,
            text="Move grades use a local 2-ply estimate, not Stockfish analysis.",
            font=("Segoe UI", 8),
            fg=MUTED,
            bg=BACKGROUND,
            wraplength=210,
            justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(7, 0))

        controls = tk.Frame(sidebar, bg=BACKGROUND)
        controls.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self._make_action_button(controls, "↻  New game", self.new_game).pack(
            fill="x", pady=(0, 6)
        )
        self._make_action_button(controls, "⇄  Flip board", self.flip_board).pack(
            fill="x"
        )

    @staticmethod
    def _make_action_button(
        parent: tk.Widget, text: str, command: object
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 10, "bold"),
            fg=TEXT,
            bg=PANEL_LIGHT,
            activeforeground=TEXT,
            activebackground="#35485c",
            relief="flat",
            bd=0,
            padx=12,
            pady=8,
            cursor="hand2",
        )

    def on_board_click(self, event: tk.Event) -> None:
        left, top, right, bottom = self.board_bounds
        if not (left <= event.x < right and top <= event.y < bottom):
            return
        square_size = (right - left) / 8
        display_col = int((event.x - left) / square_size)
        display_row = int((event.y - top) / square_size)
        if self.flipped:
            row, col = 7 - display_row, 7 - display_col
        else:
            row, col = display_row, display_col
        self.on_square_click((row, col))

    def on_square_click(self, square: Square) -> None:
        if self.game_finished:
            return
        row, col = square
        piece = self.game.board[row][col]

        if self.selected is None:
            self.select_piece(square)
            return

        if square == self.selected:
            self.selected = None
            self.legal_moves = []
            self.refresh()
            return

        matching_moves = [move for move in self.legal_moves if move.end == square]
        if matching_moves:
            promotion = self.choose_promotion() if matching_moves[0].promotion else None
            if matching_moves[0].promotion and promotion is None:
                self.selected = None
                self.legal_moves = []
                self.refresh()
                return
            start_name = self.game.square_name(self.selected)
            end_name = self.game.square_name(square)
            chosen_move = next(
                (
                    move for move in matching_moves
                    if move.promotion == promotion
                ),
                matching_moves[0],
            )
            rating, cp_loss = analyze_move(self.game, chosen_move)
            captured_piece, captured_square = self._captured_piece(chosen_move)
            played, message = self.game.play(f"{start_name}{end_name}{promotion or ''}")
            if not played:
                messagebox.showerror("Invalid move", message, parent=self.root)
            else:
                self._record_move(
                    start_name, end_name, promotion, chosen_move, rating,
                    cp_loss, captured_piece, captured_square,
                )
            self.selected = None
            self.legal_moves = []
            self.refresh()
            self.show_game_result()
            return

        if piece != "." and self.game.color_of(piece) == self.game.turn:
            self.select_piece(square)
        else:
            self.selected = None
            self.legal_moves = []
            self.refresh()

    def select_piece(self, square: Square) -> None:
        row, col = square
        piece = self.game.board[row][col]
        if piece == "." or self.game.color_of(piece) != self.game.turn:
            return
        self.selected = square
        self.legal_moves = [
            move for move in self.game.legal_moves() if move.start == square
        ]
        self.refresh()

    def _record_move(
        self,
        start: str,
        end: str,
        promotion: str | None,
        move: Move,
        rating: str,
        cp_loss: int,
        captured_piece: str,
        captured_square: Square,
    ) -> None:
        notation = f"{start}–{end}"
        if promotion:
            notation += f"={promotion.upper()}"
        if self.game.turn == "b":
            self.move_history.append((notation, None))
        else:
            if self.move_history:
                white_move, _ = self.move_history[-1]
                self.move_history[-1] = (white_move, notation)
            else:
                self.move_history.append(("", notation))
        self.last_move = move
        self.move_ratings.append(rating)
        self.move_cp_losses.append(cp_loss)
        self.flipped = self.game.turn == "b"
        self._update_history()
        self._update_captured_pieces()
        self._update_evaluation()
        if captured_piece != ".":
            self.root.after_idle(
                lambda: self.animate_capture(captured_piece, captured_square)
            )

    def _captured_piece(self, move: Move) -> tuple[str, Square]:
        row, col = move.end
        if move.en_passant:
            row += 1 if self.game.turn == "w" else -1
        return self.game.board[row][col], (row, col)

    def _update_history(self) -> None:
        self.history_text.configure(state="normal")
        self.history_text.delete("1.0", "end")
        self.history_text.tag_configure("move", foreground=TEXT)
        for rating, color in RATING_COLORS.items():
            self.history_text.tag_configure(rating, foreground=color)
        for number, (white_move, black_move) in enumerate(self.move_history, start=1):
            white_index = (number - 1) * 2
            if white_move:
                self.history_text.insert("end", f"{number:>2}.  ", "move")
                self._insert_graded_move(white_index, white_move)
            if black_move:
                self.history_text.insert("end", "      ", "move")
                self._insert_graded_move(white_index + 1, black_move)
        self.history_text.configure(state="disabled")
        self.history_text.see("end")
        count = sum(bool(white) + bool(black) for white, black in self.move_history)
        self.move_count.configure(text=f"{count} {'move' if count == 1 else 'moves'}")
        white_losses = self.move_cp_losses[::2]
        black_losses = self.move_cp_losses[1::2]
        white_accuracy = self._accuracy_estimate(white_losses)
        black_accuracy = self._accuracy_estimate(black_losses)
        self.accuracy_label.configure(
            text=f"Accuracy  W {white_accuracy:.0f}%  ·  B {black_accuracy:.0f}%"
        )

    @staticmethod
    def _accuracy_estimate(losses: list[int]) -> float:
        if not losses:
            return 100.0
        values = [math.exp(-min(loss, 1000) / 300) * 100 for loss in losses]
        return sum(values) / len(values)

    def _insert_graded_move(self, index: int, notation: str) -> None:
        rating = self.move_ratings[index]
        loss = self.move_cp_losses[index]
        loss_label = "mate" if loss >= 10000 else f"-{loss}cp"
        self.history_text.insert("end", f"{notation}  ")
        self.history_text.insert("end", rating, rating)
        self.history_text.insert("end", f"  {loss_label}\n", "move")

    def _draw_eval_bar(self) -> None:
        if not hasattr(self, "eval_bar"):
            return
        width = self.eval_bar.winfo_width()
        height = self.eval_bar.winfo_height()
        if width < 2:
            return
        self.eval_bar.delete("all")
        score = evaluate_position(self.game, "w")
        white_share = 1 / (1 + math.exp(-max(-8, min(8, score))))
        white_width = width * white_share
        self.eval_bar.create_rectangle(0, 0, white_width, height, fill="#edf2f7", outline="")
        self.eval_bar.create_rectangle(
            white_width, 0, width, height, fill="#435365", outline=""
        )
        self.eval_bar.create_rectangle(
            white_width - 1, 0, white_width + 1, height, fill=ACCENT, outline=""
        )

    def _update_evaluation(self) -> None:
        score = evaluate_position(self.game, "w")
        self.eval_label.configure(
            text=f"{'+' if score >= 0 else ''}{score:.1f}",
            fg="#edf2f7" if score >= 0 else "#91a5ba",
        )
        self._draw_eval_bar()

    def _update_captured_pieces(self) -> None:
        starting_counts = {"q": 1, "r": 2, "b": 2, "n": 2, "p": 8}
        captured_by_white = []
        captured_by_black = []
        for capturing_color, output in (("w", captured_by_white), ("b", captured_by_black)):
            captured_color = "b" if capturing_color == "w" else "w"
            present: dict[str, int] = {}
            for row in self.game.board:
                for piece in row:
                    if piece != "." and self.game.color_of(piece) == captured_color:
                        kind = piece.lower()
                        if kind != "k":
                            present[kind] = present.get(kind, 0) + 1
            for kind in "qrbnp":
                missing = max(0, starting_counts[kind] - present.get(kind, 0))
                missing_piece = kind if captured_color == "b" else kind.upper()
                output.extend([PIECE_SYMBOLS[missing_piece]] * missing)
        white_text = "White  " + (" ".join(captured_by_white) or "—")
        black_text = "Black  " + (" ".join(captured_by_black) or "—")
        self.captured_white.configure(text=white_text)
        self.captured_black.configure(text=black_text)

    def choose_promotion(self) -> str | None:
        choice = tk.StringVar(self.root, value="")
        dialog = tk.Toplevel(self.root)
        dialog.title("Promote pawn")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=PANEL)
        dialog.grab_set()
        tk.Label(
            dialog, text="Choose a piece for your pawn:",
            font=("Segoe UI", 11), fg=TEXT, bg=PANEL, padx=14, pady=10,
        ).pack()
        options = tk.Frame(dialog, bg=PANEL, padx=10, pady=8)
        options.pack()
        color = "w" if self.game.turn == "w" else "b"
        for kind in "qrbn":
            piece = kind.upper() if color == "w" else kind
            self._make_action_button(
                options,
                PIECE_SYMBOLS[piece],
                lambda selected=kind: (choice.set(selected), dialog.destroy()),
            ).pack(side="left", padx=3)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.root.wait_window(dialog)
        return choice.get() or None

    def new_game(self) -> None:
        if not self.game_finished and not messagebox.askyesno(
            "New game", "Start a new game?", parent=self.root
        ):
            return
        self.game = ChessGame()
        self.selected = None
        self.legal_moves = []
        self.last_move = None
        self.game_finished = False
        self.move_history = []
        self.move_ratings = []
        self.move_cp_losses = []
        self.flipped = False
        self._update_history()
        self._update_captured_pieces()
        self.refresh()

    def flip_board(self) -> None:
        self.flipped = not self.flipped
        self.draw_board()

    def draw_board(self) -> None:
        if not hasattr(self, "canvas"):
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        margin = max(18, min(34, int(min(width, height) * 0.055)))
        square_size = min((width - 2 * margin) / 8, (height - 2 * margin) / 8)
        if square_size <= 0:
            return
        board_size = square_size * 8
        left = (width - board_size) / 2
        top = (height - board_size) / 2
        self.board_bounds = (left, top, left + board_size, top + board_size)
        self.canvas.delete("all")

        self.canvas.create_rectangle(
            left - 5, top - 5, left + board_size + 5, top + board_size + 5,
            fill="#0b1118", outline="#354457", width=1,
        )
        legal_destinations = {move.end for move in self.legal_moves}
        checked_king = self.game.king_square(self.game.turn) if self.game.in_check(
            self.game.turn
        ) else None

        for display_row in range(8):
            for display_col in range(8):
                if self.flipped:
                    row, col = 7 - display_row, 7 - display_col
                else:
                    row, col = display_row, display_col
                square = (row, col)
                x1 = left + display_col * square_size
                y1 = top + display_row * square_size
                x2, y2 = x1 + square_size, y1 + square_size
                color = BOARD_LIGHT if (row + col) % 2 == 0 else BOARD_DARK
                if self.last_move and square in (self.last_move.start, self.last_move.end):
                    color = "#c5c66d"
                if square == checked_king:
                    color = "#d66b65"
                if square == self.selected:
                    color = "#e5cf68"
                self.canvas.create_rectangle(
                    x1, y1, x2, y2, fill=color, outline=color
                )

                piece = self.game.board[row][col]
                if square in legal_destinations:
                    if piece == ".":
                        radius = max(4, square_size * 0.13)
                        self.canvas.create_oval(
                            (x1 + x2) / 2 - radius,
                            (y1 + y2) / 2 - radius,
                            (x1 + x2) / 2 + radius,
                            (y1 + y2) / 2 + radius,
                            fill="#26352d",
                            outline="",
                            stipple="gray50",
                        )
                    else:
                        self.canvas.create_oval(
                            x1 + 3, y1 + 3, x2 - 3, y2 - 3,
                            outline="#27372d", width=max(2, int(square_size * 0.055)),
                        )
                if piece != ".":
                    self._draw_piece(piece, (x1 + x2) / 2, (y1 + y2) / 2, square_size)

                if display_col == 0:
                    rank = str(8 - row)
                    self.canvas.create_text(
                        x1 + square_size * 0.08,
                        y1 + square_size * 0.08,
                        text=rank,
                        anchor="nw",
                        fill="#34423a" if color == BOARD_LIGHT else "#f4ecda",
                        font=("Segoe UI", max(7, int(square_size * 0.14)), "bold"),
                    )
                if display_row == 7:
                    file_name = "abcdefgh"[col]
                    self.canvas.create_text(
                        x2 - square_size * 0.08,
                        y2 - square_size * 0.08,
                        text=file_name,
                        anchor="se",
                        fill="#34423a" if color == BOARD_LIGHT else "#f4ecda",
                        font=("Segoe UI", max(7, int(square_size * 0.14)), "bold"),
                    )
        self._draw_capture_animations(square_size)

    def _draw_piece(self, piece: str, x: float, y: float, square_size: float) -> None:
        font_size = max(13, int(square_size * 0.76))
        symbol = PIECE_SYMBOLS[piece]
        foreground = "#fffdf5" if piece.isupper() else "#17212b"
        shadow = "#26323a" if piece.isupper() else "#d8d0c1"
        self.canvas.create_text(
            x + 1, y + 2, text=symbol, font=("Segoe UI Symbol", font_size),
            fill=shadow, anchor="center",
        )
        self.canvas.create_text(
            x, y, text=symbol, font=("Segoe UI Symbol", font_size),
            fill=foreground, anchor="center",
        )

    def animate_capture(self, piece: str, square: Square) -> None:
        if piece == "." or not self.game.in_bounds(square):
            return
        self.animation_counter += 1
        self.capture_animations.append((time.monotonic(), square, piece, self.animation_counter))
        self.draw_board()
        self._schedule_animation_frame()

    def _schedule_animation_frame(self) -> None:
        if self.animation_job is None and self.capture_animations:
            self.animation_job = self.root.after(16, self._animation_frame)

    def _animation_frame(self) -> None:
        self.animation_job = None
        if not self.canvas.winfo_exists():
            self.capture_animations.clear()
            return
        self.draw_board()
        self._schedule_animation_frame()

    @staticmethod
    def _mix_color(start: str, end: str, amount: float) -> str:
        first = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        second = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        channels = tuple(
            round(a + (b - a) * amount) for a, b in zip(first, second)
        )
        return "#" + "".join(f"{channel:02x}" for channel in channels)

    def _draw_capture_animations(self, square_size: float) -> None:
        now = time.monotonic()
        active = []
        left, top, _right, _bottom = self.board_bounds
        for started, square, piece, animation_id in self.capture_animations:
            progress = (now - started) / 0.42
            if progress >= 1:
                continue
            active.append((started, square, piece, animation_id))
            row, col = square
            display_row, display_col = (
                (7 - row, 7 - col) if self.flipped else (row, col)
            )
            center_x = left + (display_col + 0.5) * square_size
            center_y = top + (display_row + 0.5) * square_size
            fade = 1 - progress
            tags = (f"capture_fx_{animation_id}",)

            for delay, maximum_radius, color in (
                (0.0, 0.57, "#fff0a6"),
                (0.16, 0.43, "#ff9d68"),
            ):
                phase = (progress - delay) / (1 - delay)
                if phase < 0 or phase > 1:
                    continue
                radius = square_size * maximum_radius * (0.12 + 0.88 * phase)
                ring_color = self._mix_color(color, BOARD_DARK, phase * 0.78)
                self.canvas.create_oval(
                    center_x - radius, center_y - radius,
                    center_x + radius, center_y + radius,
                    outline=ring_color,
                    width=max(1, round(square_size * 0.045 * (1 - phase * 0.7))),
                    tags=tags,
                )

            if progress < 0.58:
                burst = progress / 0.58
                inner_radius = square_size * (0.16 + burst * 0.08)
                outer_radius = square_size * (0.28 + burst * 0.32)
                ray_color = self._mix_color("#fff4bf", "#ff9a61", burst)
                ray_width = max(1, round(square_size * 0.025 * (1 - burst * 0.55)))
                for index in range(12):
                    angle = math.tau * index / 12
                    wobble = 0.035 * math.sin(index * 4.7)
                    direction = angle + wobble
                    self.canvas.create_line(
                        center_x + math.cos(direction) * inner_radius,
                        center_y + math.sin(direction) * inner_radius,
                        center_x + math.cos(direction) * outer_radius,
                        center_y + math.sin(direction) * outer_radius,
                        fill=ray_color,
                        width=ray_width,
                        capstyle=tk.ROUND,
                        tags=tags,
                    )

            flash_color = self._mix_color("#fff0a6", BOARD_DARK, 1 - fade)
            self.canvas.create_oval(
                center_x - square_size * 0.055,
                center_y - square_size * 0.055,
                center_x + square_size * 0.055,
                center_y + square_size * 0.055,
                fill=flash_color,
                outline="",
                tags=tags,
            )
        self.capture_animations = active

    def refresh(self) -> None:
        side = "White" if self.game.turn == "w" else "Black"
        checked = self.game.in_check(self.game.turn)
        label = f"{side} to move" + ("  •  CHECK" if checked else "")
        self.turn_badge.configure(
            text=label,
            fg="#ffb4ab" if checked else TEXT,
            bg="#513737" if checked else PANEL_LIGHT,
        )
        self.player_label.configure(text=f"{'♙' if side == 'White' else '♟'}  {side}")
        self.player_label.configure(fg=ACCENT if self.game.turn == "w" else TEXT)
        self.draw_board()
        self._update_evaluation()

    def show_game_result(self) -> None:
        result = self.game.game_over_message()
        if result:
            self.game_finished = True
            self.turn_badge.configure(text="GAME OVER", fg=TEXT, bg="#355448")
            self.player_label.configure(text="Game finished")
            self.show_elo_result(result)

    def show_elo_result(self, result: str) -> None:
        if result.startswith("Checkmate! White"):
            white_score = 1.0
        elif result.startswith("Checkmate! Black"):
            white_score = 0.0
        else:
            white_score = 0.5

        dialog = tk.Toplevel(self.root)
        dialog.title("Game result & Elo")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=BACKGROUND)
        dialog.grab_set()
        tk.Label(
            dialog, text=result, font=("Segoe UI", 15, "bold"),
            fg=TEXT, bg=BACKGROUND, padx=22, pady=18,
        ).pack()
        tk.Label(
            dialog, text="Enter player ratings to calculate the Elo change",
            font=("Segoe UI", 9), fg=MUTED, bg=BACKGROUND, padx=22,
        ).pack(pady=(0, 12))

        fields = tk.Frame(dialog, bg=BACKGROUND, padx=22)
        fields.pack(fill="x")
        white_entry = self._rating_field(fields, "White rating", self.white_rating)
        black_entry = self._rating_field(fields, "Black rating", self.black_rating)
        output = tk.Label(
            dialog, text="", font=("Segoe UI", 11, "bold"),
            fg=ACCENT, bg=BACKGROUND, justify="left", pady=12,
        )
        output.pack()
        button = self._make_action_button(dialog, "Calculate Elo", lambda: None)
        button.pack(fill="x", padx=22, pady=(0, 18))

        def calculate() -> None:
            try:
                white_rating = int(white_entry.get())
                black_rating = int(black_entry.get())
                if min(white_rating, black_rating) < 0 or max(
                    white_rating, black_rating
                ) > 10000:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid rating",
                    "Enter whole-number ratings from 0 to 10,000.",
                    parent=dialog,
                )
                return
            new_white, new_black = calculate_elo(
                white_rating, black_rating, white_score
            )
            self.white_rating = new_white
            self.black_rating = new_black
            output.configure(
                text=(
                    f"White  {white_rating}  →  {new_white}  "
                    f"({new_white - white_rating:+d})\n"
                    f"Black  {black_rating}  →  {new_black}  "
                    f"({new_black - black_rating:+d})"
                )
            )
            button.configure(text="Done", command=dialog.destroy)

        button.configure(command=calculate)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    @staticmethod
    def _rating_field(parent: tk.Frame, label: str, rating: int) -> tk.Entry:
        field = tk.Frame(parent, bg=BACKGROUND)
        field.pack(fill="x", pady=4)
        tk.Label(
            field, text=label, font=("Segoe UI", 10),
            fg=TEXT, bg=BACKGROUND, width=13, anchor="w",
        ).pack(side="left")
        entry = tk.Entry(
            field, font=("Segoe UI", 10), width=10, justify="right",
            bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
        )
        entry.insert(0, str(rating))
        entry.pack(side="right", ipady=4, padx=(8, 0))
        return entry


def main() -> None:
    root = tk.Tk()
    ChessWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
