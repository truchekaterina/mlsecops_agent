"""Хеш пароля Argon2. Открытый пароль в базу не пишется.

Кто вызывает: auth и mail_store. Вход Стража и пароль ящика — разные вызовы,
хеш может не совпадать.
Почему отдельно: алгоритм пароля не должен дублироваться в двух базах.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_HASHER = PasswordHasher()
POLICY_MSG = (
    "Пароль — не короче 8 знаков, с заглавной буквой, строчной буквой и символом."
)


def validate_password(password: str) -> None:
    text = password or ""
    has_upper = any(ch.isupper() for ch in text)
    has_lower = any(ch.islower() for ch in text)
    has_symbol = any(not ch.isalnum() and not ch.isspace() for ch in text)
    if len(text) < 8 or not has_upper or not has_lower or not has_symbol:
        raise ValueError(POLICY_MSG)


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def password_matches(stored: str, password: str) -> bool:
    expected = stored or ""
    if not expected.startswith("$argon2"):
        return False
    try:
        return _HASHER.verify(expected, password)
    except VerifyMismatchError:
        return False


def needs_rehash(stored: str) -> bool:
    expected = stored or ""
    if not expected.startswith("$argon2"):
        return False
    return _HASHER.check_needs_rehash(expected)
