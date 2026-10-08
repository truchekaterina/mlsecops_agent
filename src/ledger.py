"""Учебный кошелёк и каталог бумаг. Живой биржи нет. Правда — SQLite."""

from __future__ import annotations

import json
import threading
from typing import Callable

import store

DEFAULT_CASH = 2000.0
DEFAULT_RESERVE = 5000.0

INSTRUMENTS = {
    "NVDA": {"name": "NVIDIA", "price_usd": 915.75},
    "AAPL": {"name": "Apple", "price_usd": 189.50},
    "MSFT": {"name": "Microsoft", "price_usd": 415.00},
}

CURRENCIES = {
    "USD": "доллар США",
    "EUR": "евро",
    "CNY": "китайский юань",
}

_LOCK = threading.Lock()


def instrument(ticker: str) -> dict | None:
    return INSTRUMENTS.get(ticker.upper().strip())


def empty_book() -> dict:
    return {
        "cash_usd": DEFAULT_CASH,
        "reserve_usd": DEFAULT_RESERVE,
        "positions": {t: 0 for t in INSTRUMENTS},
        "pending_reserve": None,
    }


def _key(email: str) -> str:
    return (email or "").strip().lower()


def _normalize(book: dict) -> dict:
    out = empty_book()
    out["cash_usd"] = round(float(book.get("cash_usd", DEFAULT_CASH)), 2)
    out["reserve_usd"] = round(float(book.get("reserve_usd", DEFAULT_RESERVE)), 2)
    pos = dict(book.get("positions") or {})
    for t in INSTRUMENTS:
        out["positions"][t] = int(pos.get(t, 0) or 0)
    pending = book.get("pending_reserve")
    if isinstance(pending, dict) and pending.get("ticker") and int(pending.get("shares") or 0) > 0:
        out["pending_reserve"] = {
            "ticker": str(pending["ticker"]).upper(),
            "shares": int(pending["shares"]),
            "need_usd": round(float(pending.get("need_usd") or 0), 2),
            "shortfall_usd": round(float(pending.get("shortfall_usd") or 0), 2),
            "cash_usd": round(float(pending.get("cash_usd") or 0), 2),
            "reserve_usd": round(float(pending.get("reserve_usd") or 0), 2),
        }
    else:
        out["pending_reserve"] = None
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
    """Счёт нового пользователя: 2000 $ / 5000 $, пустой портфель."""
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
        inst = INSTRUMENTS[ticker]
        value = round(shares * inst["price_usd"], 2)
        pos_usd += value
        positions.append(
            {
                "ticker": ticker,
                "name": inst["name"],
                "shares": shares,
                "price_usd": inst["price_usd"],
                "value_usd": value,
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
    with store.db() as conn:
        conn.execute(
            """
            INSERT INTO trades(
              confirmation_id, email, kind, ticker, shares, usd_amount,
              reserved_usd, cash_usd, reserve_usd, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                _key(email),
                kind,
                ticker,
                shares,
                usd_amount,
                reserved_usd,
                cash_usd,
                reserve_usd,
                store.now_iso(),
            ),
        )


def last_trade(email: str, kinds: tuple[str, ...] | None = None) -> dict | None:
    key = _key(email)
    with store.db() as conn:
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
    if not row:
        return None
    return dict(row)


def trade_kinds(email: str) -> list[str]:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT kind FROM trades WHERE email=? ORDER BY id",
            (_key(email),),
        ).fetchall()
    return [str(r["kind"]) for r in rows]


def clear_history(email: str) -> None:
    key = _key(email)
    with store.db() as conn:
        conn.execute("DELETE FROM trades WHERE email=?", (key,))
        conn.execute("DELETE FROM messages WHERE email=?", (key,))


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
    with store.db() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages(
              confirmation_id, email, side, ticker, subject, body,
              smtp_ok, smtp_error, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                _key(email),
                side,
                ticker,
                subject,
                body,
                1 if smtp_ok else 0,
                smtp_error or None,
                store.now_iso(),
            ),
        )
        return int(cur.lastrowid)


def last_messages(email: str, limit: int = 10) -> list[dict]:
    with store.db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE email=? ORDER BY id DESC LIMIT ?",
            (_key(email), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message(email: str, message_id: int) -> dict | None:
    """Письмо только своего ящика. Чужой id не отдаём."""
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND email=?",
            (int(message_id), _key(email)),
        ).fetchone()
    if not row:
        return None
    return dict(row)
