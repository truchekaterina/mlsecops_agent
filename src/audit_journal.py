"""Журнал решений. Пишет обвязка, агент этот файл не читает и не правит.

Кто вызывает: web_app после входа, хода модели и кнопки.
Кого вызывает: db_cipher со своим ключом, не с ключом книги.
Почему отдельно от trades.log и strazh.db: цепочка хода лежит в другой базе,
только на дописывание. Модели и кастомному MCP ключа нет.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

import action_hash
import db_cipher
from paths import DATA

PATH = DATA / "audit" / "journal.db"
_WRITE = threading.Lock()
_TEXT_LIMIT = 800

_SECRET_KEY = re.compile(
    r"password|passwd|token|jwt|secret|authorization|cookie|api[_-]?key|bearer|credential",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SECRET_VALUE = re.compile(
    r"eyJ[A-Za-z0-9_\-]{8,}|\$argon2|STRAZH_[A-Z0-9_]+|Bearer\s+\S+",
    re.IGNORECASE,
)


def audit_secret() -> str:
    value = os.environ.get("STRAZH_AUDIT_SECRET", "").strip()
    if len(value) < 16:
        raise RuntimeError(
            "Задайте STRAZH_AUDIT_SECRET не короче 16 символов. "
            "Журнал не открывается ключом книги."
        )
    return value


def _mask(value, depth: int = 0):
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:40]:
            if _SECRET_KEY.search(str(key)):
                continue
            out[str(key)] = _mask(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_mask(item, depth + 1) for item in list(value)[:20]]
    text = _SECRET_VALUE.sub("[secret]", str(value))
    text = _EMAIL.sub("[email]", text)
    if len(text) > _TEXT_LIMIT:
        text = text[:_TEXT_LIMIT] + "…"
    return text


def _ensure(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trace (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          kind TEXT NOT NULL,
          actor TEXT NOT NULL,
          body TEXT NOT NULL,
          prev_hash TEXT NOT NULL,
          row_hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trace_no_update
        BEFORE UPDATE ON trace
        BEGIN
          SELECT RAISE(ABORT, 'append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trace_no_delete
        BEFORE DELETE ON trace
        BEGIN
          SELECT RAISE(ABORT, 'append-only');
        END
        """
    )


def record(email: str, kind: str, body: dict | None = None) -> None:
    """Дописать одну строку. Читателя у этой функции нет."""
    secret = audit_secret()
    masked = _mask(body or {})
    payload = json.dumps(masked, ensure_ascii=False, sort_keys=True, default=str)
    actor = action_hash.actor_hash(email)
    stamp = datetime.now(timezone.utc).isoformat()
    with _WRITE:
        conn = db_cipher.connect(PATH, secret=secret)
        try:
            _ensure(conn)
            prev_row = conn.execute(
                "SELECT row_hash FROM trace ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev = str(prev_row["row_hash"]) if prev_row else "0" * 64
            row_hash = action_hash.action_digest(
                {"ts": stamp, "kind": kind, "actor": actor, "body": payload, "prev": prev},
                secret=secret,
            )
            conn.execute(
                """
                INSERT INTO trace(ts, kind, actor, body, prev_hash, row_hash)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (stamp, kind, actor, payload, prev, row_hash),
            )
            conn.commit()
        finally:
            conn.close()


def record_agent_result(email: str, result: dict, reply: str) -> None:
    """Цепочка хода: вызов агента, тул с аргументами, ответ тула, реплика модели."""
    record(email, "invoke", {"step": "agent"})
    for msg in (result or {}).get("messages") or []:
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            for call in calls:
                if isinstance(call, dict):
                    name = str(call.get("name") or "")
                    args = call.get("args") or {}
                else:
                    name = str(getattr(call, "name", "") or "")
                    args = getattr(call, "args", None) or {}
                if name:
                    record(email, "tool", {"tool": name, "args": args})
            continue
        kind = str(getattr(msg, "type", "") or "")
        name = str(getattr(msg, "name", "") or "")
        if kind != "tool" and not name:
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        record(email, "tool_result", {"tool": name, "result": str(content)})
    record(email, "chat", {"reply": reply or ""})
