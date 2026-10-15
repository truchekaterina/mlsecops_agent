"""Обвязка хода. Модель только просит тул, исполняет эта проверка.

Кто вызывает: agent_mode_a.build_agent, middleware create_agent.
Кого вызывает: policy, журнал решений.
Почему отдельно от тула: право проверяется до вызова, не внутри функции.
"""

from __future__ import annotations

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

import policy

_READ = frozenset(
    {
        "get_account",
        "get_portfolio",
        "list_instruments",
        "list_currencies",
        "get_rate",
        "get_currency_rate",
        "history_rates",
        "key_rate",
        "inflation",
        "statistics",
        "notify_user",
    }
)
_MONEY = frozenset({"execute_buy", "execute_sell", "sell_all", "transfer_between"})


def _text(content) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content or "")


def current_phrase(state) -> str:
    """Последняя фраза человека в ходе. Пустая — прямой вызов без чата."""
    messages = []
    if isinstance(state, dict):
        messages = state.get("messages") or []
    for msg in reversed(list(messages)):
        if isinstance(msg, dict):
            role = str(msg.get("type") or msg.get("role") or "")
            content = msg.get("content")
        else:
            role = str(getattr(msg, "type", "") or getattr(msg, "role", "") or "")
            content = getattr(msg, "content", "")
        if role in {"human", "user"}:
            return _text(content).strip()
    return ""


def check_tool(
    name: str,
    args: dict | None,
    phrase: str,
    allowed: set[str],
    custom: set[str],
) -> tuple[str, str, dict]:
    """allow или deny. При allow третий элемент — аргументы, которые уйдут в тул.

    Пустая фраза — прямой вызов: имя должно быть из списка этого агента, сверки нет.
    """
    call = dict(args or {})
    tool = (name or "").strip()
    known = tool in allowed or tool in custom
    if not known or (tool in _MONEY and tool not in allowed):
        return "deny", "Этого инструмента нет. Вызов не выполнен.", call
    if not (phrase or "").strip():
        return "allow", "", call
    if tool in custom or tool in _READ:
        return "allow", "", call
    if tool == "sell_all":
        if not policy.is_sell_all(phrase):
            return "deny", "Эта фраза не про продажу всего портфеля. Вызов не выполнен.", call
        return "allow", "", call
    if tool == "transfer_between":
        return _check_transfer(call, phrase)
    if tool == "execute_buy":
        return _check_order(call, phrase, "buy")
    if tool == "execute_sell":
        return _check_order(call, phrase, "sell")
    return "deny", "Этот инструмент для этой фразы не разрешён. Вызов не выполнен.", call


def _check_order(call: dict, phrase: str, kind: str) -> tuple[str, str, dict]:
    if policy.is_sell_all(phrase):
        return "deny", "Эта фраза про продажу всего портфеля, а не одной бумаги. Вызов не выполнен.", call
    if policy.utterance_class(phrase) != kind:
        word = "покупку" if kind == "buy" else "продажу"
        return "deny", f"Эта фраза не про {word}. Вызов не выполнен.", call
    order = policy.parse_order(phrase)
    if order.get("error"):
        return "deny", str(order["error"]), call
    fixed = dict(call)
    fixed["ticker"] = str(order["ticker"])
    shares = order.get("shares")
    if shares is not None:
        fixed["shares"] = int(shares)
    return "allow", "", fixed


def _check_transfer(call: dict, phrase: str) -> tuple[str, str, dict]:
    parsed = policy.parse_transfer(phrase)
    if parsed is None:
        return "deny", "Эта фраза не про перевод между счетами. Вызов не выполнен.", call
    if parsed.get("error"):
        return "deny", str(parsed["error"]), call
    fixed = dict(call)
    fixed["source"] = parsed["source"]
    fixed["target"] = parsed["target"]
    if parsed.get("amount") is not None:
        fixed["amount"] = float(parsed["amount"])
    return "allow", "", fixed


def _denial(request, message: str) -> ToolMessage:
    call = request.tool_call or {}
    payload = json.dumps(
        {"status": "ERROR", "error": message, "message": message},
        ensure_ascii=False,
    )
    return ToolMessage(
        content=payload,
        tool_call_id=str(call.get("id") or ""),
        name=str(call.get("name") or ""),
        status="error",
    )


class ToolGate(AgentMiddleware):
    """Перед каждым тулом: имя из списка этого агента, сделка только по текущей фразе."""

    def __init__(self, email: str, allowed: set[str], custom: set[str]):
        self.email = email or ""
        self.allowed = set(allowed)
        self.custom = set(custom)

    def _prepare(self, request):
        call = request.tool_call or {}
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        phrase = current_phrase(request.state)
        decision, message, fixed = check_tool(name, args, phrase, self.allowed, self.custom)
        self._log(name, decision, message, fixed)
        if decision == "deny":
            return _denial(request, message)
        if fixed != args:
            return request.override(tool_call={**call, "args": fixed})
        return request

    def _log(self, name: str, decision: str, message: str, args: dict) -> None:
        if not self.email:
            return
        try:
            import audit_journal

            audit_journal.record(
                self.email,
                "gate",
                {"tool": name, "decision": decision, "message": message, "args": args},
            )
        except Exception:
            return

    def wrap_tool_call(self, request, handler):
        prepared = self._prepare(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return handler(prepared)

    async def awrap_tool_call(self, request, handler):
        prepared = self._prepare(request)
        if isinstance(prepared, ToolMessage):
            return prepared
        return await handler(prepared)
