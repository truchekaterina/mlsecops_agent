"""Список тулов чужого MCP для кнопки «Мои MCP». Модель здесь не вызывается.

Кто вызывает: web_app.api_mcp_tools.
Кого вызывает: langchain MultiServerMCPClient, только чтобы прочитать имена.
Почему отдельно от agent_mode_a: опрос для экрана не должен открывать ход с деньгами.
"""

from __future__ import annotations

import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient


def _friendly_probe_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "connect" in low or "111" in text or "refused" in low:
        return (
            "Сервер MCP не отвечает по этому URL "
            "(процесс не запущен или недоступен из контейнера Стража)."
        )
    if "taskgroup" in low or "sub-exception" in low:
        return (
            "Не удалось связаться с MCP по URL. "
            "Проверьте, что сервер запущен (например examples/mcp_stub/server.py)."
        )
    if "timeout" in low:
        return "Таймаут при опросе MCP. Сервер не ответил вовремя."
    return text[:240]


async def list_remote_tools(url: str, transport: str = "streamable_http") -> list[dict]:
    """Вернуть [{name, description, example}] или бросить ValueError с понятным текстом."""
    kind = (transport or "streamable_http").strip().lower()
    if kind in {"http", "streamable-http"}:
        kind = "streamable_http"
    client = MultiServerMCPClient(
        {
            "probe": {
                "transport": kind,
                "url": str(url or ""),
            }
        }
    )
    try:
        tools = await asyncio.wait_for(client.get_tools(), timeout=20)
    except Exception as exc:
        raise ValueError(_friendly_probe_error(exc)) from exc
    out: list[dict] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        desc = str(getattr(tool, "description", "") or "").strip()
        # первая строка description — коротко для подсказки
        short = desc.split("\n", 1)[0].strip() if desc else "Команда кастомного MCP"
        out.append(
            {
                "name": name,
                "description": short,
                "example": f"вызови {name}",
            }
        )
    return out
