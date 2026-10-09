"""База почтового сервиса. Отдельный файл, не strazh.db."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import password_hash
from paths import DATA

DB_PATH = DATA / "mail.db"

_init_lock = threading.Lock()
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (
  email TEXT PRIMARY KEY,
  password_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  confirmation_id TEXT,
  side TEXT,
  ticker TEXT,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_messages_email ON messages(email, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return password_hash.hash_password(password)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
            _seed(conn)
            conn.commit()
        finally:
            conn.close()
        _ready = True


def _seed(conn: sqlite3.Connection) -> None:
    created = now_iso()
    for email, password in (("client@demo", "demo"), ("katya@demo", "katya")):
        conn.execute(
            """
            INSERT OR IGNORE INTO mailboxes(email, password_sha256, created_at)
            VALUES(?, ?, ?)
            """,
            (email, hash_password(password), created),
        )


def mailbox_exists(email: str) -> bool:
    key = email.strip().lower()
    if not key:
        return False
    with db() as conn:
        row = conn.execute("SELECT 1 FROM mailboxes WHERE email=?", (key,)).fetchone()
    return row is not None


def find_mailbox(email: str) -> dict | None:
    key = email.strip().lower()
    with db() as conn:
        row = conn.execute(
            "SELECT email, password_sha256, created_at FROM mailboxes WHERE email=?",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def create_mailbox(email: str, password: str) -> dict:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(email) < 3:
        raise ValueError("Нужна почта с @.")
    password_hash.validate_password(password)
    if mailbox_exists(email):
        raise ValueError("Такая почта уже заведена. Войдите в ящик.")
    created = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO mailboxes(email, password_sha256, created_at)
            VALUES(?, ?, ?)
            """,
            (email, hash_password(password), created),
        )
    return {"email": email, "created_at": created}


def deliver(
    *,
    email: str,
    subject: str,
    body: str,
    confirmation_id: str = "",
    side: str = "",
    ticker: str = "",
) -> dict:
    key = email.strip().lower()
    if not mailbox_exists(key):
        raise ValueError("Такого ящика нет.")
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages(
              email, confirmation_id, side, ticker, subject, body, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (key, confirmation_id or None, side or None, ticker or None, subject, body, now_iso()),
        )
        mid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return dict(row)


def list_messages(email: str, limit: int = 30) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE email=? ORDER BY id DESC LIMIT ?",
            (email.strip().lower(), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message(email: str, message_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND email=?",
            (int(message_id), email.strip().lower()),
        ).fetchone()
    return dict(row) if row else None
