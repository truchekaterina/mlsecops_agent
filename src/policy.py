"""Класс реплики до модели. Деньги сверяются с этим классом, не с текстом ответа."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

AMOUNT_NEEDS_CONFIRM = 0.0
TICKET_SECONDS = 180

_BUY = re.compile(r"куп(?:и|ить|лю|им)|(?<!\w)капи(?!\w)", re.IGNORECASE)
_SELL = re.compile(r"прода(?:й|ть|м|дим|дите)", re.IGNORECASE)
_SELL_ALL = re.compile(
    r"прода(?:й|ть|йте)\s+вс[её]|весь\s+портфел|все\s+(?:мои\s+)?акции",
    re.IGNORECASE,
)
_TRANSFER = re.compile(r"перев(?:е|о)д", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
_ACCOUNT_NO = re.compile(r"\d{12,}")
_PANIC = re.compile(r"паник|обвал|слух", re.IGNORECASE)
_LEAK_ASK = re.compile(
    r"забудь\s+(?:правила|инструкц)"
    r"|(?:покажи(?:те)?|выведи|повтори|раскрой)\s+(?:мне\s+)?"
    r"(?:свою\s+)?(?:системн\w*|инструкц\w*|промпт|правила\s+системы)"
    r"|system\s+prompt"
    r"|ignore\s+(?:previous|all)\s+instructions?",
    re.IGNORECASE,
)
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
    if _LEAK_ASK.search(raw):
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


_SHARE_WORDS = (
    ("десять", 10),
    ("девять", 9),
    ("восемь", 8),
    ("семь", 7),
    ("шесть", 6),
    ("пять", 5),
    ("четыре", 4),
    ("три", 3),
    ("две", 2),
    ("два", 2),
    ("одну", 1),
    ("одна", 1),
    ("один", 1),
    ("одно", 1),
)

_NAME_PAPER = (
    "Назовите одну бумагу из каталога: Сбер, Газпром, Лукойл, "
    "Норникель, Роснефть или Яндекс."
)


_MONEY_SPAN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:руб(?:л\w*)?|₽)|"
    r"(?:руб(?:л\w*)?|₽)\s*\d+(?:[.,]\d+)?|"
    r"(?:по\s+)?цен[аеуыи]\s+\d+(?:[.,]\d+)?|"
    r"сто(?:ит|имост\w*)\s+\d+(?:[.,]\d+)?|"
    r"(?:за|на)\s+\d+(?:[.,]\d+)?\s*(?:руб(?:л\w*)?|₽)",
    re.IGNORECASE,
)
_QTY_AFTER = re.compile(r"(\d+)\s*(?:шт(?:ук\w*)?|акци\w*)", re.IGNORECASE)
_QTY_BEFORE = re.compile(r"(?:шт(?:ук\w*)?|акци\w*)\s*(\d+)", re.IGNORECASE)
_QTY_VERB = re.compile(
    r"(?:куп\w*|капи|прода\w*)\s+(?:мне\s+|пожалуйста\s+)*(\d+)(?!\s*(?:руб|₽))",
    re.IGNORECASE,
)


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _money_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MONEY_SPAN.finditer(text or "")]


def _qty_digit_hits(low: str) -> list[tuple[int, int]]:
    """Позиции и числа, которые выглядят как штуки, а не как рубли."""
    money = _money_spans(low)
    hits: list[tuple[int, int]] = []
    for rx in (_QTY_AFTER, _QTY_BEFORE, _QTY_VERB):
        for match in rx.finditer(low):
            raw = next((g for g in match.groups() if g), None)
            if raw is None:
                continue
            start = match.start(match.lastindex or 1)
            if _inside(start, money):
                continue
            value = int(raw)
            if value >= 1:
                hits.append((start, value))
    return hits


def _share_count(text: str) -> tuple[int | None, str | None]:
    """Число акций из фразы. Сумма в рублях и «цена 150» — не штуки.

    Нет числа штук — одна акция. Дробь и ноль — отказ.
    """
    low = (text or "").lower().replace("ё", "е")
    money = _money_spans(low)
    qty_hits = _qty_digit_hits(low)
    if qty_hits:
        return qty_hits[-1][1], None
    word_at: tuple[int, int] | None = None
    for word, count in _SHARE_WORDS:
        found = re.search(rf"(?<!\w){word}(?!\w)", low)
        if found and (word_at is None or found.start() < word_at[0]):
            word_at = (found.start(), count)
    if word_at:
        return word_at[1], None
    leftover: list[tuple[int, int]] = []
    for match in _NUMBER.finditer(low):
        if _inside(match.start(), money):
            continue
        raw = match.group(1).replace(",", ".")
        value = float(raw)
        if value != int(value) or int(value) < 1:
            return None, "Нужно целое число акций, не меньше 1."
        leftover.append((match.start(), int(value)))
    if leftover:
        return leftover[0][1], None
    if re.search(
        r"(?<!\w)(?:одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|"
        r"шестнадцать|семнадцать|восемнадцать|девятнадцать|двадцать|тридцать|"
        r"сорок|пятьдесят|сто)(?!\w)",
        low,
    ):
        return None, "Напишите число акций цифрой."
    return 1, None


def parse_order(text: str) -> dict:
    """Тикер и число из фразы человека. Бумагу выбирает не модель.

    shares is None — «мои акции» одной компании, то есть весь этот пакет.
    error — фразу нельзя отдавать в покупку или продажу.
    """
    if utterance_class(text) not in {"buy", "sell"}:
        return {"error": "Это не покупка и не продажа."}
    found = companies_in(text)
    known = {code for code in found if code != "UNKNOWN_ALFA"}
    if not known:
        return {"error": _NAME_PAPER}
    if len(known) != 1:
        return {"error": _NAME_PAPER}
    ticker = next(iter(known))
    if sell_all_of_ticker(text) == ticker:
        return {"ticker": ticker, "shares": None}
    shares, error = _share_count(text)
    if error:
        return {"error": error}
    return {"ticker": ticker, "shares": shares}


def order_conflict(phrase: str, ticker: str, shares: int) -> str | None:
    """Отказ, если тул назвал не ту бумагу или не то число, что в реплике.

    Пустая реплика — прямой вызов тула без чата, сверять не с чем.
    """
    aligned, error = apply_phrase_qty(phrase, ticker, shares)
    if error:
        return error
    if int(aligned) != int(shares):
        return f"В сообщении названо {aligned} шт., а не {shares}. Книга не менялась."
    return None


def apply_phrase_qty(phrase: str, ticker: str, shares: int) -> tuple[int, str | None]:
    """Тикер сверяем с фразой; число штук берём из фразы, не из цены в рублях.

    Пустая реплика — аргументы тула как есть (прямой вызов).
    """
    if not (phrase or "").strip():
        return int(shares), None
    order = parse_order(phrase)
    if order.get("error"):
        return int(shares), str(order["error"])
    wanted = str(order.get("ticker") or "")
    if wanted != ticker:
        return int(shares), (
            f"В сообщении названа бумага {wanted}, а не {ticker}. Книга не менялась."
        )
    expected = order.get("shares")
    if expected is None:
        return int(shares), None
    return int(expected), None


def sell_all_of_ticker(text: str) -> str | None:
    """«Мои акции» одной компании без числа — весь этот пакет, не одна штука."""
    if utterance_class(text) != "sell" or is_sell_all(text):
        return None
    if _qty_digit_hits((text or "").lower().replace("ё", "е")):
        return None
    low = (text or "").lower().replace("ё", "е")
    for word, _count in _SHARE_WORDS:
        if re.search(rf"(?<!\w){word}(?!\w)", low):
            return None
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


_CATALOG = re.compile(
    r"каталог|"
    r"доступн\w*\s+(?:для\s+покупки\s+)?акци|"
    r"акци\w*\s+доступн|"
    r"бумаг\w*\s+доступн|"
    r"доступн\w*\s+для\s+покупки|"
    r"(?:^|\s)список(?:\s*$|\s+(?:акци|бумаг|доступн))|"
    r"какие\s+(?:акции|бумаг\w*)\s+(?:я\s+)?(?:можно|могу|доступн)|"
    r"какие\s+(?:акции|бумаг\w*)\s+можно|"
    r"(?:можно|могу)\s+купить|"
    r"что\s+(?:можно|доступно)\s+купить",
    re.IGNORECASE,
)
_PORTFOLIO = re.compile(
    r"портфел|что\s+куплено|"
    r"какие\s+акции\s+(?:у\s+меня|куплен)|"
    r"какие\s+куплен|"
    r"список\s+моих\s+акци|"
    r"мо(?:и|их|й)\s+акци|"
    r"мо(?:и|их)\s+бумаг|"
    r"что\s+у\s+меня\s+(?:в\s+портфел|куплен)",
    re.IGNORECASE,
)
# Глагол «купить» в вопросе про каталог — не заявка на покупку.
_CATALOG_BUY_STRIP = re.compile(
    r"(?:можно|могу)\s+купить|доступн\w*\s+для\s+покупки|для\s+покупки",
    re.IGNORECASE,
)
_RATES = re.compile(r"курс|валют|доллар|евро|юан", re.IGNORECASE)
_ACCOUNT = re.compile(r"сч[её]т|баланс|сколько\s+денег|резерв", re.IGNORECASE)
_QUOTE = re.compile(
    r"сколько\s+сто(?:ит|ят)|"
    r"поч[её]м|"
    r"какая\s+цен[аы]|"
    r"цен[аыу]\s+(?:акци|бумаг|сбер|газпром|лукойл|яндекс|норникел|роснефт)|"
    r"котировк|"
    r"стоимость\s+(?:акци|бумаг|сбер|газпром)|"
    r"сколько\s+(?:акци|сбер|газпром)",
    re.IGNORECASE,
)

# Домен брокера: не шаблон фразы, а что речь про бумаги, свой счёт или курс.
# Какой тул звать, решает модель, не этот список.
_BROKER_DOMAIN = re.compile(
    r"акци|бумаг|тикер|котир|бирж|портфел|"
    r"сч[её]т|баланс|резерв|остат|"
    r"куп(?:и|ить|лю|им)|(?<!\w)капи(?!\w)|прода|"
    r"курс|валют|доллар|евро|юан|"
    r"каталог|лот(?:ах|ы|ов|\b)|"
    r"денег|деньг|средств|"
    r"на\s+руках|"
    r"сколько\s+у\s+меня|"
    r"руб(?:л|\b)|₽|"
    r"сбер|газпром|лукойл|яндекс|норникел|роснефт|"
    r"sber|gazp|lkoh|ydex|gmkn|rosn|"
    r"перевод",
    re.IGNORECASE,
)

OFF_TOPIC_REPLY = (
    "Это сообщение не отправляется модели. "
    "Напишите про свой счёт, портфель, акции или курс валют."
)

# Фразы к кастомному MCP, даже если внутри есть «купи Сбер».
_CUSTOM_MCP_CALL = re.compile(
    r"^\s*(?:echo|ping|вызови|вызов\w*|проверь\s+mcp|проверка\s+mcp|"
    r"бэкап|бекап|резервн\w*\s+копи|экспорт\s+портфел|"
    r"налогов|декларац|company_card|карточка|"
    r"справочник\s+инвестор|вики|notify_user|тест\s+системы)\b",
    re.IGNORECASE,
)


def prefers_custom_mcp(text: str) -> bool:
    """Сообщение явно про тул кастомного MCP, не про сделку Стража."""
    return bool(_CUSTOM_MCP_CALL.search(text or ""))


_TAX_MCP = re.compile(
    r"build_tax_declaration|налогов|декларац",
    re.IGNORECASE,
)


def prefers_tax_mcp(text: str) -> bool:
    """Фраза про учебную налоговую декларацию (легенда 2)."""
    return bool(_TAX_MCP.search(text or ""))


_WIKI_MCP = re.compile(
    r"company_card|карточка|справочник\s+инвестор|вики",
    re.IGNORECASE,
)


def prefers_wiki_mcp(text: str) -> bool:
    """Фраза про учебную карточку компании (легенда 3)."""
    return bool(_WIKI_MCP.search(text or ""))


def wiki_ticker(text: str) -> str:
    """Тикер для company_card: Яндекс → YDEX, иначе первая компания или SBER."""
    found = companies_in(text)
    if "YDEX" in found:
        return "YDEX"
    for code in ("SBER", "GAZP", "LKOH", "GMKN", "ROSN"):
        if code in found:
            return code
    return "SBER"


_INVOKE_RE = re.compile(
    r"^\s*(?:вызови|вызов)\s+([A-Za-z_][\w]*)(?:\s+(.+))?\s*$",
    re.IGNORECASE,
)


def parse_custom_invoke(text: str) -> tuple[str, str] | None:
    """«вызови company_card YDEX» → (company_card, YDEX)."""
    match = _INVOKE_RE.match(text or "")
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def in_broker_domain(text: str) -> bool:
    """Фраза про бумаги, свой счёт, портфель или курс — не про погоду и не «кто ты»."""
    raw = (text or "").lower().replace("ё", "е")
    if not raw.strip():
        return False
    if _BROKER_DOMAIN.search(raw) or companies_in(text):
        return True
    if utterance_class(text) in {"buy", "sell", "mixed"}:
        return True
    if _CATALOG.search(raw) or _QUOTE.search(raw) or _PORTFOLIO.search(raw):
        return True
    if _RATES.search(raw) or _ACCOUNT.search(raw) or _TRANSFER.search(raw):
        return True
    return False


def model_intent(text: str) -> str | None:
    """Пускать к модели, если речь про брокерский контур. Тул выбирает агент.

    buy/sell/mixed — чтобы тул сверил бумагу и штуки с фразой. Остальное
    («сколько у меня», «почём лукойл», «что на руках») — broker: смысл тула
    не зашит в регулярке. Чужой смысл вроде «кто ты» или арифметики — None.
    """
    raw = (text or "").lower().replace("ё", "е")
    if _CATALOG.search(raw):
        without_catalog_verb = _CATALOG_BUY_STRIP.sub(" ", raw)
        if utterance_class(without_catalog_verb) not in {"buy", "sell"}:
            return "catalog"
    kind = utterance_class(text)
    if kind in {"buy", "sell", "mixed"}:
        return kind
    if _QUOTE.search(raw) or (
        companies_in(text) and re.search(r"сто(?:ит|ят)|цен[аыуе]|котировк", raw)
    ):
        return "catalog"
    if _PORTFOLIO.search(raw):
        return "portfolio"
    if _RATES.search(raw):
        return "rates"
    if _ACCOUNT.search(raw):
        return "account"
    if in_broker_domain(text):
        return "broker"
    return None


_EN_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.",
    "йцукенгшщзхъфывапролджэячсмитьбю",
)


def en_layout_to_ru(text: str) -> str | None:
    """Русский текст, набранный в английской раскладке. Уже русский текст не трогает."""
    raw = text or ""
    if re.search(r"[А-Яа-яЁё]", raw) or not re.search(r"[A-Za-z]", raw):
        return None
    mapped = raw.lower().translate(_EN_TO_RU)
    if not re.search(r"[а-яё]", mapped):
        return None
    return mapped


def recover_layout(text: str) -> tuple[str | None, str | None]:
    """Вернуть русскую фразу, если после раскладки это известное намерение.

    Первый элемент — текст для дальнейших проверок. Второй — отказ story_block,
    если в русской фразе запретный оборот.
    """
    mapped = en_layout_to_ru(text)
    if not mapped:
        return None, None
    blocked = story_block(mapped)
    if blocked:
        return None, blocked
    if (
        model_intent(mapped)
        or parse_transfer(mapped)
        or is_sell_all(mapped)
        or is_yes(mapped)
        or is_no(mapped)
    ):
        return mapped, None
    return None, None


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
