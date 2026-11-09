from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
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
BOT_ELOS = {200, 1000, 1600}
DAILY_COIN_REWARD = 200
BOT_GAME_COIN_REWARDS = {
    200: {"win": 10, "draw": 5, "loss": 3},
    1000: {"win": 20, "draw": 10, "loss": 6},
    1600: {"win": 35, "draw": 18, "loss": 10},
}
BOT_GAME_DAILY_CAP = 60
PLAYER_GAME_DAILY_CAP = 250
MIN_REWARDED_GAME_PLIES = 8
OWNER_USERNAME = "zand"
SHOP_SKINS = {
    "board": (
        {"id": "classic", "name": "Stone & Slate", "price": 0},
        {"id": "grove", "name": "Moss Grove", "price": 100},
        {"id": "lagoon", "name": "Blue Lagoon", "price": 100},
        {"id": "copper", "name": "Copper Field", "price": 100},
        {"id": "amethyst", "name": "Amethyst", "price": 100},
        {"id": "midnight", "name": "Midnight", "price": 100},
        {"id": "rosewood", "name": "Rosewood", "price": 100},
        {"id": "monochrome", "name": "Monochrome", "price": 100},
        {"id": "nebula", "name": "Celestial Atlas", "price": 500},
    ),
    "piece": (
        {"id": "classic", "name": "Classic Ivory", "price": 0},
        {"id": "ivory", "name": "Gilded Ivory", "price": 100},
        {"id": "onyx", "name": "Onyx & Pearl", "price": 100},
        {"id": "jade", "name": "Jade", "price": 100},
        {"id": "sapphire", "name": "Sapphire", "price": 100},
        {"id": "rose", "name": "Rose Quartz", "price": 100},
        {"id": "spatial", "name": "Spatial", "price": 500},
        {"id": "chessnut", "name": "Chessnut", "price": 500},
    ),
}
OWNER_SPECIAL_SKINS = (
    {"id": "golden", "name": "Golden Sovereign", "price": 0},
)


def bot_display_name(elo: int) -> str:
    return "Karti" if elo == 1600 else f"Stockfish ({elo} Elo)"


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
                    coins INTEGER NOT NULL DEFAULT 0,
                    daily_claim_date TEXT,
                    board_skin TEXT NOT NULL DEFAULT 'classic',
                    piece_skin TEXT NOT NULL DEFAULT 'classic',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS user_skins (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    skin_type TEXT NOT NULL CHECK(skin_type IN ('board', 'piece')),
                    skin_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_id, skin_type, skin_id)
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
                    creator_side TEXT NOT NULL DEFAULT 'w',
                    bot_elo INTEGER,
                    is_skin_preview INTEGER NOT NULL DEFAULT 0,
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
                    game_round INTEGER NOT NULL DEFAULT 1,
                    result TEXT,
                    status TEXT NOT NULL CHECK(status IN ('waiting', 'active', 'completed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS coin_game_rewards (
                    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                    game_round INTEGER NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    opponent_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    source TEXT NOT NULL CHECK(source IN ('bot', 'player')),
                    reward_day TEXT NOT NULL,
                    PRIMARY KEY(game_id, game_round, user_id)
                );
                CREATE INDEX IF NOT EXISTS coin_game_rewards_user_day
                    ON coin_game_rewards(user_id, reward_day, source);
                CREATE INDEX IF NOT EXISTS coin_game_rewards_opponent_day
                    ON coin_game_rewards(user_id, opponent_user_id, reward_day);
                CREATE TABLE IF NOT EXISTS global_chat_messages (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room_chat_messages (
                    id INTEGER PRIMARY KEY,
                    room_code TEXT NOT NULL REFERENCES games(room_code) ON DELETE CASCADE,
                    side TEXT NOT NULL CHECK(side IN ('w', 'b')),
                    author_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room_chat_typing (
                    room_code TEXT NOT NULL REFERENCES games(room_code) ON DELETE CASCADE,
                    side TEXT NOT NULL CHECK(side IN ('w', 'b')),
                    typing_until_ms INTEGER NOT NULL,
                    PRIMARY KEY(room_code, side)
                );
                """
            )
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
            for name, definition in (
                ("rating", "INTEGER NOT NULL DEFAULT 800"),
                ("coins", "INTEGER NOT NULL DEFAULT 0"),
                ("daily_claim_date", "TEXT"),
                ("board_skin", "TEXT NOT NULL DEFAULT 'classic'"),
                ("piece_skin", "TEXT NOT NULL DEFAULT 'classic'"),
            ):
                if name not in user_columns:
                    connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
            game_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(games)")
            }
            if "rating_changes_json" not in game_columns:
                connection.execute(
                    "ALTER TABLE games ADD COLUMN rating_changes_json TEXT"
                )
            column_types = {
                "creator_side": "TEXT NOT NULL DEFAULT 'w'",
                "bot_elo": "INTEGER",
                "is_skin_preview": "INTEGER NOT NULL DEFAULT 0",
                "time_control": "TEXT NOT NULL DEFAULT 'rapid_10_0'",
                "white_time_ms": "INTEGER NOT NULL DEFAULT 600000",
                "black_time_ms": "INTEGER NOT NULL DEFAULT 600000",
                "increment_seconds": "INTEGER NOT NULL DEFAULT 0",
                "clock_started_at": "INTEGER",
                "white_last_seen_at": "INTEGER",
                "black_last_seen_at": "INTEGER",
                "rematch_requested_by": "TEXT",
                "game_round": "INTEGER NOT NULL DEFAULT 1",
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS global_chat_created "
                "ON global_chat_messages (created_at_ms DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS room_chat_room_created "
                "ON room_chat_messages (room_code, id DESC)"
            )
            connection.execute(
                "UPDATE users SET piece_skin = 'classic' WHERE piece_skin = 'kiwen-suwi'"
            )
            connection.execute(
                "DELETE FROM user_skins WHERE skin_type = 'piece' AND skin_id = 'kiwen-suwi'"
            )
            migrate_replaced_piece_skins(
                DatabaseConnection(connection, "sqlite")
            )
            migrate_removed_board_skin(DatabaseConnection(connection, "sqlite"))
            connection.commit()
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
                coins INTEGER NOT NULL DEFAULT 0,
                daily_claim_date VARCHAR(10),
                board_skin VARCHAR(24) NOT NULL DEFAULT 'classic',
                piece_skin VARCHAR(24) NOT NULL DEFAULT 'classic',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_skins (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                skin_type VARCHAR(8) NOT NULL CHECK(skin_type IN ('board', 'piece')),
                skin_id VARCHAR(24) NOT NULL,
                acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, skin_type, skin_id)
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
                creator_side VARCHAR(1) NOT NULL DEFAULT 'w',
                bot_elo INTEGER,
                is_skin_preview INTEGER NOT NULL DEFAULT 0,
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
                game_round INTEGER NOT NULL DEFAULT 1,
                result VARCHAR(255),
                status VARCHAR(16) NOT NULL CHECK(status IN ('waiting', 'active', 'completed')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS coin_game_rewards (
                game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                game_round INTEGER NOT NULL,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                opponent_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                source VARCHAR(8) NOT NULL CHECK(source IN ('bot', 'player')),
                reward_day VARCHAR(10) NOT NULL,
                PRIMARY KEY(game_id, game_round, user_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS coin_game_rewards_user_day ON coin_game_rewards (user_id, reward_day, source)",
            "CREATE INDEX IF NOT EXISTS coin_game_rewards_opponent_day ON coin_game_rewards (user_id, opponent_user_id, reward_day)",
            """
            CREATE TABLE IF NOT EXISTS global_chat_messages (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message VARCHAR(280) NOT NULL,
                created_at_ms BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS room_chat_messages (
                id BIGSERIAL PRIMARY KEY,
                room_code VARCHAR(8) NOT NULL REFERENCES games(room_code) ON DELETE CASCADE,
                side VARCHAR(1) NOT NULL CHECK(side IN ('w', 'b')),
                author_name VARCHAR(20) NOT NULL,
                message VARCHAR(280) NOT NULL,
                created_at_ms BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS room_chat_typing (
                room_code VARCHAR(8) NOT NULL REFERENCES games(room_code) ON DELETE CASCADE,
                side VARCHAR(1) NOT NULL CHECK(side IN ('w', 'b')),
                typing_until_ms BIGINT NOT NULL,
                PRIMARY KEY(room_code, side)
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_unique ON users (LOWER(username))",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS rating INTEGER NOT NULL DEFAULT 800",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS coins INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_claim_date VARCHAR(10)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS board_skin VARCHAR(24) NOT NULL DEFAULT 'classic'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS piece_skin VARCHAR(24) NOT NULL DEFAULT 'classic'",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS rating_changes_json TEXT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS creator_side VARCHAR(1) NOT NULL DEFAULT 'w'",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS bot_elo INTEGER",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS is_skin_preview INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS white_last_seen_at BIGINT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS black_last_seen_at BIGINT",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS rematch_requested_by VARCHAR(1)",
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS game_round INTEGER NOT NULL DEFAULT 1",
            "CREATE INDEX IF NOT EXISTS games_white_user_status_created ON games (white_user_id, status, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS games_black_user_status_created ON games (black_user_id, status, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS games_status_created ON games (status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS games_invited_status_created ON games (invited_user_id, status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS global_chat_created ON global_chat_messages (created_at_ms DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS room_chat_room_created ON room_chat_messages (room_code, id DESC)",
        )
        try:
            for statement in schema:
                connection.execute(statement)
            connection.execute(
                "UPDATE users SET piece_skin = 'classic' WHERE piece_skin = ?",
                ("kiwen-suwi",),
            )
            connection.execute(
                "DELETE FROM user_skins WHERE skin_type = ? AND skin_id = ?",
                ("piece", "kiwen-suwi"),
            )
            migrate_replaced_piece_skins(connection)
            migrate_removed_board_skin(connection)
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
            coins INTEGER NOT NULL DEFAULT 0,
            daily_claim_date VARCHAR(10),
            board_skin VARCHAR(24) NOT NULL DEFAULT 'classic',
            piece_skin VARCHAR(24) NOT NULL DEFAULT 'classic',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_skins (
            user_id BIGINT NOT NULL,
            skin_type VARCHAR(8) NOT NULL,
            skin_id VARCHAR(24) NOT NULL,
            acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, skin_type, skin_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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
            creator_side VARCHAR(1) NOT NULL DEFAULT 'w',
            bot_elo INTEGER,
            is_skin_preview INTEGER NOT NULL DEFAULT 0,
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
            game_round INTEGER NOT NULL DEFAULT 1,
            result VARCHAR(255),
            status VARCHAR(16) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (white_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (black_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (invited_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS coin_game_rewards (
            game_id BIGINT NOT NULL,
            game_round INTEGER NOT NULL,
            user_id BIGINT NOT NULL,
            opponent_user_id BIGINT NULL,
            amount INTEGER NOT NULL,
            source VARCHAR(8) NOT NULL,
            reward_day VARCHAR(10) NOT NULL,
            PRIMARY KEY(game_id, game_round, user_id),
            KEY coin_game_rewards_user_day (user_id, reward_day, source),
            KEY coin_game_rewards_opponent_day (user_id, opponent_user_id, reward_day),
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (opponent_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS global_chat_messages (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            message VARCHAR(280) NOT NULL,
            created_at_ms BIGINT NOT NULL,
            KEY global_chat_created (created_at_ms, id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS room_chat_messages (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            room_code VARCHAR(8) NOT NULL,
            side VARCHAR(1) NOT NULL,
            author_name VARCHAR(20) NOT NULL,
            message VARCHAR(280) NOT NULL,
            created_at_ms BIGINT NOT NULL,
            KEY room_chat_room_created (room_code, id),
            FOREIGN KEY (room_code) REFERENCES games(room_code) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS room_chat_typing (
            room_code VARCHAR(8) NOT NULL,
            side VARCHAR(1) NOT NULL,
            typing_until_ms BIGINT NOT NULL,
            PRIMARY KEY(room_code, side),
            FOREIGN KEY (room_code) REFERENCES games(room_code) ON DELETE CASCADE
        )
        """,
    )
    try:
        for statement in schema:
            connection.execute(statement)
        if backend == "postgres":
            user_columns = {
                row["column_name"] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'users'"
                ).fetchall()
            }
        else:
            user_columns = {
                row["Field"] for row in connection.execute("SHOW COLUMNS FROM users").fetchall()
            }
        user_column_types = {
            "coins": "INTEGER NOT NULL DEFAULT 0",
            "daily_claim_date": "VARCHAR(10)",
            "board_skin": "VARCHAR(24) NOT NULL DEFAULT 'classic'",
            "piece_skin": "VARCHAR(24) NOT NULL DEFAULT 'classic'",
        }
        for name, definition in user_column_types.items():
            if name not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
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
            "creator_side": "VARCHAR(1) NOT NULL DEFAULT 'w'",
            "bot_elo": "INTEGER",
            "is_skin_preview": "INTEGER NOT NULL DEFAULT 0",
            "time_control": "VARCHAR(24) NOT NULL DEFAULT 'rapid_10_0'",
            "white_time_ms": "BIGINT NOT NULL DEFAULT 600000",
            "black_time_ms": "BIGINT NOT NULL DEFAULT 600000",
            "increment_seconds": "INTEGER NOT NULL DEFAULT 0",
            "clock_started_at": "BIGINT",
            "white_last_seen_at": "BIGINT",
            "black_last_seen_at": "BIGINT",
            "rematch_requested_by": "VARCHAR(1)",
            "game_round": "INTEGER NOT NULL DEFAULT 1",
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
        if backend == "mysql":
            chat_indexes = {
                row["Key_name"]
                for table in ("global_chat_messages", "room_chat_messages")
                for row in connection.execute(f"SHOW INDEX FROM {table}").fetchall()
            }
            if "global_chat_created" not in chat_indexes:
                connection.execute(
                    "CREATE INDEX global_chat_created "
                    "ON global_chat_messages (created_at_ms, id)"
                )
            if "room_chat_room_created" not in chat_indexes:
                connection.execute(
                    "CREATE INDEX room_chat_room_created "
                    "ON room_chat_messages (room_code, id)"
                )
        connection.execute(
            "UPDATE users SET piece_skin = ? WHERE piece_skin = ?",
            ("classic", "kiwen-suwi"),
        )
        connection.execute(
            "DELETE FROM user_skins WHERE skin_type = ? AND skin_id = ?",
            ("piece", "kiwen-suwi"),
        )
        migrate_replaced_piece_skins(connection)
        migrate_removed_board_skin(connection)
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


def migrate_replaced_piece_skins(connection: DatabaseConnection) -> None:
    for old_skin, new_skin in (("fantasy", "spatial"), ("celtic", "chessnut")):
        connection.execute(
            "UPDATE users SET piece_skin = ? WHERE piece_skin = ?",
            (new_skin, old_skin),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO user_skins(user_id, skin_type, skin_id, acquired_at)
            SELECT user_id, skin_type, ?, acquired_at
            FROM user_skins WHERE skin_type = 'piece' AND skin_id = ?
            """,
            (new_skin, old_skin),
        )
        connection.execute(
            "DELETE FROM user_skins WHERE skin_type = 'piece' AND skin_id = ?",
            (old_skin,),
        )


def migrate_removed_board_skin(connection: DatabaseConnection) -> None:
    connection.execute(
        "UPDATE users SET board_skin = 'monochrome' WHERE board_skin = 'glacier'"
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO user_skins(user_id, skin_type, skin_id, acquired_at)
        SELECT user_id, skin_type, 'monochrome', acquired_at
        FROM user_skins WHERE skin_type = 'board' AND skin_id = 'glacier'
        """
    )
    connection.execute(
        "DELETE FROM user_skins WHERE skin_type = 'board' AND skin_id = 'glacier'"
    )


def shop_snapshot(user_id: int) -> dict[str, Any]:
    user = database().execute(
        """
        SELECT username, coins, daily_claim_date, board_skin, piece_skin
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if user is None:
        raise RuntimeError("The signed-in shop account could not be loaded.")
    owned = {
        (row["skin_type"], row["skin_id"])
        for row in database().execute(
            "SELECT skin_type, skin_id FROM user_skins WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    }
    owned.update({("board", "classic"), ("piece", "classic")})
    is_owner = user["username"].casefold() == OWNER_USERNAME
    if is_owner:
        owned.update(("piece", skin["id"]) for skin in OWNER_SPECIAL_SKINS)
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "coins": user["coins"],
        "dailyReward": DAILY_COIN_REWARD,
        "canClaimDaily": user["daily_claim_date"] != today,
        "boardSkin": user["board_skin"],
        "pieceSkin": user["piece_skin"],
        "skins": {
            kind: [
                {**skin, "owned": (kind, skin["id"]) in owned,
                 "equipped": user[f"{kind}_skin"] == skin["id"]}
                for skin in skins
            ]
            for kind, skins in SHOP_SKINS.items()
        },
        "specialSkins": [
            {
                **skin,
                "owned": ("piece", skin["id"]) in owned,
                "equipped": user["piece_skin"] == skin["id"],
                "ownerOnly": True,
            }
            for skin in OWNER_SPECIAL_SKINS
        ],
    }


def lock_shop_account(user_id: int):
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    statement = "SELECT coins, daily_claim_date FROM users WHERE id = ?"
    if connection.backend != "sqlite":
        statement += " FOR UPDATE"
    user = connection.execute(statement, (user_id,)).fetchone()
    if user is None:
        connection.rollback()
        raise RuntimeError("The signed-in shop account could not be loaded.")
    return connection, user


@bp.get("/api/shop")
def get_shop():
    user = current_user()
    if user is None:
        return error("Sign in to use the shop and save cosmetics to your account.", 401)
    return jsonify(shop_snapshot(user["id"]))


@bp.post("/api/shop/daily")
def claim_daily_coins():
    user = current_user()
    if user is None:
        return error("Sign in to collect daily coins.", 401)
    connection, account = lock_shop_account(user["id"])
    today = datetime.now(timezone.utc).date().isoformat()
    if account["daily_claim_date"] == today:
        connection.rollback()
        return error("Your daily coins have already been collected.", 409)
    connection.execute(
        "UPDATE users SET coins = coins + ?, daily_claim_date = ? WHERE id = ?",
        (DAILY_COIN_REWARD, today, user["id"]),
    )
    connection.commit()
    return jsonify(shop_snapshot(user["id"]))


@bp.post("/api/shop/purchase")
def purchase_shop_skin():
    user = current_user()
    if user is None:
        return error("Sign in to purchase cosmetics.", 401)
    body = json_body()
    skin_type = body.get("type")
    skin_id = body.get("skinId")
    if not isinstance(skin_type, str) or not isinstance(skin_id, str):
        return error("Choose a skin from the shop.")
    if any(skin["id"] == skin_id for skin in OWNER_SPECIAL_SKINS):
        return error("Owner-exclusive skins cannot be purchased.", 403)
    catalog = SHOP_SKINS.get(skin_type)
    skin = next(
        (entry for entry in catalog or () if entry["id"] == skin_id),
        None,
    )
    if skin is None or skin["price"] == 0:
        return error("Choose a skin from the shop.")
    connection, account = lock_shop_account(user["id"])
    exists = connection.execute(
        "SELECT 1 FROM user_skins WHERE user_id = ? AND skin_type = ? AND skin_id = ?",
        (user["id"], skin_type, skin_id),
    ).fetchone()
    if exists is not None:
        connection.rollback()
        return error("You already own this skin.", 409)
    if account["coins"] < skin["price"]:
        connection.rollback()
        return error("You do not have enough coins for this skin.", 409)
    connection.execute(
        "UPDATE users SET coins = coins - ? WHERE id = ?",
        (skin["price"], user["id"]),
    )
    connection.execute(
        "INSERT INTO user_skins(user_id, skin_type, skin_id) VALUES (?, ?, ?)",
        (user["id"], skin_type, skin_id),
    )
    connection.commit()
    return jsonify(shop_snapshot(user["id"]))


@bp.post("/api/shop/equip")
def equip_shop_skin():
    user = current_user()
    if user is None:
        return error("Sign in to equip cosmetics.", 401)
    body = json_body()
    skin_type = body.get("type")
    skin_id = body.get("skinId")
    if not isinstance(skin_type, str) or not isinstance(skin_id, str):
        return error("Choose a skin from the shop.")
    owner_skin = next(
        (skin for skin in OWNER_SPECIAL_SKINS if skin["id"] == skin_id),
        None,
    )
    is_owner = user["username"].casefold() == OWNER_USERNAME
    if owner_skin is not None and (skin_type != "piece" or not is_owner):
        return error("This skin is reserved for the site owner.", 403)
    catalog = SHOP_SKINS.get(skin_type)
    if owner_skin is None and not any(
        skin["id"] == skin_id for skin in catalog or ()
    ):
        return error("Choose a skin from the shop.")
    owned = owner_skin is not None or skin_id == "classic" or database().execute(
        "SELECT 1 FROM user_skins WHERE user_id = ? AND skin_type = ? AND skin_id = ?",
        (user["id"], skin_type, skin_id),
    ).fetchone() is not None
    if not owned:
        return error("Purchase this skin before equipping it.", 403)
    database().execute(
        f"UPDATE users SET {skin_type}_skin = ? WHERE id = ?",
        (skin_id, user["id"]),
    )
    database().commit()
    return jsonify(shop_snapshot(user["id"]))


@bp.get("/api/live-matches")
def live_matches():
    matches = database().execute(
        """
        SELECT g.room_code, g.bot_elo, white.username AS white_name,
            black.username AS black_name, g.time_control, g.moves_json
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE g.status = 'active' AND g.is_skin_preview = 0
        ORDER BY g.created_at DESC
        LIMIT 50
        """
    ).fetchall()
    return jsonify(matches=[
        {
            "roomCode": match["room_code"],
            "whiteName": match["white_name"] or "Guest",
            "blackName": (
                bot_display_name(match["bot_elo"])
                if match["bot_elo"] is not None else match["black_name"] or "Guest"
            ),
            "timeControl": TIME_CONTROLS.get(
                match["time_control"], TIME_CONTROLS["rapid_10_0"]
            )["label"],
            "movesPlayed": len(json.loads(match["moves_json"])),
        }
        for match in matches
    ])


@bp.get("/api/global-chat")
def get_global_chat():
    user = current_user()
    user_id = user["id"] if user is not None else -1
    messages = database().execute(
        """
        SELECT m.id, m.user_id, u.username, m.message, m.created_at_ms,
            f.user_one AS friend_user_one,
            sent.id AS sent_request_id,
            received.id AS received_request_id
        FROM global_chat_messages m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN friendships f ON
            (f.user_one = ? AND f.user_two = m.user_id)
            OR (f.user_two = ? AND f.user_one = m.user_id)
        LEFT JOIN friend_requests sent
            ON sent.sender_id = ? AND sent.receiver_id = m.user_id
        LEFT JOIN friend_requests received
            ON received.sender_id = m.user_id AND received.receiver_id = ?
        ORDER BY m.created_at_ms DESC, m.id DESC
        LIMIT 50
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    return jsonify(messages=[
        {
            "id": row["id"],
            "userId": row["user_id"],
            "username": row["username"],
            "message": row["message"],
            "createdAt": row["created_at_ms"],
            "own": user is not None and row["user_id"] == user["id"],
            "isFriend": row["friend_user_one"] is not None,
            "sentRequestId": row["sent_request_id"],
            "receivedRequestId": row["received_request_id"],
        }
        for row in reversed(messages)
    ])


@bp.post("/api/global-chat")
def post_global_chat():
    user = require_user()
    if user is None:
        return error("Sign in to join the global chat.", 401)
    message = json_body().get("message")
    if not isinstance(message, str):
        return error("Enter a chat message.")
    message = message.strip()
    if not message or len(message) > 280:
        return error("Chat messages must be between 1 and 280 characters.")
    database().execute(
        """
        INSERT INTO global_chat_messages(user_id, message, created_at_ms)
        VALUES (?, ?, ?)
        """,
        (user["id"], message, int(time.time() * 1000)),
    )
    database().commit()
    return jsonify(ok=True), 201


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
            AND result IS NOT NULL AND is_skin_preview = 0
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchone()

    games = database().execute(
        """
        SELECT g.result, g.created_at, g.bot_elo,
            CASE WHEN g.white_user_id = ? THEN 'white' ELSE 'black' END AS side,
            CASE WHEN g.white_user_id = ? THEN black.username ELSE white.username END AS opponent,
            g.rating_changes_json
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE (g.white_user_id = ? OR g.black_user_id = ?)
            AND g.status = 'completed' AND g.result IS NOT NULL
            AND g.is_skin_preview = 0
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
            "opponent": (
                bot_display_name(game["bot_elo"])
                if game["bot_elo"] is not None else game["opponent"] or "Guest"
            ),
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
    now_ms = int(time.time() * 1000)
    connection.execute(
        """
        UPDATE games SET
            white_last_seen_at = CASE WHEN white_user_id = ? THEN ? ELSE white_last_seen_at END,
            black_last_seen_at = CASE WHEN black_user_id = ? THEN ? ELSE black_last_seen_at END
        WHERE status = 'waiting' AND invited_user_id IS NOT NULL
            AND (
                (creator_side = 'w' AND white_user_id = ?)
                OR (creator_side = 'b' AND black_user_id = ?)
            )
        """,
        (user["id"], now_ms, user["id"], now_ms, user["id"], user["id"]),
    )
    connection.commit()

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
    sent_requests = connection.execute(
        """
        SELECT r.id, u.id AS user_id, u.username FROM friend_requests r
        JOIN users u ON u.id = r.receiver_id WHERE r.sender_id = ?
        ORDER BY r.created_at
        """,
        (user["id"],),
    ).fetchall()
    invitations = connection.execute(
        """
        SELECT g.room_code,
            CASE WHEN g.white_user_id = g.invited_user_id
                THEN black.username ELSE white.username END AS inviter
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE g.invited_user_id = ? AND g.status = 'waiting'
            AND g.is_skin_preview = 0
        ORDER BY g.created_at DESC
        """,
        (user["id"],),
    ).fetchall()
    games = connection.execute(
        """
        SELECT g.room_code, g.status, g.created_at, g.bot_elo, g.creator_side,
            g.invited_user_id, g.white_user_id, g.black_user_id,
            g.white_last_seen_at, g.black_last_seen_at,
            white.username AS white_name, black.username AS black_name
        FROM games g
        LEFT JOIN users white ON white.id = g.white_user_id
        LEFT JOIN users black ON black.id = g.black_user_id
        WHERE (g.white_user_id = ? OR g.black_user_id = ?)
            AND g.status != 'completed' AND g.is_skin_preview = 0
        ORDER BY g.created_at DESC
        """,
        (user["id"], user["id"]),
    ).fetchall()
    now_ms = int(time.time() * 1000)
    game_items = []
    for game in games:
        creator_side = game["creator_side"]
        opponent_side = "b" if creator_side == "w" else "w"
        opponent_color = "black" if opponent_side == "b" else "white"
        creator_color = "white" if creator_side == "w" else "black"
        opponent_seen = game[f"{opponent_color}_last_seen_at"]
        game_item = dict(game)
        game_item["is_creator"] = game[f"{creator_color}_user_id"] == user["id"]
        game_item["opponent_present"] = (
            game["status"] == "waiting"
            and game["invited_user_id"] is not None
            and opponent_seen is not None
            and now_ms - opponent_seen <= PRESENCE_TIMEOUT_MS
        )
        if game["bot_elo"] is not None:
            game_item["black_name"] = bot_display_name(game["bot_elo"])
        game_items.append(game_item)

    return jsonify(
        friends=[user_json(friend) for friend in friends],
        requests=[dict(row) for row in requests],
        sentRequests=[dict(row) for row in sent_requests],
        invitations=[dict(row) for row in invitations],
        games=game_items,
    )


@bp.post("/api/friends/requests")
def send_friend_request():
    user = require_user()
    if user is None:
        return error("Sign in to add friends.", 401)
    user_id_param = json_body().get("userId")
    username_param = json_body().get("username")
    if isinstance(user_id_param, int):
        target = database().execute(
            "SELECT id, username, rating FROM users WHERE id = ?",
            (user_id_param,),
        ).fetchone()
    elif isinstance(username_param, str) and username_param.strip():
        target = database().execute(
            "SELECT id, username, rating FROM users WHERE username = ? COLLATE NOCASE",
            (username_param.strip(),),
        ).fetchone()
    else:
        return error("Enter a username or select a player.")

    if target is None:
        return error("No account has that username or ID.", 404)
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
    creator_side = "w" if secrets.randbelow(2) == 0 else "b"
    white_user_id = user["id"] if user is not None and creator_side == "w" else None
    black_user_id = user["id"] if user is not None and creator_side == "b" else None
    white_guest_id = visitor_id if visitor_id is not None and creator_side == "w" else None
    black_guest_id = visitor_id if visitor_id is not None and creator_side == "b" else None
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, black_user_id, white_guest_id, black_guest_id,
            creator_side,
            moves_json, grades_json, status, time_control,
            white_time_ms, black_time_ms, increment_seconds
        )
        VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?)
        """,
        (
            code, white_user_id, black_user_id, white_guest_id, black_guest_id,
            creator_side, "waiting",
            time_control, int(settings["seconds"]) * 1000,
            int(settings["seconds"]) * 1000, int(settings["increment"]),
        ),
    )
    database().commit()
    return jsonify(
        roomCode=code, url=f"/play?room={code}", playerSide=creator_side
    ), 201


@bp.post("/api/bot-games")
def create_bot_game():
    selected_time_control = requested_time_control()
    if selected_time_control is None:
        return error("Choose a supported time control.")
    time_control, settings = selected_time_control
    body = json_body()
    bot_elo = body.get("botElo")
    skin_preview = body.get("skinPreview", False)
    if not isinstance(bot_elo, int) or isinstance(bot_elo, bool) or bot_elo not in BOT_ELOS:
        return error("Choose a supported Stockfish opponent.")
    if not isinstance(skin_preview, bool):
        return error("Choose whether this is a skin preview.")
    user = current_user()
    visitor_id = guest_id() if user is None else None
    code = new_room_code()
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, white_guest_id, creator_side, bot_elo,
            is_skin_preview, moves_json, grades_json, status, time_control,
            white_time_ms, black_time_ms, increment_seconds
        )
        VALUES (?, ?, ?, 'w', ?, ?, '[]', '[]', 'active', ?, ?, ?, ?)
        """,
        (
            code, user["id"] if user else None, visitor_id, bot_elo, int(skin_preview),
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
    inviter_side = "w" if secrets.randbelow(2) == 0 else "b"
    white_user_id, black_user_id = (
        (user["id"], friend_id) if inviter_side == "w" else (friend_id, user["id"])
    )
    database().execute(
        """
        INSERT INTO games(
            room_code, white_user_id, black_user_id, invited_user_id, creator_side,
            moves_json, grades_json, status, time_control,
            white_time_ms, black_time_ms, increment_seconds
        )
        VALUES (?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, ?, ?)
        """,
        (
            code, white_user_id, black_user_id, friend_id, inviter_side,
            "waiting", time_control,
            int(settings["seconds"]) * 1000, int(settings["seconds"]) * 1000,
            int(settings["increment"]),
        ),
    )
    database().commit()
    return jsonify(roomCode=code, playerSide=inviter_side), 201


def load_room(code: str, for_update: bool = False) -> sqlite3.Row | None:
    if not ROOM_CODE.fullmatch(code):
        return None
    statement = """
        SELECT g.*, white.username AS white_name, black.username AS black_name,
            white.rating AS white_rating, black.rating AS black_rating,
            COALESCE(white.board_skin, 'classic') AS white_board_skin,
            COALESCE(black.board_skin, 'classic') AS black_board_skin,
            COALESCE(white.piece_skin, 'classic') AS white_piece_skin,
            COALESCE(black.piece_skin, 'classic') AS black_piece_skin
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
    if room["bot_elo"] is not None:
        present["black"] = True
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
        "bestMoveSan": grade.get("bestMoveSan"),
        "fen": board.fen(),
        "check": board.is_check(),
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
    if room["is_skin_preview"] or room["rating_changes_json"] is not None:
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
    award_game_coins(connection, room, result)
    connection.execute(
        "UPDATE games SET rating_changes_json = ? WHERE id = ?",
        (json.dumps(changes), room["id"]),
    )


def award_game_coins(
    connection: DatabaseConnection, room: sqlite3.Row, result: str
) -> None:
    game = connection.execute(
        "SELECT moves_json FROM games WHERE id = ?", (room["id"],)
    ).fetchone()
    moves = json.loads(game["moves_json"])
    if len(moves) < MIN_REWARDED_GAME_PLIES:
        return

    reward_day = datetime.now(timezone.utc).date().isoformat()
    participants = []
    if room["white_user_id"] is not None:
        participants.append(("w", room["white_user_id"]))
    if room["black_user_id"] is not None:
        participants.append(("b", room["black_user_id"]))

    for player_side, user_id in participants:
        if result.startswith("White wins"):
            outcome = "win" if player_side == "w" else "loss"
        elif result.startswith("Black wins"):
            outcome = "win" if player_side == "b" else "loss"
        elif result.startswith("Draw"):
            outcome = "draw"
        else:
            continue

        if room["bot_elo"] is not None:
            if player_side != "w":
                continue
            rewards = BOT_GAME_COIN_REWARDS.get(
                room["bot_elo"], {"win": 10, "draw": 5, "loss": 3}
            )
            amount = rewards.get(outcome, 0)
            source = "bot"
            opponent_user_id = None
            daily_cap = BOT_GAME_DAILY_CAP
        else:
            opponent_user_id = room[f"{'black' if player_side == 'w' else 'white'}_user_id"]
            if opponent_user_id is None or opponent_user_id == user_id:
                continue
            user_rating = room[f"{'white' if player_side == 'w' else 'black'}_rating"] or 800
            opponent_rating = room[f"{'black' if player_side == 'w' else 'white'}_rating"] or 800
            diff = opponent_rating - user_rating

            if outcome == "win":
                amount = max(20, min(60, 35 + diff // 50))
            elif outcome == "draw":
                amount = max(10, min(35, 20 + diff // 100))
            else:
                amount = max(5, min(20, 10 + diff // 100))

            source = "player"
            daily_cap = PLAYER_GAME_DAILY_CAP

        if amount <= 0:
            continue

        if connection.backend != "sqlite":
            connection.execute(
                "SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)
            ).fetchone()

        earned_today = connection.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total FROM coin_game_rewards
            WHERE user_id = ? AND reward_day = ? AND source = ?
            """,
            (user_id, reward_day, source),
        ).fetchone()["total"]

        if source == "player":
            already_rewarded_opponent = connection.execute(
                """
                SELECT 1 FROM coin_game_rewards
                WHERE user_id = ? AND opponent_user_id = ? AND reward_day = ?
                LIMIT 1
                """,
                (user_id, opponent_user_id, reward_day),
            ).fetchone()
            if already_rewarded_opponent is not None:
                continue

        amount = min(amount, daily_cap - int(earned_today))
        if amount <= 0:
            continue

        reward_insert = connection.execute(
            """
            INSERT OR IGNORE INTO coin_game_rewards(
                game_id, game_round, user_id, opponent_user_id, amount, source, reward_day
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room["id"], room["game_round"], user_id, opponent_user_id,
                amount, source, reward_day,
            ),
        )
        if reward_insert.rowcount == 0:
            continue
        connection.execute(
            "UPDATE users SET coins = coins + ? WHERE id = ?",
            (amount, user_id),
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
    opponent_side = "b" if side == "w" else "w"
    opponent_seen_at = room[f"{'black' if opponent_side == 'b' else 'white'}_last_seen_at"]
    opponent_present = (
        room["bot_elo"] is not None
        or (
            opponent_seen_at is not None
            and int(time.time() * 1000) - opponent_seen_at <= PRESENCE_TIMEOUT_MS
        )
    )
    viewer = current_user()
    viewer_user_id = room[f"{'white' if side == 'w' else 'black'}_user_id"]
    reward = None
    if (
        room["status"] == "completed"
        and viewer is not None
        and viewer["id"] == viewer_user_id
    ):
        reward = database().execute(
            """
            SELECT COALESCE((
                SELECT amount FROM coin_game_rewards
                WHERE game_id = ? AND game_round = ? AND user_id = ?
            ), 0) AS amount
            """,
            (room["id"], room["game_round"], viewer["id"]),
        ).fetchone()
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
        "timeControlKey": room["time_control"],
        "whiteTimeMs": white_time_ms,
        "blackTimeMs": black_time_ms,
        "clockStartedAt": room["clock_started_at"],
        "rematchRequestedBy": room["rematch_requested_by"],
        "gameRound": room["game_round"],
        "roomCode": room["room_code"],
        "botElo": room["bot_elo"],
        "isSkinPreview": bool(room["is_skin_preview"]),
        "playerSide": side,
        "canCloseWaitingRoom": side == room["creator_side"],
        "opponentPresent": opponent_present,
        "whiteName": room["white_name"] or "Guest",
        "whiteBoardSkin": room["white_board_skin"],
        "blackBoardSkin": room["black_board_skin"],
        "whitePieceSkin": room["white_piece_skin"],
        "blackPieceSkin": room["black_piece_skin"],
        "blackName": (
            bot_display_name(room["bot_elo"])
            if room["bot_elo"] is not None else room["black_name"] or (
                "Waiting for opponent" if room["status"] == "waiting" else "Guest"
            )
        ),
        "whiteRating": room["white_rating"] if room["white_rating"] is not None else 800,
        "blackRating": (
            room["bot_elo"]
            if room["bot_elo"] is not None
            else room["black_rating"] if room["black_rating"] is not None else 800
        ),
        "ratingChanges": (
            json.loads(room["rating_changes_json"])
            if room["rating_changes_json"] is not None else None
        ),
        "coinsEarned": reward["amount"] if reward is not None else 0,
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


@bp.get("/api/rooms/<code>/chat")
def get_room_chat(code: str):
    room, side, failure = authorized_room(code)
    if failure is not None:
        return failure
    connection = database()
    messages = connection.execute(
        """
        SELECT id, side, author_name, message, created_at_ms
        FROM room_chat_messages WHERE room_code = ?
        ORDER BY id DESC LIMIT 100
        """,
        (room["room_code"],),
    ).fetchall()
    typing_rows = connection.execute(
        """
        SELECT side FROM room_chat_typing
        WHERE room_code = ? AND typing_until_ms > ?
        """,
        (room["room_code"], int(time.time() * 1000)),
    ).fetchall()
    return jsonify(
        playerSide=side,
        messages=[
            {
                "id": row["id"],
                "side": row["side"],
                "authorName": row["author_name"],
                "message": row["message"],
                "createdAt": row["created_at_ms"],
            }
            for row in reversed(messages)
        ],
        typing={row["side"]: True for row in typing_rows},
    )


@bp.post("/api/rooms/<code>/chat")
def post_room_chat(code: str):
    room, side, failure = authorized_room(code)
    if failure is not None:
        return failure
    message = json_body().get("message")
    if not isinstance(message, str):
        return error("Enter a chat message.")
    message = message.strip()
    if not message or len(message) > 280:
        return error("Chat messages must be between 1 and 280 characters.")
    author_name = room["white_name"] if side == "w" else room["black_name"]
    if not author_name:
        author_name = "White" if side == "w" else "Black"
    connection = database()
    connection.execute(
        """
        INSERT INTO room_chat_messages(
            room_code, side, author_name, message, created_at_ms
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            room["room_code"], side, author_name, message,
            int(time.time() * 1000),
        ),
    )
    connection.execute(
        "DELETE FROM room_chat_typing WHERE room_code = ? AND side = ?",
        (room["room_code"], side),
    )
    connection.commit()
    return jsonify(ok=True), 201


@bp.post("/api/rooms/<code>/chat/typing")
def update_room_chat_typing(code: str):
    room, side, failure = authorized_room(code)
    if failure is not None:
        return failure
    is_typing = json_body().get("isTyping")
    if not isinstance(is_typing, bool):
        return error("Choose whether you are typing.")
    connection = database()
    if is_typing:
        connection.execute(
            """
            INSERT OR IGNORE INTO room_chat_typing(room_code, side, typing_until_ms)
            VALUES (?, ?, ?)
            """,
            (room["room_code"], side, int(time.time() * 1000) + 3000),
        )
        connection.execute(
            """
            UPDATE room_chat_typing SET typing_until_ms = ?
            WHERE room_code = ? AND side = ?
            """,
            (int(time.time() * 1000) + 3000, room["room_code"], side),
        )
    else:
        connection.execute(
            "DELETE FROM room_chat_typing WHERE room_code = ? AND side = ?",
            (room["room_code"], side),
        )
    connection.commit()
    return jsonify(ok=True)


@bp.post("/api/rooms/<code>/join")
def join_room(code: str):
    room = load_room(code.upper())
    if room is None:
        return error("That room could not be found.", 404)
    user = current_user()
    visitor = guest_id() if user is None else None
    existing_side = room_side(room, user, visitor or "")
    if existing_side:
        if (
            room["status"] == "waiting"
            and room["invited_user_id"] is not None
            and user is not None
            and user["id"] == room["invited_user_id"]
        ):
            connection = database()
            connection.execute("BEGIN IMMEDIATE")
            fresh = load_room(code.upper(), for_update=True)
            if fresh is None or fresh["status"] != "waiting":
                connection.rollback()
                return error("This invitation is no longer waiting for a player.", 409)
            seen_column = "white_last_seen_at" if existing_side == "w" else "black_last_seen_at"
            connection.execute(
                f"UPDATE games SET status = 'active', {seen_column} = ?, clock_started_at = NULL "
                "WHERE id = ? AND status = 'waiting'",
                (int(time.time() * 1000), fresh["id"]),
            )
            room = synchronize_room_clock(
                load_room(code.upper()), int(time.time() * 1000)
            )
            connection.commit()
            room = load_room(code.upper())
            return jsonify(room_snapshot(room, existing_side))
        room = update_room_presence(code.upper(), existing_side, True)
        return jsonify(room_snapshot(room, existing_side))
    if room["invited_user_id"] is not None and (
        user is None or user["id"] != room["invited_user_id"]
    ):
        return error("This room is an invitation for another player.", 403)
    join_side = "b" if room["creator_side"] == "w" else "w"
    join_color = "white" if join_side == "w" else "black"
    if (
        room["status"] != "waiting"
        or room[f"{join_color}_user_id"]
        or room[f"{join_color}_guest_id"]
    ):
        return error("This room already has two players.", 409)
    connection = database()
    connection.execute("BEGIN IMMEDIATE")
    fresh = load_room(code.upper(), for_update=True)
    if fresh is None or fresh["status"] != "waiting":
        connection.rollback()
        return error("This room is no longer waiting for a player.", 409)
    connection.execute(
        f"""
        UPDATE games SET {join_color}_user_id = ?, {join_color}_guest_id = ?,
            status = 'active', {join_color}_last_seen_at = ?, clock_started_at = NULL
        WHERE id = ? AND status = 'waiting'
        """,
        (user["id"] if user else None, visitor, int(time.time() * 1000), fresh["id"]),
    )
    room = synchronize_room_clock(load_room(code.upper()), int(time.time() * 1000))
    connection.commit()
    room = load_room(code.upper())
    return jsonify(room_snapshot(room, join_side))


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
    ):
        invited_side = room_side(room, user, "")
        room = update_room_presence(code.upper(), invited_side, True)
        snapshot = room_snapshot(room, invited_side)
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
    if room is None or room["is_skin_preview"]:
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
    if room["is_skin_preview"]:
        connection.execute(
            """
            UPDATE games SET status = 'completed', result = 'Preview closed',
                clock_started_at = NULL, rematch_requested_by = NULL
            WHERE id = ? AND status = 'active'
            """,
            (room["id"],),
        )
        connection.commit()
        return jsonify(ok=True, status="completed")
    if room["status"] == "waiting":
        user = current_user()
        if side != room["creator_side"]:
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
    return apply_room_move(code)


@bp.post("/api/rooms/<code>/bot-move")
def make_bot_move(code: str):
    return apply_room_move(code, bot_move=True)


def apply_room_move(code: str, bot_move: bool = False):
    room, side, failure = authorized_room(code)
    if failure:
        return failure
    if bot_move and (room["bot_elo"] not in BOT_ELOS or side != "w"):
        return error("Only the player in a Stockfish game can submit a bot move.", 403)
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
    if bot_move and (room["bot_elo"] not in BOT_ELOS or side != "w"):
        connection.rollback()
        return error("Only the player in a Stockfish game can submit a bot move.", 403)
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
    move_side = "b" if bot_move else side
    if ("w" if board.turn else "b") != move_side:
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
    best_move_uci = body.get("bestMove")
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
        or (
            best_move_uci is not None
            and (
                not isinstance(best_move_uci, str)
                or not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", best_move_uci)
            )
        )
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
    best_move_san = None
    if best_move_uci is not None:
        best_move = chess.Move.from_uci(best_move_uci)
        if best_move not in board_before.legal_moves:
            connection.rollback()
            return error("Stockfish's recommended move is not legal in this position.")
        best_move_san = board_before.san(best_move)
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
        "bestMoveSan": best_move_san,
    }
    connection.execute(
        "UPDATE games SET grades_json = ? WHERE id = ?",
        (json.dumps(grades), fresh["id"]),
    )
    connection.commit()
    return jsonify(room_snapshot(load_room(code.upper()), fresh_side))


@bp.post("/api/rooms/<code>/review/<int:ply>/alternatives")
def review_move_alternatives(code: str, ply: int):
    room, _side, failure = authorized_room(code)
    if failure:
        return failure
    if room["status"] != "completed":
        return error("Move alternatives are available after the game is complete.", 409)
    moves = json.loads(room["moves_json"])
    candidates = json_body().get("moves")
    if (
        not 1 <= ply <= len(moves)
        or not isinstance(candidates, list)
        or not 1 <= len(candidates) <= 4
        or any(
            not isinstance(uci, str)
            or not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci)
            for uci in candidates
        )
        or len(set(candidates)) != len(candidates)
    ):
        return error("The requested review moves are invalid.")

    board = chess.Board()
    for uci in moves[:ply - 1]:
        board.push_uci(uci)
    alternatives = []
    for uci in candidates:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            return error("A Stockfish review move is not legal in this position.")
        piece = board.piece_at(move.from_square)
        captured = board.piece_at(move.to_square)
        if board.is_en_passant(move):
            captured = board.piece_at(
                move.to_square - 8 if board.turn else move.to_square + 8
            )
        san = board.san(move)
        side_to_move = board.turn
        board.push(move)
        attacked_targets = [
            (square, board.piece_at(square))
            for square in board.attacks(move.to_square)
            if board.piece_at(square) is not None
            and board.piece_at(square).color != side_to_move
        ]
        attacked_targets.sort(
            key=lambda target: target[1].piece_type if target[1] else 0,
            reverse=True,
        )
        attacked_square, attacked_piece = attacked_targets[0] if attacked_targets else (None, None)
        alternatives.append({
            "uci": uci,
            "san": san,
            "from": chess.square_name(move.from_square),
            "to": chess.square_name(move.to_square),
            "piece": chess.piece_name(piece.piece_type) if piece else "piece",
            "captured": chess.piece_name(captured.piece_type) if captured else None,
            "attackedTarget": (
                {
                    "square": chess.square_name(attacked_square),
                    "piece": chess.piece_name(attacked_piece.piece_type),
                    "color": "white" if attacked_piece.color else "black",
                }
                if attacked_piece is not None else None
            ),
        })
        board.pop()
    return jsonify(alternatives=alternatives)


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
            game_round = game_round + 1, status = 'active',
            white_time_ms = ?, black_time_ms = ?,
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
