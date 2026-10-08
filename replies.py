"""Тексты клиенту из счёта, не из выдумки модели."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from ledger import last_trade


def _fresh_trade(row: dict | None) -> dict | None:
    if not row:
        return None
    raw = str(row.get("created_at") or "")
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ts > timedelta(seconds=60):
            return None
    except ValueError:
        return row
    return row


def format_holdings(snap: dict) -> str:
    pos = snap.get("positions") or []
    if not pos:
        return "В портфеле нет купленных акций."
    lines = [
        f"- {p['name']} ({p['ticker']}): {p['shares']} шт., стоимость ${p['value_usd']}"
        for p in pos
        if int(p.get("shares") or 0) > 0
    ]
    if not lines:
        return "В портфеле нет купленных акций."
    return "\n".join(lines)


def format_portfolio(snap: dict) -> str:
    body = format_holdings(snap)
    return (
        f"{body}\n"
        f"Оценка позиций: {snap.get('positions_usd', 0)} $.\n"
        f"Основной счёт: {snap.get('cash_usd', 0)} $, резерв: {snap.get('reserve_usd', 0)} $."
    )


def format_account(snap: dict) -> str:
    return (
        f"Основной счёт: {snap.get('cash_usd', 0)} $.\n"
        f"Резерв: {snap.get('reserve_usd', 0)} $.\n"
        f"Бумаги:\n{format_holdings(snap)}"
    )


def _left(snap: dict, ticker: str) -> int:
    ticker = (ticker or "").upper()
    for p in snap.get("positions") or []:
        if str(p.get("ticker") or "").upper() == ticker:
            return int(p.get("shares") or 0)
    return 0


def format_sell(payload: dict, snap: dict, email: str) -> str:
    ticker = payload.get("ticker")
    left = _left(snap, str(ticker or ""))
    return (
        f"Вы продали {payload.get('shares')} акции {ticker}. "
        f"Сумма продажи составила ${payload.get('usd_amount')}. "
        f"Деньги зачислены на основной счёт.\n"
        f"Ваш текущий портфель выглядит так:\n\n"
        f"{format_holdings(snap)}\n\n"
        f"Осталось {ticker}: {left} шт.\n"
        f"Ваш текущий остаток на основном счёте составляет ${snap.get('cash_usd')}, "
        f"а резерв в размере ${snap.get('reserve_usd')}.\n"
        f"Письмо «Продано» отправлено в почтовый сервис на {email}."
    )


def format_buy(payload: dict, snap: dict, email: str | None = None) -> str:
    extra = ""
    reserved = payload.get("reserved_from_reserve_usd") or 0
    if reserved:
        extra = f" С резерва снято {reserved} $."
    mail = ""
    if email:
        mail = f"\nПисьмо «Куплено» отправлено в почтовый сервис на {email}."
    return (
        f"Вы купили {payload.get('shares')} акцию {payload.get('ticker')}. "
        f"Стоимость покупки составила ${payload.get('usd_amount')}.{extra}\n"
        f"Ваш текущий портфель выглядит так:\n\n"
        f"{format_holdings(snap)}\n\n"
        f"Ваш текущий остаток на основном счёте составляет ${snap.get('cash_usd')}, "
        f"а резерв в размере ${snap.get('reserve_usd')}."
        f"{mail}"
    )


OTHER_USER_REPLY = (
    "Можно смотреть и менять только свой счёт. Чужие действия, портфель и почта недоступны."
)

_EMAIL_RE = re.compile(r"[a-z0-9._+-]+@[a-z0-9.-]+", re.IGNORECASE)
_PERSON_RE = re.compile(
    r"(?:пользовател\w*|клиент\w*|юзер\w*|user)\s+[\"'«]*([a-zа-яё0-9_.@+-]+)",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(
    r"(?:get_portfolio|get_account|execute_buy|execute_sell|send_trade_email)\s+[\"']?([a-zа-яё0-9_.@+-]+)",
    re.IGNORECASE,
)
_NOT_A_PERSON = {
    "я", "мой", "моя", "мои", "моё", "мое", "мне", "меня", "себя", "свой", "своя", "свои",
    "моего", "моей", "моём", "моем", "этот", "эта", "это", "том", "этого",
    "и", "в", "на", "с", "по", "для", "от", "из", "не", "ли",
    "счёте", "счете", "счёта", "счета", "покупке", "покупки", "продаже", "продажи",
    "сделке", "сделки", "акциях", "акции", "акцию", "портфеле", "портфеля",
    "резерве", "резерва", "курсе", "курса", "долларе", "доллара", "деньгах",
    "бумагах", "бумаги", "действиях", "действия", "информации", "информацию",
    "пользователя", "пользователе", "клиента", "клиенте",
    "счёт", "счет", "портфель", "баланс", "почту", "почта", "акции", "акцию", "список",
    "nvda", "aapl", "msft", "nvidia", "apple", "microsoft",
    "доллар", "доллара", "долларе", "евро", "юань", "юаня",
    "валюту", "валюта", "валюте", "курс", "курсе", "резерв",
    "покупку", "продажу", "сделку",
}


def _own_tokens(email: str, name: str) -> set[str]:
    email = (email or "").strip().lower()
    name = (name or "").strip().lower()
    tokens = set(_NOT_A_PERSON)
    if email:
        tokens.add(email)
        tokens.add(email.split("@", 1)[0])
    if name:
        tokens.add(name)
        tokens.update(part for part in name.split() if part)
    return tokens


def _foreign_token(token: str, own: set[str]) -> bool:
    token = token.strip(" \"'«».,:;!?()[]").lower()
    if not token or token in own or token.isdigit():
        return False
    return True


def asks_about_other_user(text: str, email: str, name: str = "") -> bool:
    """Просьба узнать или изменить данные не того, кто вошёл."""
    raw = text or ""
    if not raw.strip():
        return False
    own = _own_tokens(email, name)
    folded = raw.lower()
    for found in _EMAIL_RE.findall(folded):
        if found not in own:
            return True
    if any(
        piece in folded
        for piece in (
            "чуж",
            "другого пользоват",
            "другой пользоват",
            "другие пользоват",
            "всех пользоват",
            "все пользовател",
            "список пользовател",
            "список клиент",
            "все клиент",
            "всех клиент",
        )
    ):
        return True
    for rx in (_PERSON_RE, _TOOL_RE):
        for match in rx.finditer(folded):
            token = match.group(1)
            if "@" in token and token.strip(" \"'«».,:;!?()[]").lower() not in own:
                return True
            if rx is _PERSON_RE and _foreign_token(token, own):
                return True
    return False


def _transfer_side(word: str) -> str | None:
    if word.startswith("основн"):
        return "cash"
    if word.startswith("резерв"):
        return "reserve"
    return None


def parse_transfer(text: str) -> dict | None:
    """«переведи 100 долларов с основного счета на резервный» и обратный перевод."""
    raw = (text or "").strip().lower().replace("ё", "е")
    if not any(word in raw for word in ("перевед", "перевод", "перекинь", "переложи")):
        return None
    num = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    if not num:
        return None
    amount = round(float(num.group(1).replace(",", ".")), 2)
    if amount <= 0:
        return None
    src_m = re.search(r"(?:^|\s)с\s+(основн\w*|резерв\w*)", raw)
    dst_m = re.search(r"(?:^|\s)(?:на|в)\s+(основн\w*|резерв\w*)", raw)
    source = _transfer_side(src_m.group(1)) if src_m else None
    target = _transfer_side(dst_m.group(1)) if dst_m else None
    if source and target and source != target:
        return {"source": source, "target": target, "amount": amount}
    if "резерв" not in raw or "основн" not in raw:
        return None
    if raw.find("резерв") < raw.find("основн"):
        return {"source": "reserve", "target": "cash", "amount": amount}
    return {"source": "cash", "target": "reserve", "amount": amount}


def format_transfer(payload: dict) -> str:
    if payload.get("status") != "SUCCESS":
        return str(payload.get("error") or "Перевод не выполнен.")
    src = "резерва" if payload.get("from") == "reserve" else "основного счёта"
    dst = "основной счёт" if payload.get("to") == "cash" else "резерв"
    mail = ""
    if payload.get("email_to"):
        if payload.get("mail_ok"):
            mail = f"\nПисьмо о переводе отправлено в почтовый сервис на {payload['email_to']}."
        else:
            mail = f"\nПеревод записан, письмо не доставлено: {payload.get('mail_error') or 'почтовый сервис недоступен'}."
    return (
        f"Переведено {payload.get('transferred')} $ с {src} на {dst}.\n"
        f"Основной: {payload.get('cash_usd')} $. Резерв: {payload.get('reserve_usd')} $."
        f"{mail}"
    )


def is_portfolio_question(text: str) -> bool:
    t = text.lower()
    if any(x in t for x in ("можно купить", "доступн", "для покупк", "список акци", "какие акции я могу")):
        return False
    return any(
        x in t
        for x in (
            "у меня",
            "портфел",
            "куплено",
            "мои акци",
            "что есть",
            "какие акции есть",
            "сколько акци",
        )
    )


def is_account_question(text: str) -> bool:
    t = text.lower().replace("ё", "е")
    return any(
        x in t
        for x in (
            "состояни",
            "сколько денег",
            "баланс",
            "остаток",
            "на счет",
            "мой счет",
            "моего счет",
            "мои деньги",
            "резерв сколько",
        )
    )


def is_catalog_question(text: str) -> bool:
    t = text.lower()
    return any(
        x in t
        for x in (
            "список акци",
            "какие акции можно",
            "какие акции я могу",
            "какие акции",
            "что я могу купить",
            "что могу купить",
            "доступн",
            "для покупк",
            "каталог",
        )
    )


def is_fx_question(text: str) -> bool:
    t = text.lower()
    if is_trade_request(t) or is_catalog_question(t) or parse_transfer(t):
        return False
    return any(x in t for x in ("курс", "валют", "цб", "доллар", "евро", "юан"))


def format_instruments() -> str:
    from ledger import INSTRUMENTS

    lines = ["Доступные для покупки акции:"]
    for ticker, meta in INSTRUMENTS.items():
        lines.append(f"- {meta['name']} ({ticker}): ${meta['price_usd']}")
    return "\n".join(lines)


def is_trade_request(text: str) -> bool:
    return parse_trade_command(text) is not None


_ALIASES = (
    ("nvidia", "NVDA"),
    ("nvda", "NVDA"),
    ("нвидиа", "NVDA"),
    ("энвидиа", "NVDA"),
    ("apple", "AAPL"),
    ("aapl", "AAPL"),
    ("эппл", "AAPL"),
    ("эпл", "AAPL"),
    ("яблок", "AAPL"),
    ("microsoft", "MSFT"),
    ("msft", "MSFT"),
    ("майкрософт", "MSFT"),
)


def _resolve_ticker(text: str) -> str | None:
    t = text.lower()
    for alias, ticker in _ALIASES:
        if alias in t:
            return ticker
    return None


_last_order: dict[str, dict] = {}
_CLAIM_RE = re.compile(
    r"вы\s+(купили|продали)|сумма\s+покупки|стоимость\s+покупки|письмо\s+[«\"]куплено",
    re.IGNORECASE,
)


def _bare_ticker(text: str) -> str | None:
    """Сообщение из одного тикера: «NVDA», «тогда nvidia», без новой команды."""
    raw = (text or "").strip().lower().rstrip(".!")
    raw = re.sub(r"^(тогда|ну|это|тикер)\s+", "", raw).strip()
    ticker = _resolve_ticker(raw)
    if not ticker:
        return None
    leftover = raw
    for alias, _ticker in _ALIASES:
        leftover = leftover.replace(alias, " ")
    leftover = re.sub(r"\b(акци\w*|штук\w*|бумаг\w*)\b", " ", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip(" .,")
    if leftover:
        return None
    return ticker


def resolve_followup_order(text: str, email: str) -> dict | None:
    """«Купи 5 штук» после NVDA берёт тот тикер. «NVDA» после вопроса «какую бумагу» закрывает заявку."""
    order = parse_trade_command(text)
    prev = _last_order.get(email)
    if order:
        if not order.get("ticker") and prev and prev.get("ticker"):
            order = {**order, "ticker": prev["ticker"]}
        _last_order[email] = {
            "side": order["side"],
            "ticker": order.get("ticker"),
            "shares": int(order["shares"]),
        }
        return order
    ticker = _bare_ticker(text)
    if ticker and prev and prev.get("side") and not prev.get("ticker"):
        done = {"side": prev["side"], "ticker": ticker, "shares": int(prev["shares"])}
        _last_order[email] = done
        return done
    return None


def parse_trade_command(text: str) -> dict | None:
    """Явная команда купить/продать. Вопросы и прошедшее время не считаются."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if any(
        x in t
        for x in (
            "почему",
            "зачем",
            "можно купить",
            "список",
            "доступн",
            "какие акции я могу",
        )
    ):
        return None
    if is_portfolio_question(t) or is_account_question(t):
        return None
    side = None
    if re.search(r"(^|\s)(купи|купить|buy)(\s|$)", t) or t.startswith("купи ") or t.startswith("купить ") or t.startswith("buy "):
        if not re.match(r"купил", t):
            side = "BUY"
    if re.search(r"(^|\s)(продай|продать|sell)(\s|$)", t) or t.startswith("прод") or t.startswith("sell "):
        if not re.match(r"продал", t):
            side = "SELL"
    if t in {"купи", "купить", "buy", "продай", "продать", "sell"}:
        side = "BUY" if t in {"купи", "купить", "buy"} else "SELL"
    if not side:
        return None
    ticker = _resolve_ticker(t)
    num = re.search(r"(\d+)", t)
    shares = int(num.group(1)) if num else 1
    if shares < 1:
        return None
    return {"side": side, "ticker": ticker, "shares": shares}


def format_trade_result(payload: dict, email: str) -> str:
    from ledger import snapshot

    snap = snapshot(email)
    if payload.get("status") == "ERROR":
        text = payload.get("message") or payload.get("error") or "Сделка не прошла."
        if payload.get("have") is not None:
            return (
                f"Нельзя продать {payload.get('want')} {payload.get('ticker')}: "
                f"в портфеле {payload.get('have')} шт.\n"
                f"{format_portfolio(snap)}"
            )
        return text
    if payload.get("status") == "NEED_RESERVE_CONFIRM":
        p = snap.get("pending_reserve") or payload
        return (
            f"На основном счёте {p.get('cash_usd')} $, резерв {p.get('reserve_usd')} $. "
            f"Покупка {p.get('shares')} {p.get('ticker')} стоит {p.get('need_usd')} $ "
            f"(не хватает {p.get('shortfall_usd')} $). "
            "Снять нехватку с резервного счёта? Напишите «да» или «нет»."
        )
    if payload.get("status") == "SUCCESS" and payload.get("side") == "SELL":
        return format_sell(payload, snap, email)
    if payload.get("status") == "SUCCESS" and payload.get("side") == "BUY":
        return format_buy(payload, snap, email)
    return payload.get("message") or str(payload)


def apply_trade_order(order: dict, email: str, name: str = "") -> tuple[str, list[str]]:
    """Купить/продать без модели: тот же execute_buy / execute_sell."""
    from mcp_strazh import execute_buy, execute_sell, set_client

    set_client(email, name)
    if not order.get("ticker"):
        return (
            "Не понял, какую бумагу купить или продать. "
            "Доступны NVIDIA (NVDA), Apple (AAPL), Microsoft (MSFT).",
            [],
        )
    if order["side"] == "BUY":
        payload = json.loads(execute_buy(order["ticker"], int(order["shares"])))
        used = ["execute_buy"]
    else:
        payload = json.loads(execute_sell(order["ticker"], int(order["shares"])))
        used = ["execute_sell"]
    return format_trade_result(payload, email), used


def _trade_payload(row: dict) -> dict:
    kind = str(row.get("kind") or "")
    return {
        "status": "SUCCESS",
        "side": kind,
        "ticker": row.get("ticker"),
        "shares": row.get("shares"),
        "usd_amount": row.get("usd_amount"),
        "cash_usd": row.get("cash_usd"),
        "reserve_usd": row.get("reserve_usd"),
        "reserved_from_reserve_usd": row.get("reserved_usd") or 0,
        "position": None,
    }


def _pick_trade_payload(payloads: list[dict]) -> dict | None:
    for p in reversed(payloads):
        tool = str(p.get("_tool") or "")
        if tool == "send_trade_email":
            continue
        if p.get("side") in ("BUY", "SELL"):
            return p
        if p.get("status") in ("ERROR", "NEED_RESERVE_CONFIRM", "CANCELLED"):
            return p
        if tool in ("execute_buy", "execute_sell"):
            return p
    return None


def compose_chat_reply(
    *,
    model_text: str,
    used: list[str],
    payloads: list[dict],
    snap: dict,
    email: str,
    user_message: str,
) -> str:
    """Цифры портфеля и остатков — только из учёта, не из текста модели."""
    if asks_about_other_user(user_message, email):
        return OTHER_USER_REPLY
    used_set = set(used)
    payload = _pick_trade_payload(payloads)
    pending = snap.get("pending_reserve")

    if payload and payload.get("status") == "ERROR":
        text = payload.get("message") or payload.get("error") or model_text
        if payload.get("have") is not None:
            text = (
                f"Нельзя продать {payload.get('want')} {payload.get('ticker')}: "
                f"в портфеле {payload.get('have')} шт.\n"
                f"{format_portfolio(snap)}"
            )
        return text

    if payload and payload.get("status") == "NEED_RESERVE_CONFIRM" and pending:
        p = pending
        return (
            f"На основном счёте {p['cash_usd']} $, резерв {p['reserve_usd']} $. "
            f"Покупка {p['shares']} {p['ticker']} стоит {p['need_usd']} $ "
            f"(не хватает {p['shortfall_usd']} $). "
            "Снять нехватку с резервного счёта? Напишите «да» или «нет»."
        )

    if payload and payload.get("status") == "SUCCESS" and payload.get("side") == "SELL":
        return format_sell(payload, snap, email)
    if payload and payload.get("status") == "SUCCESS" and payload.get("side") == "BUY":
        return format_buy(payload, snap, email)

    if "execute_sell" in used_set:
        row = _fresh_trade(last_trade(email, ("SELL",)))
        if row:
            return format_sell(_trade_payload(row), snap, email)
    if "execute_buy" in used_set:
        if pending:
            p = pending
            return (
                f"На основном счёте {p['cash_usd']} $, резерв {p['reserve_usd']} $. "
                f"Покупка {p['shares']} {p['ticker']} стоит {p['need_usd']} $ "
                f"(не хватает {p['shortfall_usd']} $). "
                "Снять нехватку с резервного счёта? Напишите «да» или «нет»."
            )
        row = _fresh_trade(last_trade(email, ("BUY",)))
        if row:
            return format_buy(_trade_payload(row), snap, email)

    if is_trade_request(user_message) and "execute_buy" not in used_set and "execute_sell" not in used_set:
        return (
            "Сделка не записана: торговый тул не вызвался, в учёте ничего не менялось.\n"
            f"{format_portfolio(snap)}"
        )
    if _CLAIM_RE.search(model_text or "") and "execute_buy" not in used_set and "execute_sell" not in used_set:
        return (
            "Сделка не записана: в учёте ничего не менялось.\n"
            f"{format_portfolio(snap)}"
        )
    return model_text
