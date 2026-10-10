"""Котировки акций с Московской биржи, доска TQBR. Цена в рублях.

Последняя сделка (LAST). Если торгов нет, предыдущее закрытие (PREVPRICE).
Сайт молчит — файл data/moex_cache.json.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from paths import DATA

FILE_CACHE = DATA / "moex_cache.json"
BOARD = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json?iss.meta=off&iss.only=securities,marketdata"
HOST = "iss.moex.com"
JUMP = 0.20

CATALOG = {
    "SBER": "Сбербанк",
    "GAZP": "Газпром",
    "LKOH": "Лукойл",
    "GMKN": "Норникель",
    "ROSN": "Роснефть",
    "YDEX": "Яндекс",
}

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"ts": 0.0, "rows": {}}
_ACCEPTED: dict[str, float] = {}
_TTL = 120


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        price = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def _row(table: dict, columns: list[str]) -> dict[str, Any]:
    if not table.get("data"):
        return {}
    data = table["data"][0]
    cols = table.get("columns") or []
    return {name: data[cols.index(name)] for name in columns if name in cols}


def _corridor(row: dict) -> dict:
    price = float(row["price"])
    old = _ACCEPTED.get(row["ticker"])
    out = dict(row)
    if old and abs(price - old) / old > JUMP:
        out["doubtful"] = True
        out["price_note"] = "котировка сомнительна"
        return out
    _ACCEPTED[row["ticker"]] = price
    out["doubtful"] = False
    return out


def _fetch_one(ticker: str) -> dict | None:
    url = BOARD.format(ticker=ticker)
    if urllib.parse.urlparse(url).hostname != HOST or not url.startswith("https://"):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8"))
    sec = _row(payload.get("securities") or {}, ["SECID", "SHORTNAME", "PREVPRICE"])
    market = _row(payload.get("marketdata") or {}, ["SECID", "LAST"])
    last = _num(market.get("LAST"))
    prev = _num(sec.get("PREVPRICE"))
    price = last if last is not None else prev
    if price is None:
        return None
    return {
        "ticker": ticker,
        "name": CATALOG[ticker],
        "shortname": str(sec.get("SHORTNAME") or CATALOG[ticker]),
        "price": price,
        "source": "LAST" if last is not None else "PREVPRICE",
        "price_note": "последняя сделка" if last is not None else "предыдущее закрытие",
    }


def _load_file() -> dict[str, dict]:
    if not FILE_CACHE.exists():
        return {}
    data = json.loads(FILE_CACHE.read_text(encoding="utf-8"))
    rows = data.get("rows") or {}
    return {k: v for k, v in rows.items() if k in CATALOG and _num(v.get("price")) is not None}


def _save_file(rows: dict[str, dict]) -> None:
    FILE_CACHE.write_text(
        json.dumps({"source": "iss.moex.com TQBR", "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _try_one(ticker: str) -> dict | None:
    try:
        return _fetch_one(ticker)
    except Exception:
        return None


def _fetch_all() -> dict[str, dict]:
    found: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(CATALOG)) as pool:
        for ticker, row in pool.map(lambda t: (t, _try_one(t)), list(CATALOG)):
            if row:
                found[ticker] = _corridor(row)
    for ticker in CATALOG:
        if ticker in found:
            continue
        row = _try_one(ticker)
        if row:
            found[ticker] = _corridor(row)
    return found


def _fresh_file() -> dict[str, dict] | None:
    if not FILE_CACHE.exists():
        return None
    if time.time() - FILE_CACHE.stat().st_mtime > _TTL:
        return None
    try:
        rows = _load_file()
    except (OSError, json.JSONDecodeError):
        return None
    if len(rows) < len(CATALOG):
        return None
    return rows


def load(force: bool = False) -> dict[str, dict]:
    now = time.time()
    with _LOCK:
        ttl = _TTL if len(_CACHE["rows"]) == len(CATALOG) else 15
        if not force and _CACHE["rows"] and now - float(_CACHE["ts"]) < ttl:
            return dict(_CACHE["rows"])
        if not force:
            fresh = _fresh_file()
            if fresh:
                _CACHE["ts"] = now
                _CACHE["rows"] = fresh
                return dict(fresh)
    live = _fetch_all()
    saved: dict[str, dict] = {}
    if len(live) < len(CATALOG):
        try:
            saved = _load_file()
        except (OSError, json.JSONDecodeError):
            saved = {}
    trusted = {}
    for ticker, row in saved.items():
        price = _num(row.get("price"))
        old = _ACCEPTED.get(ticker)
        if price is None or old is None or abs(price - old) / old > JUMP:
            continue
        trusted[ticker] = row
    rows = dict(trusted)
    rows.update(live)
    if live:
        try:
            _save_file(rows)
        except OSError:
            pass
    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["rows"] = rows
        return dict(rows)


def quote(ticker: str) -> dict | None:
    ticker = (ticker or "").upper().strip()
    if ticker not in CATALOG:
        return None
    return load().get(ticker)
