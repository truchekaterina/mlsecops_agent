"""Клиент Стража к отдельному почтовому сервису."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

MAIL_URL = os.environ.get("STRAZH_MAIL_URL", "http://127.0.0.1:8090").rstrip("/")
MAIL_SECRET = os.environ.get("STRAZH_MAIL_SECRET", "strazh-mail-demo")


class MailUnavailable(RuntimeError):
    pass


def _call(method: str, path: str, payload: dict | None = None, *, internal: bool = False) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if internal:
        headers["X-Mail-Token"] = MAIL_SECRET
    req = urllib.request.Request(MAIL_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
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
            "Почтовый сервис недоступен. Откройте его на http://127.0.0.1:8090."
        ) from exc


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
    status, body = _call("POST", "/api/mailbox", {"email": email, "password": password})
    if status == 200:
        return body
    raise ValueError(_detail(body, "Не удалось завести почту."))


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
    if status == 404:
        raise ValueError(_detail(payload, "Такого ящика нет."))
    raise MailUnavailable(_detail(payload, "Почтовый сервис не принял письмо."))
