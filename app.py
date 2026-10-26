from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import chess
from flask import Blueprint, Flask, current_app, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
bp = Blueprint("chess", __name__)
ROOM_CODE = re.compile(r"^[A-Z2-9]{8}$")
PRESENCE_TIMEOUT_MS = 5000
USERNAME = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PIECE_VALUES = {"p": 1.0, "n": 3.2, "b": 3.3, "r": 5.0, "q": 9.0}
TIME_CONTROLS = {
    "bullet_1_0": {"label": "Bullet · 1+0", "seconds": 60, "increment": 0},
    "bullet_2_1": {"label": "Bullet · 2+1", "seconds": 120, "increment": 1},
    "blitz_3_0": {"label": "Blitz · 3+0", "seconds": 180, "increment": 0},
    "blitz_3_2": {"label": "Blitz · 3+2", "seconds": 180, "increment": 2},
    "blitz_5_0": {"label": "Blitz · 5+0", "seconds": 300, "increment": 0},
    "rapid_10_0": {"label": "Rapid · 10+0", "seconds": 600, "increment": 0},
    "rapid_15_10": {"label": "Rapid · 15+10", "seconds": 900, "increment": 10},
}


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

    @app.after_request
    def cache_stockfish_assets(response):
        if request.path in {
            "/engine/stockfish-19-lite-single.js",
            "/engine/stockfish-19-lite-single.wasm",
        }:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

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
                    time_control TEXT NOT NULL DEFAULT 'rapid_10_0',
                    white_time_ms INTEGER NOT NULL DEFAULT 600000,
                    black_time_ms INTEGER NOT NULL DEFAULT 600000,
                    increment_seconds INTEGER NOT NULL DEFAULT 0,
                    clock_started_at INTEGER,
                    white_last_seen_at INTEGER,
                    black_last_seen_at INTEGER,
                    rematch_requested_by TEXT,
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
            column_types = {
                "time_control": "TEXT NOT NULL DEFAULT 'rapid_10_0'",
                "white_time_ms": "INTEGER NOT NULL DEFAULT 600000",
                "black_time_ms": "INTEGER NOT NULL DEFAULT 600000",
                "increment_seconds": "INTEGER NOT NULL DEFAULT 0",
                "clock_started_at": "INTEGER",
                "white_last_seen_at": "INTEGER",
                "black_last_seen_at": "INTEGER",
                "rematch_requested_by": "TEXT",
            }
            for name, definition in column_types.items():
                if name not in game_columns:
                    connection.execute(
                        f"ALTER TABLE games ADD COLUMN {name} {definition}"
                    )
            for name, columns in (
                ("games_white_user_status_created", "white_user_id, status, created_at DESC, id DESC"),
                ("games_black_user_status_created", "black_user_id, status, created_at DESC, id DESC"),
                ("games_status_created", "status, created_at DESC"),
                ("games_invited_status_created", "invited_user_id, status, created_at DESC"),
            ):
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON games ({columns})"
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
                time_control VARCHAR(24) NOT NULL DEFAULT 'rapid_10_0',
                white_time_ms BIGINT NOT NULL DEFAULT 600000,
                black_time_ms BIGINT NOT NULL DEFAULT 600000,
                increment_seconds INTEGER NOT NULL DEFAULT 0,
                clock_started_at BIGINT,
                white_last_seen_at BIGINT,
                black_last_seen_at BIGINT,
                rematch_requested_by VARCHAR(1),
                result VARCHAR(255),
                status VARCHAR(16) NOT NULL CHECK(status IN ('waiting', 'active', 'completed')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_unique ON users (LOWER(username))",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS rating INTEGER NOT NULL DEFAULT 800",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS rating_changes_json TEXT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS white_last_seen_at BIGINT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS black_last_seen_at BIGINT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS rematch_requested_by VARCHAR(1)",
            "CREATE INDEX IF NOT EXISTS games_white_user_status_created ON games (white_user_id, status, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS games_black_user_status_created ON games (black_user_id, status, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS games_status_created ON games (status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS games_invited_status_created ON games (invited_user_id, status, created_at DESC)",
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
            time_control VARCHAR(24) NOT NULL DEFAULT 'rapid_10_0',
            white_time_ms BIGINT NOT NULL DEFAULT 600000,
            black_time_ms BIGINT NOT NULL DEFAULT 600000,
            increment_seconds INTEGER NOT NULL DEFAULT 0,
            clock_started_at BIGINT,
            white_last_seen_at BIGINT,
            black_last_seen_at BIGINT,
            rematch_requested_by VARCHAR(1),
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
        if backend == "postgres":
            columns = {
                row["column_name"] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'games'"
                ).fetchall()
            }
        else:
            columns = {
                row["Field"] for row in connection.execute("SHOW COLUMNS FROM games").fetchall()
            }
        column_types = {
            "time_control": "VARCHAR(24) NOT NULL DEFAULT 'rapid_10_0'",
            "white_time_ms": "BIGINT NOT NULL DEFAULT 600000",
            "black_time_ms": "BIGINT NOT NULL DEFAULT 600000",
            "increment_seconds": "INTEGER NOT NULL DEFAULT 0",
            "clock_started_at": "BIGINT",
            "white_last_seen_at": "BIGINT",
            "black_last_seen_at": "BIGINT",
            "rematch_requested_by": "VARCHAR(1)",
        }
        for name, definition in column_types.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE games ADD COLUMN {name} {definition}")
        existing_indexes = {
            row["Key_name"] for row in connection.execute("SHOW INDEX FROM games").fetchall()
        }
        for name, columns in (
            ("games_white_user_status_created", "white_user_id, status, created_at DESC, id DESC"),
            ("games_black_user_status_created", "black_user_id, status, created_at DESC, id DESC"),
            ("games_status_created", "status, created_at DESC"),
            ("games_invited_status_created", "invited_user_id, status, created_at DESC"),
        ):
            if name not in existing_indexes:
                connection.execute(f"CREATE INDEX {name} ON games ({columns})")
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


@bp.get("/api/live-matches")
def live_matches():
    matches = database().execute(
        """
        SELECT g.room_code, white.username AS white_name,
            black.username AS black_name, g.time_control, g.moves_json
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE g.status = 'active'
        ORDER BY g.created_at DESC
        LIMIT 50
        """
    ).fetchall()
    return jsonify(matches=[
        {
            "roomCode": match["room_code"],
            "whiteName": match["white_name"] or "Guest",
            "blackName": match["black_name"] or "Guest",
            "timeControl": TIME_CONTROLS.get(
                match["time_control"], TIME_CONTROLS["rapid_10_0"]
            )["label"],
            "movesPlayed": len(json.loads(match["moves_json"])),
        }
        for match in matches
    ])


@bp.get("/api/leaderboard")
def leaderboard():
    leaders = database().execute(
        """
        SELECT id, username, rating FROM users
        ORDER BY rating DESC, LOWER(username) ASC, id ASC
        LIMIT 10
        """
    ).fetchall()
    return jsonify(leaders=[user_json(user) for user in leaders])


@bp.get("/api/users/<int:user_id>/profile")
def user_profile(user_id: int):
    profile = database().execute(
        "SELECT id, username, rating, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if profile is None:
        return error("That account could not be found.", 404)

    record = database().execute(
        """
        SELECT
            COUNT(CASE WHEN
                (white_user_id = ? AND result LIKE 'White wins%')
                OR (black_user_id = ? AND result LIKE 'Black wins%')
                THEN 1 END) AS wins,
            COUNT(CASE WHEN result LIKE 'Draw%' THEN 1 END) AS draws,
            COUNT(*) AS total
        FROM games
        WHERE ((white_user_id = ? AND status = 'completed')
            OR (black_user_id = ? AND status = 'completed'))
            AND result IS NOT NULL
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchone()

    games = database().execute(
        """
        SELECT g.result, g.created_at,
            CASE WHEN g.white_user_id = ? THEN 'white' ELSE 'black' END AS side,
            CASE WHEN g.white_user_id = ? THEN black.username ELSE white.username END AS opponent,
            g.rating_changes_json
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE (g.white_user_id = ? OR g.black_user_id = ?)
            AND g.status = 'completed' AND g.result IS NOT NULL
        ORDER BY g.created_at DESC, g.id DESC
        LIMIT 10
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()

    wins = record["wins"] or 0
    draws = record["draws"] or 0
    recent_games = []
    for game in games:
        won = game["result"].startswith(
            "White wins" if game["side"] == "white" else "Black wins"
        )
        drawn = game["result"].startswith("Draw")
        outcome = "win" if won else "draw" if drawn else "loss"
        rating_change = None
        if game["rating_changes_json"]:
            changes = json.loads(game["rating_changes_json"])
            side_change = changes.get(game["side"])
            if side_change:
                rating_change = side_change.get("change")
        recent_games.append({
            "outcome": outcome,
            "result": game["result"],
            "opponent": game["opponent"] or "Guest",
            "playedAt": str(game["created_at"]),
            "ratingChange": rating_change,
        })

    return jsonify(
        profile={
            "id": profile["id"],
            "username": profile["username"],
            "rating": profile["rating"],
            "memberSince": str(profile["created_at"]),
            "record": {
                "wins": wins,
                "draws": draws,
                "losses": (record["total"] or 0) - wins - draws,
            },
            "recentGames": recent_games,
        }
    )


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
        SELECT g.room_code, g.status, g.created_at,
            white.username AS white_name, black.username AS black_name
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE (g.white_user_id = ? OR g.black_user_id = ?) AND g.status != 'completed'
        ORDER BY g.created_at DESC
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


def requested_time_control() -> tuple[str, dict[str, int | str]] | None:
    value = json_body().get("timeControl", "rapid_10_0")
    if not isinstance(value, str) or value not in TIME_CONTROLS:
        return None
    return value, TIME_CONTROLS[value]


@bp.post("/api/rooms")
def create_room():
    selected_time_control = requested_time_control()
    if selected_time_control is None:
        return error("Choose a supported time control.")
    time_control, settings = selected_time_control
    user = current_user()
    visitor_id = guest_id() if user is None else None
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, white_guest_id,
            moves_json, grades_json, status, time_control,
            white_time_ms, black_time_ms, increment_seconds
        )
        VALUES (?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?)
        """,
        (
            code, user["id"] if user else None, visitor_id, "waiting",
            time_control, int(settings["seconds"]) * 1000,
            int(settings["seconds"]) * 1000, int(settings["increment"]),
        ),
    )
    database().commit()
    return jsonify(roomCode=code, url=f"/play?room={code}"), 201


@bp.post("/api/invitations")
def invite_friend():
    user = require_user()
    if user is None:
        return error("Sign in to invite a friend.", 401)
    selected_time_control = requested_time_control()
    if selected_time_control is None:
        return error("Choose a supported time control.")
    time_control, settings = selected_time_control
    friend_id = json_body().get("friendId")
    if not isinstance(friend_id, int) or not room_user_is_friend(user["id"], friend_id):
        return error("Choose someone from your friends list.")
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, invited_user_id,
            moves_json, grades_json, status, time_control,
            white_time_ms, black_time_ms, increment_seconds
        )
        VALUES (?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?)
        """,
        (
            code, user["id"], friend_id, "waiting", time_control,
            int(settings["seconds"]) * 1000, int(settings["seconds"]) * 1000,
            int(settings["increment"]),
        ),
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


def remaining_clocks(room: sqlite3.Row, now_ms: int | None = None) -> tuple[int, int]:
    white_ms = int(room["white_time_ms"])
    black_ms = int(room["black_time_ms"])
    if room["status"] != "active" or room["clock_started_at"] is None:
        return white_ms, black_ms
    board, _history = room_board(room)
    elapsed = max(0, (now_ms or int(time.time() * 1000)) - room["clock_started_at"])
    if board.turn == chess.WHITE:
        white_ms = max(0, white_ms - elapsed)
    else:
        black_ms = max(0, black_ms - elapsed)
    return white_ms, black_ms


def synchronize_room_clock(room: sqlite3.Row, now_ms: int) -> sqlite3.Row:
    if room["status"] != "active":
        return room

    present = {
        side: room[f"{side}_last_seen_at"] is not None
        and now_ms - room[f"{side}_last_seen_at"] <= PRESENCE_TIMEOUT_MS
        for side in ("white", "black")
    }
    started_at = room["clock_started_at"]
    if all(present.values()) and started_at is not None:
        white_ms, black_ms = remaining_clocks(room, now_ms)
        board, _history = room_board(room)
        if (white_ms if board.turn == chess.WHITE else black_ms) <= 0:
            result = "Black wins on time" if board.turn == chess.WHITE else "White wins on time"
            connection = database()
            connection.execute(
                """
                UPDATE games SET status = 'completed', result = ?, white_time_ms = ?,
                    black_time_ms = ?, clock_started_at = NULL WHERE id = ?
                """,
                (result, white_ms, black_ms, room["id"]),
            )
            rate_completed_game(connection, room, result)
            return load_room(room["room_code"])

    if not all(present.values()) and started_at is not None:
        stale_at = min(
            room[f"{side}_last_seen_at"] + PRESENCE_TIMEOUT_MS
            if room[f"{side}_last_seen_at"] is not None else started_at
            for side in ("white", "black")
            if not present[side]
        )
        stopped_at = max(started_at, min(now_ms, stale_at))
        elapsed = stopped_at - started_at
        board, _history = room_board(room)
        white_ms = int(room["white_time_ms"])
        black_ms = int(room["black_time_ms"])
        if board.turn == chess.WHITE:
            white_ms = max(0, white_ms - elapsed)
        else:
            black_ms = max(0, black_ms - elapsed)

        if (white_ms if board.turn == chess.WHITE else black_ms) <= 0:
            result = "Black wins on time" if board.turn == chess.WHITE else "White wins on time"
            connection = database()
            connection.execute(
                """
                UPDATE games SET status = 'completed', result = ?, white_time_ms = ?,
                    black_time_ms = ?, clock_started_at = NULL WHERE id = ?
                """,
                (result, white_ms, black_ms, room["id"]),
            )
            rate_completed_game(connection, room, result)
        else:
            database().execute(
                """
                UPDATE games SET white_time_ms = ?, black_time_ms = ?,
                    clock_started_at = NULL WHERE id = ?
                """,
                (white_ms, black_ms, room["id"]),
            )
        return load_room(room["room_code"])

    moves = json.loads(room["moves_json"])
    if all(present.values()) and started_at is None and moves:
        database().execute(
            "UPDATE games SET clock_started_at = ? WHERE id = ?",
            (now_ms, room["id"]),
        )
        return load_room(room["room_code"])
    return room


def update_room_presence(
    code: str, side: str, present: bool
) -> sqlite3.Row | None:
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code, for_update=True)
    if room is None:
        connection.rollback()
        return None
    now_ms = int(time.time() * 1000)
    room = synchronize_room_clock(room, now_ms)
    if room["status"] != "completed":
        seen_column = "white_last_seen_at" if side == "w" else "black_last_seen_at"
        seen_at = now_ms if present else now_ms - PRESENCE_TIMEOUT_MS - 1
        connection.execute(
            f"UPDATE games SET {seen_column} = ? WHERE id = ?",
            (seen_at, room["id"]),
        )
        room = synchronize_room_clock(load_room(code), now_ms)
    connection.commit()
    return load_room(code)


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


def _history_entry(
    board: chess.Board, move: chess.Move, grade: dict[str, Any]
) -> dict[str, Any]:
    color = "w" if board.turn else "b"
    captured_piece = board.piece_at(move.to_square)
    captured_at = chess.square_name(move.to_square)
    if board.is_en_passant(move):
        captured_square = move.to_square - 8 if board.turn else move.to_square + 8
        captured_piece = board.piece_at(captured_square)
        captured_at = chess.square_name(captured_square)
    san = board.san(move)
    board.push(move)
    piece = board.piece_at(move.to_square)
    return {
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "san": san,
        "color": color,
        "piece": piece.symbol().lower() if piece else "",
        "captured": captured_piece.symbol().lower() if captured_piece else None,
        "promotion": chess.piece_symbol(move.promotion) if move.promotion else None,
        "grade": grade.get("grade"),
        "centipawnLoss": grade.get("centipawnLoss"),
        "capturedAt": captured_at if captured_piece else None,
    }


def room_board(room: sqlite3.Row) -> tuple[chess.Board, list[dict[str, Any]]]:
    moves = json.loads(room["moves_json"])
    grades = json.loads(room["grades_json"])
    board = chess.Board()
    history = []
    for index, uci in enumerate(moves):
        move = chess.Move.from_uci(uci)
        grade = grades[index] if index < len(grades) else {}
        history.append(_history_entry(board, move, grade))
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


def is_brilliant_sacrifice(board: chess.Board, move: chess.Move) -> bool:
    piece = board.piece_at(move.from_square)
    if (
        piece is None
        or piece.piece_type == chess.PAWN
        or board.is_capture(move)
        or board.is_attacked_by(not piece.color, move.from_square)
    ):
        return False
    after_move = board.copy()
    after_move.push(move)
    if after_move.is_check():
        return False
    return any(
        reply.to_square == move.to_square and after_move.is_capture(reply)
        for reply in after_move.legal_moves
    )


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


def room_snapshot(
    room: sqlite3.Row,
    side: str,
    position: tuple[chess.Board, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    board, history = position if position is not None else room_board(room)
    grades = json.loads(room["grades_json"])
    white_time_ms, black_time_ms = remaining_clocks(room)
    last_analysis = grades[-1] if grades else {}
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
        "timeControl": TIME_CONTROLS.get(
            room["time_control"], TIME_CONTROLS["rapid_10_0"]
        )["label"],
        "whiteTimeMs": white_time_ms,
        "blackTimeMs": black_time_ms,
        "clockStartedAt": room["clock_started_at"],
        "rematchRequestedBy": room["rematch_requested_by"],
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
        "evaluation": (
            last_analysis.get(
                "evaluation", round(position_score(board, chess.WHITE) * 100)
            )
            / 100
        ),
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
    existing_side = room_side(room, user, visitor or "")
    if existing_side:
        room = update_room_presence(code.upper(), existing_side, True)
        return jsonify(room_snapshot(room, existing_side))
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
        UPDATE games SET black_user_id = ?, black_guest_id = ?, status = 'active',
            black_last_seen_at = ?, clock_started_at = NULL
        WHERE id = ? AND status = 'waiting'
        """,
        (user["id"] if user else None, visitor, int(time.time() * 1000), fresh["id"]),
    )
    room = synchronize_room_clock(load_room(code.upper()), int(time.time() * 1000))
    connection.commit()
    room = load_room(code.upper())
    return jsonify(room_snapshot(room, "b"))


@bp.get("/api/rooms/<code>")
def get_room_state(code: str):
    room = load_room(code.upper())
    if room is None:
        return error("That room could not be found.", 404)
    user = current_user()
    if (
        room["status"] == "waiting"
        and room["invited_user_id"] is not None
        and user is not None
        and user["id"] == room["invited_user_id"]
        and room["black_user_id"] is None
        and room["black_guest_id"] is None
    ):
        snapshot = room_snapshot(room, "b")
        snapshot["requiresAcceptance"] = True
        return jsonify(snapshot)
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    room = update_room_presence(code.upper(), side, True)
    return jsonify(room_snapshot(room, side))


@bp.get("/api/rooms/<code>/spectate")
def spectate_room(code: str):
    room = load_room(code.upper())
    if room is None:
        return error("That room could not be found.", 404)
    if room["status"] not in ("active", "completed"):
        return error("That match is not live.", 409)
    snapshot = room_snapshot(room, "w")
    snapshot["spectating"] = True
    return jsonify(snapshot)


@bp.post("/api/rooms/<code>/leave")
def leave_room(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    update_room_presence(code.upper(), side, False)
    return jsonify(ok=True)


@bp.post("/api/rooms/<code>/close")
def close_room(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code.upper(), for_update=True)
    side = room_side(room, current_user(), guest_id()) if room else None
    if room is None or side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    if room["status"] == "waiting":
        if side != "w":
            connection.rollback()
            return error("Only the room creator can close a waiting room.", 403)
        connection.execute(
            """
            UPDATE games SET status = 'completed', result = 'Room closed by creator',
                clock_started_at = NULL, rematch_requested_by = NULL
            WHERE id = ? AND status = 'waiting'
            """,
            (room["id"],),
        )
        connection.commit()
        return jsonify(ok=True, status="completed")
    if room["status"] != "active":
        connection.rollback()
        return error("This room is already closed.", 409)
    now_ms = int(time.time() * 1000)
    room = synchronize_room_clock(room, now_ms)
    if room["status"] != "active":
        connection.commit()
        return jsonify(ok=True, status="completed", result=room["result"])
    result = "Black wins by forfeit" if side == "w" else "White wins by forfeit"
    white_ms, black_ms = remaining_clocks(room, now_ms)
    connection.execute(
        """
        UPDATE games SET status = 'completed', result = ?, white_time_ms = ?,
            black_time_ms = ?, clock_started_at = NULL, rematch_requested_by = NULL
        WHERE id = ? AND status = 'active'
        """,
        (result, white_ms, black_ms, room["id"]),
    )
    rate_completed_game(connection, room, result)
    connection.commit()
    return jsonify(ok=True, status="completed", result=result)


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
    now_ms = int(time.time() * 1000)
    room = synchronize_room_clock(room, now_ms)
    if room["status"] != "active":
        connection.commit()
        return jsonify(room_snapshot(room, side))
    seen_column = "white_last_seen_at" if side == "w" else "black_last_seen_at"
    connection.execute(
        f"UPDATE games SET {seen_column} = ? WHERE id = ?",
        (now_ms, room["id"]),
    )
    room = synchronize_room_clock(load_room(code.upper()), now_ms)
    if room["status"] != "active":
        connection.commit()
        return jsonify(room_snapshot(room, side))
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
    white_ms, black_ms = remaining_clocks(room, now_ms)
    remaining_ms = white_ms if board.turn == chess.WHITE else black_ms
    if remaining_ms <= 0:
        result = "Black wins on time" if board.turn == chess.WHITE else "White wins on time"
        connection.execute(
            """
            UPDATE games SET status = 'completed', result = ?, white_time_ms = ?,
                black_time_ms = ?, clock_started_at = NULL WHERE id = ?
            """,
            (result, white_ms, black_ms, room["id"]),
        )
        rate_completed_game(connection, room, result)
        connection.commit()
        return jsonify(room_snapshot(load_room(code.upper()), side, (board, _history)))
    moves = json.loads(room["moves_json"])
    grades = json.loads(room["grades_json"])
    moves.append(uci)
    grades.append({})
    history = _history
    history.append(_history_entry(board, move, {}))
    result = result_for(board)
    if board.turn == chess.BLACK:
        white_ms += room["increment_seconds"] * 1000
    else:
        black_ms += room["increment_seconds"] * 1000
    connection.execute(
        """
        UPDATE games SET moves_json = ?, grades_json = ?, result = ?,
            status = ?, white_time_ms = ?, black_time_ms = ?,
            clock_started_at = NULL WHERE id = ?
        """,
        (
            json.dumps(moves),
            json.dumps(grades),
            result,
            "completed" if result else "active",
            white_ms,
            black_ms,
            room["id"],
        ),
    )
    if result:
        rate_completed_game(connection, room, result)
    else:
        synchronize_room_clock(load_room(code.upper()), int(time.time() * 1000))
    connection.commit()
    room = load_room(code.upper())
    return jsonify(room_snapshot(room, side, (board, history)))


@bp.post("/api/rooms/<code>/analysis")
def submit_move_analysis(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    body = json_body()
    ply = body.get("ply")
    centipawn_loss = body.get("centipawnLoss")
    evaluation = body.get("evaluation")
    missed_win = body.get("missedWin", False)
    if (
        not isinstance(ply, int)
        or isinstance(ply, bool)
        or not isinstance(centipawn_loss, int)
        or isinstance(centipawn_loss, bool)
        or not isinstance(evaluation, int)
        or isinstance(evaluation, bool)
        or not isinstance(missed_win, bool)
        or not 1 <= ply <= 1000
        or not 0 <= centipawn_loss <= 3000
        or not -10000 <= evaluation <= 10000
    ):
        return error("The engine analysis values are invalid.")
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    fresh = load_room(code.upper(), for_update=True)
    fresh_side = room_side(fresh, current_user(), guest_id())
    if fresh_side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    moves = json.loads(fresh["moves_json"])
    grades = json.loads(fresh["grades_json"])
    if ply > len(moves) or ply > len(grades):
        connection.rollback()
        return error("That move does not exist in this game.", 409)
    if not isinstance(grades[ply - 1], dict):
        grades[ply - 1] = {}
    if "grade" in grades[ply - 1]:
        connection.commit()
        return jsonify(room_snapshot(fresh, fresh_side))
    board_before = chess.Board()
    for uci in moves[:ply - 1]:
        board_before.push_uci(uci)
    played_move = chess.Move.from_uci(moves[ply - 1])
    if missed_win:
        grade = "Missed win"
    elif centipawn_loss <= 10 and is_brilliant_sacrifice(board_before, played_move):
        grade = "Brilliant"
    elif centipawn_loss >= 500:
        grade = "Grave error"
    elif centipawn_loss >= 300:
        grade = "Blunder"
    elif centipawn_loss >= 150:
        grade = "Mistake"
    elif centipawn_loss >= 75:
        grade = "Inaccuracy"
    elif centipawn_loss >= 30:
        grade = "Good"
    elif centipawn_loss >= 15:
        grade = "Excellent"
    elif centipawn_loss > 10:
        grade = "Great"
    else:
        grade = "Best"
    grades[ply - 1] = {
        "grade": grade,
        "centipawnLoss": centipawn_loss,
        "evaluation": evaluation,
    }
    connection.execute(
        "UPDATE games SET grades_json = ? WHERE id = ?",
        (json.dumps(grades), fresh["id"]),
    )
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), fresh_side))


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
    now_ms = int(time.time() * 1000)
    room = synchronize_room_clock(room, now_ms)
    if room["status"] != "active":
        connection.commit()
        return jsonify(room_snapshot(room, side))
    seen_column = "white_last_seen_at" if side == "w" else "black_last_seen_at"
    connection.execute(
        f"UPDATE games SET {seen_column} = ? WHERE id = ?",
        (now_ms, room["id"]),
    )
    room = synchronize_room_clock(load_room(code.upper()), now_ms)
    if room["status"] != "active":
        connection.commit()
        return jsonify(room_snapshot(room, side))
    result = "Black wins by forfeit" if side == "w" else "White wins by forfeit"
    white_ms, black_ms = remaining_clocks(room, now_ms)
    connection.execute(
        """
        UPDATE games SET status = 'completed', result = ?, white_time_ms = ?,
            black_time_ms = ?, clock_started_at = NULL
        WHERE id = ? AND status = 'active'
        """,
        (result, white_ms, black_ms, room["id"]),
    )
    rate_completed_game(connection, room, result)
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), side))


@bp.post("/api/rooms/<code>/new-game")
def request_new_game(code: str):
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
    if room["rematch_requested_by"] == side:
        connection.commit()
        return jsonify(room_snapshot(room, side))
    if room["rematch_requested_by"] is not None:
        connection.rollback()
        return error("Your opponent has already requested a rematch.", 409)
    seen_column = "white_last_seen_at" if side == "w" else "black_last_seen_at"
    connection.execute(
        f"UPDATE games SET rematch_requested_by = ?, {seen_column} = ? WHERE id = ?",
        (side, int(time.time() * 1000), room["id"]),
    )
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), side))


@bp.post("/api/rooms/<code>/new-game/respond")
def respond_to_new_game(code: str):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    accept = json_body().get("accept")
    if not isinstance(accept, bool):
        return error("Choose whether to accept the rematch.")
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    room = load_room(code.upper(), for_update=True)
    side = room_side(room, current_user(), guest_id())
    if side is None:
        connection.rollback()
        return error("You are not a player in this room.", 403)
    requester = room["rematch_requested_by"]
    if room["status"] != "completed" or requester is None:
        connection.rollback()
        return error("There is no rematch request to respond to.", 409)
    if accept and requester == side:
        connection.rollback()
        return error("The player who requested the rematch cannot accept it.", 409)
    if not accept:
        connection.execute(
            "UPDATE games SET rematch_requested_by = NULL WHERE id = ?",
            (room["id"],),
        )
        connection.commit()
        return jsonify(room_snapshot(load_room(code.upper()), side))

    clock = TIME_CONTROLS.get(room["time_control"], TIME_CONTROLS["rapid_10_0"])
    starting_ms = int(clock["seconds"]) * 1000
    now_ms = int(time.time() * 1000)
    seen_column = "white_last_seen_at" if side == "w" else "black_last_seen_at"
    connection.execute(
        """
        UPDATE games SET moves_json = '[]', grades_json = '[]',
            rating_changes_json = NULL, result = NULL, rematch_requested_by = NULL,
            status = 'active', white_time_ms = ?, black_time_ms = ?,
            clock_started_at = NULL
        WHERE id = ?
        """,
        (starting_ms, starting_ms, room["id"]),
    )
    connection.execute(
        f"UPDATE games SET {seen_column} = ? WHERE id = ?",
        (now_ms, room["id"]),
    )
    room = synchronize_room_clock(load_room(code.upper()), now_ms)
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), side))


app = create_app()


if __name__ == "__main__":
    production = os.environ.get("CHESS_ENV", "").lower() == "production"
    port = int(os.environ.get("PORT", "20202" if production else "20202"))
    host = os.environ.get("CHESS_HOST", "0.0.0.0" if production else "0.0.0.0")
    app.run(host=host, port=port, threaded=True)
