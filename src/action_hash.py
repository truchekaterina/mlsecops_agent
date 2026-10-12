"""Хеш журнала действий. Почта и состав хода в базе не лежат открытым текстом.

Счёт и портфель этим хешем не закрываются: их сервис показывает вошедшему клиенту.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os


def _strazh_secret() -> str:
    import store

    return store.get_secret()


def mail_secret() -> str:
    value = os.environ.get("STRAZH_MAIL_SECRET", "").strip()
    if len(value) < 16:
        raise RuntimeError(
            "Задайте STRAZH_MAIL_SECRET не короче 16 символов. "
            "Почта не подставляет секрет из кода."
        )
    return value


def actor_hash(email: str, *, secret: str | None = None) -> str:
    key = secret if secret is not None else _strazh_secret()
    subject = (email or "").strip().lower().encode("utf-8")
    return hmac.new(key.encode("utf-8"), b"actor:" + subject, hashlib.sha256).hexdigest()


def action_digest(fields: dict, *, secret: str | None = None) -> str:
    key = secret if secret is not None else _strazh_secret()
    body = json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
    return hmac.new(key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
