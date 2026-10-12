"""Кастомные MCP пользователя: только строки с его email."""

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
          PRIMARY KEY (email, name)
        )
        """
    )


def list_mcp(email: str) -> list[dict]:
    key = (email or "").strip().lower()
    with store.db() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT name, url, transport, status, created_at
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
        }
        for r in rows
    ]


def list_allowed(email: str) -> list[dict]:
    return [row for row in list_mcp(email) if row.get("status") == "allowed"]


def has_allowed(email: str) -> bool:
    return bool(list_allowed(email))


def get_mcp(email: str, name: str) -> dict | None:
    key = (email or "").strip().lower()
    label = (name or "").strip()
    for row in list_mcp(key):
        if row.get("name") == label:
            return row
    return None


def add_mcp(email: str, name: str, url: str, transport: str = "streamable_http") -> dict:
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
            INSERT INTO user_mcp(email, name, url, transport, status, created_at)
            VALUES(?, ?, ?, ?, 'allowed', ?)
            """,
            (key, label, safe_url, kind, created),
        )
    return {
        "name": label,
        "url": safe_url,
        "transport": kind,
        "status": "allowed",
        "created_at": created,
    }


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
