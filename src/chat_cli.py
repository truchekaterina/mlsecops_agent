"""Чат в терминале. Сначала вход, потом сделки. Режим A, без слоёв защиты."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("OLLAMA_NUM_GPU", "0")

import mail_client  # noqa: E402
from auth import login, register, require_session  # noqa: E402
from agent_mode_a import build_agent, history_text, last_text, tool_json_payloads, tools_used  # noqa: E402
from ledger import snapshot  # noqa: E402
from mcp_strazh import set_client  # noqa: E402
from replies import OTHER_USER_REPLY, asks_about_other_user, compose_chat_reply  # noqa: E402

DIR = Path(__file__).resolve().parent


def _fix_stdio() -> None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _ask(prompt: str, secret: bool = False) -> str:
    # Учебный стенд: пароль виден в терминале, иначе getpass в Windows «молчит».
    return input(prompt).strip()


def _prepare_mailbox(email: str) -> None:
    """Регистрация только на ящик отдельного почтового сервиса."""
    try:
        if mail_client.mailbox_exists(email):
            return
    except mail_client.MailUnavailable as exc:
        raise ValueError(str(exc)) from exc
    print(f"Ящика {email} нет. Сначала заведите его в почтовом сервисе.")
    if _ask("Завести эту почту? (д/н) ").lower() not in ("д", "да", "y", "yes"):
        raise ValueError("Без заведённой почты регистрация недоступна.")
    box_password = _ask("Пароль ящика: ", secret=True)
    try:
        mail_client.create_mailbox(email, box_password)
    except mail_client.MailUnavailable as exc:
        raise ValueError(str(exc)) from exc
    print(f"Почта {email} заведена. Теперь можно регистрироваться в Страже.")


def authorize() -> dict:
    print("Вход в учебный стенд «Страж». Без логина чат закрыт.")
    print("Письма о сделках приходят в почтовый сервис http://127.0.0.1:8090")
    print("Готовые входы: client@demo / demo   или   katya@demo / katya")
    print("Новая учётка: сначала заведите почту в почтовом сервисе, потом регистрируйтесь здесь.")
    print()
    for _attempt in range(5):
        ident = _ask("Email или имя: ")
        if not ident:
            print("Нужен email или имя.")
            continue
        password = _ask("Пароль: ", secret=True)
        if not password:
            print("Регистрация новой учётки.")
            name = _ask("Как к вам обращаться: ") or ident
            email = ident if "@" in ident else _ask("Почта для писем: ")
            password = _ask("Придумайте пароль: ", secret=True)
            try:
                _prepare_mailbox(email)
                user = register(name, email, password)
                return login(user["email"], password, persist=True)
            except ValueError as e:
                print("Не вышло:", e)
                continue
        try:
            session = login(ident, password, persist=True)
            return session
        except KeyError:
            print("Учётки нет. Зарегистрировать эту почту? (д/н)")
            if _ask("> ").lower() not in ("д", "да", "y", "yes"):
                continue
            name = _ask("Как к вам обращаться: ") or ident
            email = ident if "@" in ident else _ask("Почта для писем: ")
            try:
                _prepare_mailbox(email)
                register(name, email, password)
                return login(email, password, persist=True)
            except ValueError as e:
                print("Не вышло:", e)
        except PermissionError:
            print("Неверный пароль.")
    raise SystemExit("Вход не удался. Чат не открыт.")


async def main() -> None:
    _fix_stdio()
    session = authorize()
    session = require_session(session)
    set_client(session["email"], session["name"])
    os.environ["STRAZH_CLIENT_EMAIL"] = session["email"]
    os.environ["STRAZH_CLIENT_NAME"] = session["name"]
    print()
    print(f"Вошли: {session['name']} <{session['email']}>")
    print("Письма о покупке/продаже — в вашем ящике: http://127.0.0.1:8090")
    print("Режим A — без защиты. Модель думает, тулы жмутся.")
    print("Выход: пустая строка или Ctrl+C.")
    print("Собираю агент и MCP (ЦБ + strazh)...")
    agent, tools, _client = await build_agent(session)
    names = ", ".join(sorted(t.name for t in tools))
    print(f"Тулы: {names}")
    print()
    hist: list[dict] = []
    while True:
        try:
            text = input("Клиент: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            break
        if asks_about_other_user(text, session["email"], session["name"]):
            print("Агент:", OTHER_USER_REPLY)
            print()
            continue
        print("... думаю (на CPU может быть 20–60 сек)")
        hist.append({"role": "user", "content": text})
        result = await agent.ainvoke(
            {"messages": list(hist)},
            config={"recursion_limit": 25},
        )
        snap = snapshot(session["email"])
        used = tools_used(result)
        payloads = tool_json_payloads(result)
        reply = compose_chat_reply(
            model_text=last_text(result),
            used=used,
            payloads=payloads,
            snap=snap,
            email=session["email"],
            user_message=text,
        )
        hist.append({"role": "assistant", "content": history_text(reply)})
        if len(hist) > 40:
            del hist[:-40]
        print("Агент:", reply)
        print()


if __name__ == "__main__":
    sys.path.insert(0, str(DIR))
    asyncio.run(main())
