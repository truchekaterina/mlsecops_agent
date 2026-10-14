"""Заглушка MCP — только проверка, что Страж видит чужой сервер.

Запуск (из корня репозитория, в venv где стоит fastmcp из requirements.txt):

  .venv/bin/python examples/mcp_stub/server.py

В Docker Стража заглушка поднимается сама (entrypoint).
В «Мои MCP»:
  Имя:       stub
  URL:       http://127.0.0.1:9100/mcp
  Transport: streamable_http

Этот URL на localhost:9100/mcp — исключение для учебной заглушки.
Остальной localhost по-прежнему запрещён (анти-SSRF).

В чат после подключения: «вызови ping» или «скажи echo привет».
Отключить автозапуск: STRAZH_MCP_STUB=0
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("strazh-mcp-stub")


@mcp.tool
def ping() -> str:
    """Проверка связи с кастомным MCP. Вызови, если просят ping или проверить MCP."""
    return "Проверка связи с кастомным MCP выполнена."


@mcp.tool
def echo(text: str = "привет") -> str:
    """Повтори текст пользователя. Для проверки, что аргументы доходят до MCP."""
    return f"echo: {text}"


if __name__ == "__main__":
    print("MCP-заглушка: http://127.0.0.1:9100/mcp  (в «Мои MCP» Стража)")
    mcp.run(transport="streamable-http", host="127.0.0.1", port=9100)
