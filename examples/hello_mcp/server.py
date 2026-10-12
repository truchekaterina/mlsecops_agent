"""Учебный «добрый» MCP для этапа 1: подключение своего сервера к Стражу.

Запуск из корня репозитория (venv с fastmcp):

  python examples/hello_mcp/server.py

URL для формы «Мои MCP» (со стороны хоста, не 127.0.0.1 из Docker):

  http://<IP-вашего-ПК>:9100/mcp
  transport: streamable_http

В Docker-контейнере Стража localhost — это сам контейнер, поэтому
указывайте IP машины в LAN (или host.docker.internal, если резолвится
не в loopback после проверки).
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("hello-strazh")


@mcp.tool
def ping() -> str:
    """Проверка связи: кастомный MCP отвечает pong."""
    return "Проверка связи с кастомным MCP выполнена."


@mcp.tool
def add_numbers(a: float, b: float) -> str:
    """Сложить два числа. Не трогает счёт и почту Стража."""
    return f"{a} + {b} = {a + b}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=9100)
