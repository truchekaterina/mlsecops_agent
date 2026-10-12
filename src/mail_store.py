"""База почтового сервиса. Отдельный файл, не strazh.db. Тоже закрыт ключом."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import action_hash
import db_cipher
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


def connect() -> db_cipher.Connection:
    conn = db_cipher.connect(DB_PATH)
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
        db_cipher.restrict(DB_PATH)


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
            db_cipher.restrict(DB_PATH)
            conn.executescript(SCHEMA)
            _ensure_action_hash(conn)
            _drop_demo(conn)
            conn.commit()
        finally:
            conn.close()
            db_cipher.restrict(DB_PATH)
        _ready = True


def _ensure_action_hash(conn: db_cipher.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "action_hash" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN action_hash TEXT")


_DEMO_EMAILS = ("client@demo", "katya@demo")


def _drop_demo(conn: db_cipher.Connection) -> None:
    keys = list(_DEMO_EMAILS)
    try:
        secret = action_hash.mail_secret()
    except RuntimeError:
        secret = ""
    if secret:
        keys.extend(action_hash.actor_hash(email, secret=secret) for email in _DEMO_EMAILS)
    for key in keys:
        conn.execute("DELETE FROM messages WHERE email=?", (key,))
        conn.execute("DELETE FROM mailboxes WHERE email=?", (key,))


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
    owner = email.strip().lower()
    stored_email = action_hash.actor_hash(owner, secret=action_hash.mail_secret())
    digest = action_hash.action_digest(
        {
            "email": stored_email,
            "confirmation_id": confirmation_id,
            "side": side,
            "ticker": ticker,
            "subject": subject,
        },
        secret=action_hash.mail_secret(),
    )
    if not mailbox_exists(owner):
        raise ValueError("Такого ящика нет.")
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages(
              email, confirmation_id, side, ticker, subject, body, created_at, action_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_email,
                confirmation_id or None,
                side or None,
                ticker or None,
                subject,
                body,
                now_iso(),
                digest,
            ),
        )
        mid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    saved = dict(row)
    saved["email"] = owner
    return saved


def _mail_keys(email: str) -> tuple[str, str]:
    owner = email.strip().lower()
    return action_hash.actor_hash(owner, secret=action_hash.mail_secret()), owner


def list_messages(email: str, limit: int = 30) -> list[dict]:
    hashed, owner = _mail_keys(email)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE email=? OR email=? ORDER BY id DESC LIMIT ?",
            (hashed, owner, int(limit)),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["email"] = owner
        out.append(item)
    return out


def get_message(email: str, message_id: int) -> dict | None:
    hashed, owner = _mail_keys(email)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND (email=? OR email=?)",
            (int(message_id), hashed, owner),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["email"] = owner
    return item
