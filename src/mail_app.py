"""HTTP почты, отдельный процесс от Стража. Порт 8090, в Docker снаружи 38490.

Ящик создаёт сам человек. Страж ящик не заводит и в mail.db не лезет:
он шлёт письмо через mail_client.
Кого вызывает: mail_store, password_hash, rate_limit, action_hash.
Почему отдельно: секрет почты и база писем не должны быть в процессе с моделью.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import action_hash
import mail_store
import password_hash
import rate_limit
from api_schema import fastapi_kwargs
from paths import STATIC

SECRET = action_hash.mail_secret()
TTL_HOURS = 8
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_WINDOW = 60.0
_LOGIN_LIMIT = 8

app = FastAPI(title="Почта Стража", version="1", **fastapi_kwargs())


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _reg_locked(ip: str) -> bool:
    now = time.monotonic()
    hits = [stamp for stamp in _LOGIN_FAILS.get(ip, []) if now - stamp < _LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = hits
    return len(hits) >= _LOGIN_LIMIT


def _reg_hit(ip: str) -> None:
    now = time.monotonic()
    hits = [stamp for stamp in _LOGIN_FAILS.get(ip, []) if now - stamp < _LOGIN_WINDOW]
    hits.append(now)
    _LOGIN_FAILS[ip] = hits[-_LOGIN_LIMIT:]


class MailboxIn(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=8, max_length=120)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=120)


class DeliverIn(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    confirmation_id: str = ""
    side: str = ""
    ticker: str = ""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _issue(email: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {"email": email, "exp": now + TTL_HOURS * 3600},
            separators=(",", ":"),
        ).encode()
    )
    sig = hmac.new(SECRET.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def _email_from_token(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("не токен")
    header, payload, sig = parts
    expect = _b64url(
        hmac.new(SECRET.encode("utf-8"), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expect, sig):
        raise ValueError("подпись не сошлась")
    data = json.loads(_b64url_decode(payload).decode("utf-8"))
    if int(data.get("exp") or 0) < int(time.time()):
        raise ValueError("сессия истекла")
    email = str(data.get("email") or "").strip().lower()
    if not email:
        raise ValueError("в сессии нет почты")
    return email


def _session(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    else:
        token = (request.cookies.get("mail_token") or "").strip()
    if not token:
        raise HTTPException(401, "Сначала войдите в ящик")
    try:
        return _email_from_token(token)
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc


def _internal(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1"}:
        raise HTTPException(404, "Нет такого адреса")
    token = request.headers.get("x-mail-token") or ""
    if not hmac.compare_digest(token, SECRET):
        raise HTTPException(403, "нет доступа к почтовому сервису")


@app.get("/")
async def index():
    return FileResponse(STATIC / "mail.html")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/register")
async def api_register(body: MailboxIn, request: Request):
    """Открытая регистрация ящика. Учётка Стража создаётся отдельно."""
    ip = _client_ip(request)
    if _reg_locked(ip):
        raise HTTPException(429, "Слишком много попыток. Подождите минуту.")
    try:
        created = mail_store.create_mailbox(body.email, body.password)
    except ValueError as exc:
        _reg_hit(ip)
        raise HTTPException(400, str(exc)) from exc
    token = _issue(created["email"])
    resp = JSONResponse({"email": created["email"], "token": token})
    resp.set_cookie(
        "mail_token",
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=TTL_HOURS * 3600,
    )
    return resp


@app.post("/api/login")
async def api_login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    if _reg_locked(ip):
        raise HTTPException(429, "Слишком много попыток. Подождите минуту.")
    box = mail_store.find_mailbox(body.email)
    if box is None:
        _reg_hit(ip)
        raise HTTPException(401, "Такого ящика нет. Сначала зарегистрируйте почту.")
    if not password_hash.password_matches(box["password_sha256"], body.password):
        _reg_hit(ip)
        raise HTTPException(401, "Неверный пароль ящика")
    if password_hash.needs_rehash(box["password_sha256"]):
        with mail_store.db() as conn:
            conn.execute(
                "UPDATE mailboxes SET password_sha256=? WHERE email=?",
                (password_hash.hash_password(body.password), box["email"]),
            )
    token = _issue(box["email"])
    resp = JSONResponse({"email": box["email"], "token": token})
    resp.set_cookie(
        "mail_token",
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=TTL_HOURS * 3600,
    )
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mail_token", secure=True, httponly=True, samesite="lax")
    return resp


@app.get("/api/me")
async def api_me(request: Request):
    email = _session(request)
    return {"email": email}


@app.get("/api/inbox")
async def api_inbox(request: Request):
    email = _session(request)
    return {"email": email, "messages": mail_store.list_messages(email, limit=30)}


@app.get("/api/inbox/{message_id}")
async def api_inbox_one(message_id: int, request: Request):
    email = _session(request)
    row = mail_store.get_message(email, message_id)
    if not row:
        raise HTTPException(404, "Письмо не найдено")
    return row


@app.post("/api/internal/mailbox")
async def api_internal_mailbox(body: MailboxIn, request: Request):
    """Служебное создание ящика. Обычная регистрация — POST /api/register."""
    _internal(request)
    try:
        return mail_store.create_mailbox(body.email, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/internal/exists")
async def api_exists(email: str, request: Request):
    _internal(request)
    return {"email": email.strip().lower(), "exists": mail_store.mailbox_exists(email)}


@app.post("/api/internal/check")
async def api_internal_check(body: LoginIn, request: Request):
    """Страж проверяет пароль ящика при своей регистрации."""
    _internal(request)
    box = mail_store.find_mailbox(body.email)
    if box is None:
        return {"ok": False, "exists": False}
    ok = password_hash.password_matches(box["password_sha256"], body.password)
    return {"ok": ok, "exists": True}


_TRADE_SIDES = frozenset({"BUY", "SELL", "TRANSFER", "SELL_ALL"})


@app.post("/api/internal/rate_reset")
async def api_rate_reset(request: Request):
    """Selfcheck: обнулить счётчик писем. Без X-Mail-Token — 403."""
    _internal(request)
    email = (request.query_params.get("email") or "").strip().lower()
    rate_limit.reset(email or None)
    return {"ok": True}


@app.post("/api/internal/deliver")
async def api_deliver(body: DeliverIn, request: Request):
    _internal(request)
    if not (body.confirmation_id or "").strip():
        raise HTTPException(400, "нужен номер сделки")
    side_u = (body.side or "").upper().strip()
    if side_u not in _TRADE_SIDES:
        try:
            rate_limit.check_mail(body.email)
        except rate_limit.RateLimitExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
    try:
        row = mail_store.deliver(
            email=body.email,
            subject=body.subject,
            body=body.body,
            confirmation_id=body.confirmation_id,
            side=body.side,
            ticker=body.ticker,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": row["id"], "email": row["email"], "subject": row["subject"]}
