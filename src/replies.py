"""Тексты клиенту из счёта, не из выдумки модели."""

from __future__ import annotations

import json
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


_FX_NAMES = {"USD": "доллар США", "EUR": "евро", "CNY": "китайский юань"}
NO_TOOL_REPLY = "Повторите, что купить, продать или показать: портфель, счёт или курс валют."

_TRUSTED_REPLY_TOOLS = frozenset(
    {
        "execute_buy",
        "execute_sell",
        "get_currency_rate",
        "get_rate",
        "list_instruments",
        "list_currencies",
        "get_account",
        "get_portfolio",
        "sell_all",
        "confirm_reserve_purchase",
        "deny_reserve_purchase",
        "transfer_between",
        "send_trade_email",
        "notify_user",
    }
)


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
_WORD_RE = re.compile(r"[a-zа-яё][a-zа-яё0-9_.-]{2,40}", re.IGNORECASE)
_AT_NAME = re.compile(r"(?:^|[\s,])у\s+([a-zа-яё][a-zа-яё0-9_.-]{1,40})", re.IGNORECASE)
_FOR_NAME = re.compile(
    r"(?:для|про|вместо|от\s+имени)\s+([a-zа-яё][a-zа-яё0-9_.-]{1,40})",
    re.IGNORECASE,
)
_PERSON_RE = re.compile(
    r"(?:пользовател\w*|клиент\w*|юзер\w*|\buser\b|аккаунт\w*|профил\w*)\s+[\"'«]*([a-zа-яё0-9_.@+-]+)",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(
    r"(?:get_portfolio|get_account|execute_buy|execute_sell|send_trade_email)\s+[\"']?([a-zа-яё0-9_.@+-]+)",
    re.IGNORECASE,
)
_ACCOUNT_OF = re.compile(
    r"(?:счёт|счет|портфел\w*|почт\w*|баланс\w*|остат\w*|данн\w*|профил\w*|кабинет\w*|логин\w*)"
    r"\s+(?:у\s+)?([a-zа-яё][a-zа-яё0-9_.-]{1,40})",
    re.IGNORECASE,
)
_PERSONAL_MARKERS = (
    "счёт",
    "счет",
    "портфел",
    "почт",
    "баланс",
    "остат",
    "деньг",
    "клиент",
    "пользовател",
    "аккаун",
    "профил",
    "кабинет",
    "данн",
    "имя",
    "фамил",
    "телефон",
    "паспорт",
    "логин",
    "email",
    "e-mail",
)
_OTHER_USER_PHRASES = (
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
    "чужого клиент",
    "чужой клиент",
    "чужую почт",
    "чужой портфел",
    "чужой счёт",
    "чужой счет",
)
_NOT_A_PERSON = {
    "я", "мой", "моя", "мои", "моё", "мое", "мне", "меня", "себя", "свой", "своя", "свои",
    "моего", "моей", "моём", "моем", "этот", "эта", "это", "том", "этого", "той", "текущий",
    "текущего", "текущем", "какой", "какое", "какие", "какая", "сколько", "можно", "нужно",
    "покажи", "покажите", "купи", "купите", "продай", "продайте", "переведи", "переведите",
    "состояние", "состоянии", "остаток", "остатке", "и", "в", "на", "с", "по", "для", "от",
    "из", "не", "ли", "или", "ещё", "еще", "все", "всех", "весь", "вся",
    "счёте", "счете", "счёта", "счета", "покупке", "покупки", "продаже", "продажи",
    "сделке", "сделки", "акциях", "акции", "акцию", "акция", "акций", "портфеле", "портфеля",
    "резерве", "резерва", "резерв", "резервный", "резервного", "резервном", "резервные",
    "основной", "основного", "основном", "основные", "курсе", "курса", "долларе", "доллара",
    "деньгах", "деньги", "денег", "рубль", "рубля", "рублей", "руб",
    "бумагах", "бумаги", "бумагу", "бумага", "действиях", "действия", "информации", "информацию",
    "пользователя", "пользователе", "клиента", "клиенте",
    "счёт", "счет", "портфель", "баланс", "почту", "почта", "почты", "список", "тикер",
    "цена", "цены", "котировка", "котировки", "кнопку", "кнопка", "заявку", "заявка",
    "подтвердить", "отмена", "чат", "модель", "агент", "промпт", "инструкцию", "инструкция",
    "notify", "notify_user", "тест", "системы", "вызови", "вызов", "тул", "mcp",
    "правила", "системный", "системную", "nvda", "aapl", "msft", "nvidia", "apple", "microsoft",
    "доллар", "доллара", "долларе", "евро", "юань", "юаня", "юаней", "usd", "eur", "cny",
    "валюту", "валюта", "валюте", "курс", "курсе",
    "покупку", "продажу", "сделку", "один", "одна", "одно", "одну", "два", "две", "три",
    "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
    "со", "сумму", "сумма", "суммы", "спиши", "спишите", "списать", "начисли", "начислить",
    "проверяй", "проверь", "проверять", "достоверная", "достоверный", "достоверно",
    "бирже", "биржа", "биржи", "штук", "штука", "штуки", "этих", "этой", "этим",
    "подтверждения", "подтверждение", "подтвердить", "без", "стоимости", "стоимость",
    "начислите", "начислить", "стоит", "стоят", "стояла",
}


def _market_tokens() -> set[str]:
    tokens = set(_NOT_A_PERSON)
    try:
        from moex_quotes import CATALOG
        from policy import _COMPANY_HINTS
    except Exception:
        return tokens
    for ticker, title in CATALOG.items():
        low = ticker.lower()
        tokens.add(low)
        tokens.add(title.lower())
        tokens.add(title.lower().replace("ё", "е"))
    for hint, code in _COMPANY_HINTS:
        tokens.add(hint)
        tokens.add(str(code).lower())
    return tokens


_MARKET_ALLOW = _market_tokens()


def _clean_token(token: str) -> str:
    return token.strip(" \"'«».,:;!?()[]").lower().replace("ё", "е")


def _name_stem(word: str) -> str:
    text = _clean_token(word)
    while len(text) > 3 and text[-1] in "аиыуеояёйюь":
        text = text[:-1]
    return text


def _own_tokens(email: str, name: str) -> set[str]:
    email = (email or "").strip().lower()
    name = (name or "").strip().lower().replace("ё", "е")
    tokens = set(_MARKET_ALLOW)
    if email:
        tokens.add(email)
        local, _, domain = email.partition("@")
        if local:
            tokens.add(local)
            tokens.add(_name_stem(local))
        if domain:
            tokens.add(domain)
            for part in domain.split("."):
                if part:
                    tokens.add(part)
    if name:
        tokens.add(name)
        for part in name.split():
            if not part:
                continue
            tokens.add(part)
            tokens.add(_name_stem(part))
    return tokens


def _known_word(token: str, own: set[str]) -> bool:
    token = _clean_token(token)
    if not token or token.isdigit():
        return True
    if token in own:
        return True
    if "@" in token:
        return token in own
    stem = _name_stem(token)
    if stem in own:
        return True
    for item in own:
        if len(item) < 3:
            continue
        if token.startswith(item) or item.startswith(token):
            return True
        if len(stem) >= 3 and (stem.startswith(item) or item.startswith(stem) or stem == _name_stem(item)):
            return True
    return False


def _foreign_token(token: str, own: set[str]) -> bool:
    token = _clean_token(token)
    if not token or token.isdigit():
        return False
    return not _known_word(token, own)


def _has_personal_marker(folded: str) -> bool:
    text = folded.replace("ё", "е")
    return any(marker in text for marker in _PERSONAL_MARKERS)


def asks_about_other_user(text: str, email: str, name: str = "") -> bool:
    """Просьба узнать или изменить чужие персональные данные, не свои."""
    raw = text or ""
    if not raw.strip():
        return False
    own = _own_tokens(email, name)
    folded = raw.lower()
    for found in _EMAIL_RE.findall(folded):
        if found not in own:
            return True
    if any(piece in folded for piece in _OTHER_USER_PHRASES):
        return True
    for rx in (_AT_NAME, _FOR_NAME, _ACCOUNT_OF, _PERSON_RE, _TOOL_RE):
        for match in rx.finditer(folded):
            if _foreign_token(match.group(1), own):
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
    tool_texts: list[str] | None = None,
) -> str:
    """Цифры после тула — из учёта. Решение, какой тул жать, принимает агент."""
    used_set = set(used)
    payload = _pick_trade_payload(payloads)
    pending = snap.get("pending_reserve")

    if payload and payload.get("status") == "ERROR":
        text = payload.get("message") or payload.get("error") or "Операция не выполнена."
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

    if "notify_user" in used_set:
        notify = payload if isinstance(payload, dict) else None
        if notify is None:
            for raw in reversed(tool_texts or []):
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    if (raw or "").strip():
                        return str(raw)
                    continue
                if isinstance(row, dict):
                    notify = row
                    break
        if notify:
            if notify.get("status") == "ERROR":
                return str(
                    notify.get("message")
                    or notify.get("error")
                    or "Rate limit exceeded"
                )
            if notify.get("status") == "SUCCESS":
                return str(notify.get("message") or "Письмо записано в ваш ящик.")
        return "Уведомление не записано. Повторите «вызови notify_user Тест системы»."

    info = _market_reply(used, payloads, snap, asked_rates)
    if info:
        return info

    if _CLAIM_RE.search(model_text or "") and "execute_buy" not in used_set and "execute_sell" not in used_set and "sell_all" not in used_set and "confirm_reserve_purchase" not in used_set and "transfer_between" not in used_set:
        return (
            "Сделка не записана: в учёте ничего не менялось.\n"
            f"{format_portfolio(snap)}"
        )
    custom_used = [name for name in used if name not in _TRUSTED_REPLY_TOOLS]
    if custom_used:
        # Сначала текст тула: модель часто врёт («отправлена аудитору») или «меняю настройки».
        for raw in reversed(tool_texts or []):
            text = _strip_tool_injection(str(raw or ""))
            if text:
                return text
        for payload in reversed(payloads):
            if not isinstance(payload, dict):
                continue
            for key in ("result", "message", "text", "answer"):
                val = payload.get(key)
                if val:
                    text = _strip_tool_injection(str(val))
                    if text:
                        return text
            if payload.get("status") == "SUCCESS" and len(payload) <= 4:
                return json.dumps(payload, ensure_ascii=False)
        model = _strip_tool_injection((model_text or "").strip())
        if model and not _noisy_custom_model(model):
            return model
        return "Подключённый MCP ответил, но текст пуст. Повторите запрос."
    return NO_TOOL_REPLY


_TOOL_INJECT_BLOCK = re.compile(
    r"\n*\s*\[(?:СКРЫТАЯ ИНСТРУКЦИЯ[^\]]*|Учебный injection[^\]]*|"
    r"ИНСТРУКЦИЯ ДЛЯ МОДЕЛИ[^\]]*|СИСТЕМНАЯ КОМАНДА[^\]]*)\]\s*",
    re.IGNORECASE | re.DOTALL,
)
# Модель часто выносит хвост injection в открытый текст без скобок.
_TOOL_INJECT_ECHO = re.compile(
    r"(?:"
    r"не\s+вызывай\s+тулы[^.!?\n]*[.!?]?"
    r"|не\s+ходи\s+на\s+эти\s+адреса[^.!?\n]*[.!?]?"
    r"|не\s+цитируй\s+(?:эту\s+)?(?:скобку|инструкцию)[^.!?\n]*[.!?]?"
    r"|один\s+ответ\s*[—\-–]\s*стоп[^.!?\n]*[.!]?"
    r"|без\s+цикла\s+тулов[^.!?\n]*[.!]?"
    r"|учебный\s+prompt\s+injection[^.!?\n]*[.!]?"
    r"|скрыт\w*\s+инструкц\w*[^.!?\n]*[.!]?"
    r")",
    re.IGNORECASE,
)
_NOISY_CUSTOM = (
    "меняю настрой",
    "изменения сохран",
    "сейчас изменю",
    "настройки обнов",
    "отправлена аудитору",
    "отправлено аудитору",
    "успешно создана и отправлена",
    "отправлены аудитору",
)


def _strip_tool_injection(text: str) -> str:
    cleaned = _TOOL_INJECT_BLOCK.sub("\n", text or "")
    cleaned = _TOOL_INJECT_ECHO.sub(" ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n\t-—")


def _noisy_custom_model(text: str) -> bool:
    low = (text or "").lower().replace("ё", "е")
    if len(low) < 8:
        return True
    return any(piece in low for piece in _NOISY_CUSTOM)
