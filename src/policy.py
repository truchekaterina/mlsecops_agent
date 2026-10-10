"""Класс реплики до модели. Деньги сверяются с этим классом, не с текстом ответа."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

AMOUNT_NEEDS_CONFIRM = 1000.0
TICKET_SECONDS = 180

_BUY = re.compile(r"куп(?:и|ить|лю|им)", re.IGNORECASE)
_SELL = re.compile(r"прода(?:й|ть|м|дим|дите)", re.IGNORECASE)
_SELL_ALL = re.compile(
    r"прода(?:й|ть|йте)\s+вс[её]|весь\s+портфел|все\s+(?:мои\s+)?акции",
    re.IGNORECASE,
)
_TRANSFER = re.compile(r"перев(?:е|о)д", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
_ACCOUNT_NO = re.compile(r"\d{12,}")
_PANIC = re.compile(r"паник|обвал|слух", re.IGNORECASE)
_YES = re.compile(r"^\s*да\s*[.!]?\s*$", re.IGNORECASE)
_NO = re.compile(r"^\s*(?:нет|отмена)\s*[.!]?\s*$", re.IGNORECASE)
_NAME = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё \-]{0,79}\Z")


def clean_name(name: str) -> str:
    text = "".join(ch for ch in (name or "") if ch >= " " and ch != "\x7f").strip()
    if not text or _NAME.fullmatch(text) is None:
        raise ValueError("Имя — буквы, пробел и дефис, до 80 знаков.")
    return text


def story_block(text: str) -> str | None:
    raw = text or ""
    if _PANIC.search(raw):
        return "Такое сообщение не исполняется. Напишите обычную покупку, продажу или справку."
    if _ACCOUNT_NO.search(raw):
        return "Номер счёта в сообщении не принимается."
    return None


def is_yes(text: str) -> bool:
    return _YES.fullmatch(text or "") is not None


def is_no(text: str) -> bool:
    return _NO.fullmatch(text or "") is not None


def is_sell_all(text: str) -> bool:
    return _SELL_ALL.search(text or "") is not None


_COMPANY_HINTS = (
    ("норникел", "GMKN"),
    ("газпром", "GAZP"),
    ("лукойл", "LKOH"),
    ("роснефт", "ROSN"),
    ("яндекс", "YDEX"),
    ("сбер", "SBER"),
    ("альфа", "UNKNOWN_ALFA"),
    ("alfa", "UNKNOWN_ALFA"),
    ("gmkn", "GMKN"),
    ("gazp", "GAZP"),
    ("lkoh", "LKOH"),
    ("rosn", "ROSN"),
    ("ydex", "YDEX"),
    ("sber", "SBER"),
)


def companies_in(text: str) -> set[str]:
    low = (text or "").lower().replace("ё", "е")
    found: set[str] = set()
    for hint, code in _COMPANY_HINTS:
        if hint in low:
            found.add(code)
    return found


def several_companies(text: str) -> str | None:
    """Отказ, если в одной фразе две компании. Сделку не исполняет."""
    if utterance_class(text) not in {"buy", "sell"}:
        return None
    if len(companies_in(text)) < 2:
        return None
    return (
        "Одним сообщением можно купить или продать акции только одной компании. "
        "Сначала напишите одну заявку, например: купи 3 акции сбера. "
        "Следующим сообщением — другую, например: купи 2 акции норникеля."
    )


def sell_all_of_ticker(text: str) -> str | None:
    """«Мои акции» одной компании без числа — весь этот пакет, не одна штука."""
    if utterance_class(text) != "sell" or is_sell_all(text):
        return None
    if _NUMBER.search(text or ""):
        return None
    low = (text or "").lower().replace("ё", "е")
    if re.search(r"мо[ий]\s+акци", low) is None:
        return None
    found = companies_in(text)
    if len(found) != 1:
        return None
    return next(iter(found))


def utterance_class(text: str) -> str:
    """buy, sell, mixed или info. Продажа всего сюда не входит."""
    raw = text or ""
    if is_sell_all(raw):
        return "info"
    buy = _BUY.search(raw) is not None
    sell = _SELL.search(raw) is not None
    if buy and sell:
        return "mixed"
    if buy:
        return "buy"
    if sell:
        return "sell"
    return "info"


def parse_transfer(text: str) -> dict | None:
    raw = text or ""
    if _TRANSFER.search(raw) is None:
        return None
    low = raw.lower().replace("ё", "е")
    if any(part in low for part in ("с резерв", "из резерв", "на основн", "в основн")):
        source, target = "reserve", "cash"
    elif any(part in low for part in ("в резерв", "на резерв")):
        source, target = "cash", "reserve"
    else:
        return {"error": "Напишите, откуда и куда: с резерва на основной или с основного в резерв."}
    if "все" in low:
        return {"source": source, "target": target, "all": True, "amount": None}
    match = _NUMBER.search(low)
    if not match:
        return {"error": "Напишите сумму перевода."}
    amount = round(float(match.group(1).replace(",", ".")), 2)
    if amount <= 0:
        return {"error": "Сумма перевода должна быть больше нуля."}
    return {"source": source, "target": target, "all": False, "amount": amount}


def expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=TICKET_SECONDS)).isoformat()


def ticket_alive(expires_at_value: str) -> bool:
    raw = str(expires_at_value or "")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= datetime.now(timezone.utc)
