"""A dependency-free, two-player chess game for the terminal.

Run with ``python two_player_chess.py``. Enter moves as coordinates such as
``e2e4`` or ``e7e8q``; promotion defaults to a queen. Enter ``help`` for
commands.
"""

from dataclasses import dataclass
import re


Square = tuple[int, int]
FILES = "abcdefgh"
PROMOTION_PIECES = "qrbn"
PIECE_NAMES = {
    "p": "pawn",
    "n": "knight",
    "b": "bishop",
    "r": "rook",
    "q": "queen",
    "k": "king",
}
STARTING_BOARD = [
    list("rnbqkbnr"),
    list("pppppppp"),
    list("........"),
    list("........"),
    list("........"),
    list("........"),
    list("PPPPPPPP"),
    list("RNBQKBNR"),
]


@dataclass(frozen=True)
class Move:
    start: Square
    end: Square
    promotion: str | None = None
    en_passant: bool = False
    castle: bool = False


class ChessGame:
    def __init__(self) -> None:
        self.board = [row[:] for row in STARTING_BOARD]
        self.turn = "w"
        self.castling_rights = {"K", "Q", "k", "q"}
        self.en_passant_target: Square | None = None
        self.halfmove_clock = 0
        self.fullmove_number = 1
        self.position_counts = {self.position_key(): 1}

    @staticmethod
    def color_of(piece: str) -> str:
        return "w" if piece.isupper() else "b"

    @staticmethod
    def in_bounds(square: Square) -> bool:
        row, col = square
        return 0 <= row < 8 and 0 <= col < 8

    @staticmethod
    def square_name(square: Square) -> str:
        row, col = square
        return f"{FILES[col]}{8 - row}"

    @staticmethod
    def parse_square(text: str) -> Square:
        return 8 - int(text[1]), FILES.index(text[0])

    def king_square(self, color: str) -> Square:
        king = "K" if color == "w" else "k"
        for row in range(8):
            for col in range(8):
                if self.board[row][col] == king:
                    return row, col
        raise ValueError(f"The {'white' if color == 'w' else 'black'} king is missing.")

    def is_attacked(self, square: Square, by_color: str) -> bool:
        row, col = square

        pawn = "P" if by_color == "w" else "p"
        pawn_row = row + (1 if by_color == "w" else -1)
        for pawn_col in (col - 1, col + 1):
            if self.in_bounds((pawn_row, pawn_col)):
                if self.board[pawn_row][pawn_col] == pawn:
                    return True

        knight = "N" if by_color == "w" else "n"
        for row_delta, col_delta in (
            (-2, -1), (-2, 1), (-1, -2), (-1, 2),
            (1, -2), (1, 2), (2, -1), (2, 1),
        ):
            target = row + row_delta, col + col_delta
            if self.in_bounds(target) and self.board[target[0]][target[1]] == knight:
                return True

        king = "K" if by_color == "w" else "k"
        for row_delta in (-1, 0, 1):
            for col_delta in (-1, 0, 1):
                if row_delta == col_delta == 0:
                    continue
                target = row + row_delta, col + col_delta
                if self.in_bounds(target) and self.board[target[0]][target[1]] == king:
                    return True

        directions = (
            (-1, 0, "rq"), (1, 0, "rq"), (0, -1, "rq"), (0, 1, "rq"),
            (-1, -1, "bq"), (-1, 1, "bq"), (1, -1, "bq"), (1, 1, "bq"),
        )
        for row_delta, col_delta, attackers in directions:
            check_row, check_col = row + row_delta, col + col_delta
            distance = 1
            while self.in_bounds((check_row, check_col)):
                piece = self.board[check_row][check_col]
                if piece != ".":
                    if self.color_of(piece) == by_color:
                        kind = piece.lower()
                        if kind in attackers or (kind == "k" and distance == 1):
                            return True
                    break
                check_row += row_delta
                check_col += col_delta
                distance += 1
        return False

    def in_check(self, color: str) -> bool:
        return self.is_attacked(self.king_square(color), "b" if color == "w" else "w")

    def pseudo_legal_moves(self) -> list[Move]:
        moves: list[Move] = []
        for row in range(8):
            for col in range(8):
                piece = self.board[row][col]
                if piece == "." or self.color_of(piece) != self.turn:
                    continue
                start = row, col
                kind = piece.lower()
                if kind == "p":
                    self._pawn_moves(start, moves)
                elif kind == "n":
                    for dr, dc in (
                        (-2, -1), (-2, 1), (-1, -2), (-1, 2),
                        (1, -2), (1, 2), (2, -1), (2, 1),
                    ):
                        self._add_move_if_available(start, (row + dr, col + dc), moves)
                elif kind == "k":
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            if dr or dc:
                                self._add_move_if_available(start, (row + dr, col + dc), moves)
                    self._castle_moves(start, moves)
                else:
                    directions = []
                    if kind in "rq":
                        directions.extend(((-1, 0), (1, 0), (0, -1), (0, 1)))
                    if kind in "bq":
                        directions.extend(((-1, -1), (-1, 1), (1, -1), (1, 1)))
                    for dr, dc in directions:
                        target_row, target_col = row + dr, col + dc
                        while self.in_bounds((target_row, target_col)):
                            target = self.board[target_row][target_col]
                            if target == ".":
                                moves.append(Move(start, (target_row, target_col)))
                            else:
                                if self.color_of(target) != self.turn:
                                    moves.append(Move(start, (target_row, target_col)))
                                break
                            target_row += dr
                            target_col += dc
        return moves

    def _add_move_if_available(
        self, start: Square, end: Square, moves: list[Move]
    ) -> None:
        if not self.in_bounds(end):
            return
        target = self.board[end[0]][end[1]]
        if target == "." or self.color_of(target) != self.turn:
            moves.append(Move(start, end))

    def _pawn_moves(self, start: Square, moves: list[Move]) -> None:
        row, col = start
        direction = -1 if self.turn == "w" else 1
        starting_row = 6 if self.turn == "w" else 1
        promotion_row = 0 if self.turn == "w" else 7
        one_step = row + direction

        if self.board[one_step][col] == ".":
            if one_step == promotion_row:
                moves.extend(Move(start, (one_step, col), piece) for piece in PROMOTION_PIECES)
            else:
                moves.append(Move(start, (one_step, col)))
                two_steps = row + 2 * direction
                if row == starting_row and self.board[two_steps][col] == ".":
                    moves.append(Move(start, (two_steps, col)))

        for target_col in (col - 1, col + 1):
            target = one_step, target_col
            if not self.in_bounds(target):
                continue
            captured = self.board[one_step][target_col]
            if captured != "." and self.color_of(captured) != self.turn:
                if one_step == promotion_row:
                    moves.extend(
                        Move(start, target, piece) for piece in PROMOTION_PIECES
                    )
                else:
                    moves.append(Move(start, target))
            elif target == self.en_passant_target:
                moves.append(Move(start, target, en_passant=True))

    def _castle_moves(self, start: Square, moves: list[Move]) -> None:
        row, col = start
        if col != 4 or self.in_check(self.turn):
            return
        enemy = "b" if self.turn == "w" else "w"
        if self.turn == "w":
            options = (
                ("K", 7, (5, 6), 6),
                ("Q", 0, (1, 2, 3), 2),
            )
        else:
            options = (
                ("k", 7, (5, 6), 6),
                ("q", 0, (1, 2, 3), 2),
            )
        for right, rook_col, empty_cols, king_col in options:
            if right not in self.castling_rights:
                continue
            rook = self.board[row][rook_col]
            expected_rook = "R" if self.turn == "w" else "r"
            if rook != expected_rook:
                continue
            if any(self.board[row][empty_col] != "." for empty_col in empty_cols):
                continue
            transit_cols = (5, 6) if king_col == 6 else (3, 2)
            if any(self.is_attacked((row, c), enemy) for c in transit_cols):
                continue
            moves.append(Move(start, (row, king_col), castle=True))

    def legal_moves(self) -> list[Move]:
        legal = []
        for move in self.pseudo_legal_moves():
            candidate = self._copy_for_analysis()
            candidate._apply_unchecked(move)
            if not candidate.in_check(self.turn):
                legal.append(move)
        return legal

    def _copy_for_analysis(self) -> "ChessGame":
        candidate = object.__new__(ChessGame)
        candidate.board = [row[:] for row in self.board]
        candidate.turn = self.turn
        candidate.castling_rights = set(self.castling_rights)
        candidate.en_passant_target = self.en_passant_target
        candidate.halfmove_clock = self.halfmove_clock
        candidate.fullmove_number = self.fullmove_number
        candidate.position_counts = {}
        return candidate

    def _apply_unchecked(self, move: Move) -> None:
        start_row, start_col = move.start
        end_row, end_col = move.end
        piece = self.board[start_row][start_col]
        captured = self.board[end_row][end_col]
        is_pawn = piece.lower() == "p"

        if move.en_passant:
            captured_row = end_row + (1 if self.turn == "w" else -1)
            captured = self.board[captured_row][end_col]
            self.board[captured_row][end_col] = "."

        self.board[start_row][start_col] = "."
        self.board[end_row][end_col] = move.promotion.upper() if (
            move.promotion and self.turn == "w"
        ) else move.promotion if move.promotion else piece

        if move.castle:
            rook_start = 7 if end_col == 6 else 0
            rook_end = 5 if end_col == 6 else 3
            rook = self.board[start_row][rook_start]
            self.board[start_row][rook_start] = "."
            self.board[start_row][rook_end] = rook

        if piece == "K":
            self.castling_rights.difference_update({"K", "Q"})
        elif piece == "k":
            self.castling_rights.difference_update({"k", "q"})
        rook_rights = {
            (7, 0): "Q", (7, 7): "K", (0, 0): "q", (0, 7): "k",
        }
        if piece.lower() == "r":
            self.castling_rights.discard(rook_rights.get(move.start, ""))
        if captured.lower() == "r":
            self.castling_rights.discard(rook_rights.get(move.end, ""))

        self.en_passant_target = None
        if is_pawn and abs(end_row - start_row) == 2:
            self.en_passant_target = ((start_row + end_row) // 2, start_col)

        self.halfmove_clock = 0 if is_pawn or captured != "." else self.halfmove_clock + 1
        if self.turn == "b":
            self.fullmove_number += 1
        self.turn = "b" if self.turn == "w" else "w"

    def position_key(self) -> tuple[object, ...]:
        board_key = tuple("".join(row) for row in self.board)
        rights_key = "".join(sorted(self.castling_rights))
        return board_key, self.turn, rights_key, self.en_passant_target

    def play(self, move_text: str) -> tuple[bool, str]:
        match = re.fullmatch(r"([a-h][1-8])([a-h][1-8])([qrbn])?", move_text.lower())
        if not match:
            return False, "Use coordinate notation such as e2e4 or e7e8q."

        start = self.parse_square(match.group(1))
        end = self.parse_square(match.group(2))
        promotion = match.group(3)
        choices = [
            move for move in self.legal_moves()
            if move.start == start and move.end == end
        ]
        if not choices:
            return False, "That move is not legal."
        if any(move.promotion for move in choices):
            requested_piece = promotion or "q"
            selected = next(
                (move for move in choices if move.promotion == requested_piece), None
            )
            if selected is None:
                return False, "Promotion must be q, r, b, or n."
        else:
            if promotion:
                return False, "Only a pawn reaching the last rank can promote."
            selected = choices[0]

        self._apply_unchecked(selected)
        key = self.position_key()
        self.position_counts[key] = self.position_counts.get(key, 0) + 1
        return True, ""

    def game_over_message(self) -> str | None:
        moves = self.legal_moves()
        if not moves:
            if self.in_check(self.turn):
                winner = "Black" if self.turn == "w" else "White"
                return f"Checkmate! {winner} wins."
            return "Draw by stalemate."
        if self.halfmove_clock >= 100:
            return "Draw by the 50-move rule."
        if self.position_counts.get(self.position_key(), 0) >= 3:
            return "Draw by threefold repetition."
        if self._insufficient_material():
            return "Draw by insufficient material."
        return None

    def _insufficient_material(self) -> bool:
        pieces = [
            (piece.lower(), row, col)
            for row, board_row in enumerate(self.board)
            for col, piece in enumerate(board_row)
            if piece != "." and piece.lower() != "k"
        ]
        if not pieces:
            return True
        if len(pieces) == 1 and pieces[0][0] in ("b", "n"):
            return True
        if pieces and all(piece == "b" for piece, _, _ in pieces):
            square_colors = {(row + col) % 2 for _, row, col in pieces}
            return len(square_colors) == 1
        return False

    def display(self) -> None:
        print()
        for row_number, row in enumerate(self.board):
            print(f"{8 - row_number}  " + " ".join(row))
        print("\n   a b c d e f g h")
        print(f"\nMove {self.fullmove_number} - {'White' if self.turn == 'w' else 'Black'} to move")
        if self.in_check(self.turn):
            print("Check!")


def main() -> None:
    game = ChessGame()
    print("Two-player chess. White is uppercase; Black is lowercase.")
    print("Enter a move (e.g. e2e4), 'help', or 'quit'.")

    while True:
        game.display()
        result = game.game_over_message()
        if result:
            print(result)
            break
        command = input("> ").strip().lower()
        if command in ("quit", "exit"):
            print("Game ended.")
            break
        if command == "help":
            print("Enter the start and end squares, e.g. e2e4. For promotion,")
            print("append q, r, b, or n (e.g. e7e8n). Promotion defaults to queen.")
            print("Castling uses the king's move (e1g1, e1c1, e8g8, or e8c8).")
            continue
        played, message = game.play(command)
        if not played:
            print(message)


if __name__ == "__main__":
    main()
