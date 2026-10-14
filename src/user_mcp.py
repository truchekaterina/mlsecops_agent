"""Строки «Мои MCP» в strazh.db. Чужой логин эти строки не видит.

Кто вызывает: web_app (кнопка в чате) и agent_mode_a.build_agent (какие тулы отдать модели).
Кого вызывает: store и mcp_url_guard.
Почему отдельно от mcp_strazh: здесь только адрес сервера человека, не сделки.
"""

from __future__ import annotations

import re

import store
from mcp_url_guard import assert_safe_mcp_url

_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 _.-]{1,40}$")
_TRANSPORTS = {"sse", "streamable_http"}


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mcp (
          email TEXT NOT NULL,
          name TEXT NOT NULL,
          url TEXT NOT NULL,
          transport TEXT NOT NULL DEFAULT 'streamable_http',
          status TEXT NOT NULL DEFAULT 'allowed',
          created_at TEXT NOT NULL,
          tools_hash TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (email, name)
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(user_mcp)")}
    if "tools_hash" not in cols:
        conn.execute(
            "ALTER TABLE user_mcp ADD COLUMN tools_hash TEXT NOT NULL DEFAULT ''"
        )


def list_mcp(email: str) -> list[dict]:
    key = (email or "").strip().lower()
    with store.db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT name, url, transport, status, created_at, tools_hash
            FROM user_mcp
            WHERE email=?
            ORDER BY created_at
            """,
            (key,),
        ).fetchall()
    return [
        {
            "name": str(r["name"]),
            "url": str(r["url"]),
            "transport": str(r["transport"] or "streamable_http"),
            "status": str(r["status"]),
            "created_at": str(r["created_at"]),
            "tools_hash": str(r["tools_hash"] or ""),
        }
        for r in rows
    ]


def list_allowed(email: str) -> list[dict]:
    return [row for row in list_mcp(email) if row.get("status") == "allowed"]


def has_allowed(email: str) -> bool:
    return bool(list_allowed(email))


def has_review(email: str) -> bool:
    return any(row.get("status") == "review" for row in list_mcp(email))


def review_names(email: str) -> list[str]:
    return [str(row["name"]) for row in list_mcp(email) if row.get("status") == "review"]


def get_mcp(email: str, name: str) -> dict | None:
    key = (email or "").strip().lower()
    label = (name or "").strip()
    for row in list_mcp(key):
        if row.get("name") == label:
            return row
    return None


def add_mcp(
    email: str,
    name: str,
    url: str,
    transport: str = "streamable_http",
    tools_hash: str = "",
) -> dict:
    key = (email or "").strip().lower()
    label = (name or "").strip()
    if not _NAME_RE.match(label):
        raise ValueError("Имя MCP: 1–40 знаков, буквы, цифры, пробел, _ . -")
    safe_url = assert_safe_mcp_url(url)
    kind = (transport or "streamable_http").strip().lower()
    if kind in {"http", "streamable-http"}:
        kind = "streamable_http"
    if kind not in _TRANSPORTS:
        raise ValueError("transport: sse или streamable_http")
    digest = (tools_hash or "").strip().lower()
    if len(digest) != 64:
        raise ValueError("Не удалось снять отпечаток команд MCP.")
    with store.db() as conn:
        _ensure_table(conn)
        exists = conn.execute(
            "SELECT 1 FROM user_mcp WHERE email=? AND name=?",
            (key, label),
        ).fetchone()
        if exists:
            raise ValueError("MCP с таким именем уже есть. Удалите или выберите другое имя.")
        created = store.now_iso()
        conn.execute(
            """
            INSERT INTO user_mcp(email, name, url, transport, status, created_at, tools_hash)
            VALUES(?, ?, ?, ?, 'allowed', ?, ?)
            """,
            (key, label, safe_url, kind, created, digest),
        )
    return {
        "name": label,
        "url": safe_url,
        "transport": kind,
        "status": "allowed",
        "created_at": created,
        "tools_hash": digest,
    }


def mark_review(email: str, name: str) -> None:
    """Команды изменились. Старый отпечаток остаётся, статус ждёт человека."""
    key = (email or "").strip().lower()
    label = (name or "").strip()
    with store.db() as conn:
        _ensure_table(conn)
        conn.execute(
            """
            UPDATE user_mcp
            SET status='review'
            WHERE email=? AND name=? AND status='allowed'
            """,
            (key, label),
        )


def accept_tools(email: str, name: str, tools_hash: str) -> dict:
    """Человек подтвердил текущий список команд. После этого модель снова их видит."""
    key = (email or "").strip().lower()
    label = (name or "").strip()
    digest = (tools_hash or "").strip().lower()
    if len(digest) != 64:
        raise ValueError("Не удалось снять отпечаток команд MCP.")
    with store.db() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            """
            UPDATE user_mcp
            SET status='allowed', tools_hash=?
            WHERE email=? AND name=?
            """,
            (digest, key, label),
        )
        if cur.rowcount != 1:
            raise ValueError("Такого MCP нет.")
    row = get_mcp(key, label)
    if not row:
        raise ValueError("Такого MCP нет.")
    return row


async def refresh_consent(email: str) -> list[str]:
    """Сверить отпечаток разрешённых MCP. Изменившиеся уходят в review."""
    from mcp_tools_probe import tool_fingerprint

    changed: list[str] = []
    for row in list_allowed(email):
        try:
            digest = await tool_fingerprint(
                str(row.get("url") or ""),
                str(row.get("transport") or "streamable_http"),
                timeout=5,
            )
        except Exception:
            continue
        if digest == str(row.get("tools_hash") or ""):
            continue
        mark_review(email, str(row.get("name") or ""))
        changed.append(str(row.get("name") or ""))
    return changed


def remove_mcp(email: str, name: str) -> None:
    key = (email or "").strip().lower()
    label = (name or "").strip()
    with store.db() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "DELETE FROM user_mcp WHERE email=? AND name=?",
            (key, label),
        )
        if cur.rowcount != 1:
            raise ValueError("Такого MCP нет.")
