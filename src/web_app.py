"""Веб-сервис Стража: регистрация любого клиента + чат LangChain.

Агент: langchain.agents.create_agent (LangGraph ReAct).
Тулы: MCP (strazh-mcp + ЦБ). Письма — на почту из JWT, не из чата.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OLLAMA_NUM_GPU", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from paths import STATIC  # noqa: E402

from agent_mode_a import build_agent, last_text, rate_codes, tool_json_payloads, tools_used  # noqa: E402
from auth import LOGIN_FAIL, TTL_HOURS, login, register, session_from_token, signing_secret  # noqa: E402
from ledger import snapshot  # noqa: E402
from store import open_turn, revoke_token  # noqa: E402
from mcp_strazh import (  # noqa: E402
    confirm_ticket,
    deny_ticket,
    propose_sell_all,
    propose_transfer,
    set_client,
)
from policy import (  # noqa: E402
    is_no,
    is_sell_all,
    is_yes,
    parse_transfer,
    several_companies,
    story_block,
    utterance_class,
)
from replies import (  # noqa: E402
    OTHER_USER_REPLY,
    asks_about_other_user,
    compose_chat_reply,
    format_buy,
    format_sell,
    format_transfer,
)

signing_secret()

app = FastAPI(title="Страж", version="mode-a")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_agents: dict[str, tuple] = {}
_history: dict[str, list] = {}
_lock = asyncio.Lock()
_user_locks: dict[str, asyncio.Lock] = {}
_user_locks_guard = asyncio.Lock()
_login_fails: dict[str, list[float]] = {}
_chat_hits: dict[str, list[float]] = {}
_LOGIN_LIMIT = 8
_LOGIN_WINDOW = 60.0
_CHAT_LIMIT = 4
_CHAT_WINDOW = 60.0
_AGENT_TIMEOUT_SECONDS = 180


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=8, max_length=120)


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=120)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class TicketIn(BaseModel):
    id: str = Field(min_length=4, max_length=40)


async def _hold_user(email: str) -> asyncio.Lock:
    """Один активный запрос на пользователя: второй ждёт, пока первый не закончится."""
    async with _user_locks_guard:
        lock = _user_locks.get(email)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[email] = lock
        return lock


def _bind_client(session: dict) -> None:
    set_client(session["email"], session["name"])
    os.environ["STRAZH_CLIENT_EMAIL"] = session["email"]
    os.environ["STRAZH_CLIENT_NAME"] = session["name"]


def _fresh_chat(email: str) -> None:
    _history[email] = []


def _pack(session: dict, text: str, tools: list[str]) -> dict:
    acc = snapshot(session["email"])
    return {
        "text": text,
        "tools": tools,
        "client": {"name": session["name"], "email": session["email"]},
        "account": acc,
        "needs_reserve_confirm": acc.get("pending_reserve"),
    }


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _login_locked(ip: str) -> bool:
    now = time.monotonic()
    hits = [stamp for stamp in _login_fails.get(ip, []) if now - stamp < _LOGIN_WINDOW]
    _login_fails[ip] = hits
    return len(hits) >= _LOGIN_LIMIT


def _login_failed(ip: str) -> None:
    now = time.monotonic()
    hits = [stamp for stamp in _login_fails.get(ip, []) if now - stamp < _LOGIN_WINDOW]
    hits.append(now)
    _login_fails[ip] = hits[-_LOGIN_LIMIT:]


def _login_ok(ip: str) -> None:
    _login_fails.pop(ip, None)


def _chat_limited(email: str) -> bool:
    now = time.monotonic()
    key = (email or "").strip().lower()
    hits = [stamp for stamp in _chat_hits.get(key, []) if now - stamp < _CHAT_WINDOW]
    _chat_hits[key] = hits
    return len(hits) >= _CHAT_LIMIT


def _chat_used(email: str) -> None:
    now = time.monotonic()
    key = (email or "").strip().lower()
    hits = [stamp for stamp in _chat_hits.get(key, []) if now - stamp < _CHAT_WINDOW]
    hits.append(now)
    _chat_hits[key] = hits[-_CHAT_LIMIT:]


def _set_session_cookie(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(
        "strazh_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=TTL_HOURS * 3600,
        path="/",
    )
    return resp


def _bearer(request: Request) -> dict:
    token = (request.cookies.get("strazh_token") or "").strip()
    if not token:
        raise HTTPException(401, "Сначала войдите")
    try:
        return session_from_token(token)
    except (ValueError, RuntimeError):
        raise HTTPException(401, "Сначала войдите") from None


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/me")
async def me(request: Request):
    s = _bearer(request)
    return {
        "name": s["name"],
        "email": s["email"],
        "account": snapshot(s["email"]),
    }


@app.get("/api/account")
async def api_account(request: Request):
    s = _bearer(request)
    return snapshot(s["email"])


@app.post("/api/register")
async def api_register(body: RegisterIn):
    try:
        user = register(body.name, body.email, body.password)
        session = login(user["email"], body.password)
        _fresh_chat(session["email"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    resp = JSONResponse(
        {
            "name": session["name"],
            "email": session["email"],
            "account": snapshot(session["email"]),
        }
    )
    return _set_session_cookie(resp, session["token"])


@app.post("/api/login")
async def api_login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    if _login_locked(ip):
        raise HTTPException(429, "Слишком много попыток. Подождите минуту.")
    try:
        session = login(body.login, body.password)
        _fresh_chat(session["email"])
    except (KeyError, PermissionError):
        _login_failed(ip)
        raise HTTPException(401, LOGIN_FAIL) from None
    _login_ok(ip)
    resp = JSONResponse(
        {
            "name": session["name"],
            "email": session["email"],
            "account": snapshot(session["email"]),
        }
    )
    return _set_session_cookie(resp, session["token"])


@app.post("/api/logout")
async def api_logout(request: Request):
    token = (request.cookies.get("strazh_token") or "").strip()
    if token:
        try:
            session = session_from_token(token)
            revoke_token(session["jti"], session["exp"])
        except (ValueError, RuntimeError):
            pass
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("strazh_token", path="/")
    return resp


async def _agent_for(session: dict):
    key = session["email"]
    async with _lock:
        if key not in _agents:
            agent, tools, client = await build_agent(session)
            _agents[key] = (agent, tools, client)
            _history.setdefault(key, [])
        return _agents[key]


def _note(session: dict, text: str) -> dict:
    hist = _history.setdefault(session["email"], [])
    hist.append({"role": "assistant", "content": text})
    return _pack(session, text, [])


def _open_money_turn(session: dict, message: str) -> str | None:
    kind = utterance_class(message)
    if kind == "mixed":
        return "В одном сообщении можно одну покупку или одну продажу."
    open_turn(session["email"], kind, message)
    return None


@app.post("/api/transfer")
async def api_transfer(body: TicketIn, request: Request):
    raise HTTPException(400, "Перевод выполняется кнопкой заявки, не этим запросом.")


@app.post("/api/chat")
async def api_chat(body: ChatIn, request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        return await _chat_locked(session, body)


async def _chat_locked(session: dict, body: ChatIn):
    _bind_client(session)
    hist = _history.setdefault(session["email"], [])
    text = body.message.strip()
    hist.append({"role": "user", "content": text})
    blocked = story_block(text)
    if blocked:
        return _note(session, blocked)
    if asks_about_other_user(text, session["email"], session["name"]):
        return _note(session, OTHER_USER_REPLY)
    pending = snapshot(session["email"]).get("pending_reserve")
    if is_yes(text):
        reply = "Подтверждение — только кнопка «Да, подтвердить» под заявкой."
        if not pending:
            reply = "Открытой заявки нет."
        return _note(session, reply)
    if is_no(text):
        if pending:
            deny_ticket(str(pending.get("id") or ""))
            return _note(session, "Заявка отменена, счёт не менялся.")
        return _note(session, "Открытой заявки нет.")
    if is_sell_all(text):
        payload = propose_sell_all()
        return _note(session, str(payload.get("message") or payload.get("error") or "Заявка не создана."))
    transfer = parse_transfer(text)
    if transfer is not None:
        if transfer.get("error"):
            return _note(session, str(transfer["error"]))
        amount = transfer["amount"]
        if transfer.get("all"):
            book = snapshot(session["email"])
            amount = book["reserve_usd"] if transfer["source"] == "reserve" else book["cash_usd"]
        payload = propose_transfer(transfer["source"], transfer["target"], float(amount or 0))
        return _note(session, str(payload.get("message") or payload.get("error") or "Заявка не создана."))
    several = several_companies(text)
    if several:
        return _note(session, several)
    mixed = _open_money_turn(session, text)
    if mixed:
        return _note(session, mixed)
    if _chat_limited(session["email"]):
        return _note(session, "Слишком много запросов к модели. Подождите минуту.")
    _chat_used(session["email"])
    agent, _tools, _client = await _agent_for(session)
    try:
        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": text}]},
                config={"recursion_limit": 12},
            ),
            timeout=_AGENT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return _note(
            session,
            "Агент не успел завершить запрос за 3 минуты. "
            "Проверьте состояние счёта перед повтором.",
        )
    used = tools_used(result)
    acc = snapshot(session["email"])
    payloads = tool_json_payloads(result)
    answer = compose_chat_reply(
        model_text=last_text(result),
        used=used,
        payloads=payloads,
        snap=acc,
        email=session["email"],
        user_message=text,
        asked_rates=rate_codes(result),
    )
    if len(hist) > 40:
        del hist[:-40]
    return _pack(session, answer, used)


def _finish_ticket(session: dict, payload: dict) -> dict:
    if payload.get("status") == "SUCCESS" and payload.get("side") == "BUY":
        text = format_buy(payload, snapshot(session["email"]), session["email"])
    elif payload.get("status") == "SUCCESS" and payload.get("side") == "SELL":
        text = format_sell(payload, snapshot(session["email"]), session["email"])
    elif payload.get("status") == "SUCCESS" and payload.get("side") == "SELL_ALL":
        text = compose_chat_reply(
            model_text="",
            used=["sell_all"],
            payloads=[payload],
            snap=snapshot(session["email"]),
            email=session["email"],
            user_message="",
        )
    elif payload.get("side") == "TRANSFER":
        text = format_transfer(payload)
    else:
        text = str(payload.get("message") or payload.get("error") or "Заявка не выполнена.")
    if payload.get("status") not in ("SUCCESS", "CANCELLED"):
        raise HTTPException(400, text)
    hist = _history.setdefault(session["email"], [])
    packed = _pack(session, text, [])
    packed["text"] = text
    return packed


@app.post("/api/reserve/confirm")
async def api_reserve_confirm(body: TicketIn, request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        _bind_client(session)
        return _finish_ticket(session, confirm_ticket(body.id))


@app.post("/api/reserve/deny")
async def api_reserve_deny(body: TicketIn, request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        _bind_client(session)
        payload = deny_ticket(body.id)
        if payload.get("status") != "CANCELLED":
            raise HTTPException(400, payload.get("error") or "Заявка не отменена")
        return _note(session, payload.get("message") or "Заявка отменена, счёт не менялся.")


@app.get("/api/health")
async def health():
    return {"ok": True}
