"""Терминальный клиент уже запущенного Стража. Своей модели нет.

Кого вызывает: только HTTP /api/chat и /api/reserve/confirm.
Заявку двигает слово «подтвердить», не «да».
Почему отдельно от web_app: это второй вход в тот же конвейер, не второй агент.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from tls_cert import CERT, client_context


def web_base() -> str:
    return os.environ.get("STRAZH_WEB_URL", "https://127.0.0.1:8080").rstrip("/")


def ticket_action(word: str) -> str | None:
    text = (word or "").strip().lower()
    if text == "подтвердить":
        return "confirm"
    if text == "отмена":
        return "deny"
    return None


def ticket_path(action: str) -> str:
    if action == "confirm":
        return "/api/reserve/confirm"
    if action == "deny":
        return "/api/reserve/deny"
    raise ValueError("Нет такой кнопки заявки.")


class Client:
    def __init__(self, base: str | None = None):
        self.base = (base or web_base()).rstrip("/")
        handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
        if self.base.startswith("https://") and CERT.is_file():
            handlers.append(urllib.request.HTTPSHandler(context=client_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def post(self, path: str, body: dict, timeout: float = 30) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"detail": raw or str(exc.reason)}
            if not isinstance(payload, dict):
                payload = {"detail": str(payload)}
            return exc.code, payload
        except urllib.error.URLError as exc:
            return 0, {"detail": f"Страница Стража не отвечает на {self.base}. Сначала запустите сервис. {exc.reason}"}


def detail(payload: dict) -> str:
    value = payload.get("detail")
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("msg") or item))
            else:
                parts.append(str(item))
        return "; ".join(parts) or "Сервис не ответил."
    if value:
        return str(value)
    return str(payload.get("text") or "Сервис не ответил.")


def _fix_stdio() -> None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _ask(prompt: str) -> str:
    return input(prompt).strip()


def authorize(client: Client) -> dict:
    print("Вход в учебный стенд «Страж». Без логина чат закрыт.")
    print("Сначала ящик на https://127.0.0.1:8090, потом регистрация в Страже на ту же почту (пароль можно другой).")
    print()
    for _attempt in range(5):
        ident = _ask("Email или имя: ")
        if not ident:
            print("Нужен email или имя.")
            continue
        password = _ask("Пароль: ")
        if not password:
            print("Регистрация новой учётки.")
            name = _ask("Как к вам обращаться: ") or ident
            email = ident if "@" in ident else _ask("Почта для писем: ")
            password = _ask("Придумайте пароль: ")
            status, payload = client.post(
                "/api/register",
                {"name": name, "email": email, "password": password},
            )
            if status == 200:
                return payload
            print("Не вышло:", detail(payload))
            continue
        status, payload = client.post("/api/login", {"login": ident, "password": password})
        if status == 200:
            return payload
        if status == 401:
            print("Учётки нет или пароль неверный. Зарегистрировать эту почту? (д/н)")
            if _ask("> ").lower() not in ("д", "да", "y", "yes"):
                continue
            name = _ask("Как к вам обращаться: ") or ident
            email = ident if "@" in ident else _ask("Почта для писем: ")
            status, payload = client.post(
                "/api/register",
                {"name": name, "email": email, "password": password},
            )
            if status == 200:
                return payload
            print("Не вышло:", detail(payload))
            continue
        print("Не вышло:", detail(payload))
    raise SystemExit("Вход не удался. Чат не открыт.")


def main() -> None:
    _fix_stdio()
    client = Client()
    session = authorize(client)
    print()
    print(f"Вошли: {session.get('name')} <{session.get('email')}>")
    print("Письма о покупке/продаже — в вашем ящике: https://127.0.0.1:8090")
    print(f"Фразы идут на тот же адрес, что страница: {client.base}/api/chat")
    print("Заявка подтверждается словом «подтвердить», не словом «да».")
    print("Выход: пустая строка или Ctrl+C.")
    print()
    while True:
        try:
            text = input("Клиент: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            break
        status, payload = client.post("/api/chat", {"message": text}, timeout=200)
        if status != 200:
            print("Агент:", detail(payload))
            print()
            continue
        print("Агент:", payload.get("text") or "")
        ticket = payload.get("needs_reserve_confirm")
        if isinstance(ticket, dict) and ticket.get("id"):
            answer = _ask("Кнопка заявки — «подтвердить» или «отмена»: ")
            action = ticket_action(answer)
            if action is None:
                print("Заявка остаётся открытой. Подтверждение — только кнопка.")
            else:
                done_status, done = client.post(ticket_path(action), {"id": str(ticket["id"])})
                print("Агент:", done.get("text") if done_status == 200 else detail(done))
        print()


if __name__ == "__main__":
    main()
