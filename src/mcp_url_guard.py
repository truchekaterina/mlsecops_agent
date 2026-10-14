"""Адрес чужого MCP до сохранения. Loopback и metadata запрещены.

Исключение — учебные http://127.0.0.1:9100–9103/mcp.
Кто вызывает: user_mcp.add_mcp.
Почему отдельно: правило адреса не должно смешиваться с вызовом тула.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
}

# Учебные MCP внутри контейнера (entrypoint): stub + три злых легенды.
_TEACHING_MCP_PORTS = frozenset({9100, 9101, 9102, 9103})
_TEACHING_MCP_PATH = "/mcp"


def is_builtin_mcp_stub_url(url: str) -> bool:
    """Разрешённый URL учебных MCP: localhost и только порты 9100–9103, путь /mcp."""
    parsed = urlparse((url or "").strip())
    if (parsed.scheme or "").lower() != "http":
        return False
    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost"}:
        return False
    port = parsed.port if parsed.port is not None else 80
    path = (parsed.path or "").rstrip("/") or "/"
    return port in _TEACHING_MCP_PORTS and path == _TEACHING_MCP_PATH


def builtin_stub_url() -> str:
    return (os.environ.get("STRAZH_MCP_STUB_URL") or "http://127.0.0.1:9100/mcp").strip()


def teaching_mcp_catalog() -> list[dict]:
    """Подсказки для UI: добрый stub + три легенды."""
    return [
        {
            "name": "stub",
            "url": "http://127.0.0.1:9100/mcp",
            "title": "Заглушка (ping / echo)",
        },
        {
            "name": "backup",
            "url": "http://127.0.0.1:9101/mcp",
            "title": "Легенда 1: PortfolioBackupHelper",
        },
        {
            "name": "tax",
            "url": "http://127.0.0.1:9102/mcp",
            "title": "Легенда 2: TaxDeclarationHelper",
        },
        {
            "name": "wiki",
            "url": "http://127.0.0.1:9103/mcp",
            "title": "Легенда 3: StockWiki",
        },
    ]


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return True
    if ip.is_multicast:
        return True
    # Cloud metadata / типичный SSRF
    if str(ip) in {"169.254.169.254", "metadata.google.internal"}:
        return True
    return False


def assert_safe_mcp_url(url: str) -> str:
    """Вернуть нормализованный URL или ValueError с текстом для API."""
    raw = (url or "").strip()
    if not raw or len(raw) > 500:
        raise ValueError("Укажите URL MCP не длиннее 500 символов.")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("URL MCP: только http или https.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("В URL MCP нет хоста.")
    # без завершающего слэша у корня path — меньше редиректов у streamable HTTP
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    netloc = parsed.netloc
    query = f"?{parsed.query}" if parsed.query else ""
    normalized = f"{scheme}://{netloc}{path}{query}"
    if is_builtin_mcp_stub_url(normalized):
        return normalized
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("SSRF: localhost и metadata запрещены.")
    if parsed.username or parsed.password:
        raise ValueError("URL MCP не должен содержать логин и пароль.")
    try:
        as_ip = ipaddress.ip_address(host)
    except ValueError:
        as_ip = None
    if as_ip is not None and _bad_ip(as_ip):
        raise ValueError("SSRF: внутренний IP запрещён.")
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        # Имя могут поднять позже; localhost уже отсечён по hostname.
        infos = []
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _bad_ip(ip):
            raise ValueError("SSRF: хост указывает на внутренний адрес.")
    return normalized
