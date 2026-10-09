"""Приёмка режима A: прямые тулы + три прогона агента."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OLLAMA_NUM_GPU", "0")

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))

from paths import DATA  # noqa: E402

import mcp_strazh as tools  # noqa: E402
from agent_mode_a import build_agent, last_text  # noqa: E402
from auth import decode_jwt, load_store, login  # noqa: E402
from ledger import clear_history, reset_book, snapshot  # noqa: E402
from mail_store import list_messages  # noqa: E402
from replies import compose_chat_reply  # noqa: E402

LOG = DATA / "trades.log"


def _reset_log() -> None:
    if LOG.exists():
        LOG.unlink()


def _kinds() -> list[str]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line)["kind"])
    return out


def check_auth() -> dict:
    print("=== Вход клиента ===")
    session = login("client@demo", "demo", persist=True)
    store = load_store()
    claims = decode_jwt(session["token"], store["secret"])
    assert claims["email"] == "client@demo", claims
    os.environ["STRAZH_CLIENT_EMAIL"] = session["email"]
    os.environ["STRAZH_CLIENT_NAME"] = session["name"]
    print("  JWT:", session["email"], session["name"], "OK")
    return session


def check_direct_tools() -> None:
    print("=== Прямые тулы MCP (без модели) ===")
    _reset_log()
    reset_book("client@demo")
    clear_history("client@demo")
    inst = json.loads(tools.list_instruments())
    sber = next((x for x in inst["instruments"] if x["ticker"] == "SBER"), None)
    assert inst["status"] == "SUCCESS" and sber and sber.get("price_rub"), inst
    price = float(sber["price_rub"])
    shares = int(2000 // price) + 1
    cur = json.loads(tools.list_currencies())
    codes = {x["char_code"] for x in cur["currencies"]}
    assert codes >= {"USD", "EUR", "CNY"}, cur
    acc = json.loads(tools.get_account())
    assert acc["cash_usd"] == 2000.0 and acc["reserve_usd"] == 5000.0, acc
    port0 = json.loads(tools.get_portfolio())
    assert port0["empty"] is True, port0
    ask = json.loads(tools.execute_buy("SBER", shares))
    assert ask["status"] == "NEED_RESERVE_CONFIRM", ask
    assert ask["shortfall_usd"] > 0, ask
    buy = json.loads(tools.confirm_reserve_purchase())
    assert buy["status"] == "SUCCESS", buy
    assert buy["cash_usd"] == 0.0, buy
    huge = json.loads(tools.execute_buy("SBER", int(7000 // price) + 5))
    assert huge["status"] == "ERROR", huge
    assert huge.get("missing_usd", 0) > 0, huge
    assert "пополнить" in (huge.get("message") or ""), huge
    port = json.loads(tools.get_portfolio())
    held = next(p for p in port["positions"] if p["ticker"] == "SBER")
    assert held["shares"] == shares, port
    sell_bad = json.loads(tools.execute_sell("SBER", 1000))
    assert sell_bad["status"] == "ERROR", sell_bad
    sell = json.loads(tools.execute_sell("SBER", shares))
    assert sell["status"] == "SUCCESS", sell
    assert sell["position"] == 0, sell
    assert sell["cash_usd"] > 0, sell
    assert sell.get("email_to") == "client@demo", sell
    mail = json.loads(
        tools.send_trade_email(
            side="SELL",
            ticker="SBER",
            shares=3,
            usd_amount=sell["usd_amount"],
            confirmation_id=sell["confirmation_id"],
            rub_amount="",
        )
    )
    assert mail["status"] == "SUCCESS", mail
    assert mail.get("to") == "client@demo", mail
    assert mail.get("message_id"), mail
    saved = list_messages("client@demo")
    assert saved and saved[0]["email"] == "client@demo", saved
    kinds = _kinds()
    assert "BUY" in kinds and "SELL" in kinds and "RESERVE_ASK" in kinds and "ACCOUNT" in kinds, kinds
    snap = snapshot("client@demo")
    fake = compose_chat_reply(
        model_text="Вы купили 1 Apple. NVIDIA (NVDA): 6 шт.",
        used=[],
        payloads=[],
        snap=snap,
        email="client@demo",
        user_message="купи 1 акцию Apple",
    )
    assert "не записана" in fake, fake
    assert "6 шт" not in fake, fake
    katya = snapshot("katya@demo")
    assert katya["email"] == "katya@demo"
    assert snap["cash_usd"] > 0
    assert katya["cash_usd"] != snap["cash_usd"]
    print("  catalog / account / portfolio / buy / reserve / sell — OK")
    print("  trades.log:", kinds)
    print("  smtp_ok:", mail.get("smtp_ok"))


async def check_agent(session: dict) -> None:
    print("=== Агент + MCP ЦБ (модель) ===")
    reset_book(session["email"])
    agent, tool_list, _client = await build_agent(session)
    names = sorted(t.name for t in tool_list)
    print("  тулы:", names)
    assert "execute_buy" in names
    assert "execute_sell" in names
    assert "get_account" in names and "get_portfolio" in names
    assert "list_instruments" in names and "list_currencies" in names
    assert "get_rate" in names or "get_currency_rate" in names, names

    prompts = [
        (
            "catalog",
            "Выведи список доступных акций для покупки.",
        ),
        (
            "account",
            "Какое у меня состояние счёта?",
        ),
        (
            "portfolio",
            "Какие акции куплены?",
        ),
        (
            "buy",
            "Купи 1 акцию Сбербанка",
        ),
        (
            "fx",
            "Какой сегодня курс доллара ЦБ? Только курс, без сделки.",
        ),
    ]
    for tag, prompt in prompts:
        print(f"\n--- {tag} ---")
        print("  Клиент:", prompt)
        _reset_log()
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"recursion_limit": 25},
        )
        text = last_text(result)
        kinds = _kinds()
        print("  Агент:", text[:800])
        print("  log:", kinds)
        if tag == "buy":
            assert "BUY" in kinds, f"ожидали BUY, получили {kinds}"
        if tag == "fx":
            assert "BUY" not in kinds and "SELL" not in kinds, f"курс не должен торговать: {kinds}"
        if tag in ("account", "portfolio", "catalog"):
            low = text.lower()
            assert "брокер" not in low, text
    print("\nSELFCHECK PASSED")


def main() -> None:
    session = check_auth()
    reset_book(session["email"])
    clear_history(session["email"])
    check_direct_tools()
    skip = os.environ.get("STRAZH_SKIP_AGENT", "").strip() in ("1", "true", "yes")
    if skip:
        print("STRAZH_SKIP_AGENT=1 — прогоны модели пропущены")
        print("DIRECT TOOLS PASSED")
        return
    asyncio.run(check_agent(session))


if __name__ == "__main__":
    main()
