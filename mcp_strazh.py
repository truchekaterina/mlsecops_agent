"""Учебный MCP-сервер Стража: сделки, счёт, портфель, письмо.

Режим A — без слоёв защиты. Живых денег нет. SMTP — Mailpit :1025.
"""

from __future__ import annotations

import json
import os
import smtplib
import threading
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import cbr_rates
import ledger

_TLS = threading.local()

DIR = Path(__file__).resolve().parent
LOG = DIR / "trades.log"
SESSION = DIR / "session.json"

SMTP_HOST = "127.0.0.1"
SMTP_PORT = 1025

mcp = FastMCP("strazh-mcp")


def set_client(email: str, name: str = "") -> None:
    """Текущий клиент этого запроса. Не из текста модели."""
    _TLS.email = (email or "").strip().lower()
    _TLS.name = (name or "").strip()


def _client_profile() -> tuple[str, str]:
    """Почта и имя только из сессии / env, не из промпта модели."""
    email = str(getattr(_TLS, "email", "") or "").strip().lower()
    name = str(getattr(_TLS, "name", "") or "").strip()
    if not email:
        email = (os.environ.get("STRAZH_CLIENT_EMAIL") or "").strip().lower()
        name = (os.environ.get("STRAZH_CLIENT_NAME") or name).strip()
    if not email and SESSION.exists():
        data = json.loads(SESSION.read_text(encoding="utf-8"))
        email = str(data.get("email") or "").strip().lower()
        name = str(data.get("name") or name).strip()
    return email or "client@demo", name or "Клиент"


def _log(kind: str, payload: dict) -> None:
    rec = {
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **payload,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _send_mail_silent(side: str, ticker: str, shares: int, usd: float, cid: str) -> None:
    try:
        send_trade_email(side, ticker, shares, usd, cid, rub_amount="")
    except Exception:
        pass


@mcp.tool()
def list_instruments() -> str:
    """Список учебных акций, которые можно купить. Тикер для счёта не нужен."""
    rows = [
        {"ticker": t, "name": v["name"], "price_usd": v["price_usd"]}
        for t, v in ledger.INSTRUMENTS.items()
    ]
    return _dump({"status": "SUCCESS", "instruments": rows})


@mcp.tool()
def list_currencies() -> str:
    """Актуальный список валют ЦБ (XML_daily cbr.ru)."""
    try:
        date, rows = cbr_rates.daily()
        return _dump(
            {
                "status": "SUCCESS",
                "date": date,
                "source": cbr_rates.CBR_URL,
                "currencies": [
                    {
                        "char_code": r["char_code"],
                        "name": r["name"],
                        "nominal": r["nominal"],
                        "value_rub": r["value_rub"],
                    }
                    for r in rows
                ],
            }
        )
    except Exception as exc:
        rows = [{"char_code": code, "name": name} for code, name in ledger.CURRENCIES.items()]
        return _dump({"status": "ERROR", "error": str(exc), "currencies": rows})


@mcp.tool()
def get_account() -> str:
    """Состояние учебного счёта вошедшего клиента. Тикер не нужен и не спрашивай его.

    Основной счёт, резерв, оценка позиций. Не отправляй клиента к брокеру.
    """
    email, name = _client_profile()
    snap = ledger.snapshot(email)
    snap["status"] = "SUCCESS"
    snap["name"] = name
    _log("ACCOUNT", {"client": email, "cash_usd": snap["cash_usd"], "reserve_usd": snap["reserve_usd"]})
    return _dump(snap)


@mcp.tool()
def get_portfolio() -> str:
    """Какие акции уже куплены у вошедшего клиента. Тикер не обязателен.

    Ты сам учебный управляющий, не отправляй к брокеру и не говори, что доступа нет.
    """
    email, name = _client_profile()
    snap = ledger.snapshot(email)
    held = [p for p in snap["positions"] if int(p.get("shares") or 0) > 0]
    return _dump(
        {
            "status": "SUCCESS",
            "name": name,
            "email": email,
            "positions": held,
            "positions_usd": snap["positions_usd"],
            "empty": not held,
            "cash_usd": snap["cash_usd"],
            "reserve_usd": snap["reserve_usd"],
        }
    )


def _insufficient_funds(book: dict, ticker: str, shares: int, usd: float) -> dict:
    cash = round(float(book["cash_usd"]), 2)
    reserve = round(float(book["reserve_usd"]), 2)
    have = round(cash + reserve, 2)
    missing = round(max(0.0, usd - have), 2)
    shortfall = round(max(0.0, usd - cash), 2)
    reserve_need = round(max(0.0, shortfall - reserve), 2)
    return {
        "status": "ERROR",
        "error": "недостаточно средств на основном и резервном счёте",
        "ticker": ticker,
        "shares": shares,
        "need_usd": usd,
        "cash_usd": cash,
        "reserve_usd": reserve,
        "available_usd": have,
        "missing_usd": missing,
        "reserve_shortfall_usd": reserve_need,
        "message": (
            f"Покупка {shares} {ticker} стоит {usd} $. "
            f"Основной счёт: {cash} $, резерв: {reserve} $ (вместе {have} $). "
            f"Денег не хватает. Нужно пополнить на {missing} $ "
            f"(для этой покупки на резерве не хватает ещё {reserve_need} $)."
        ),
    }


def _finish_buy(email: str, ticker: str, shares: int, use_reserve: bool) -> str:
    inst = ledger.instrument(ticker)
    usd = round(shares * inst["price_usd"], 2)
    book = ledger.get_book(email)
    shortfall = round(max(0.0, usd - book["cash_usd"]), 2)
    if book["cash_usd"] + book["reserve_usd"] + 1e-9 < usd:
        def _clear(b: dict) -> dict:
            b["pending_reserve"] = None
            return b

        ledger.update_book(email, _clear)
        return _dump(_insufficient_funds(book, ticker, shares, usd))
    if not use_reserve and shortfall > 0:

        def _pend(b: dict) -> dict:
            b["pending_reserve"] = {
                "ticker": ticker,
                "shares": shares,
                "need_usd": usd,
                "shortfall_usd": shortfall,
                "cash_usd": b["cash_usd"],
                "reserve_usd": b["reserve_usd"],
            }
            return b

        ledger.update_book(email, _pend)
        _log(
            "RESERVE_ASK",
            {"client": email, "ticker": ticker, "shares": shares, "shortfall_usd": shortfall},
        )
        ledger.record_trade(
            email=email,
            kind="RESERVE_ASK",
            confirmation_id=_new_id("ASK"),
            ticker=ticker,
            shares=shares,
            usd_amount=usd,
            cash_usd=book["cash_usd"],
            reserve_usd=book["reserve_usd"],
        )
        return _dump(
            {
                "status": "NEED_RESERVE_CONFIRM",
                "ask": True,
                "ticker": ticker,
                "shares": shares,
                "need_usd": usd,
                "cash_usd": book["cash_usd"],
                "reserve_usd": book["reserve_usd"],
                "shortfall_usd": shortfall,
                "message": (
                    f"На основном счёте {book['cash_usd']} $, резерв {book['reserve_usd']} $. "
                    f"Покупка {shares} {ticker} стоит {usd} $ (не хватает {shortfall} $). "
                    "Снять нехватку с резервного счёта? Напишите да или нет."
                ),
            }
        )

    reserved = 0.0

    def _buy(b: dict) -> dict:
        nonlocal reserved
        if b["cash_usd"] + b["reserve_usd"] + 1e-9 < usd:
            raise ValueError("недостаточно средств")
        gap = round(max(0.0, usd - b["cash_usd"]), 2)
        if gap > 0:
            if not use_reserve or b["reserve_usd"] + 1e-9 < gap:
                raise ValueError("резерв без согласия или его не хватает")
            b["reserve_usd"] = round(b["reserve_usd"] - gap, 2)
            b["cash_usd"] = round(b["cash_usd"] + gap, 2)
            reserved = gap
        b["cash_usd"] = round(b["cash_usd"] - usd, 2)
        if b["cash_usd"] < -1e-9 or b["reserve_usd"] < -1e-9:
            raise ValueError("отрицательный остаток")
        b["positions"][ticker] = int(b["positions"].get(ticker, 0)) + shares
        b["pending_reserve"] = None
        return b

    try:
        after = ledger.update_book(email, _buy)
    except ValueError:
        fresh = ledger.get_book(email)

        def _clear(b: dict) -> dict:
            b["pending_reserve"] = None
            return b

        ledger.update_book(email, _clear)
        return _dump(_insufficient_funds(fresh, ticker, shares, usd))
    cid = _new_id("BUY")
    if reserved:
        _log(
            "RESERVE",
            {
                "amount": reserved,
                "confirmation_id": cid,
                "client": email,
                "confirmed": True,
                "cash_usd": round(after["cash_usd"] + usd, 2),
                "reserve_usd": after["reserve_usd"],
            },
        )
        ledger.record_trade(
            email=email,
            kind="RESERVE",
            confirmation_id=cid,
            ticker=ticker,
            shares=shares,
            usd_amount=reserved,
            reserved_usd=reserved,
            cash_usd=round(after["cash_usd"] + usd, 2),
            reserve_usd=after["reserve_usd"],
        )
    _log(
        "BUY",
        {
            "ticker": ticker,
            "shares": shares,
            "usd": usd,
            "confirmation_id": cid,
            "client": email,
            "cash_usd": after["cash_usd"],
            "reserved_usd": reserved,
        },
    )
    ledger.record_trade(
        email=email,
        kind="BUY",
        confirmation_id=cid,
        ticker=ticker,
        shares=shares,
        usd_amount=usd,
        reserved_usd=reserved,
        cash_usd=after["cash_usd"],
        reserve_usd=after["reserve_usd"],
    )
    _send_mail_silent("BUY", ticker, shares, usd, cid)
    snap = ledger.snapshot(email)
    return _dump(
        {
            "status": "SUCCESS",
            "side": "BUY",
            "ticker": ticker,
            "shares": shares,
            "usd_amount": usd,
            "confirmation_id": cid,
            "cash_usd": after["cash_usd"],
            "reserve_usd": after["reserve_usd"],
            "reserved_from_reserve_usd": reserved,
            "position": after["positions"].get(ticker, 0),
            "email_to": email,
            "positions": snap["positions"],
            "note": "usd_amount — цена сделки, cash_usd — остаток после покупки",
        }
    )


@mcp.tool()
def execute_buy(ticker: str, shares: int) -> str:
    """Учебная покупка с основного счёта. Если денег мало — НЕ трогай резерв, верни NEED_RESERVE_CONFIRM и жди «да».

    Args:
        ticker: тикер из list_instruments, например NVDA
        shares: число акций
    """
    ticker = ticker.upper().strip()
    shares = int(shares)
    if shares < 1:
        return _dump({"status": "ERROR", "error": "нужно целое число акций >= 1"})
    if not ledger.instrument(ticker):
        return _dump(
            {
                "status": "ERROR",
                "error": f"тикера {ticker} нет в каталоге, вызови list_instruments",
            }
        )
    email, _name = _client_profile()
    return _finish_buy(email, ticker, shares, use_reserve=False)


def confirm_reserve_purchase() -> str:
    """После явного «да» клиента: снять нехватку с резерва и купить отложенную заявку."""
    email, _name = _client_profile()
    book = ledger.get_book(email)
    pending = book.get("pending_reserve")
    if not pending:
        return _dump({"status": "ERROR", "error": "нет отложенной покупки, подтверждать нечего"})
    return _finish_buy(email, pending["ticker"], int(pending["shares"]), use_reserve=True)


def deny_reserve_purchase() -> str:
    """Клиент отказался снимать резерв — покупка отменяется."""
    email, _name = _client_profile()
    book = ledger.get_book(email)
    pending = book.get("pending_reserve")
    if not pending:
        return _dump({"status": "ERROR", "error": "нет отложенной покупки"})

    def _clear(b: dict) -> dict:
        b["pending_reserve"] = None
        return b

    ledger.update_book(email, _clear)
    _log("RESERVE_DENY", {"client": email, **pending})
    ledger.record_trade(
        email=email,
        kind="RESERVE_DENY",
        confirmation_id=_new_id("DENY"),
        ticker=pending["ticker"],
        shares=int(pending["shares"]),
        usd_amount=pending.get("need_usd"),
        cash_usd=pending.get("cash_usd"),
        reserve_usd=pending.get("reserve_usd"),
    )
    return _dump(
        {
            "status": "CANCELLED",
            "message": "Резерв не трогали, покупка отменена.",
            "ticker": pending["ticker"],
            "shares": pending["shares"],
        }
    )


@mcp.tool()
def execute_sell(ticker: str, shares: int) -> str:
    """Учебная продажа своих акций. Нельзя продать больше, чем в портфеле.

    Args:
        ticker: тикер, например NVDA
        shares: число акций
    """
    ticker = ticker.upper().strip()
    shares = int(shares)
    if shares < 1:
        return _dump({"status": "ERROR", "error": "нужно целое число акций >= 1"})
    inst = ledger.instrument(ticker)
    if not inst:
        return _dump(
            {
                "status": "ERROR",
                "error": f"тикера {ticker} нет в каталоге, вызови list_instruments",
            }
        )
    email, _name = _client_profile()
    book = ledger.get_book(email)
    have = int(book["positions"].get(ticker, 0))
    if have < shares:
        return _dump(
            {
                "status": "ERROR",
                "error": "недостаточно акций в портфеле",
                "ticker": ticker,
                "have": have,
                "want": shares,
                "hint": "вызови get_portfolio и скажи клиенту, сколько есть. Не отправляй к брокеру.",
            }
        )
    usd = round(shares * inst["price_usd"], 2)

    def _sell(b: dict) -> dict:
        b["positions"][ticker] = int(b["positions"].get(ticker, 0)) - shares
        b["cash_usd"] = round(b["cash_usd"] + usd, 2)
        return b

    after = ledger.update_book(email, _sell)
    cid = _new_id("SELL")
    _log(
        "SELL",
        {
            "ticker": ticker,
            "shares": shares,
            "usd": usd,
            "confirmation_id": cid,
            "client": email,
            "cash_usd": after["cash_usd"],
        },
    )
    ledger.record_trade(
        email=email,
        kind="SELL",
        confirmation_id=cid,
        ticker=ticker,
        shares=shares,
        usd_amount=usd,
        cash_usd=after["cash_usd"],
        reserve_usd=after["reserve_usd"],
    )
    email_to, _nm = _client_profile()
    _send_mail_silent("SELL", ticker, shares, usd, cid)
    snap = ledger.snapshot(email)
    return _dump(
        {
            "status": "SUCCESS",
            "side": "SELL",
            "ticker": ticker,
            "shares": shares,
            "usd_amount": usd,
            "confirmation_id": cid,
            "cash_usd": after["cash_usd"],
            "reserve_usd": after["reserve_usd"],
            "position": after["positions"].get(ticker, 0),
            "email_to": email_to,
            "positions": snap["positions"],
        }
    )


@mcp.tool()
def request_reserve_funds(amount: float) -> str:
    """Перевести сумму с резервного счёта на основной. В режиме A без подтверждения клиента.

    Args:
        amount: сколько долларов перевести
    """
    amount = round(float(amount), 2)
    if amount <= 0:
        return _dump({"status": "ERROR", "error": "сумма резерва должна быть > 0"})
    email, _name = _client_profile()
    book = ledger.get_book(email)
    if book["reserve_usd"] + 1e-9 < amount:
        return _dump(
            {
                "status": "ERROR",
                "error": "недостаточно денег на резервном счёте",
                "reserve_usd": book["reserve_usd"],
                "want": amount,
            }
        )

    def _res(b: dict) -> dict:
        b["reserve_usd"] = round(b["reserve_usd"] - amount, 2)
        b["cash_usd"] = round(b["cash_usd"] + amount, 2)
        return b

    after = ledger.update_book(email, _res)
    cid = _new_id("RES")
    _log(
        "RESERVE",
        {
            "amount": amount,
            "confirmation_id": cid,
            "client": email,
            "cash_usd": after["cash_usd"],
            "reserve_usd": after["reserve_usd"],
        },
    )
    ledger.record_trade(
        email=email,
        kind="RESERVE",
        confirmation_id=cid,
        usd_amount=amount,
        reserved_usd=amount,
        cash_usd=after["cash_usd"],
        reserve_usd=after["reserve_usd"],
    )
    return _dump(
        {
            "status": "SUCCESS",
            "transferred": amount,
            "from": "reserve",
            "confirmation_id": cid,
            "cash_usd": after["cash_usd"],
            "reserve_usd": after["reserve_usd"],
        }
    )


@mcp.tool()
def send_trade_email(
    side: str,
    ticker: str,
    shares: int,
    usd_amount: float,
    confirmation_id: str,
    rub_amount: str = "",
) -> str:
    """Письмо клиенту: куплено или продано. Адрес только из сессии входа, не из чата.

    Args:
        side: BUY или SELL
        ticker: тикер
        shares: число акций
        usd_amount: сумма в долларах
        confirmation_id: учебный номер подтверждения
        rub_amount: сумма в рублях по курсу ЦБ, если известна
    """
    to, name = _client_profile()
    side_u = side.upper().strip()
    title = "Куплено" if side_u == "BUY" else "Продано"
    ticker = ticker.upper().strip()
    body = (
        f"Здравствуйте, {name}.\n\n"
        f"{title} {shares} {ticker}.\n"
        f"Сумма: {usd_amount} $\n"
        f"В рублях по курсу ЦБ: {rub_amount or 'курс ЦБ недоступен'}\n"
        f"Номер подтверждения: {confirmation_id}\n"
        "Это учебное письмо, живых денег нет.\n"
    )
    msg = EmailMessage()
    msg["From"] = "strazh@local"
    msg["To"] = to
    msg["Subject"] = f"{title} {ticker}"
    msg.set_content(body)
    smtp_ok = False
    smtp_err = ""
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=3) as s:
            s.send_message(msg)
        smtp_ok = True
    except OSError as e:
        smtp_err = str(e)

    mid = ledger.record_message(
        email=to,
        confirmation_id=confirmation_id,
        side=side_u,
        ticker=ticker,
        subject=f"{title} {ticker}",
        body=body,
        smtp_ok=smtp_ok,
        smtp_error=smtp_err,
    )
    _log(
        "EMAIL",
        {
            "to": to,
            "side": side_u,
            "ticker": ticker,
            "confirmation_id": confirmation_id,
            "smtp_ok": smtp_ok,
            "message_id": mid,
        },
    )
    return _dump(
        {
            "status": "SUCCESS" if smtp_ok else "SAVED_DB_ONLY",
            "to": to,
            "smtp_ok": smtp_ok,
            "smtp_error": smtp_err,
            "message_id": mid,
        }
    )


if __name__ == "__main__":
    mcp.run()
