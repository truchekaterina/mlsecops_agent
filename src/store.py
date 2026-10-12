"""SQLite-учёт стенда: пользователи, счета, сделки, письма.

Один файл strazh.db. WAL, чтобы веб и MCP-процесс писали одновременно.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from paths import DATA

DB_PATH = DATA / "strazh.db"
USERS_JSON = DATA / "users.json"
LEDGERS_JSON = DATA / "ledgers.json"

SECRET_DEFAULT = "strazh-demo-hs256"

_init_lock = threading.Lock()
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  email TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  password_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  email TEXT PRIMARY KEY,
  cash_usd REAL NOT NULL,
  reserve_usd REAL NOT NULL,
  pending_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
  email TEXT NOT NULL,
  ticker TEXT NOT NULL,
  shares INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (email, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  confirmation_id TEXT NOT NULL,
  email TEXT NOT NULL,
  kind TEXT NOT NULL,
  ticker TEXT,
  shares INTEGER,
  usd_amount REAL,
  reserved_usd REAL,
  cash_usd REAL,
  reserve_usd REAL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  confirmation_id TEXT,
  email TEXT NOT NULL,
  side TEXT,
  ticker TEXT,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  smtp_ok INTEGER NOT NULL DEFAULT 0,
  smtp_error TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mailboxes (
  email TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_email ON trades(email, id);
CREATE INDEX IF NOT EXISTS idx_messages_email ON messages(email, id);
"""


def _ensure_action_hash(conn: sqlite3.Connection) -> None:
    for table in ("trades", "messages"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "action_hash" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN action_hash TEXT")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def db():
    init()
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    global _ready
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
            _ensure_action_hash(conn)
            _ensure_revoked(conn)
            _ensure_turn_gate(conn)
            _migrate_json(conn)
            _drop_demo_accounts(conn)
            _backfill_mailboxes(conn)
            conn.commit()
        finally:
            conn.close()
        _ready = True


def get_secret() -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='jwt_secret'").fetchone()
        if row:
            return str(row["value"])
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('jwt_secret', ?)",
            (SECRET_DEFAULT,),
        )
        return SECRET_DEFAULT


def _migrate_json(conn: sqlite3.Connection) -> None:
    done = conn.execute("SELECT value FROM meta WHERE key='migrated_json'").fetchone()
    if done:
        return
    secret = SECRET_DEFAULT
    if USERS_JSON.exists():
        try:
            raw = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        if isinstance(raw, dict):
            secret = str(raw.get("secret") or SECRET_DEFAULT)
            for user in raw.get("users") or []:
                email = str(user.get("email") or "").strip().lower()
                if not email:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO users(email, name, password_sha256, created_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        email,
                        str(user.get("name") or "Клиент"),
                        str(user.get("password_sha256") or ""),
                        now_iso(),
                    ),
                )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('jwt_secret', ?)",
        (secret,),
    )
    if LEDGERS_JSON.exists():
        try:
            store = json.loads(LEDGERS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            store = {}
        if isinstance(store, dict):
            for email, book in store.items():
                key = str(email or "").strip().lower()
                if not key or not isinstance(book, dict):
                    continue
                pending = book.get("pending_reserve")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO accounts(email, cash_usd, reserve_usd, pending_json, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        float(book.get("cash_usd") or 0),
                        float(book.get("reserve_usd") or 0),
                        json.dumps(pending, ensure_ascii=False) if pending else None,
                        now_iso(),
                    ),
                )
                positions = book.get("positions") or {}
                if isinstance(positions, dict):
                    for ticker, shares in positions.items():
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO positions(email, ticker, shares)
                            VALUES(?, ?, ?)
                            """,
                            (key, str(ticker).upper(), int(shares or 0)),
                        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_json', '1')"
    )


def _ensure_turn_gate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_gate (
          email TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          trades_left INTEGER NOT NULL
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(turn_gate)")}
    if "phrase" not in cols:
        conn.execute("ALTER TABLE turn_gate ADD COLUMN phrase TEXT NOT NULL DEFAULT ''")


def open_turn(email: str, kind: str, phrase: str = "") -> None:
    left = 1 if kind in {"buy", "sell"} else 0
    with db() as conn:
        conn.execute(
            """
            INSERT INTO turn_gate(email, kind, trades_left, phrase) VALUES(?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              kind=excluded.kind,
              trades_left=excluded.trades_left,
              phrase=excluded.phrase
            """,
            (email.strip().lower(), kind, left, phrase or ""),
        )


def turn_phrase(email: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT phrase FROM turn_gate WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()
    if not row:
        return ""
    return str(row["phrase"] or "")


def turn_block(email: str, tool: str) -> str | None:
    need = "buy" if tool == "execute_buy" else "sell"
    with db() as conn:
        row = conn.execute(
            "SELECT kind, trades_left FROM turn_gate WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()
    if not row or row["kind"] != need:
        return "Реплика не про эту операцию. Книга не менялась."
    if int(row["trades_left"]) <= 0:
        return "В этой реплике уже есть проводка."
    return None


def claim_trade(email: str) -> bool:
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE turn_gate
            SET trades_left = trades_left - 1
            WHERE email=? AND trades_left > 0
            """,
            (email.strip().lower(),),
        )
        return cur.rowcount == 1


_DEMO_EMAILS = ("client@demo", "katya@demo")


def _drop_demo_accounts(conn: sqlite3.Connection) -> None:
    """Учебные входы из README больше не живут в базе. Чужие учётки не трогает."""
    keys = list(_DEMO_EMAILS)
    row = conn.execute("SELECT value FROM meta WHERE key='jwt_secret'").fetchone()
    secret = str(row["value"]).strip() if row else ""
    if len(secret) >= 16:
        import action_hash

        keys.extend(action_hash.actor_hash(email, secret=secret) for email in _DEMO_EMAILS)
    existing = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("users", "accounts", "positions", "mailboxes", "messages", "trades", "turn_gate"):
        if table not in existing:
            continue
        cols = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        if "email" not in cols:
            continue
        for key in keys:
            conn.execute(f"DELETE FROM {table} WHERE email=?", (key,))


def _ensure_revoked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS revoked_tokens (
          jti TEXT PRIMARY KEY,
          exp INTEGER NOT NULL
        )
        """
    )


def revoke_token(jti: str, exp: int) -> None:
    if not jti:
        return
    with db() as conn:
        conn.execute("DELETE FROM revoked_tokens WHERE exp < ?", (int(time.time()),))
        conn.execute(
            "INSERT OR REPLACE INTO revoked_tokens(jti, exp) VALUES(?, ?)",
            (jti, int(exp)),
        )


def token_revoked(jti: str) -> bool:
    if not jti:
        return True
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM revoked_tokens WHERE jti=?",
            (jti,),
        ).fetchone()
    return row is not None


def _backfill_mailboxes(conn: sqlite3.Connection) -> None:
    """Старый список в strazh.db. Регистрация и вход его не читают."""
    conn.execute(
        """
        INSERT OR IGNORE INTO mailboxes(email, created_at)
        SELECT email, created_at FROM users
        """
    )
