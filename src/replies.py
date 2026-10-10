"""Тексты клиенту из счёта, не из выдумки модели."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import cbr_rates
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
        f"- {p['name']} ({p['ticker']}): {p['shares']} шт., стоимость {p['value_usd']} ₽"
        for p in pos
        if int(p.get("shares") or 0) != 0
    ]
    if not lines:
        return "В портфеле нет купленных акций."
    return "\n".join(lines)


_PLACEHOLDER = re.compile(
    r"^(?:ok|ок|ход закрыт|hod закрыт|·)[.!]?$",
    re.IGNORECASE,
)
_FX_NAMES = {"USD": "доллар США", "EUR": "евро", "CNY": "китайский юань"}


def is_placeholder(text: str) -> bool:
    return _PLACEHOLDER.fullmatch((text or "").strip()) is not None


def format_rates(payloads: list[dict]) -> str | None:
    seen: dict[str, dict] = {}
    for payload in payloads:
        rows = list(payload.get("currencies") or [])
        code = str(payload.get("char_code") or payload.get("code") or "").upper()
        if code and payload.get("value_rub") is not None:
            rows.append(payload)
        for row in rows:
            code = str(row.get("char_code") or row.get("code") or "").upper()
            if code not in _FX_NAMES or row.get("value_rub") is None:
                continue
            seen[code] = row
    if not seen:
        return None
    lines = ["Курс ЦБ РФ:" if len(seen) == 1 else "Курсы ЦБ РФ:"]
    for code in ("USD", "EUR", "CNY"):
        row = seen.get(code)
        if not row:
            continue
        nominal = row.get("nominal") or 1
        lines.append(f"- {_FX_NAMES[code]} ({code}): {row.get('value_rub')} ₽ за {nominal}")
    return "\n".join(lines) if len(lines) > 1 else None


def official_rates(codes: list[str]) -> str | None:
    """Цифра курса из ответа ЦБ, не из пересказа модели."""
    wanted = [code for code in ("USD", "EUR", "CNY") if code in set(codes or [])]
    if not wanted:
        return None
    _date, rows = cbr_rates.daily()
    picked = []
    for code in wanted:
        row = next((item for item in rows if item.get("char_code") == code), None)
        if row:
            picked.append(row)
    return format_rates(picked)


def format_portfolio(snap: dict) -> str:
    body = format_holdings(snap)
    return (
        f"{body}\n"
        f"Оценка позиций: {snap.get('positions_usd', 0)} ₽.\n"
        f"Основной счёт: {snap.get('cash_usd', 0)} ₽, резерв: {snap.get('reserve_usd', 0)} ₽."
    )


def _left(snap: dict, ticker: str) -> int:
    ticker = (ticker or "").upper()
    for p in snap.get("positions") or []:
        if str(p.get("ticker") or "").upper() == ticker:
            return int(p.get("shares") or 0)
    return 0


def _share_word(value: object) -> str:
    amount = int(value or 0)
    if amount % 10 == 1 and amount % 100 != 11:
        return "акцию"
    if amount % 10 in (2, 3, 4) and amount % 100 not in (12, 13, 14):
        return "акции"
    return "акций"


def format_sell(payload: dict, snap: dict, email: str) -> str:
    ticker = payload.get("ticker")
    left = _left(snap, str(ticker or ""))
    shares = payload.get("shares")
    return (
        f"Вы продали {shares} {_share_word(shares)} {ticker}. "
        f"Сумма продажи составила {payload.get('usd_amount')} ₽. "
        f"Деньги зачислены на основной счёт.\n"
        f"Ваш текущий портфель выглядит так:\n\n"
        f"{format_holdings(snap)}\n\n"
        f"Осталось {ticker}: {left} шт.\n"
        f"Ваш текущий остаток на основном счёте составляет {snap.get('cash_usd')} ₽, "
        f"а резерв в размере {snap.get('reserve_usd')} ₽.\n"
        f"Письмо «Продано» отправлено в почтовый сервис на {email}."
    )


def format_buy(payload: dict, snap: dict, email: str | None = None) -> str:
    extra = ""
    reserved = payload.get("reserved_from_reserve_usd") or 0
    if reserved:
        extra = f" С резерва снято {reserved} ₽."
    mail = ""
    if email:
        mail = f"\nПисьмо «Куплено» отправлено в почтовый сервис на {email}."
    shares = payload.get("shares")
    return (
        f"Вы купили {shares} {_share_word(shares)} {payload.get('ticker')}. "
        f"Стоимость покупки составила {payload.get('usd_amount')} ₽.{extra}\n"
        f"Ваш текущий портфель выглядит так:\n\n"
        f"{format_holdings(snap)}\n\n"
        f"Ваш текущий остаток на основном счёте составляет {snap.get('cash_usd')} ₽, "
        f"а резерв в размере {snap.get('reserve_usd')} ₽."
        f"{mail}"
    )


OTHER_USER_REPLY = (
    "Можно смотреть и менять только свой счёт. Чужие действия, портфель и почта недоступны."
)

_EMAIL_RE = re.compile(r"[a-z0-9._+-]+@[a-z0-9.-]+", re.IGNORECASE)
_AT_NAME = re.compile(r"(?:^|[\s,])у\s+([a-zа-яё][a-zа-яё0-9_.-]{1,40})", re.IGNORECASE)
_NAMED_ACCOUNT = re.compile(
    r"(?:сч[её]т\w*|портфел\w*|деньг\w*|баланс\w*)\s+(?:у\s+)?"
    r"([a-zа-яё][a-zа-яё0-9_.-]{1,40})",
    re.IGNORECASE,
)
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
    for rx in (_AT_NAME, _NAMED_ACCOUNT):
        for match in rx.finditer(folded):
            if _foreign_token(match.group(1), own):
                return True
    for rx in (_PERSON_RE, _TOOL_RE):
        for match in rx.finditer(folded):
            token = match.group(1)
            if "@" in token and token.strip(" \"'«».,:;!?()[]").lower() not in own:
                return True
            if rx is _PERSON_RE and _foreign_token(token, own):
                return True
    return False


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
        f"Переведено {payload.get('transferred')} ₽ с {src} на {dst}.\n"
        f"Основной: {payload.get('cash_usd')} ₽. Резерв: {payload.get('reserve_usd')} ₽."
        f"{mail}"
    )


_CLAIM_RE = re.compile(
    r"вы\s+(купили|продали)|сумма\s+покупки|стоимость\s+покупки|письмо\s+[«\"]куплено|перевед|вызван тул|status\s*=\s*success",
    re.IGNORECASE,
)
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
        if p.get("side") in ("BUY", "SELL", "TRANSFER", "SELL_ALL"):
            return p
        if p.get("status") in ("ERROR", "NEED_RESERVE_CONFIRM", "NEED_BUTTON", "CANCELLED"):
            return p
        if tool in ("execute_buy", "execute_sell"):
            return p
    return None


def _market_reply(used: list[str], payloads: list[dict], snap: dict, asked_rates: list[str] | None = None) -> str | None:
    """Каталог, счёт и портфель — из тула, в рублях."""
    trade = {"execute_buy", "execute_sell", "sell_all", "confirm_reserve_purchase", "deny_reserve_purchase", "transfer_between"}
    if trade.intersection(used):
        return None
    for name in reversed(used):
        if name == "list_instruments":
            payload = next((p for p in payloads if p.get("instruments")), None)
            lines = ["Доступные акции Мосбиржи:"]
            for row in (payload or {}).get("instruments") or []:
                price = row.get("price_rub")
                if price is None:
                    lines.append(
                        f"- {row.get('name')} ({row.get('ticker')}): цена с Мосбиржи сейчас недоступна"
                    )
                else:
                    note = f", {row.get('price_note')}" if row.get("price_note") else ""
                    lines.append(f"- {row.get('name')} ({row.get('ticker')}): {price} ₽{note}")
            return "\n".join(lines) if len(lines) > 1 else None
        if name == "get_portfolio":
            return format_portfolio(snap)
        if name == "get_account":
            return (
                f"Основной счёт: {snap.get('cash_usd', 0)} ₽.\n"
                f"Резерв: {snap.get('reserve_usd', 0)} ₽.\n"
                f"Бумаги:\n{format_holdings(snap)}"
            )
        if name in {"get_currency_rate", "get_rate"}:
            rates = official_rates(asked_rates or []) or format_rates(payloads)
            if rates:
                return rates
        if name == "list_currencies":
            rates = format_rates(payloads)
            if rates:
                return rates
    return None


def compose_chat_reply(
    *,
    model_text: str,
    used: list[str],
    payloads: list[dict],
    snap: dict,
    email: str,
    user_message: str,
    asked_rates: list[str] | None = None,
) -> str:
    """Цифры после тула — из учёта. Решение, какой тул жать, принимает агент."""
    used_set = set(used)
    payload = _pick_trade_payload(payloads)
    pending = snap.get("pending_reserve")

    if payload and payload.get("status") == "ERROR":
        text = payload.get("message") or payload.get("error") or model_text
        if payload.get("have") is not None:
            have = int(payload.get("have") or 0)
            if have <= 0:
                text = (
                    f"Продать {payload.get('ticker')} нельзя: таких акций в портфеле нет.\n"
                    f"{format_portfolio(snap)}"
                )
            else:
                text = (
                    f"Нельзя продать {payload.get('want')} {payload.get('ticker')}: "
                    f"в портфеле {have} шт.\n"
                    f"{format_portfolio(snap)}"
                )
        return text

    if payload and payload.get("status") in ("NEED_RESERVE_CONFIRM", "NEED_BUTTON"):
        pending = snap.get("pending_reserve") or {}
        return str(pending.get("prompt") or payload.get("message") or "Нажмите кнопку подтверждения под сообщением.")

    if payload and payload.get("status") == "SUCCESS" and payload.get("side") == "TRANSFER":
        return format_transfer(payload)
    if payload and payload.get("status") == "ERROR" and payload.get("side") == "TRANSFER":
        return format_transfer(payload)
    if payload and payload.get("status") == "CANCELLED":
        return str(payload.get("message") or "Покупка отменена, резерв не трогали.")

    if payload and payload.get("side") == "SELL_ALL":
        if payload.get("status") != "SUCCESS":
            return str(payload.get("error") or "Продажа не выполнена.")
        lines = [
            f"Вы продали {p.get('shares')} {_share_word(p.get('shares'))} "
            f"{p.get('ticker')} на {p.get('usd_amount')} ₽."
            for p in (payload.get("sales") or [])
        ]
        lines.append(format_portfolio(snap))
        lines.append(f"Письма «Продано» отправлены в почтовый сервис на {email}.")
        return "\n".join(lines)

    sells = [
        p for p in payloads
        if p.get("status") == "SUCCESS" and p.get("side") == "SELL"
    ]
    if len(sells) > 1:
        lines = [
            f"Вы продали {p.get('shares')} {_share_word(p.get('shares'))} "
            f"{p.get('ticker')} на {p.get('usd_amount')} ₽."
            for p in sells
        ]
        lines.append(format_portfolio(snap))
        lines.append(f"Письма «Продано» отправлены в почтовый сервис на {email}.")
        return "\n".join(lines)
    if payload and payload.get("status") == "SUCCESS" and payload.get("side") == "SELL":
        return format_sell(payload, snap, email)
    if payload and payload.get("status") == "SUCCESS" and payload.get("side") == "BUY":
        return format_buy(payload, snap, email)

    if "execute_sell" in used_set:
        row = _fresh_trade(last_trade(email, ("SELL",)))
        if row:
            return format_sell(_trade_payload(row), snap, email)
    if "execute_buy" in used_set:
        if pending and pending.get("prompt"):
            return str(pending["prompt"])
        row = _fresh_trade(last_trade(email, ("BUY",)))
        if row:
            return format_buy(_trade_payload(row), snap, email)

    info = _market_reply(used, payloads, snap, asked_rates)
    if info:
        return info

    if _CLAIM_RE.search(model_text or "") and "execute_buy" not in used_set and "execute_sell" not in used_set and "sell_all" not in used_set and "confirm_reserve_purchase" not in used_set and "transfer_between" not in used_set:
        return (
            "Сделка не записана: в учёте ничего не менялось.\n"
            f"{format_portfolio(snap)}"
        )
    if is_placeholder(model_text) or not (model_text or "").strip():
        return "Повторите, что купить, продать или показать: портфель, счёт или курс валют."
    return model_text
