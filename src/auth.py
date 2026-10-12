"""Учебный вход клиента: учётки в SQLite, JWT на 8 часов.

Адрес письма берём из сессии, не из текста чата.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import ledger
import mail_client
import password_hash
import policy
import store

TTL_HOURS = 8
LOGIN_FAIL = "Неверная почта или пароль."


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    return password_hash.hash_password(password)


def load_store() -> dict:
    """Совместимый снимок: secret + users (для JWT и selfcheck)."""
    store.init()
    with store.db() as conn:
        users = [
            {
                "email": row["email"],
                "name": row["name"],
                "password_sha256": row["password_sha256"],
            }
            for row in conn.execute("SELECT email, name, password_sha256 FROM users ORDER BY email")
        ]
    return {"secret": store.get_secret(), "users": users}


def find_user(store_snap: dict | None, login: str) -> dict | None:
    key = login.strip().lower()
    if not key:
        return None
    store.init()
    with store.db() as conn:
        row = conn.execute(
            "SELECT email, name, password_sha256 FROM users WHERE email=?",
            (key,),
        ).fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "name": row["name"],
        "password_sha256": row["password_sha256"],
    }


def register(name: str, email: str, password: str) -> dict:
    name = policy.clean_name(name)
    email = email.strip().lower()
    if not name or "@" not in email:
        raise ValueError("Нужны имя и почта с @.")
    password_hash.validate_password(password)
    store.init()
    with store.db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
    if exists:
        raise ValueError("На эту почту уже есть учётка. Войдите.")
    try:
        if not mail_client.mailbox_exists(email):
            mail_client.create_mailbox(email, password)
    except mail_client.MailUnavailable as exc:
        raise ValueError(str(exc)) from exc
    except ValueError as exc:
        if "уже заведена" not in str(exc):
            raise
    store.init()
    with store.db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if exists:
            raise ValueError("На эту почту уже есть учётка. Войдите.")
        stored = hash_password(password)
        conn.execute(
            """
            INSERT INTO users(email, name, password_sha256, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (email, name, stored, store.now_iso()),
        )
    ledger.ensure_account(email)
    return {"email": email, "name": name}


def _store_hash(email: str, stored: str) -> None:
    with store.db() as conn:
        conn.execute(
            "UPDATE users SET password_sha256=? WHERE email=?",
            (stored, email),
        )


def check_password(user: dict, password: str) -> bool:
    expected = str(user.get("password_sha256") or "")
    ok = password_hash.password_matches(expected, password)
    if not ok:
        plain = str(user.get("password") or "")
        ok = bool(plain) and hmac.compare_digest(plain, password)
    if not ok:
        return False
    if password_hash.needs_rehash(expected):
        _store_hash(user["email"], hash_password(password))
    return True


def signing_secret() -> str:
    """Ключ подписи только из окружения. Учебная константа процесс не принимает."""
    value = (os.environ.get("STRAZH_JWT_SECRET") or "").strip()
    if len(value) < 16 or value == store.SECRET_DEFAULT:
        raise RuntimeError(
            "Задайте STRAZH_JWT_SECRET длиннее учебной константы. Без этого Страж не подписывает вход."
        )
    return value


def issue_jwt(user: dict, secret: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": user["email"],
                "jti": uuid.uuid4().hex,
                "iat": now,
                "exp": now + TTL_HOURS * 3600,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    sig = hmac.new(secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def decode_jwt(token: str, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("не JWT")
    header, payload, sig = parts
    expect = _b64url(
        hmac.new(secret.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expect, sig):
        raise ValueError("подпись JWT не сошлась")
    data = json.loads(_b64url_decode(payload).decode("utf-8"))
    if int(data.get("exp") or 0) < int(time.time()):
        raise ValueError("JWT истёк")
    email = str(data.get("sub") or "").strip().lower()
    if "@" not in email:
        raise ValueError("в токене нет email")
    if not data.get("jti"):
        raise ValueError("в токене нет номера")
    data["sub"] = email
    return data


def login(login: str, password: str, persist: bool = False) -> dict:
    del persist  # токен остаётся в памяти вызывающего, файл сессии не пишем
    user = find_user(None, login)
    if user is None:
        raise KeyError(LOGIN_FAIL)
    if not check_password(user, password):
        raise PermissionError(LOGIN_FAIL)
    ledger.ensure_account(user["email"])
    token = issue_jwt(user, signing_secret())
    return {
        "email": user["email"],
        "name": user["name"],
        "token": token,
    }


def _user_by_email(email: str) -> dict | None:
    key = email.strip().lower()
    if not key:
        return None
    store.init()
    with store.db() as conn:
        row = conn.execute(
            "SELECT email, name FROM users WHERE email=?",
            (key,),
        ).fetchone()
    if not row:
        return None
    return {"email": row["email"], "name": row["name"]}


def session_from_token(token: str) -> dict:
    claims = decode_jwt(token.strip(), signing_secret())
    jti = str(claims["jti"])
    if store.token_revoked(jti):
        raise ValueError("Сначала войдите")
    user = _user_by_email(str(claims["sub"]))
    if user is None:
        raise ValueError("Сначала войдите")
    return {
        "email": user["email"],
        "name": user["name"],
        "token": token.strip(),
        "jti": jti,
        "exp": int(claims["exp"]),
    }


def require_session(session: dict | None = None) -> dict:
    if not session or not session.get("token"):
        raise PermissionError("нет сессии")
    return session_from_token(str(session["token"]))
