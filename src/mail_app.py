"""Почтовый сервис: свои ящики и только свои письма.

Страж ходит сюда по HTTP. Общего ящика нет.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import mail_store
import password_hash
from paths import STATIC

SECRET = os.environ.get("STRAZH_MAIL_SECRET", "strazh-mail-demo")
TTL_HOURS = 8

app = FastAPI(title="Почта Стража", version="1")


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
    token = request.headers.get("x-mail-token") or ""
    if not hmac.compare_digest(token, SECRET):
        raise HTTPException(403, "нет доступа к почтовому сервису")


@app.get("/")
async def index():
    return FileResponse(STATIC / "mail.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "mail", "db": str(mail_store.DB_PATH)}


@app.post("/api/mailbox")
async def api_mailbox(body: MailboxIn):
    try:
        return mail_store.create_mailbox(body.email, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/login")
async def api_login(body: LoginIn):
    box = mail_store.find_mailbox(body.email)
    if box is None:
        raise HTTPException(401, "Такого ящика нет. Сначала заведите почту.")
    if not password_hash.password_matches(box["password_sha256"], body.password):
        raise HTTPException(401, "Неверный пароль ящика")
    if password_hash.needs_rehash(box["password_sha256"]):
        with mail_store.db() as conn:
            conn.execute(
                "UPDATE mailboxes SET password_sha256=? WHERE email=?",
                (password_hash.hash_password(body.password), box["email"]),
            )
    token = _issue(box["email"])
    resp = JSONResponse({"email": box["email"], "token": token})
    resp.set_cookie("mail_token", token, httponly=True, samesite="lax", max_age=TTL_HOURS * 3600)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mail_token")
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


@app.get("/api/internal/exists")
async def api_exists(email: str, request: Request):
    _internal(request)
    return {"email": email.strip().lower(), "exists": mail_store.mailbox_exists(email)}


@app.post("/api/internal/deliver")
async def api_deliver(body: DeliverIn, request: Request):
    _internal(request)
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
