from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any

import chess
from flask import Blueprint, Flask, current_app, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
bp = Blueprint("chess", __name__)
ROOM_CODE = re.compile(r"^[A-Z2-9]{8}$")
USERNAME = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PIECE_VALUES = {"p": 1.0, "n": 3.2, "b": 3.3, "r": 5.0, "q": 9.0}
MATE_SCORE = 10000


class DatabaseIntegrityError(Exception):
    pass


class DatabaseConnection:
    def __init__(self, connection, backend: str) -> None:
        self.connection = connection
        self.backend = backend

    def execute(self, query: str, parameters: tuple = ()):
        if self.backend == "sqlite":
            try:
                return self.connection.execute(query, parameters)
            except sqlite3.IntegrityError as exception:
                raise DatabaseIntegrityError from exception

        ignore_conflict = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", query, re.IGNORECASE))
        query = re.sub(
            r"([A-Za-z_.]+)\s*=\s*\?\s+COLLATE NOCASE",
            r"LOWER(\1) = LOWER(?)",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"([A-Za-z_.]+)\s+COLLATE NOCASE",
            r"LOWER(\1)",
            query,
            flags=re.IGNORECASE,
        )
        query = query.replace("?", "%s")
        if self.backend == "postgres":
            query = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO",
                "INSERT INTO",
                query,
                flags=re.IGNORECASE,
            )
            if ignore_conflict:
                query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            query = re.sub(r"BEGIN\s+IMMEDIATE", "BEGIN", query, flags=re.IGNORECASE)
            import psycopg

            try:
                return self.connection.execute(query, parameters)
            except psycopg.IntegrityError as exception:
                raise DatabaseIntegrityError from exception

        if self.backend == "mysql":
            query = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO",
                "INSERT IGNORE INTO",
                query,
                flags=re.IGNORECASE,
            )
            query = re.sub(r"BEGIN\s+IMMEDIATE", "BEGIN", query, flags=re.IGNORECASE)
            import pymysql

            try:
                cursor = self.connection.cursor()
                cursor.execute(query, parameters)
                return cursor
            except pymysql.err.IntegrityError as exception:
                raise DatabaseIntegrityError from exception

        raise RuntimeError(f"Unsupported database backend: {self.backend}")

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def connect_database(backend: str, config: dict[str, Any]) -> DatabaseConnection:
    if backend == "sqlite":
        connection = sqlite3.connect(config["DATABASE"], timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return DatabaseConnection(connection, backend)
    if backend == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        ca_file = config.get("DB_SSL_CA")
        ssl_options = (
            {"sslmode": "verify-full", "sslrootcert": ca_file}
            if ca_file else {"sslmode": "require"}
        )
        connection = psycopg.connect(
            host=config["DB_HOST"],
            port=int(config["DB_PORT"]),
            dbname=config["DB_NAME"],
            user=config["DB_USERNAME"],
            password=config["DB_PASSWORD"],
            **ssl_options,
            row_factory=dict_row,
            connect_timeout=10,
        )
        return DatabaseConnection(connection, backend)
    if backend == "mysql":
        import pymysql
        from pymysql.cursors import DictCursor

        connection = pymysql.connect(
            host=config["DB_HOST"],
            port=int(config["DB_PORT"]),
            database=config["DB_NAME"],
            user=config["DB_USERNAME"],
            password=config["DB_PASSWORD"],
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=10,
            ssl_disabled=False,
            ssl_ca=config.get("DB_SSL_CA"),
            ssl_verify_cert=bool(config.get("DB_SSL_CA")),
            ssl_verify_identity=bool(config.get("DB_SSL_CA")),
        )
        return DatabaseConnection(connection, backend)
    raise RuntimeError(f"Unsupported database backend: {backend}")


def create_app(
    database_path: str | Path | None = None,
    secret_key: str | None = None,
) -> Flask:
    app = Flask(__name__, static_folder="public", static_url_path="")
    secure_cookies = os.environ.get("CHESS_SECURE_COOKIES")
    if secure_cookies is None:
        secure_cookies = os.environ.get("CHESS_ENV", "").lower() == "production"
    app.config.update(
        SECRET_KEY=secret_key or os.environ.get("CHESS_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=8 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(
            secure_cookies if isinstance(secure_cookies, bool)
            else secure_cookies.lower() == "true"
        ),
    )
    wasmer_database = {
        name: os.environ.get(name)
        for name in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USERNAME", "DB_PASSWORD")
    }
    if database_path is not None or not wasmer_database["DB_HOST"]:
        if os.environ.get("CHESS_ENV", "").lower() == "production":
            raise RuntimeError(
                "Production requires Wasmer's managed database connection variables."
            )
        app.config["DATABASE_BACKEND"] = "sqlite"
        app.config["DATABASE"] = str(
            database_path or os.environ.get("CHESS_DATABASE") or ROOT / "chess.sqlite3"
        )
        Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    else:
        missing = [name for name, value in wasmer_database.items() if not value]
        if missing:
            raise RuntimeError(
                "Wasmer database configuration is incomplete: "
                + ", ".join(missing)
            )
        configured_engine = os.environ.get("CHESS_DB_ENGINE", "").lower()
        host = wasmer_database["DB_HOST"].lower()
        if configured_engine in ("postgres", "postgresql"):
            backend = "postgres"
        elif configured_engine == "mysql":
            backend = "mysql"
        elif host.startswith("psql."):
            backend = "postgres"
        elif host.startswith("db."):
            backend = "mysql"
        else:
            raise RuntimeError(
                "Cannot identify Wasmer database engine from DB_HOST. "
                "Set CHESS_DB_ENGINE to 'postgres' or 'mysql'."
            )
        app.config["DATABASE_BACKEND"] = backend
        app.config.update(wasmer_database)
        app.config["DB_SSL_CA"] = os.environ.get("CHESS_DB_SSL_CA")

    initialize_database(app.config["DATABASE_BACKEND"], app.config)
    app.register_blueprint(bp)
    return app


def initialize_database(backend: str, config: dict[str, Any]) -> None:
    if backend == "sqlite":
        connection = sqlite3.connect(config["DATABASE"])
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 800,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS friend_requests (
                    id INTEGER PRIMARY KEY,
                    sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(sender_id, receiver_id),
                    CHECK(sender_id != receiver_id)
                );
                CREATE TABLE IF NOT EXISTS friendships (
                    user_one INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    user_two INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_one, user_two),
                    CHECK(user_one < user_two)
                );
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY,
                    room_code TEXT NOT NULL UNIQUE,
                    white_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    black_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    white_guest_id TEXT,
                    black_guest_id TEXT,
                    invited_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    moves_json TEXT NOT NULL DEFAULT '[]',
                    grades_json TEXT NOT NULL DEFAULT '[]',
                    rating_changes_json TEXT,
                    result TEXT,
                    status TEXT NOT NULL CHECK(status IN ('waiting', 'active', 'completed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
            if "rating" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN rating INTEGER NOT NULL DEFAULT 800"
                )
            game_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(games)")
            }
            if "rating_changes_json" not in game_columns:
                connection.execute(
                    "ALTER TABLE games ADD COLUMN rating_changes_json TEXT"
                )
        finally:
            connection.close()
        return

    connection = connect_database(backend, config)
    if backend == "postgres":
        schema = (
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username VARCHAR(20) NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 800,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS friend_requests (
                id BIGSERIAL PRIMARY KEY,
                sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                receiver_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sender_id, receiver_id),
                CHECK(sender_id != receiver_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS friendships (
                user_one BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user_two BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_one, user_two),
                CHECK(user_one < user_two)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS games (
                id BIGSERIAL PRIMARY KEY,
                room_code VARCHAR(8) NOT NULL UNIQUE,
                white_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                black_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                white_guest_id TEXT,
                black_guest_id TEXT,
                invited_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                moves_json TEXT NOT NULL DEFAULT '[]',
                grades_json TEXT NOT NULL DEFAULT '[]',
                rating_changes_json TEXT,
                result VARCHAR(255),
                status VARCHAR(16) NOT NULL CHECK(status IN ('waiting', 'active', 'completed')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_unique ON users (LOWER(username))",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS rating INTEGER NOT NULL DEFAULT 800",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS rating_changes_json TEXT",
        )
        try:
            for statement in schema:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()
        return

    schema = (
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(20) NOT NULL UNIQUE,
            username_key VARCHAR(20) GENERATED ALWAYS AS (LOWER(username)) STORED UNIQUE,
            password_hash TEXT NOT NULL,
            rating INTEGER NOT NULL DEFAULT 800,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS friend_requests (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            sender_id BIGINT NOT NULL,
            receiver_id BIGINT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sender_id, receiver_id),
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS friendships (
            user_one BIGINT NOT NULL,
            user_two BIGINT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_one, user_two),
            FOREIGN KEY (user_one) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (user_two) REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS games (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(8) NOT NULL UNIQUE,
            white_user_id BIGINT,
            black_user_id BIGINT,
            white_guest_id VARCHAR(64),
            black_guest_id VARCHAR(64),
            invited_user_id BIGINT,
            moves_json TEXT NOT NULL,
            grades_json TEXT NOT NULL,
            rating_changes_json TEXT,
            result VARCHAR(255),
            status VARCHAR(16) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (white_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (black_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (invited_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """,
    )
    try:
        for statement in schema:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def database() -> DatabaseConnection:
    if "database" not in g:
        connection = connect_database(
            current_app.config["DATABASE_BACKEND"], current_app.config
        )
        g.database = connection
    return g.database


@bp.teardown_app_request
def close_database(_error: BaseException | None) -> None:
    connection = g.pop("database", None)
    if connection is not None:
        connection.close()


@bp.get("/")
def lobby_page():
    return send_from_directory(ROOT / "public", "lobby.html")


@bp.get("/play")
def chess_page():
    return send_from_directory(ROOT / "public", "index.html")


@bp.get("/health")
def health():
    return jsonify(status="ok")


def error(message: str, status: int = 400):
    return jsonify(error=message), status


def json_body() -> dict[str, Any]:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return database().execute(
        "SELECT id, username, rating FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def guest_id() -> str:
    value = session.get("guest_id")
    if not isinstance(value, str):
        value = secrets.token_urlsafe(24)
        session["guest_id"] = value
    return value


def user_json(user: sqlite3.Row | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {"id": user["id"], "username": user["username"], "rating": user["rating"]}


@bp.get("/api/session")
def session_info():
    return jsonify(user=user_json(current_user()))


@bp.post("/api/register")
def register():
    body = json_body()
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        return error("Usernames must be 3–20 letters, numbers, or underscores.")
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        return error("Passwords must be between 10 and 128 characters.")
    try:
        database().execute(
            "INSERT INTO users(username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        database().commit()
    except DatabaseIntegrityError:
        return error("That username is already taken.", 409)
    user = database().execute(
        "SELECT id, username, rating FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    if user is None:
        raise RuntimeError("The newly created account could not be loaded.")
    session.clear()
    session["user_id"] = user["id"]
    return jsonify(user=user_json(user)), 201


@bp.post("/api/login")
def login():
    body = json_body()
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error("Enter your username and password.")
    user = database().execute(
        "SELECT id, username, password_hash, rating FROM users WHERE username = ? COLLATE NOCASE",
        (username,),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return error("Username or password is incorrect.", 401)
    session.clear()
    session["user_id"] = user["id"]
    return jsonify(user=user_json(user))


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


def require_user() -> sqlite3.Row | None:
    return current_user()


@bp.get("/api/dashboard")
def dashboard():
    user = require_user()
    if user is None:
        return error("Sign in to use your friends list.", 401)
    connection = database()
    friends = connection.execute(
        """
        SELECT u.id, u.username, u.rating FROM friendships f
        JOIN users u ON u.id = CASE WHEN f.user_one = ? THEN f.user_two ELSE f.user_one END
        WHERE f.user_one = ? OR f.user_two = ?
        ORDER BY u.username COLLATE NOCASE
        """,
        (user["id"], user["id"], user["id"]),
    ).fetchall()
    requests = connection.execute(
        """
        SELECT r.id, u.id AS user_id, u.username FROM friend_requests r
        JOIN users u ON u.id = r.sender_id WHERE r.receiver_id = ?
        ORDER BY r.created_at
        """,
        (user["id"],),
    ).fetchall()
    invitations = connection.execute(
        """
        SELECT g.room_code, u.username AS inviter FROM games g
        JOIN users u ON u.id = g.white_user_id
        WHERE g.invited_user_id = ? AND g.status = 'waiting'
        ORDER BY g.created_at DESC
        """,
        (user["id"],),
    ).fetchall()
    games = connection.execute(
        """
        SELECT room_code, status, created_at FROM games
        WHERE (white_user_id = ? OR black_user_id = ?) AND status != 'completed'
        ORDER BY created_at DESC
        """,
        (user["id"], user["id"]),
    ).fetchall()
    return jsonify(
        friends=[user_json(friend) for friend in friends],
        requests=[dict(row) for row in requests],
        invitations=[dict(row) for row in invitations],
        games=[dict(row) for row in games],
    )


@bp.post("/api/friends/requests")
def send_friend_request():
    user = require_user()
    if user is None:
        return error("Sign in to add friends.", 401)
    username = json_body().get("username")
    if not isinstance(username, str):
        return error("Enter a username.")
    target = database().execute(
        "SELECT id, username, rating FROM users WHERE username = ? COLLATE NOCASE",
        (username.strip(),),
    ).fetchone()
    if target is None:
        return error("No account has that username.", 404)
    if target["id"] == user["id"]:
        return error("You cannot add yourself as a friend.")
    first, second = sorted((user["id"], target["id"]))
    if database().execute(
        "SELECT 1 FROM friendships WHERE user_one = ? AND user_two = ?",
        (first, second),
    ).fetchone():
        return error("You are already friends.", 409)
    incoming = database().execute(
        "SELECT id FROM friend_requests WHERE sender_id = ? AND receiver_id = ?",
        (target["id"], user["id"]),
    ).fetchone()
    if incoming:
        database().execute(
            "DELETE FROM friend_requests WHERE id = ?", (incoming["id"],)
        )
        database().execute(
            "INSERT INTO friendships(user_one, user_two) VALUES (?, ?)",
            (first, second),
        )
        database().commit()
        return jsonify(status="friends", friend=user_json(target))
    try:
        database().execute(
            "INSERT INTO friend_requests(sender_id, receiver_id) VALUES (?, ?)",
            (user["id"], target["id"]),
        )
        database().commit()
    except DatabaseIntegrityError:
        return error("A friend request is already waiting.", 409)
    return jsonify(status="sent", friend=user_json(target)), 201


@bp.post("/api/friends/requests/<int:request_id>")
def answer_friend_request(request_id: int):
    user = require_user()
    if user is None:
        return error("Sign in to manage friend requests.", 401)
    body = json_body()
    accept = body.get("accept")
    if not isinstance(accept, bool):
        return error("Choose whether to accept this request.")
    connection = database()
    row = connection.execute(
        "SELECT sender_id FROM friend_requests WHERE id = ? AND receiver_id = ?",
        (request_id, user["id"]),
    ).fetchone()
    if row is None:
        return error("That friend request no longer exists.", 404)
    connection.execute("DELETE FROM friend_requests WHERE id = ?", (request_id,))
    if accept:
        first, second = sorted((user["id"], row["sender_id"]))
        connection.execute(
            "INSERT OR IGNORE INTO friendships(user_one, user_two) VALUES (?, ?)",
            (first, second),
        )
    connection.commit()
    return jsonify(ok=True)


def new_room_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    connection = database()
    for _attempt in range(10):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if connection.execute(
            "SELECT 1 FROM games WHERE room_code = ?", (code,)
        ).fetchone() is None:
            return code
    raise RuntimeError("Could not allocate a unique room code.")


def room_user_is_friend(user_id: int, friend_id: int) -> bool:
    first, second = sorted((user_id, friend_id))
    return database().execute(
        "SELECT 1 FROM friendships WHERE user_one = ? AND user_two = ?",
        (first, second),
    ).fetchone() is not None


@bp.post("/api/rooms")
def create_room():
    user = current_user()
    visitor_id = guest_id() if user is None else None
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, white_guest_id,
            moves_json, grades_json, status
        )
        VALUES (?, ?, ?, '[]', '[]', 'waiting')
        """,
        (code, user["id"] if user else None, visitor_id),
    )
    database().commit()
    return jsonify(roomCode=code, url=f"/play?room={code}"), 201


@bp.post("/api/invitations")
def invite_friend():
    user = require_user()
    if user is None:
        return error("Sign in to invite a friend.", 401)
    friend_id = json_body().get("friendId")
    if not isinstance(friend_id, int) or not room_user_is_friend(user["id"], friend_id):
        return error("Choose someone from your friends list.")
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, invited_user_id,
            moves_json, grades_json, status
        )
        VALUES (?, ?, ?, '[]', '[]', 'waiting')
        """,
        (code, user["id"], friend_id),
    )
    database().commit()
    return jsonify(roomCode=code), 201


def load_room(code: str, for_update: bool = False) -> sqlite3.Row | None:
    if not ROOM_CODE.fullmatch(code):
        return None
    statement = """
        SELECT g.*, white.username AS white_name, black.username AS black_name,
            white.rating AS white_rating, black.rating AS black_rating
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE g.room_code = ?
        """
    if for_update and current_app.config["DATABASE_BACKEND"] == "postgres":
        statement += " FOR UPDATE OF g"
    elif for_update and current_app.config["DATABASE_BACKEND"] == "mysql":
        statement += " FOR UPDATE"
    return database().execute(statement, (code,)).fetchone()


def room_side(room: sqlite3.Row, user: sqlite3.Row | None, visitor_id: str) -> str | None:
    if user is not None:
        if room["white_user_id"] == user["id"]:
            return "w"
        if room["black_user_id"] == user["id"]:
            return "b"
    else:
        if room["white_guest_id"] and hmac.compare_digest(room["white_guest_id"], visitor_id):
            return "w"
        if room["black_guest_id"] and hmac.compare_digest(room["black_guest_id"], visitor_id):
            return "b"
    return None


def room_board(room: sqlite3.Row) -> tuple[chess.Board, list[dict[str, Any]]]:
    moves = json.loads(room["moves_json"])
    grades = json.loads(room["grades_json"])
    board = chess.Board()
    history = []
    for index, uci in enumerate(moves):
        move = chess.Move.from_uci(uci)
        color = "w" if board.turn else "b"
        captured_piece = board.piece_at(move.to_square)
        captured_at = chess.square_name(move.to_square)
        if board.is_en_passant(move):
            captured_square = move.to_square - 8 if board.turn else move.to_square + 8
            captured_piece = board.piece_at(captured_square)
            captured_at = chess.square_name(captured_square)
        san = board.san(move)
        board.push(move)
        grade = grades[index] if index < len(grades) else {}
        history.append({
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
            "san": san,
            "color": color,
            "piece": (board.piece_at(move.to_square).symbol().lower()
                      if board.piece_at(move.to_square) else ""),
            "captured": captured_piece.symbol().lower() if captured_piece else None,
            "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
            "grade": grade.get("grade"),
            "centipawnLoss": grade.get("centipawnLoss"),
            "capturedAt": captured_at if captured_piece else None,
        })
    return board, history


def position_score(board: chess.Board, perspective: chess.Color) -> float:
    score = 0.0
    for square, piece in board.piece_map().items():
        if piece.piece_type == chess.KING:
            continue
        sign = 1 if piece.color == perspective else -1
        score += sign * PIECE_VALUES[chess.piece_symbol(piece.piece_type)]
    if board.is_check():
        score += -0.08 if board.turn == perspective else 0.08
    return score


def terminal_score(board: chess.Board, perspective: chess.Color) -> float | None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    if outcome.winner is None:
        return 0.0
    return MATE_SCORE if outcome.winner == perspective else -MATE_SCORE


def grade_move(board: chess.Board, move: chess.Move) -> dict[str, Any]:
    mover = board.turn
    initial_score = position_score(board, mover)
    candidates = list(board.legal_moves)
    scores = []
    for candidate in candidates:
        child = board.copy(stack=True)
        is_capture = child.is_capture(candidate)
        child.push(candidate)
        terminal = terminal_score(child, mover)
        if terminal is not None:
            score = terminal
        else:
            replies = list(child.legal_moves)
            reply_scores = []
            for reply in replies:
                grandchild = child.copy(stack=True)
                grandchild.push(reply)
                reply_scores.append(
                    terminal_score(grandchild, mover)
                    if terminal_score(grandchild, mover) is not None
                    else position_score(grandchild, mover)
                )
            score = min(reply_scores) if reply_scores else position_score(child, mover)

        winning_recapture = False
        moving_piece = board.piece_at(candidate.from_square)
        if (not is_capture and moving_piece is not None
                and moving_piece.piece_type != chess.PAWN and child.is_check()):
            for reply in child.legal_moves:
                if reply.to_square != candidate.to_square or not child.is_capture(reply):
                    continue
                after_capture = child.copy(stack=True)
                after_capture.push(reply)
                for recapture in after_capture.legal_moves:
                    if recapture.to_square != candidate.to_square or not after_capture.is_capture(recapture):
                        continue
                    final = after_capture.copy(stack=True)
                    final.push(recapture)
                    final_score = terminal_score(final, mover)
                    if (final_score if final_score is not None else position_score(final, mover)) >= initial_score + 1:
                        winning_recapture = True
                        break
        scores.append((candidate, score, winning_recapture))

    best = max(score for _candidate, score, _winning in scores)
    _chosen, chosen_score, winning_recapture = next(
        value for value in scores if value[0] == move
    )
    loss = max(0, round((best - chosen_score) * 100))
    if loss >= 500:
        grade = "Grave error"
    elif winning_recapture and loss <= 10:
        grade = "Brilliant"
    elif loss <= 10:
        grade = "Best"
    elif loss <= 15:
        grade = "Great"
    elif loss <= 30:
        grade = "Excellent"
    elif loss <= 75:
        grade = "Good"
    elif loss <= 150:
        grade = "Inaccuracy"
    elif loss <= 300:
        grade = "Mistake"
    else:
        grade = "Blunder"
    return {"grade": grade, "centipawnLoss": loss}


def result_for(board: chess.Board) -> str | None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    if outcome.winner is None:
        if board.is_stalemate():
            return "Draw by stalemate"
        if board.is_insufficient_material():
            return "Draw by insufficient material"
        if board.is_fivefold_repetition() or board.can_claim_threefold_repetition():
            return "Draw by threefold repetition"
        return "Draw by the 50-move rule"
    winner = "White" if outcome.winner == chess.WHITE else "Black"
    return f"{winner} wins by checkmate"


def elo_changes(
    white_rating: int, black_rating: int, white_score: float
) -> tuple[int, int]:
    expected_white = 1 / (1 + 10 ** ((black_rating - white_rating) / 400))
    white_change = round(32 * (white_score - expected_white))
    return white_change, -white_change


def rate_completed_game(
    connection: sqlite3.Connection, room: sqlite3.Row, result: str
) -> None:
    if room["rating_changes_json"] is not None:
        return
    changes: dict[str, dict[str, int]] = {}
    if room["white_user_id"] is not None and room["black_user_id"] is not None:
        white_rating = room["white_rating"]
        black_rating = room["black_rating"]
        white_score = (
            1.0 if result.startswith("White wins")
            else 0.0 if result.startswith("Black wins")
            else 0.5
        )
        white_change, black_change = elo_changes(
            white_rating, black_rating, white_score
        )
        connection.execute(
            "UPDATE users SET rating = rating + ? WHERE id = ?",
            (white_change, room["white_user_id"]),
        )
        connection.execute(
            "UPDATE users SET rating = rating + ? WHERE id = ?",
            (black_change, room["black_user_id"]),
        )
        changes = {
            "white": {
                "before": white_rating,
                "after": white_rating + white_change,
                "change": white_change,
            },
            "black": {
                "before": black_rating,
                "after": black_rating + black_change,
                "change": black_change,
            },
        }
    connection.execute(
        "UPDATE games SET rating_changes_json = ? WHERE id = ?",
        (json.dumps(changes), room["id"]),
    )


def room_snapshot(room: sqlite3.Row, side: str) -> dict[str, Any]:
    board, history = room_board(room)
    result = room["result"] or result_for(board)
    over = room["status"] == "completed" or result is not None
    if room["status"] == "active" and result:
        database().execute(
            "UPDATE games SET status = 'completed', result = ? WHERE id = ?",
            (result, room["id"]),
        )
        database().commit()
    last_move = None
    if history:
        final = history[-1]
        last_move = {
            key: final[key]
            for key in ("from", "to", "captured", "capturedAt", "grade", "centipawnLoss")
        }
    return {
        "board": [
            [
                f"{'w' if piece.color else 'b'}{chess.piece_symbol(piece.piece_type)}"
                if (piece := board.piece_at(chess.square(file_index, rank))) else None
                for file_index in range(8)
            ]
            for rank in range(7, -1, -1)
        ],
        "turn": "w" if board.turn else "b",
        "fen": board.fen(),
        "check": board.is_check(),
        "over": over,
        "result": room["result"] or result,
        "status": room["status"],
        "roomCode": room["room_code"],
        "playerSide": side,
        "whiteName": room["white_name"] or "Guest",
        "blackName": room["black_name"] or (
            "Waiting for opponent" if room["status"] == "waiting" else "Guest"
        ),
        "whiteRating": room["white_rating"] if room["white_rating"] is not None else 800,
        "blackRating": room["black_rating"] if room["black_rating"] is not None else 800,
        "ratingChanges": (
            json.loads(room["rating_changes_json"])
            if room["rating_changes_json"] is not None else None
        ),
        "legalMoves": [] if over or room["status"] != "active" else [
            {
                "from": chess.square_name(move.from_square),
                "to": chess.square_name(move.to_square),
                "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
            }
            for move in board.legal_moves
        ],
        "history": history,
        "lastMove": last_move,
        "evaluation": round(position_score(board, chess.WHITE), 1),
    }


def authorized_room(code: str):
    room = load_room(code.upper())
    if room is None:
        return None, None, error("That room could not be found.", 404)
    side = room_side(room, current_user(), guest_id())
    if side is None:
        return room, None, error("You are not a player in this room.", 403)
    return room, side, None


@bp.post("/api/rooms/<code>/join")
def join_room(code: str):
    room = load_room(code.upper())
    if room is None:
        return error("That room could not be found.", 404)
    user = current_user()
    visitor = guest_id() if user is None else None
    if room_side(room, user, visitor or ""):
        return jsonify(room_snapshot(room, room_side(room, user, visitor or "")))
    if room["status"] != "waiting" or room["black_user_id"] or room["black_guest_id"]:
        return error("This room already has two players.", 409)
    if room["white_user_id"] is not None and user is not None and room["white_user_id"] == user["id"]:
        return error("You cannot join your own room as the opponent.", 409)
    if room["white_guest_id"] and visitor and hmac.compare_digest(room["white_guest_id"], visitor):
        return error("You cannot join your own room as the opponent.", 409)
    if room["invited_user_id"] is not None and (
        user is None or user["id"] != room["invited_user_id"]
    ):
        return error("This room is an invitation for another player.", 403)
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    fresh = load_room(code.upper(), for_update=True)
    if fresh is None or fresh["status"] != "waiting":
        connection.rollback()
        return error("This room is no longer waiting for a player.", 409)
    connection.execute(
        """
        UPDATE games SET black_user_id = ?, black_guest_id = ?, status = 'active'
        WHERE id = ? AND status = 'waiting'
        """,
        (user["id"] if user else None, visitor, fresh["id"]),
    )
    connection.commit()
    room = load_room(code.upper())
    return jsonify(room_snapshot(room, "b"))


@bp.get("/api/rooms/<code>")
def get_room_state(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    return jsonify(room_snapshot(room, side))


@bp.post("/api/rooms/<code>/move")
def make_move(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    body = json_body()
    start, end, promotion = body.get("from"), body.get("to"), body.get("promotion")
    square_pattern = re.compile(r"^[a-h][1-8]$")
    if (not isinstance(start, str) or not square_pattern.fullmatch(start)
            or not isinstance(end, str) or not square_pattern.fullmatch(end)
            or (promotion is not None and promotion not in ("q", "r", "b", "n"))):
        return error("Provide valid from/to squares and an optional q, r, b, or n promotion.")
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code.upper(), for_update=True)
    side = room_side(room, current_user(), guest_id())
    if side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    if room["status"] != "active":
        connection.rollback()
        return error("Both players must join before a move can be made.", 409)
    board, _history = room_board(room)
    if ("w" if board.turn else "b") != side:
        connection.rollback()
        return error("It is the other player's turn.", 403)
    uci = f"{start}{end}{promotion or ''}"
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        connection.rollback()
        return error("That move is not legal.")
    if move not in board.legal_moves:
        connection.rollback()
        return error("That move is not legal.")
    grade = grade_move(board, move)
    moves = json.loads(room["moves_json"])
    grades = json.loads(room["grades_json"])
    moves.append(uci)
    grades.append(grade)
    board.push(move)
    result = result_for(board)
    connection.execute(
        """
        UPDATE games SET moves_json = ?, grades_json = ?, result = ?,
            status = ? WHERE id = ?
        """,
        (
            json.dumps(moves),
            json.dumps(grades),
            result,
            "completed" if result else "active",
            room["id"],
        ),
    )
    if result:
        rate_completed_game(connection, room, result)
    connection.commit()
    room = load_room(code.upper())
    return jsonify(room_snapshot(room, side))


@bp.post("/api/rooms/<code>/forfeit")
def forfeit(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code.upper(), for_update=True)
    side = room_side(room, current_user(), guest_id())
    if side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    if room["status"] != "active":
        connection.rollback()
        return error("Only an active game can be forfeited.", 409)
    result = "Black wins by forfeit" if side == "w" else "White wins by forfeit"
    connection.execute(
        "UPDATE games SET status = 'completed', result = ? WHERE id = ? AND status = 'active'",
        (result, room["id"]),
    )
    rate_completed_game(connection, room, result)
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), side))


@bp.post("/api/rooms/<code>/new-game")
def restart_game(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code.upper(), for_update=True)
    side = room_side(room, current_user(), guest_id())
    if side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    if room["status"] != "completed":
        connection.rollback()
        return error("A new game can start after this match ends.", 409)
    connection.execute(
        """
        UPDATE games SET moves_json = '[]', grades_json = '[]', rating_changes_json = NULL, result = NULL,
            status = CASE WHEN black_user_id IS NOT NULL OR black_guest_id IS NOT NULL
                          THEN 'active' ELSE 'waiting' END
        WHERE id = ?
        """,
        (room["id"],),
    )
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), side))


app = create_app()


if __name__ == "__main__":
    production = os.environ.get("CHESS_ENV", "").lower() == "production"
    port = int(os.environ.get("PORT", "80" if production else "20202"))
    host = os.environ.get("CHESS_HOST", "0.0.0.0" if production else "127.0.0.1")
    app.run(host=host, port=port, threaded=True)
