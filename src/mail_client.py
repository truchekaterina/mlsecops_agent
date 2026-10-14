"""HTTP-клиент к mail_app. Прямого доступа к mail.db нет.

Кто вызывает: auth (ящик уже существует), mcp_strazh (письмо о сделке).
Кого вызывает: rate_limit и action_hash, чтобы подписать служебный запрос.
Почему отдельно от mail_app: вызывающий процесс — Страж или MCP, не почта.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import action_hash
import rate_limit

MAIL_URL = os.environ.get("STRAZH_MAIL_URL", "https://127.0.0.1:8090").rstrip("/")


class MailUnavailable(RuntimeError):
    pass


def _call(method: str, path: str, payload: dict | None = None, *, internal: bool = False) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if internal:
        try:
            headers["X-Mail-Token"] = action_hash.mail_secret()
        except RuntimeError as exc:
            raise MailUnavailable(str(exc)) from exc
    req = urllib.request.Request(MAIL_URL + path, data=data, headers=headers, method=method)
    context = _tls_context()
    try:
        with urllib.request.urlopen(req, timeout=3, context=context) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"detail": raw or exc.reason}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise MailUnavailable(
            "Почтовый сервис недоступен. Откройте его на https://127.0.0.1:8090."
        ) from exc


def _tls_context() -> ssl.SSLContext | None:
    if not MAIL_URL.startswith("https://"):
        return None
    from tls_cert import client_context

    return client_context()


def _detail(body: dict, fallback: str) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return fallback


def mailbox_exists(email: str) -> bool:
    status, body = _call(
        "GET",
        "/api/internal/exists?email=" + urllib.parse.quote(email.strip().lower()),
        internal=True,
    )
    if status != 200:
        raise MailUnavailable(_detail(body, "Почтовый сервис не ответил."))
    return bool(body.get("exists"))


def create_mailbox(email: str, password: str) -> dict:
    status, body = _call(
        "POST",
        "/api/internal/mailbox",
        {"email": email, "password": password},
        internal=True,
    )
    if status == 200:
        return body
    raise ValueError(_detail(body, "Не удалось завести почту."))


def mailbox_password_ok(email: str, password: str) -> bool:
    """Пароль совпал с ящиком. Для регистрации Стража на уже существующую почту."""
    status, body = _call(
        "POST",
        "/api/internal/check",
        {"email": email, "password": password},
        internal=True,
    )
    if status != 200:
        raise MailUnavailable(_detail(body, "Почтовый сервис не ответил."))
    return bool(body.get("ok"))


def deliver(
    *,
    email: str,
    subject: str,
    body: str,
    confirmation_id: str = "",
    side: str = "",
    ticker: str = "",
) -> dict:
    status, payload = _call(
        "POST",
        "/api/internal/deliver",
        {
            "email": email,
            "subject": subject,
            "body": body,
            "confirmation_id": confirmation_id,
            "side": side,
            "ticker": ticker,
        },
        internal=True,
    )
    if status == 200:
        return payload
    if status == 429:
        raise rate_limit.RateLimitExceeded(email)
    if status == 404:
        raise ValueError(_detail(payload, "Такого ящика нет."))
    raise MailUnavailable(_detail(payload, "Почтовый сервис не принял письмо."))


def reset_mail_limit(email: str | None = None) -> None:
    """Сбросить счётчик защиты №4 в процессе почты (selfcheck)."""
    path = "/api/internal/rate_reset"
    if email:
        path += "?email=" + urllib.parse.quote((email or "").strip().lower())
    status, body = _call("POST", path, internal=True)
    if status != 200:
        raise MailUnavailable(_detail(body, "Не удалось сбросить лимит писем."))
