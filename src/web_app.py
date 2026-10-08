"""Веб-сервис Стража: регистрация любого клиента + чат LangChain.

Агент: langchain.agents.create_agent (LangGraph ReAct).
Тулы: MCP (strazh-mcp + ЦБ). Письма — на почту из JWT, не из чата.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OLLAMA_NUM_GPU", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from paths import STATIC  # noqa: E402

from agent_mode_a import build_agent, last_text, tool_json_payloads, tools_used  # noqa: E402
from auth import login, register, session_from_token  # noqa: E402
from cbr_rates import format_rates  # noqa: E402
from ledger import snapshot  # noqa: E402
from store import DB_PATH  # noqa: E402
from mcp_strazh import (  # noqa: E402
    confirm_reserve_purchase,
    deny_reserve_purchase,
    set_client,
    transfer_between,
)
from replies import (  # noqa: E402
    OTHER_USER_REPLY,
    apply_trade_order,
    asks_about_other_user,
    compose_chat_reply,
    format_account,
    format_buy,
    format_instruments,
    format_portfolio,
    format_transfer,
    is_account_question,
    is_catalog_question,
    is_fx_question,
    is_portfolio_question,
    parse_transfer,
    resolve_followup_order,
)

app = FastAPI(title="Страж", version="mode-a")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_agents: dict[str, tuple] = {}
_history: dict[str, list] = {}
_lock = asyncio.Lock()
_user_locks: dict[str, asyncio.Lock] = {}
_user_locks_guard = asyncio.Lock()


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=3, max_length=120)


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=120)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class TransferIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    amount: float = Field(gt=0, le=1_000_000_000)
    source: str = Field(alias="from", min_length=2, max_length=40)
    target: str = Field(alias="to", min_length=2, max_length=40)


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


def _is_yes(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return t in {"да", "yes", "y", "ок", "ok", "согласен", "согласна", "подтверждаю"}


def _is_no(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return t in {"нет", "no", "n", "отмена", "не согласен", "не согласна"}


def _pack(session: dict, text: str, tools: list[str]) -> dict:
    acc = snapshot(session["email"])
    return {
        "text": text,
        "tools": tools,
        "client": {"name": session["name"], "email": session["email"]},
        "account": acc,
        "needs_reserve_confirm": acc.get("pending_reserve"),
    }


def _bearer(request: Request) -> dict:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    else:
        token = (request.cookies.get("strazh_token") or "").strip()
    if not token:
        raise HTTPException(401, "Сначала войдите")
    try:
        return session_from_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e)) from e


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
            "token": session["token"],
            "account": snapshot(session["email"]),
        }
    )
    resp.set_cookie("strazh_token", session["token"], httponly=True, samesite="lax", max_age=8 * 3600)
    return resp


@app.post("/api/login")
async def api_login(body: LoginIn):
    try:
        session = login(body.login, body.password)
        _fresh_chat(session["email"])
    except KeyError:
        raise HTTPException(401, "Нет учётки. Зарегистрируйтесь.") from None
    except PermissionError:
        raise HTTPException(401, "Неверный пароль") from None
    resp = JSONResponse(
        {
            "name": session["name"],
            "email": session["email"],
            "token": session["token"],
            "account": snapshot(session["email"]),
        }
    )
    resp.set_cookie("strazh_token", session["token"], httponly=True, samesite="lax", max_age=8 * 3600)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("strazh_token")
    return resp


async def _agent_for(session: dict):
    key = session["email"]
    async with _lock:
        if key not in _agents:
            agent, tools, client = await build_agent(session)
            _agents[key] = (agent, tools, client)
            _history.setdefault(key, [])
        return _agents[key]


@app.post("/api/transfer")
async def api_transfer(body: TransferIn, request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        _bind_client(session)
        payload = transfer_between(body.source, body.target, body.amount)
        if payload.get("status") != "SUCCESS":
            raise HTTPException(400, payload.get("error") or "Перевод не выполнен")
        payload["text"] = format_transfer(payload)
        payload["account"] = snapshot(session["email"])
        return payload


@app.post("/api/chat")
async def api_chat(body: ChatIn, request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        return await _chat_locked(session, body)


async def _chat_locked(session: dict, body: ChatIn):
    _bind_client(session)
    hist = _history.setdefault(session["email"], [])
    if asks_about_other_user(body.message, session["email"], session["name"]):
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": OTHER_USER_REPLY})
        return _pack(session, OTHER_USER_REPLY, [])
    pending = snapshot(session["email"]).get("pending_reserve")
    if pending and _is_yes(body.message):
        payload = json.loads(confirm_reserve_purchase())
        if payload.get("status") == "SUCCESS":
            text = format_buy(payload, snapshot(session["email"]), session["email"])
        else:
            text = payload.get("message") or payload.get("error") or str(payload)
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["confirm_reserve_purchase"])
    if pending and _is_no(body.message):
        payload = json.loads(deny_reserve_purchase())
        text = payload.get("message") or "Покупка отменена, резерв не трогали."
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["deny_reserve_purchase"])

    snap = snapshot(session["email"])
    if is_portfolio_question(body.message):
        text = format_portfolio(snap)
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["get_portfolio"])
    if is_account_question(body.message):
        text = format_account(snap)
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["get_account"])
    if is_catalog_question(body.message):
        text = format_instruments()
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["list_instruments"])
    move = parse_transfer(body.message)
    if move:
        payload = transfer_between(move["source"], move["target"], move["amount"])
        text = format_transfer(payload)
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["transfer_between"])
    if is_fx_question(body.message):
        try:
            text = format_rates(body.message)
        except Exception as exc:
            text = f"Курс ЦБ сейчас недоступен: {exc}"
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, ["cbr_xml_daily"])
    order = resolve_followup_order(body.message, session["email"])
    if order:
        text, used = apply_trade_order(order, session["email"], session["name"])
        hist.append({"role": "user", "content": body.message})
        hist.append({"role": "assistant", "content": text})
        return _pack(session, text, used)

    agent, _tools, _client = await _agent_for(session)
    hist.append({"role": "user", "content": body.message})
    result = await agent.ainvoke(
        {"messages": list(hist)},
        config={"recursion_limit": 25},
    )
    used = tools_used(result)
    acc = snapshot(session["email"])
    text = compose_chat_reply(
        model_text=last_text(result),
        used=used,
        payloads=tool_json_payloads(result),
        snap=acc,
        email=session["email"],
        user_message=body.message,
    )
    hist.append({"role": "assistant", "content": text})
    if len(hist) > 40:
        del hist[:-40]
    return _pack(session, text, used)


@app.post("/api/reserve/confirm")
async def api_reserve_confirm(request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        return await _reserve_confirm_locked(session)


async def _reserve_confirm_locked(session: dict):
    _bind_client(session)
    payload = json.loads(confirm_reserve_purchase())
    if payload.get("status") == "SUCCESS":
        text = format_buy(payload, snapshot(session["email"]), session["email"])
    else:
        text = payload.get("error") or str(payload)
        raise HTTPException(400, text)
    return _pack(session, text, ["confirm_reserve_purchase"])


@app.post("/api/reserve/deny")
async def api_reserve_deny(request: Request):
    session = _bearer(request)
    async with await _hold_user(session["email"]):
        return await _reserve_deny_locked(session)


async def _reserve_deny_locked(session: dict):
    _bind_client(session)
    payload = json.loads(deny_reserve_purchase())
    return _pack(session, payload.get("message") or "Отменено", ["deny_reserve_purchase"])


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "stack": "LangChain create_agent + LangGraph + MCP",
        "model": os.environ.get("STRAZH_MODEL", "qwen2.5:3b"),
        "users_online_agents": len(_agents),
        "db": str(DB_PATH),
    }
