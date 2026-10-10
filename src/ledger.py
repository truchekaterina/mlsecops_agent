"""Учебный кошелёк. Каталог — акции Мосбиржи, цена в рублях приходит с ISS. Учёт — SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Callable

import action_hash
import moex_quotes
import policy
import store

DEFAULT_CASH = 2000.0
DEFAULT_RESERVE = 5000.0

INSTRUMENTS = {ticker: {"name": name} for ticker, name in moex_quotes.CATALOG.items()}

CURRENCIES = {
    "USD": "доллар США",
    "EUR": "евро",
    "CNY": "китайский юань",
}

_LOCK = threading.Lock()


def instrument(ticker: str) -> dict | None:
    ticker = (ticker or "").upper().strip()
    meta = INSTRUMENTS.get(ticker)
    if not meta:
        return None
    row = moex_quotes.quote(ticker)
    price = None if not row else row.get("price")
    return {
        "name": meta["name"],
        "price_usd": price,
        "price_rub": price,
        "price_source": None if not row else row.get("source"),
        "price_note": None if not row else row.get("price_note"),
    }


def empty_book() -> dict:
    return {
        "cash_usd": DEFAULT_CASH,
        "reserve_usd": DEFAULT_RESERVE,
        "positions": {t: 0 for t in INSTRUMENTS},
        "pending_reserve": None,
    }


def _key(email: str) -> str:
    return (email or "").strip().lower()


def _actor(email: str) -> str:
    return action_hash.actor_hash(_key(email))


def _normalize(book: dict) -> dict:
    out = empty_book()
    out["cash_usd"] = round(float(book.get("cash_usd", DEFAULT_CASH)), 2)
    out["reserve_usd"] = round(float(book.get("reserve_usd", DEFAULT_RESERVE)), 2)
    pos = dict(book.get("positions") or {})
    for t in INSTRUMENTS:
        out["positions"][t] = int(pos.get(t, 0) or 0)
    pending = book.get("pending_reserve")
    if not isinstance(pending, dict) or not pending.get("id") or not pending.get("kind"):
        out["pending_reserve"] = None
        return out
    if not policy.ticket_alive(str(pending.get("expires_at") or "")):
        out["pending_reserve"] = None
        return out
    out["pending_reserve"] = dict(pending)
    return out


def _read_book(conn, email: str) -> dict:
    acc = conn.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
    if not acc:
        book = empty_book()
        _write_book(conn, email, book)
        return book
    pending = None
    if acc["pending_json"]:
        try:
            pending = json.loads(acc["pending_json"])
        except json.JSONDecodeError:
            pending = None
    positions = {t: 0 for t in INSTRUMENTS}
    for row in conn.execute("SELECT ticker, shares FROM positions WHERE email=?", (email,)):
        ticker = str(row["ticker"]).upper()
        if ticker in positions:
            positions[ticker] = int(row["shares"] or 0)
    return _normalize(
        {
            "cash_usd": acc["cash_usd"],
            "reserve_usd": acc["reserve_usd"],
            "positions": positions,
            "pending_reserve": pending,
        }
    )


def _write_book(conn, email: str, book: dict) -> None:
    pending = book.get("pending_reserve")
    conn.execute(
        """
        INSERT INTO accounts(email, cash_usd, reserve_usd, pending_json, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
          cash_usd=excluded.cash_usd,
          reserve_usd=excluded.reserve_usd,
          pending_json=excluded.pending_json,
          updated_at=excluded.updated_at
        """,
        (
            email,
            book["cash_usd"],
            book["reserve_usd"],
            json.dumps(pending, ensure_ascii=False) if pending else None,
            store.now_iso(),
        ),
    )
    for ticker, shares in book["positions"].items():
        conn.execute(
            """
            INSERT INTO positions(email, ticker, shares)
            VALUES(?, ?, ?)
            ON CONFLICT(email, ticker) DO UPDATE SET shares=excluded.shares
            """,
            (email, ticker, int(shares)),
        )


def ensure_account(email: str) -> dict:
    """Счёт нового пользователя: 2000 ₽ / 5000 ₽, пустой портфель."""
    return get_book(email)


def get_book(email: str) -> dict:
    key = _key(email)
    with _LOCK:
        with store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return json.loads(json.dumps(_read_book(conn, key)))


def reset_book(email: str) -> dict:
    key = _key(email)
    with _LOCK:
        with store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            book = empty_book()
            _write_book(conn, key, book)
            return json.loads(json.dumps(book))


def update_book(email: str, mutator: Callable[[dict], dict]) -> dict:
    key = _key(email)
    with _LOCK:
        with store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            book = _normalize(mutator(_read_book(conn, key)))
            _write_book(conn, key, book)
            return json.loads(json.dumps(book))


def snapshot(email: str) -> dict:
    book = get_book(email)
    positions = []
    pos_usd = 0.0
    for ticker, shares in book["positions"].items():
        if shares <= 0:
            continue
        inst = instrument(ticker) or {"name": ticker, "price_rub": None, "price_note": None}
        price = inst.get("price_rub")
        value = round(shares * float(price), 2) if price is not None else 0.0
        pos_usd += value
        positions.append(
            {
                "ticker": ticker,
                "name": inst["name"],
                "shares": shares,
                "price_usd": price,
                "price_rub": price,
                "value_usd": value,
                "price_note": inst.get("price_note"),
            }
        )
    return {
        "email": _key(email),
        "cash_usd": book["cash_usd"],
        "reserve_usd": book["reserve_usd"],
        "positions_usd": round(pos_usd, 2),
        "positions": positions,
        "pending_reserve": book.get("pending_reserve"),
    }


def record_trade(
    *,
    email: str,
    kind: str,
    confirmation_id: str,
    ticker: str | None = None,
    shares: int | None = None,
    usd_amount: float | None = None,
    reserved_usd: float | None = None,
    cash_usd: float | None = None,
    reserve_usd: float | None = None,
) -> None:
    owner = _key(email)
    stored_email = _actor(owner)
    digest = action_hash.action_digest(
        {
            "email": stored_email,
            "kind": kind,
            "confirmation_id": confirmation_id,
            "ticker": ticker,
            "shares": shares,
            "usd_amount": usd_amount,
            "reserved_usd": reserved_usd,
            "cash_usd": cash_usd,
            "reserve_usd": reserve_usd,
        }
    )
    with store.db() as conn:
        conn.execute(
            """
            INSERT INTO trades(
              confirmation_id, email, kind, ticker, shares, usd_amount,
              reserved_usd, cash_usd, reserve_usd, created_at, action_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                stored_email,
                kind,
                ticker,
                shares,
                usd_amount,
                reserved_usd,
                cash_usd,
                reserve_usd,
                store.now_iso(),
                digest,
            ),
        )


def _trade_row(conn, email: str, kinds: tuple[str, ...] | None) -> sqlite3.Row | None:
    for key in (_actor(email), _key(email)):
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            row = conn.execute(
                f"""
                SELECT * FROM trades
                WHERE email=? AND kind IN ({placeholders})
                ORDER BY id DESC LIMIT 1
                """,
                (key, *kinds),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM trades WHERE email=? ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
        if row:
            return row
    return None


def last_trade(email: str, kinds: tuple[str, ...] | None = None) -> dict | None:
    owner = _key(email)
    with store.db() as conn:
        row = _trade_row(conn, owner, kinds)
    if not row:
        return None
    data = dict(row)
    data["email"] = owner
    return data


def trade_kinds(email: str) -> list[str]:
    owner = _key(email)
    with store.db() as conn:
        rows = conn.execute(
            "SELECT kind FROM trades WHERE email=? OR email=? ORDER BY id",
            (_actor(owner), owner),
        ).fetchall()
    return [str(r["kind"]) for r in rows]


def clear_history(email: str) -> None:
    owner = _key(email)
    with store.db() as conn:
        conn.execute(
            "DELETE FROM trades WHERE email=? OR email=?",
            (_actor(owner), owner),
        )
        conn.execute(
            "DELETE FROM messages WHERE email=? OR email=?",
            (_actor(owner), owner),
        )


def record_message(
    *,
    email: str,
    confirmation_id: str,
    side: str,
    ticker: str,
    subject: str,
    body: str,
    smtp_ok: bool,
    smtp_error: str = "",
) -> int:
    owner = _key(email)
    stored_email = _actor(owner)
    digest = action_hash.action_digest(
        {
            "email": stored_email,
            "confirmation_id": confirmation_id,
            "side": side,
            "ticker": ticker,
            "subject": subject,
        }
    )
    with store.db() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages(
              confirmation_id, email, side, ticker, subject, body,
              smtp_ok, smtp_error, created_at, action_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                stored_email,
                side,
                ticker,
                subject,
                body,
                1 if smtp_ok else 0,
                smtp_error or None,
                store.now_iso(),
                digest,
            ),
        )
        return int(cur.lastrowid)


def last_messages(email: str, limit: int = 10) -> list[dict]:
    owner = _key(email)
    with store.db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE email=? OR email=? ORDER BY id DESC LIMIT ?",
            (_actor(owner), owner, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["email"] = owner
        out.append(item)
    return out


def get_message(email: str, message_id: int) -> dict | None:
    """Письмо только своего ящика. Чужой id не отдаём."""
    owner = _key(email)
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND (email=? OR email=?)",
            (int(message_id), _actor(owner), owner),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["email"] = owner
    return item
