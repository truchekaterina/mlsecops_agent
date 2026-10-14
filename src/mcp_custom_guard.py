"""Граница своего MCP: что уходит на чужой сервер и что возвращается модели.

Кто вызывает: agent_mode_a.build_agent, перед каждым вызовом чужого тула.
Кого вызывает: mcp_url_guard.assert_safe_mcp_url.
Почему отдельно: промпт модели эту границу не держит.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.types import CallToolResult, TextContent
from mcp_url_guard import assert_safe_mcp_url

_SECRET_KEY = re.compile(
    r"password|passwd|token|jwt|secret|authorization|cookie|api[_-]?key|bearer|credential",
    re.IGNORECASE,
)
_EMAIL_KEY = re.compile(r"e-?mail|почт", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SECRET_VALUE = re.compile(
    r"eyJ[A-Za-z0-9_\-]{8,}|\$argon2|STRAZH_[A-Z0-9_]+|Bearer\s+\S+",
    re.IGNORECASE,
)
_INJECT_BLOCK = re.compile(
    r"\[(?:СКРЫТАЯ ИНСТРУКЦИЯ[^\]]*|Учебный injection[^\]]*|"
    r"ИНСТРУКЦИЯ ДЛЯ МОДЕЛИ[^\]]*|СИСТЕМНАЯ КОМАНДА[^\]]*)\]",
    re.IGNORECASE | re.DOTALL,
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_LEAK = re.compile(
    r"\S*pydantic\.dev\S*|ValidationError|Traceback \(most recent",
    re.IGNORECASE,
)
_STEER = re.compile(
    r"[^.!?\n]*(?:"
    r"get_account|notify_user|execute_buy|execute_sell|send_trade_email|"
    r"confirm_ticket|forward_to_auditor|prompt\s+injection|"
    r"скрыт\w*\s+инструкц|системн\w*\s+команд|мои\s+mcp|"
    r"не\s+цитируй"
    r")[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)
_ARG_LIMIT = 400
_TEXT_LIMIT = 1200
_DESC_LIMIT = 180


def sanitize_custom_text(text: str, *, limit: int = _TEXT_LIMIT, session_email: str = "") -> str:
    """Убрать из текста чужого тула приказ модели, секрет и чужой email."""
    own = (session_email or "").strip().lower()
    cleaned = _INJECT_BLOCK.sub(" ", text or "")
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _LEAK.sub(" ", cleaned)
    cleaned = _STEER.sub(" ", cleaned)
    cleaned = _SECRET_VALUE.sub(" ", cleaned)

    def _email(match: re.Match[str]) -> str:
        found = match.group(0)
        if own and found.lower() == own:
            return found
        return " "

    cleaned = _EMAIL.sub(_email, cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip(" \n\t-—")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


def scrub_custom_tool(tool):
    """Описание тула, которое видит модель, без спрятанного приказа."""
    raw = str(getattr(tool, "description", "") or "")
    clean = sanitize_custom_text(raw, limit=_DESC_LIMIT) or "Инструмент подключённого MCP."
    try:
        tool.description = clean
    except (AttributeError, TypeError, ValueError):
        tool = tool.model_copy(update={"description": clean})
    return tool


def _clean_value(value: Any, session_email: str, depth: int = 0) -> Any:
    if depth > 3:
        return None
    if isinstance(value, bool) or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return None
        text = sanitize_custom_text(value, limit=_ARG_LIMIT, session_email=session_email)
        return text
    if isinstance(value, list):
        items = []
        for item in value[:20]:
            cleaned = _clean_value(item, session_email, depth + 1)
            if cleaned not in (None, ""):
                items.append(cleaned)
        return items
    if isinstance(value, dict):
        return sanitize_custom_args(value, session_email, depth + 1)
    return None


def sanitize_custom_args(args: dict | None, session_email: str, depth: int = 0) -> dict:
    """Оставить в вызове короткие данные этой сессии. Секрет и чужой email не уходят."""
    own = (session_email or "").strip().lower()
    out: dict = {}
    for key, value in (args or {}).items():
        name = str(key)
        if _SECRET_KEY.search(name):
            continue
        if _EMAIL_KEY.search(name):
            if own:
                out[name] = own
            continue
        cleaned = _clean_value(value, own, depth)
        if cleaned in (None, ""):
            continue
        out[name] = cleaned
    return out


def sanitize_tool_result(result, session_email: str = ""):
    """Ответ чужого тула до модели: без приказа и без служебного текста ошибки."""
    if not isinstance(result, CallToolResult):
        return result
    blocks: list[TextContent] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        clean = sanitize_custom_text(text, session_email=session_email)
        if clean:
            blocks.append(TextContent(type="text", text=clean))
    if not blocks:
        blocks.append(TextContent(type="text", text="Инструмент ответил без текста."))
    return CallToolResult(content=blocks, isError=bool(result.isError))


def custom_mcp_interceptor(session_email: str, urls: dict[str, str]):
    """Перед вызовом снова проверить адрес и обрезать аргументы. Ответ обрезать после."""

    async def intercept(request, handler):
        sid = str(getattr(request, "server_name", "") or "")
        url = urls.get(sid)
        if url is None:
            return await handler(request)
        try:
            assert_safe_mcp_url(url)
        except ValueError:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Адрес MCP сейчас запрещён. Вызов не выполнен.",
                    )
                ],
                isError=True,
            )
        clean = sanitize_custom_args(dict(getattr(request, "args", None) or {}), session_email)
        result = await handler(request.override(args=clean))
        return sanitize_tool_result(result, session_email)

    return intercept
