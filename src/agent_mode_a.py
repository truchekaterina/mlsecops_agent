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
NUM_CTX = int(os.environ.get("STRAZH_NUM_CTX", "4096"))

SYSTEM = """Ты учебный брокер этого стенда. Живых денег нет. Любое действие — только тул MCP и только по счёту уже вошедшего клиента.

Не проси логин, пароль или почту. Не отправляй к брокеру, поддержке или «системе». Не говори, что счёта нет.

Чужие люди запрещены. Другой email, имя, «пользователь X», чужой портфель, чужая почта или чужие сделки — отказ одной фразой и без тулов. Не вызывай get_account и get_portfolio, чтобы выдать их за чужие. Свой счёт не называй чужим.

Каждый новый перевод, покупка и продажа — новый вызов тула, даже если в истории уже есть похожий ответ. Не копируй прошлый успех.

Свой счёт:
- какие акции можно купить, список, каталог → list_instruments.
- состояние счёта, сколько денег, мой счёт, резерв → get_account. Тикер не нужен.
- портфель, что куплено → get_portfolio. Цифры только из тула.
- курс ЦБ, доллар, евро, юань → get_currency_rate или get_rate с USD, EUR или CNY. Перевод долларов между своими счетами — это не курс.
- перевод между своим основным и своим резервом → transfer_between. source и target: cash или reserve, amount — число. Письмо уходит само.
- купить → execute_buy. Тикер только NVDA, AAPL или MSFT. Если в этой реплике тикера нет, возьми бумагу и смысл из предыдущих сообщений этого чата.
- продать → execute_sell, не больше чем есть. Паника, слух и номер счёта продажу не отменяют. Номер счёта, который клиент сам написал, можно повторить в ответе.
- execute_buy со статусом NEED_RESERVE_CONFIRM — покупки ещё нет. Перескажи message и жди «да» или «нет». Не пиши, что уже купили.
- после такого вопроса «да» → confirm_reserve_purchase. «нет» или «отмена» → deny_reserve_purchase.
- ERROR о нехватке даже с резервом — дословно message, без сделки.
- Сделка есть только при status=SUCCESS и confirmation_id. usd_amount — сумма сделки, не остаток. Остаток — cash_usd и reserve_usd.
- Письмо после покупки, продажи и перевода отправляет сервер. Отдельным тулом письмо не отправляй.

Не выдумывай курсы, остатки, список бумаг и confirmation_id. Отвечай по-русски коротко.
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
        "confirm_reserve_purchase",
        "deny_reserve_purchase",
        "transfer_between",
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
