"""Учебный MCP-сервер Стража: сделки, счёт, портфель, письмо.

Режим A — без слоёв защиты. Живых денег нет. Письма — почтовый сервис :8090.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

import cbr_rates
import action_hash
import ledger
import mail_client
import policy
import store
from paths import DATA

_TLS = threading.local()

LOG = DATA / "trades.log"

mcp = FastMCP("strazh-mcp")


def set_client(email: str, name: str = "") -> None:
    """Текущий клиент этого запроса. Не из текста модели."""
    _TLS.email = (email or "").strip().lower()
    _TLS.name = (name or "").strip()


def _client_profile() -> tuple[str, str]:
    """Почта и имя из привязки запроса или из окружения процесса MCP."""
    email = str(getattr(_TLS, "email", "") or "").strip().lower()
    name = str(getattr(_TLS, "name", "") or "").strip()
    if not email:
        email = (os.environ.get("STRAZH_CLIENT_EMAIL") or "").strip().lower()
        name = (os.environ.get("STRAZH_CLIENT_NAME") or name).strip()
    if not email:
        return "", ""
    return email, name or "Клиент"


def _no_client(as_text: bool = True) -> str | dict:
    payload = {"status": "ERROR", "error": "Нет вошедшего клиента."}
    if as_text:
        return _dump(payload)
    return payload


def _log(kind: str, payload: dict) -> None:
    hidden = {}
    for key, value in payload.items():
        if key in {"client", "to", "email_to", "email"} and isinstance(value, str) and "@" in value:
            hidden[key] = action_hash.actor_hash(value)
        else:
            hidden[key] = value
    rec = {
        "time": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "action_hash": action_hash.action_digest({"kind": kind, **hidden}),
        **hidden,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _from_ticket() -> bool:
    return bool(getattr(_TLS, "ticket_ok", False))


def _arm_ticket(email: str, ticket: dict) -> dict:
    current = ledger.get_book(email).get("pending_reserve")
    if isinstance(current, dict) and current.get("id") and policy.ticket_alive(str(current.get("expires_at") or "")):
        return current

    def _set(book: dict) -> dict:
        book["pending_reserve"] = ticket
        return book

    ledger.update_book(email, _set)
    return ticket


def _new_ticket(kind: str, prompt: str, **fields) -> dict:
    return {
        "id": _new_id("ASK"),
        "kind": kind,
        "expires_at": policy.expires_at(),
        "prompt": prompt,
        **fields,
    }


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _send_mail_silent(side: str, ticker: str, shares: int, usd: float, cid: str) -> None:
    try:
        send_trade_email(side, ticker, shares, usd, cid, rub_amount="")
    except Exception:
        pass


@mcp.tool()
def list_instruments() -> str:
    """Список акций Мосбиржи, которые можно купить. Цена в рублях, с ISS. Тикер для счёта не нужен."""
    rows = []
    for ticker in ledger.INSTRUMENTS:
        inst = ledger.instrument(ticker)
        if not inst:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": inst["name"],
                "price_rub": inst["price_rub"],
                "price_note": inst["price_note"],
            }
        )
    return _dump({"status": "SUCCESS", "currency": "RUB", "instruments": rows})


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
    if not email:
        return _no_client()
    snap = ledger.snapshot(email)
    snap["status"] = "SUCCESS"
    snap["name"] = name
    _log("ACCOUNT", {"client": email, "cash_usd": snap["cash_usd"], "reserve_usd": snap["reserve_usd"]})
    return _dump(snap)


@mcp.tool()
def get_portfolio() -> str:
    """Какие акции уже куплены у вошедшего клиента. Тикер не обязателен.

    Если клиент просит продать все, shares для execute_sell бери из positions[].shares.
    Ты сам учебный управляющий, не отправляй к брокеру и не говори, что доступа нет.
    """
    email, name = _client_profile()
    if not email:
        return _no_client()
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
            f"Покупка {shares} {ticker} стоит {usd} ₽. "
            f"Основной счёт: {cash} ₽, резерв: {reserve} ₽ (вместе {have} ₽). "
            f"Денег не хватает. Нужно пополнить на {missing} ₽ "
            f"(для этой покупки на резерве не хватает ещё {reserve_need} ₽)."
        ),
    }


def _finish_buy(email: str, ticker: str, shares: int, use_reserve: bool) -> str:
    inst = ledger.instrument(ticker)
    if not inst or inst.get("price_rub") is None:
        return _dump({"status": "ERROR", "error": "нет котировки Мосбиржи, покупка не записана"})
    if inst.get("doubtful") and not _from_ticket():
        return _dump({"status": "ERROR", "error": "котировка сомнительна", "message": "Котировка сомнительна, покупка не записана."})
    live = float(inst["price_rub"])
    usd = round(shares * live, 2)
    if _from_ticket():
        pending = ledger.get_book(email).get("pending_reserve") or {}
        frozen = pending.get("price_rub")
        if frozen is None:
            return _dump({"status": "ERROR", "error": "в заявке нет цены", "message": "Заявка без цены погашена. Напишите покупку ещё раз."})
        frozen = float(frozen)
        if frozen <= 0 or abs(live - frozen) / frozen > 0.02:
            def _drop(b: dict) -> dict:
                b["pending_reserve"] = None
                return b

            ledger.update_book(email, _drop)
            return _dump({"status": "ERROR", "error": "котировка сомнительна", "message": "Цена уехала от заявки. Заявка погашена, напишите покупку ещё раз."})
        usd = round(shares * frozen, 2)
    book = ledger.get_book(email)
    shortfall = round(max(0.0, usd - book["cash_usd"]), 2)
    if book["cash_usd"] + book["reserve_usd"] + 1e-9 < usd:
        def _clear(b: dict) -> dict:
            b["pending_reserve"] = None
            return b

        ledger.update_book(email, _clear)
        return _dump(_insufficient_funds(book, ticker, shares, usd))
    if not use_reserve and shortfall > 0:
        prompt = (
            f"На основном счёте {book['cash_usd']} ₽, резерв {book['reserve_usd']} ₽. "
            f"Покупка {shares} {ticker} стоит {usd} ₽ (не хватает {shortfall} ₽). "
            "Нажмите «Да, подтвердить», чтобы снять нехватку с резерва."
        )
        fresh = _new_ticket(
            "reserve_buy",
            prompt,
            ticker=ticker,
            shares=shares,
            price_rub=round(live, 2),
            need_usd=usd,
            shortfall_usd=shortfall,
            cash_usd=book["cash_usd"],
            reserve_usd=book["reserve_usd"],
        )
        stored = _arm_ticket(email, fresh)
        if stored.get("id") != fresh["id"]:
            return _dump({"status": "NEED_BUTTON", "message": stored.get("prompt")})
        return _dump(
            {
                "status": "NEED_RESERVE_CONFIRM",
                "ask": True,
                "ticker": ticker,
                "shares": shares,
                "need_usd": usd,
                "message": prompt,
            }
        )
    if not _from_ticket() and usd > policy.AMOUNT_NEEDS_CONFIRM:
        prompt = (
            f"Покупка {shares} {ticker} стоит {usd} ₽, это выше порога "
            f"{policy.AMOUNT_NEEDS_CONFIRM:.0f} ₽. Нажмите «Да, подтвердить»."
        )
        fresh = _new_ticket(
            "large_buy",
            prompt,
            ticker=ticker,
            shares=shares,
            price_rub=round(live, 2),
            need_usd=usd,
            shortfall_usd=0,
            cash_usd=book["cash_usd"],
            reserve_usd=book["reserve_usd"],
        )
        stored = _arm_ticket(email, fresh)
        if stored.get("id") != fresh["id"]:
            return _dump({"status": "NEED_BUTTON", "message": stored.get("prompt")})
        return _dump({"status": "NEED_BUTTON", "message": prompt})

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
            "note": "usd_amount и cash_usd в этом JSON — рубли. Клиенту говори ₽, не доллары.",
        }
    )


@mcp.tool()
def execute_buy(ticker: str, shares: int) -> str:
    """Купить акции этого клиента с основного счёта. Цена в рублях берётся с Мосбиржи в момент вызова.

    Сбер, сбера, сберу → SBER. Газпром → GAZP. Лукойл → LKOH. Норникель, норникеля → GMKN. Роснефть → ROSN. Яндекс → YDEX.
    «купи 4 акции сбера» → ticker=SBER, shares=4. Если число не названо («купи акцию»), shares = 1.
    Нулевой резерв и выход из почты не повод отказывать: решит этот тул.
    Если денег на основном мало — не трогай резерв, верни NEED_RESERVE_CONFIRM. Покупку завершит кнопка, не слово «да».
    Новая фраза клиента — новый вызов, даже если такая покупка уже была.
    Не пиши, что покупка прошла, пока ответ не SUCCESS.

    Args:
        ticker: SBER, GAZP, LKOH, GMKN, ROSN или YDEX
        shares: целое число акций, по умолчанию 1
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
    if not email:
        return _no_client()
    if not _from_ticket():
        blocked = store.turn_block(email, "execute_buy")
        if blocked:
            return _dump({"status": "ERROR", "error": blocked})
        if not store.claim_trade(email):
            return _dump({"status": "ERROR", "error": "В этой реплике уже есть проводка."})
    return _finish_buy(email, ticker, shares, use_reserve=False)


def _load_ticket(email: str, ticket_id: str) -> dict | None:
    pending = ledger.get_book(email).get("pending_reserve")
    if not isinstance(pending, dict) or str(pending.get("id") or "") != ticket_id:
        return None
    return pending


def confirm_ticket(ticket_id: str) -> dict:
    """Кнопка заявки. Текст чата сюда не попадает."""
    email, _name = _client_profile()
    if not email:
        return {"status": "ERROR", "error": "Нет вошедшего клиента."}
    pending = _load_ticket(email, ticket_id)
    if not pending:
        return {"status": "ERROR", "error": "Заявка не найдена или срок истёк."}
    kind = str(pending.get("kind") or "")
    _TLS.ticket_ok = True
    try:
        if kind == "reserve_buy":
            return json.loads(_finish_buy(email, str(pending["ticker"]), int(pending["shares"]), True))
        if kind == "large_buy":
            return json.loads(_finish_buy(email, str(pending["ticker"]), int(pending["shares"]), False))
        if kind == "large_sell":
            return json.loads(execute_sell(str(pending["ticker"]), int(pending["shares"])))
        if kind == "sell_all":
            return json.loads(sell_all())
        if kind == "transfer":
            return transfer_between(str(pending["source"]), str(pending["target"]), float(pending["amount"]))
    finally:
        _TLS.ticket_ok = False
    return {"status": "ERROR", "error": "Неизвестная заявка."}


def deny_ticket(ticket_id: str) -> dict:
    email, _name = _client_profile()
    if not email:
        return {"status": "ERROR", "error": "Нет вошедшего клиента."}
    if not _load_ticket(email, ticket_id):
        return {"status": "ERROR", "error": "Заявка не найдена или срок истёк."}

    def _clear(book: dict) -> dict:
        book["pending_reserve"] = None
        return book

    ledger.update_book(email, _clear)
    return {"status": "CANCELLED", "message": "Заявка отменена, счёт не менялся."}


def propose_sell_all() -> dict:
    email, _name = _client_profile()
    if not email:
        return {"status": "ERROR", "error": "Нет вошедшего клиента."}
    book = ledger.get_book(email)
    held = [ticker for ticker, shares in book["positions"].items() if int(shares) > 0]
    if not held:
        return {"status": "ERROR", "error": "В портфеле нет акций, продавать нечего."}
    prompt = "Продать весь портфель? Нажмите «Да, подтвердить». Срок заявки — 3 минуты."
    ticket = _new_ticket("sell_all", prompt)
    stored = _arm_ticket(email, ticket)
    if stored.get("id") != ticket["id"]:
        return {"status": "NEED_BUTTON", "message": stored.get("prompt"), "pending": stored}
    return {"status": "NEED_BUTTON", "message": prompt, "pending": ticket}


def propose_transfer(source: str, target: str, amount: float) -> dict:
    email, _name = _client_profile()
    if not email:
        return {"status": "ERROR", "error": "Нет вошедшего клиента."}
    book = ledger.get_book(email)
    src = "reserve_usd" if source == "reserve" else "cash_usd"
    amount = round(float(amount), 2)
    if float(book[src]) + 1e-9 < amount:
        return {
            "status": "ERROR",
            "error": "На этом счёте не хватает денег для перевода.",
            "cash_usd": book["cash_usd"],
            "reserve_usd": book["reserve_usd"],
        }
    src_label = "резерва" if source == "reserve" else "основного счёта"
    dst_label = "основной счёт" if target == "cash" else "резерв"
    prompt = (
        f"Перевести {amount} ₽ с {src_label} на {dst_label}? "
        "Нажмите «Да, подтвердить». Срок заявки — 3 минуты."
    )
    ticket = _new_ticket("transfer", prompt, source=source, target=target, amount=amount)
    stored = _arm_ticket(email, ticket)
    if stored.get("id") != ticket["id"]:
        return {"status": "NEED_BUTTON", "message": stored.get("prompt"), "pending": stored}
    return {"status": "NEED_BUTTON", "message": prompt, "pending": ticket}


@mcp.tool()
def confirm_reserve_purchase() -> str:
    """После явного «да» клиента: снять нехватку с резерва и купить отложенную заявку."""
    email, _name = _client_profile()
    if not email:
        return _no_client()
    book = ledger.get_book(email)
    pending = book.get("pending_reserve")
    if not pending:
        return _dump({"status": "ERROR", "error": "нет отложенной покупки, подтверждать нечего"})
    return _finish_buy(email, pending["ticker"], int(pending["shares"]), use_reserve=True)


@mcp.tool()
def deny_reserve_purchase() -> str:
    """Клиент отказался снимать резерв — покупка отменяется."""
    email, _name = _client_profile()
    if not email:
        return _no_client()
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
    """Продать названное число акций одного тикера. Цена в рублях с Мосбиржи в момент вызова. Не для «продай все» — для этого sell_all.

    Сбер, сбера → SBER. Газпром → GAZP. Лукойл → LKOH. Норникель, норникеля → GMKN. Роснефть → ROSN. Яндекс → YDEX.
    «продай 4 акции сбера» → ticker=SBER, shares=4. «продай акцию» без числа → shares = 1.

    Args:
        ticker: SBER, GAZP, LKOH, GMKN, ROSN или YDEX
        shares: сколько продать
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
    if not email:
        return _no_client()
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
    if not inst or inst.get("price_rub") is None:
        return _dump({"status": "ERROR", "error": "нет котировки Мосбиржи, продажа не записана"})
    if inst.get("doubtful") and not _from_ticket():
        return _dump({"status": "ERROR", "error": "котировка сомнительна", "message": "Котировка сомнительна, продажа не записана."})
    live = float(inst["price_rub"])
    usd = round(shares * live, 2)
    if _from_ticket():
        pending = ledger.get_book(email).get("pending_reserve") or {}
        frozen = pending.get("price_rub")
        if frozen is None:
            return _dump({"status": "ERROR", "error": "в заявке нет цены", "message": "Заявка без цены погашена. Напишите продажу ещё раз."})
        frozen = float(frozen)
        if frozen <= 0 or abs(live - frozen) / frozen > 0.02:
            def _drop(b: dict) -> dict:
                b["pending_reserve"] = None
                return b

            ledger.update_book(email, _drop)
            return _dump({"status": "ERROR", "error": "котировка сомнительна", "message": "Цена уехала от заявки. Заявка погашена, напишите продажу ещё раз."})
        usd = round(shares * frozen, 2)
    if not _from_ticket():
        blocked = store.turn_block(email, "execute_sell")
        if blocked:
            return _dump({"status": "ERROR", "error": blocked})
        if not store.claim_trade(email):
            return _dump({"status": "ERROR", "error": "В этой реплике уже есть проводка."})
        if usd > policy.AMOUNT_NEEDS_CONFIRM:
            prompt = (
                f"Продажа {shares} {ticker} на {usd} ₽ выше порога "
                f"{policy.AMOUNT_NEEDS_CONFIRM:.0f} ₽. Нажмите «Да, подтвердить»."
            )
            fresh = _new_ticket("large_sell", prompt, ticker=ticker, shares=shares, price_rub=round(live, 2), need_usd=usd)
            stored = _arm_ticket(email, fresh)
            if stored.get("id") != fresh["id"]:
                return _dump({"status": "NEED_BUTTON", "message": stored.get("prompt")})
            return _dump({"status": "NEED_BUTTON", "message": prompt, "ticker": ticker, "shares": shares})

    def _sell(b: dict) -> dict:
        have_now = int(b["positions"].get(ticker, 0))
        if have_now < shares:
            raise ValueError("недостаточно акций в портфеле")
        b["pending_reserve"] = None
        b["positions"][ticker] = have_now - shares
        b["cash_usd"] = round(b["cash_usd"] + usd, 2)
        return b

    try:
        after = ledger.update_book(email, _sell)
    except ValueError:
        fresh = ledger.get_book(email)
        return _dump(
            {
                "status": "ERROR",
                "error": "недостаточно акций в портфеле",
                "ticker": ticker,
                "have": int(fresh["positions"].get(ticker, 0)),
                "want": shares,
                "cash_usd": fresh["cash_usd"],
                "reserve_usd": fresh["reserve_usd"],
            }
        )
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
def sell_all() -> str:
    """Продать все акции портфеля. Единственный тул для «продай все», «все мои акции», «весь портфель».

    Вызывай сразу, без get_portfolio и без execute_sell. Число акций не передаётся.
    """
    email, _name = _client_profile()
    if not email:
        return _no_client()
    book = ledger.get_book(email)
    held = [(ticker, int(shares)) for ticker, shares in book["positions"].items() if int(shares) > 0]
    if not held:
        return _dump(
            {
                "status": "ERROR",
                "side": "SELL_ALL",
                "error": "В портфеле нет акций, продавать нечего.",
                "cash_usd": book["cash_usd"],
                "reserve_usd": book["reserve_usd"],
            }
        )
    sales = []
    for ticker, shares in held:
        row = json.loads(execute_sell(ticker, shares))
        if row.get("status") == "SUCCESS":
            sales.append(
                {
                    "ticker": row.get("ticker"),
                    "shares": row.get("shares"),
                    "usd_amount": row.get("usd_amount"),
                    "confirmation_id": row.get("confirmation_id"),
                }
            )
    snap = ledger.snapshot(email)
    return _dump(
        {
            "status": "SUCCESS" if sales else "ERROR",
            "side": "SELL_ALL",
            "sales": sales,
            "cash_usd": snap["cash_usd"],
            "reserve_usd": snap["reserve_usd"],
            "positions": snap["positions"],
            "error": None if sales else "Продажа не выполнена.",
        }
    )


def _bucket(name: str) -> str | None:
    t = (name or "").strip().lower().replace("ё", "е")
    if t in {"cash", "main", "основной", "основного", "основном", "cash_usd"}:
        return "cash_usd"
    if t in {"reserve", "резерв", "резерва", "резервный", "reserve_usd"}:
        return "reserve_usd"
    return None


def transfer_between(source: str, target: str, amount: float) -> dict:
    """Перевод между основным и резервом. Проверка денег внутри записи счёта."""
    src = _bucket(source)
    dst = _bucket(target)
    amount = round(float(amount), 2)
    if src is None or dst is None:
        return {"status": "ERROR", "side": "TRANSFER", "error": "счёт: cash/основной или reserve/резерв"}
    if src == dst:
        return {"status": "ERROR", "side": "TRANSFER", "error": "нельзя перевести на тот же счёт"}
    if amount <= 0:
        return {"status": "ERROR", "side": "TRANSFER", "error": "сумма перевода должна быть > 0"}
    email, name = _client_profile()
    if not email:
        return _no_client(as_text=False)
    label = {"cash_usd": "основном счёте", "reserve_usd": "резервном счёте"}

    def _move(b: dict) -> dict:
        b["pending_reserve"] = None
        if float(b[src]) + 1e-9 < amount:
            raise ValueError(label[src])
        b[src] = round(float(b[src]) - amount, 2)
        b[dst] = round(float(b[dst]) + amount, 2)
        return b

    try:
        after = ledger.update_book(email, _move)
    except ValueError as exc:
        fresh = ledger.get_book(email)
        return {
            "status": "ERROR",
            "side": "TRANSFER",
            "error": f"недостаточно денег на {exc}",
            "cash_usd": fresh["cash_usd"],
            "reserve_usd": fresh["reserve_usd"],
            "want": amount,
        }
    cid = _new_id("XFR")
    src_name = "cash" if src == "cash_usd" else "reserve"
    dst_name = "cash" if dst == "cash_usd" else "reserve"
    kind = "TO_CASH" if dst == "cash_usd" else "TO_RESERVE"
    _log(
        kind,
        {
            "amount": amount,
            "confirmation_id": cid,
            "client": email,
            "from": src_name,
            "to": dst_name,
            "cash_usd": after["cash_usd"],
            "reserve_usd": after["reserve_usd"],
        },
    )
    ledger.record_trade(
        email=email,
        kind=kind,
        confirmation_id=cid,
        usd_amount=amount,
        reserved_usd=amount if src == "reserve_usd" else 0,
        cash_usd=after["cash_usd"],
        reserve_usd=after["reserve_usd"],
    )
    mail_ok, mail_error = _mail_transfer(
        email=email,
        name=name,
        amount=amount,
        src_name=src_name,
        dst_name=dst_name,
        confirmation_id=cid,
        cash_usd=after["cash_usd"],
        reserve_usd=after["reserve_usd"],
    )
    return {
        "status": "SUCCESS",
        "side": "TRANSFER",
        "transferred": amount,
        "from": src_name,
        "to": dst_name,
        "confirmation_id": cid,
        "cash_usd": after["cash_usd"],
        "reserve_usd": after["reserve_usd"],
        "email_to": email,
        "mail_ok": mail_ok,
        "mail_error": mail_error,
    }


@mcp.tool(name="transfer_between")
def transfer_between_tool(source: str, target: str, amount: float) -> str:
    """Перевести деньги между основным и резервным счётом этого клиента.

    Args:
        source: откуда: cash или reserve
        target: куда: cash или reserve
        amount: сумма в рублях
    """
    return _dump(transfer_between(source, target, amount))


def _mail_transfer(
    *,
    email: str,
    name: str,
    amount: float,
    src_name: str,
    dst_name: str,
    confirmation_id: str,
    cash_usd: float,
    reserve_usd: float,
) -> tuple[bool, str]:
    if not ledger.has_trade(email, confirmation_id):
        return False, "письма без записанной сделки нет"
    src = "основного счёта" if src_name == "cash" else "резерва"
    dst = "основной счёт" if dst_name == "cash" else "резерв"
    subject = "Перевод между счетами"
    body = (
        f"Здравствуйте, {name or 'Клиент'}.\n\n"
        f"Переведено {amount} ₽ с {src} на {dst}.\n"
        f"Основной счёт: {cash_usd} ₽\n"
        f"Резерв: {reserve_usd} ₽\n"
        f"Номер подтверждения: {confirmation_id}\n"
        "Это учебное письмо, живых денег нет.\n"
    )
    try:
        mail_client.deliver(
            email=email,
            subject=subject,
            body=body,
            confirmation_id=confirmation_id,
            side="TRANSFER",
            ticker="",
        )
    except (mail_client.MailUnavailable, ValueError) as exc:
        _log("EMAIL", {"to": email, "side": "TRANSFER", "confirmation_id": confirmation_id, "ok": False, "error": str(exc)})
        return False, str(exc)
    _log("EMAIL", {"to": email, "side": "TRANSFER", "confirmation_id": confirmation_id, "ok": True})
    return True, ""


@mcp.tool()
def request_reserve_funds(amount: float) -> str:
    """Перевести сумму с резервного счёта на основной.

    Args:
        amount: сколько рублей перевести
    """
    return _dump(transfer_between("reserve", "cash", amount))


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
        usd_amount: сумма в рублях
        confirmation_id: учебный номер подтверждения
        rub_amount: сумма в рублях по курсу ЦБ, если известна
    """
    to, name = _client_profile()
    if not to:
        return _no_client()
    if not ledger.has_trade(to, confirmation_id):
        return _dump({"status": "ERROR", "error": "письма без записанной сделки нет"})
    side_u = side.upper().strip()
    title = "Куплено" if side_u == "BUY" else "Продано"
    ticker = ticker.upper().strip()
    body = (
        f"Здравствуйте, {name}.\n\n"
        f"{title} {shares} {ticker}.\n"
        f"Сумма: {usd_amount} ₽\n"
        "Цена с Мосбиржи.\n"
        f"Номер подтверждения: {confirmation_id}\n"
        "Это учебное письмо, живых денег нет.\n"
    )
    subject = f"{title} {ticker}"
    try:
        saved = mail_client.deliver(
            email=to,
            subject=subject,
            body=body,
            confirmation_id=confirmation_id,
            side=side_u,
            ticker=ticker,
        )
    except (mail_client.MailUnavailable, ValueError) as exc:
        _log(
            "EMAIL",
            {
                "to": to,
                "side": side_u,
                "ticker": ticker,
                "confirmation_id": confirmation_id,
                "ok": False,
                "error": str(exc),
            },
        )
        return _dump({"status": "ERROR", "to": to, "error": str(exc)})
    mid = int(saved["id"])
    _log(
        "EMAIL",
        {
            "to": to,
            "side": side_u,
            "ticker": ticker,
            "confirmation_id": confirmation_id,
            "ok": True,
            "message_id": mid,
        },
    )
    return _dump({"status": "SUCCESS", "to": to, "message_id": mid})


if __name__ == "__main__":
    mcp.run()
