"""Список тулов чужого MCP для кнопки «Мои MCP». Модель здесь не вызывается.

Кто вызывает: web_app.api_mcp_tools.
Кого вызывает: langchain MultiServerMCPClient, только чтобы прочитать имена.
Почему отдельно от agent_mode_a: опрос для экрана не должен открывать ход с деньгами.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp_url_guard import assert_safe_mcp_url


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


def _tool_schema(tool) -> str:
    args = getattr(tool, "args", None)
    if isinstance(args, dict) and args:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    return ""


def fingerprint_tools(tools) -> str:
    """Отпечаток имён, описаний и схемы аргументов. Пустой список тоже имеет отпечаток."""
    rows = []
    for tool in tools or []:
        if isinstance(tool, dict):
            name = str(tool.get("name") or "").strip()
            desc = str(tool.get("description") or "")
            schema = tool.get("schema") or ""
            if not isinstance(schema, str):
                schema = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        else:
            name = str(getattr(tool, "name", "") or "").strip()
            desc = str(getattr(tool, "description", "") or "")
            schema = _tool_schema(tool)
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "description": " ".join(desc.split()),
                "schema": schema,
            }
        )
    rows.sort(key=lambda row: row["name"])
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _transport(transport: str) -> str:
    kind = (transport or "streamable_http").strip().lower()
    if kind in {"http", "streamable-http"}:
        kind = "streamable_http"
    return kind


async def _load_remote(url: str, transport: str, timeout: float = 20):
    safe = assert_safe_mcp_url(url)
    client = MultiServerMCPClient(
        {
            "probe": {
                "transport": _transport(transport),
                "url": safe,
            }
        }
    )
    try:
        return await asyncio.wait_for(client.get_tools(), timeout=timeout)
    except Exception as exc:
        raise ValueError(_friendly_probe_error(exc)) from exc


async def tool_fingerprint(
    url: str, transport: str = "streamable_http", timeout: float = 20
) -> str:
    """Отпечаток команд по адресу. Адрес проверяется так же, как при сохранении."""
    tools = await _load_remote(url, transport, timeout=timeout)
    return fingerprint_tools(tools)


async def list_remote_tools(url: str, transport: str = "streamable_http") -> list[dict]:
    """Вернуть [{name, description, example}] или бросить ValueError с понятным текстом."""
    tools = await _load_remote(url, transport)
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
