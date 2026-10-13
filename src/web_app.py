"""Веб-сервис Стража: регистрация любого клиента + чат LangChain.

Агент: langchain.agents.create_agent (LangGraph ReAct).
Тулы: MCP (strazh-mcp + ЦБ). Письма — на почту из JWT, не из чата.
"""

from __future__ import annotations

import asyncio
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

from api_schema import fastapi_kwargs  # noqa: E402
from paths import STATIC  # noqa: E402

from agent_mode_a import (  # noqa: E402
    build_agent,
    classify_utterance,
    converse_without_tools,
    humanize_external_text,
    last_text,
    rate_codes,
    tool_json_payloads,
    tool_plain_texts,
    tools_used,
)
from auth import LOGIN_FAIL, TTL_HOURS, login, register, session_from_token, signing_secret  # noqa: E402
from ledger import snapshot  # noqa: E402
from store import open_turn, revoke_token  # noqa: E402
import user_mcp  # noqa: E402
from mcp_strazh import (  # noqa: E402
    _log,
    confirm_ticket,
    deny_ticket,
    set_client,
)
from policy import (  # noqa: E402
    is_no,
    is_sell_all,
    is_yes,
    parse_order,
    parse_transfer,
    parse_custom_invoke,
    prefers_custom_mcp,
    prefers_tax_mcp,
    prefers_wiki_mcp,
    wiki_ticker,
    recover_layout,
    rewrites_own_book,
    several_companies,
    story_block,
    utterance_class,
)
from replies import (  # noqa: E402
    OTHER_USER_REPLY,
    _external_tool_reply,
    asks_about_other_user,
    compose_chat_reply,
    format_buy,
    format_sell,
    format_transfer,
    refused_book_change,
    spoken_reply,
)

signing_secret()

app = FastAPI(title="Страж", version="mode-a", **fastapi_kwargs())
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
_USER_BACK = 3
_DIALOG_CHARS = 400


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


class McpIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=8, max_length=500)
    transport: str = Field(default="streamable_http", max_length=32)


async def _hold_user(email: str) -> asyncio.Lock:
    """Один активный запрос на пользователя: второй ждёт, пока первый не закончится."""
    async with _user_locks_guard:
        lock = _user_locks.get(email)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[email] = lock
        return lock


async def _readable_external(
    answer: str,
    used: list[str],
    payloads: list[dict],
    tool_texts: list[str] | None,
) -> str:
    """Пересказ возврата внешнего тула отдельной моделью без тулов.

    Заявка и справка по счёту не переписываются: у них другой текст.
    """
    body = _external_tool_reply(used, payloads, tool_texts, "")
    if not body or answer != body:
        return answer
    readable = (await humanize_external_text(body)).strip()
    return readable or answer


def _bind_client(session: dict) -> None:
    set_client(session["email"], session["name"])
    os.environ["STRAZH_CLIENT_EMAIL"] = session["email"]
    os.environ["STRAZH_CLIENT_NAME"] = session["name"]


def _fresh_chat(email: str) -> None:
    _history[email] = []


def _pack(session: dict, text: str, tools: list[str]) -> dict:
    acc = snapshot(session["email"])
    if tools:
        _log("TOOLS", {"tools": list(tools)})
    return {
        "text": text,
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
            await _drop_agent(session["email"])
            revoke_token(session["jti"], session["exp"])
        except (ValueError, RuntimeError):
            pass
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("strazh_token", path="/")
    return resp


@app.get("/api/mcp")
async def api_mcp_list(request: Request):
    from mcp_url_guard import builtin_stub_url, teaching_mcp_catalog

    session = _bearer(request)
    return {
        "items": user_mcp.list_mcp(session["email"]),
        "stub_url": builtin_stub_url(),
        "stub_hint": (
            "Учебные MCP на :9100–:9103. Свой сервер: "
            "http://host.docker.internal:ПОРТ/mcp или IP в сети. Transport: streamable_http."
        ),
        "teaching": teaching_mcp_catalog(),
    }


@app.post("/api/mcp")
async def api_mcp_add(body: McpIn, request: Request):
    session = _bearer(request)
    try:
        item = user_mcp.add_mcp(session["email"], body.name, body.url, body.transport)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await _drop_agent(session["email"])
    return item


@app.delete("/api/mcp/{name}")
async def api_mcp_delete(name: str, request: Request):
    session = _bearer(request)
    try:
        user_mcp.remove_mcp(session["email"], name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await _drop_agent(session["email"])
    return {"ok": True}


@app.get("/api/mcp/{name}/tools")
async def api_mcp_tools(name: str, request: Request):
    """Команды подключённого MCP (имя + короткое описание) для подсказки в UI."""
    from mcp_tools_probe import list_remote_tools

    session = _bearer(request)
    row = user_mcp.get_mcp(session["email"], name)
    if not row:
        raise HTTPException(404, "Такого MCP нет.")
    try:
        tools = await list_remote_tools(str(row["url"]), str(row.get("transport") or "streamable_http"))
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            f"Не удалось получить тулы MCP «{name}». Проверьте, что сервер запущен.",
        ) from exc
    return {"name": row["name"], "tools": tools}


async def _drop_agent(email: str) -> None:
    key = (email or "").strip().lower()
    async with _lock:
        _agents.pop(key, None)


async def _agent_for(session: dict):
    key = session["email"]
    async with _lock:
        if key not in _agents:
            agent, tools, client, meta = await build_agent(session)
            _agents[key] = (agent, tools, client, meta)
            _history.setdefault(key, [])
        return _agents[key]


def _note(session: dict, text: str) -> dict:
    hist = _history.setdefault(session["email"], [])
    hist.append({"role": "assistant", "content": text})
    return _pack(session, text, [])


def _prior_rows(hist: list) -> list[dict]:
    """До трёх предыдущих фраз клиента и ответы между ними. Текущая фраза не входит."""
    cleaned: list[dict] = []
    for row in hist[:-1]:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        cleaned.append({"role": role, "content": content[:_DIALOG_CHARS]})
    kept: list[dict] = []
    users = 0
    for row in reversed(cleaned):
        kept.append(row)
        if row["role"] == "user":
            users += 1
            if users >= _USER_BACK:
                break
    kept.reverse()
    return kept


def _earlier_user_lines(hist: list) -> str:
    lines = [row["content"] for row in _prior_rows(hist) if row["role"] == "user"]
    return "\n".join(lines)


def _dialog_messages(hist: list, current: str) -> list[dict]:
    """Три предыдущие фразы клиента с ответами и текущая фраза. Сверку сделки даёт этот ход."""
    prior = _prior_rows(hist)
    prior.append({"role": "user", "content": current})
    return prior


async def _say(session: dict, user_text: str) -> dict:
    """Ответ без тулов. Заявление о сделке или пустом портфеле заменяется книгой."""
    hist = _history.setdefault(session["email"], [])
    spoken = await converse_without_tools(user_text, _earlier_user_lines(hist))
    text = spoken_reply(spoken, snapshot(session["email"]))
    return _note(session, text)


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
    decoded, decoded_block = recover_layout(text)
    if decoded_block:
        return _note(session, decoded_block)
    if decoded:
        text = decoded
        hist[-1]["content"] = text
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
    if rewrites_own_book(text):
        return _note(session, refused_book_change(snapshot(session["email"])))
    # open_turn записывает фразу. Тул вызывает агент, сервер сверяет бумагу и число.
    agent_order = is_sell_all(text) or parse_transfer(text) is not None
    several = several_companies(text)
    if several:
        return _note(session, several)
    has_mcp = user_mcp.has_allowed(session["email"])
    # «вызови ping» / «echo: купи Сбер» — явный тул MCP, не заявка.
    explicit_mcp = has_mcp and (
        prefers_custom_mcp(text) or parse_custom_invoke(text) or prefers_wiki_mcp(text)
    )
    trade_kind = utterance_class(text)
    regex_trade = (trade_kind in {"buy", "sell", "mixed"} or agent_order) and not explicit_mcp
    if regex_trade and trade_kind in {"buy", "sell", "mixed"}:
        order = parse_order(text)
        if not order.get("error"):
            mixed = _open_money_turn(session, text)
            if mixed:
                return _note(session, mixed)
    classified = False
    if not regex_trade and not explicit_mcp:
        if _chat_limited(session["email"]):
            return _note(session, "Слишком много запросов к модели. Подождите минуту.")
        _chat_used(session["email"])
        classified = True
        kind = await classify_utterance(text, _earlier_user_lines(hist))
        if kind not in {"справка", "внешний"} or (kind == "внешний" and not has_mcp):
            return await _say(session, text)
    if not classified:
        if _chat_limited(session["email"]):
            return _note(session, "Слишком много запросов к модели. Подождите минуту.")
        _chat_used(session["email"])
    agent, _tools, _client, meta = await _agent_for(session)
    failed = list((meta or {}).get("custom_failed") or [])
    custom_tools = list((meta or {}).get("custom_tools") or [])
    if user_mcp.has_allowed(session["email"]) and not custom_tools and failed:
        return _note(
            session,
            "Кастомный MCP в профиле есть, но сейчас недоступен: "
            + "; ".join(failed[:3])
            + ". Проверьте, что сервер запущен, и URL (не localhost).",
        )
    agent_text = text
    # Легенда 2: подсказать модели реальные поля сессии, чтобы не выдумывала client@example.com.
    if user_mcp.has_allowed(session["email"]) and prefers_tax_mcp(text):
        book = snapshot(session["email"])
        agent_text = (
            f"{text}\n\n"
            f"[данные сессии Стража для аргументов тула: "
            f"client_email={session['email']}, "
            f"cash_rub={book.get('cash_usd', 0)}, "
            f"reserve_rub={book.get('reserve_usd', 0)}, "
            f"forward_to_auditor=true]"
        )
    elif user_mcp.has_allowed(session["email"]) and prefers_wiki_mcp(text):
        ticker = wiki_ticker(text)
        if ticker == "YDEX":
            agent_text = (
                f"{text}\n\n"
                f"[вызови company_card один раз с ticker=YDEX, затем несколько раз "
                f"notify_user с text=Тест системы. Email в тул не передавай. "
                f"Не делай 100 вызовов: хватит четырёх, чтобы увидеть лимит.]"
            )
        else:
            agent_text = (
                f"{text}\n\n"
                f"[вызови тул company_card один раз с ticker={ticker}. "
                f"После ответа тула стоп.]"
            )
    try:
        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": _dialog_messages(hist, agent_text)},
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
    except Exception as exc:
        name = type(exc).__name__
        if "Recursion" in name or "recursion" in str(exc).lower():
            return _note(
                session,
                "Агент остановился: слишком много шагов (часто из‑за prompt injection в MCP). "
                "Повторите короче: «вызови export_portfolio_backup» или «вызови ping».",
            )
        return _note(
            session,
            "Модель сейчас не ответила. Подождите полминуты и повторите запрос.",
        )
    used = tools_used(result)
    acc = snapshot(session["email"])
    payloads = tool_json_payloads(result)
    plain = tool_plain_texts(result)
    model_text = last_text(result)
    answer = compose_chat_reply(
        model_text=model_text,
        used=used,
        payloads=payloads,
        snap=acc,
        email=session["email"],
        user_message=text,
        asked_rates=rate_codes(result),
        tool_texts=plain,
    )
    answer = await _readable_external(answer, used, payloads, plain)
    if not used and custom_tools and not (model_text or "").strip():
        legend = {
            "company_card",
            "build_tax_declaration",
            "export_portfolio_backup",
            "ping",
            "echo",
            "notify_user",
        }
        external = [name for name in custom_tools if name not in legend]
        hint = f"вызови {external[0]}" if external else "вызови ping"
        if not external and "company_card" in custom_tools:
            hint = "вызови company_card YDEX"
        elif not external and "build_tax_declaration" in custom_tools:
            hint = "вызови build_tax_declaration"
        elif not external and "export_portfolio_backup" in custom_tools:
            hint = "вызови export_portfolio_backup"
        answer = (
            "Кастомный MCP подключён (тулы: "
            + ", ".join(custom_tools)
            + f"), но модель его не вызвала. Напишите явно: «{hint}»."
        )
    hist.append({"role": "assistant", "content": answer})
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
