"""Учебный вход клиента: учётки в SQLite, JWT на 8 часов.

Адрес письма берём из сессии, не из текста чата.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import ledger
import store

DIR = Path(__file__).resolve().parent
SESSION = DIR / "session.json"
TTL_HOURS = 8


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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
            "SELECT email, name, password_sha256 FROM users WHERE email=? OR lower(name)=?",
            (key, key),
        ).fetchone()
    if not row:
        return None
    return {
        "email": row["email"],
        "name": row["name"],
        "password_sha256": row["password_sha256"],
    }


def register(name: str, email: str, password: str) -> dict:
    name = name.strip()
    email = email.strip().lower()
    if not name or "@" not in email or len(password) < 3:
        raise ValueError("Нужны имя, почта с @ и пароль от 3 символов.")
    store.init()
    with store.db() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if exists:
            raise ValueError("Такая почта уже есть. Войдите.")
        conn.execute(
            """
            INSERT INTO users(email, name, password_sha256, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (email, name, hash_password(password), store.now_iso()),
        )
    ledger.ensure_account(email)
    return {"email": email, "name": name, "password_sha256": hash_password(password)}


def check_password(user: dict, password: str) -> bool:
    expected = str(user.get("password_sha256") or "")
    if expected:
        return hmac.compare_digest(expected, hash_password(password))
    plain = str(user.get("password") or "")
    return hmac.compare_digest(plain, password)


def issue_jwt(user: dict, secret: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": user["email"],
                "email": user["email"],
                "name": user["name"],
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
    if not data.get("email"):
        raise ValueError("в токене нет email")
    return data


def login(login: str, password: str, persist: bool = False) -> dict:
    user = find_user(None, login)
    if user is None:
        raise KeyError("нет учётки")
    if not check_password(user, password):
        raise PermissionError("неверный пароль")
    ledger.ensure_account(user["email"])
    token = issue_jwt(user, store.get_secret())
    session = {
        "email": user["email"],
        "name": user["name"],
        "token": token,
    }
    if persist:
        write_session(session)
    return session


def session_from_token(token: str) -> dict:
    claims = decode_jwt(token.strip(), store.get_secret())
    return {
        "email": str(claims["email"]),
        "name": str(claims.get("name") or "Клиент"),
        "token": token.strip(),
    }


def write_session(session: dict) -> None:
    SESSION.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_session() -> dict | None:
    if not SESSION.exists():
        return None
    data = json.loads(SESSION.read_text(encoding="utf-8"))
    if not data.get("email") or not data.get("token"):
        return None
    return data


def require_session(session: dict | None = None) -> dict:
    sess = session or read_session()
    if not sess:
        raise PermissionError("нет сессии")
    claims = decode_jwt(str(sess["token"]), store.get_secret())
    sess["email"] = claims["email"]
    sess["name"] = str(claims.get("name") or sess.get("name") or "Клиент")
    return sess
