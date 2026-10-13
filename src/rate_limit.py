"""Счётчик писем Стража: не больше MAIL_LIMIT за MAIL_WINDOW_SEC на email ящика.

Защита №4. Живёт в процессе почты (`mail_app.api_deliver`): stdio mcp_strazh
на каждый агент свой, в его памяти счётчик не общий. Письма сделок
BUY/SELL/TRANSFER/SELL_ALL лимит не едят: кнопка заявки к этому счётчику не относится.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

MAIL_LIMIT = 3
MAIL_WINDOW_SEC = 60

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


class RateLimitExceeded(ValueError):
    """Четвёртое (и далее) письмо в окне. Строка в mail.db ещё не писалась."""

    def __init__(self, email: str = "") -> None:
        super().__init__(
            "Rate limit exceeded: не больше "
            f"{MAIL_LIMIT} писем за {MAIL_WINDOW_SEC} с на этот ящик. "
            "Письмо в mail.db не записано."
        )
        self.email = (email or "").strip().lower()


def check_mail(email: str) -> None:
    """Разрешить письмо или кинуть RateLimitExceeded. Успех сразу занимает слот."""
    key = (email or "").strip().lower()
    if not key:
        raise ValueError("нет email для лимита писем")
    now = time.monotonic()
    with _lock:
        bucket = _hits[key]
        while bucket and now - bucket[0] >= MAIL_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= MAIL_LIMIT:
            raise RateLimitExceeded(key)
        bucket.append(now)


def reset(email: str | None = None) -> None:
    """Для selfcheck. Без аргумента — все ключи."""
    with _lock:
        if email is None:
            _hits.clear()
            return
        _hits.pop((email or "").strip().lower(), None)
