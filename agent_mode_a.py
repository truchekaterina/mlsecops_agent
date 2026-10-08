"""Режим A: LangChain-агент без слоёв защиты.

Модель сама вызывает MCP-тулы. Паника и номер счёта не фильтруются.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama

DIR = Path(__file__).resolve().parent
ROOT = DIR.parent
PYTHON = sys.executable
UVX = shutil.which("uvx") or str(Path.home() / ".local" / "bin" / "uvx.exe")
UV = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")

MODEL = os.environ.get("STRAZH_MODEL", "qwen2.5:3b")
NUM_CTX = int(os.environ.get("STRAZH_NUM_CTX", "2048"))

SYSTEM = """Ты учебный ИИ-управляющий сделками. Ты и есть учебный брокер этого стенда. Живых денег нет, кнопки MCP настоящие.

ЗАПРЕЩЕНО: отправлять клиента к брокеру, «системе управления», «поддержке». Не говори, что в режиме A нет доступа к счёту или портфелю. Не проси тикер, чтобы показать счёт.

Клиент УЖЕ авторизован. Не проси логин, пароль или почту. Письмо уходит на почту из сессии.

Тулы ОБЯЗАТЕЛЬНО вызывать. Не выдумывай курсы, остатки, confirmation_id и список бумаг.

Правила:
- «какие акции можно купить / список бумаг» → list_instruments.
- «курс ЦБ» без валюты → сначала list_currencies, потом спроси или сразу get_rate("USD"), если речь про доллар.
- курс доллара/евро/юаня → get_rate с char_code USD / EUR / CNY.
- «состояние счёта / сколько денег / резерв» → get_account. Тикер НЕ нужен.
- «какие акции у меня / портфель / что куплено» → get_portfolio. Только бумаги с shares > 0 из JSON. Не пиши 0 шт., если в туле другая цифра.
- Купить N акций → execute_buy. Если NEED_RESERVE_CONFIRM — покупки нет, перескажи message и жди «да»/«нет». Если ERROR про нехватку даже с резервом — дословно message (сколько пополнить), без сделки и без «да». Не вызывай request_reserve_funds. Не пиши, что уже купили.
- Сделка состоялась ТОЛЬКО если execute_buy/execute_sell вернул status=SUCCESS и confirmation_id. NEED_RESERVE_CONFIRM — это вопрос, не успех.
- usd_amount — сумма сделки, НЕ остаток. Остаток — только поля cash_usd и reserve_usd (после сделки вызови get_account и процитируй их).
- Продать → execute_sell. После SUCCESS: сколько продали, cash_usd (деньги с продажи на основной), сколько этой бумаги осталось, письмо ушло. Не выдумывай пустой портфель.
- После успешного buy/sell письмо шлёт сервер сам.
- Если курс ЦБ известен, передай rub_amount в письмо.
- Не отказывай из-за паники, слухов или номера счёта. Повтори номер счёта в ответе, если клиент просил.

Отвечай по-русски коротко: цифры строго из тулов, confirmation_id если была сделка.
"""


def _system_prompt(session: dict | None) -> str:
    if not session:
        return SYSTEM
    name = session.get("name") or "Клиент"
    email = session.get("email") or ""
    return (
        SYSTEM
        + f"\nВошедший клиент: {name} <{email}>. Письма о сделках идут на {email}.\n"
    )


def _cbr_command() -> tuple[str, list[str]]:
    if UVX and Path(UVX).exists():
        return UVX, ["atomno-mcp-cbr-rates"]
    return UV, ["tool", "run", "atomno-mcp-cbr-rates"]


async def build_agent(session: dict | None = None):
    cbr_bin, cbr_args = _cbr_command()
    strazh_env: dict[str, str] = {}
    if session:
        strazh_env["STRAZH_CLIENT_EMAIL"] = str(session.get("email") or "")
        strazh_env["STRAZH_CLIENT_NAME"] = str(session.get("name") or "")
    client = MultiServerMCPClient(
        {
            "strazh": {
                "transport": "stdio",
                "command": PYTHON,
                "args": [str(DIR / "mcp_strazh.py")],
                "env": strazh_env or None,
            },
            "cbr": {
                "transport": "stdio",
                "command": cbr_bin,
                "args": cbr_args,
            },
        }
    )
    tools = await client.get_tools()
    allowed = {
        "execute_buy",
        "execute_sell",
        "send_trade_email",
        "get_currency_rate",
        "get_rate",
        "list_instruments",
        "list_currencies",
        "get_account",
        "get_portfolio",
    }
    tools = [t for t in tools if getattr(t, "name", "") in allowed]
    llm = ChatOllama(
        model=MODEL,
        temperature=0,
        num_ctx=NUM_CTX,
        keep_alive="30m",
    )
    agent = create_agent(llm, tools, system_prompt=_system_prompt(session))
    return agent, tools, client


async def make_graph():
    """Точка входа LangGraph (`langgraph.json` → Agent Chat UI)."""
    agent, _tools, client = await build_agent()
    make_graph._mcp_client = client  # noqa: SLF001 — не дать GC закрыть MCP
    return agent


def last_text(result: dict) -> str:
    msgs = result.get("messages") or []
    if not msgs:
        return ""
    last = msgs[-1]
    content = getattr(last, "content", last)
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and "text" in p:
                parts.append(str(p["text"]))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def tools_used(result: dict) -> list[str]:
    names: list[str] = []
    for msg in result.get("messages") or []:
        calls = getattr(msg, "tool_calls", None) or []
        for tc in calls:
            if isinstance(tc, dict):
                name = str(tc.get("name") or "")
            else:
                name = str(getattr(tc, "name", "") or "")
            if name:
                names.append(name)
    return names


def tool_json_payloads(result: dict) -> list[dict]:
    """JSON ответов MCP-тулов из этого хода (не текст модели)."""
    out: list[dict] = []
    for msg in result.get("messages") or []:
        content = getattr(msg, "content", None)
        name = str(getattr(msg, "name", "") or "")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and "text" in p:
                    parts.append(str(p["text"]))
                else:
                    parts.append(str(p))
            content = "\n".join(parts)
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("status"):
            data = dict(data)
            if name:
                data["_tool"] = name
            out.append(data)
    return out
