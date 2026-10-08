"""Официальные курсы ЦБ: https://www.cbr.ru/scripts/XML_daily.asp"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from paths import DATA

FILE_CACHE = DATA / "cbr_cache.json"

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_URLS = (
    "http://www.cbr.ru/scripts/XML_daily.asp",
)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"ts": 0.0, "date": "", "rows": [], "source": ""}

_HINTS = (
    ("доллар сша", "USD"),
    ("доллар", "USD"),
    ("usd", "USD"),
    ("евро", "EUR"),
    ("eur", "EUR"),
    ("юань", "CNY"),
    ("cny", "CNY"),
    ("фунт", "GBP"),
    ("gbp", "GBP"),
    ("иен", "JPY"),
    ("йен", "JPY"),
    ("jpy", "JPY"),
    ("тенге", "KZT"),
    ("kzt", "KZT"),
    ("франк", "CHF"),
    ("chf", "CHF"),
    ("злотый", "PLN"),
    ("pln", "PLN"),
    ("лир", "TRY"),
    ("try", "TRY"),
    ("грив", "UAH"),
    ("uah", "UAH"),
    ("белорус", "BYN"),
    ("byn", "BYN"),
    ("дирхам", "AED"),
    ("aed", "AED"),
    ("канад", "CAD"),
    ("cad", "CAD"),
    ("австрал", "AUD"),
    ("aud", "AUD"),
    ("франк", "CHF"),
)


def _parse(xml_bytes: bytes) -> tuple[str, list[dict]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "windows-1251"):
        try:
            text = xml_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("не удалось декодировать XML ЦБ")
    root = ET.fromstring(text)
    date = str(root.attrib.get("Date") or "")
    rows: list[dict] = []
    for node in root.findall("Valute"):
        code = (node.findtext("CharCode") or "").strip().upper()
        name = (node.findtext("Name") or "").strip()
        nominal = int(node.findtext("Nominal") or "1")
        raw = (node.findtext("Value") or "0").replace(",", ".")
        value = float(raw)
        unit_raw = (node.findtext("VunitRate") or "").replace(",", ".")
        unit = float(unit_raw) if unit_raw else round(value / max(nominal, 1), 6)
        rows.append(
            {
                "char_code": code,
                "name": name,
                "nominal": nominal,
                "value_rub": value,
                "unit_rub": unit,
            }
        )
    return date, rows


def _load_file() -> tuple[str, list[dict]]:
    data = json.loads(FILE_CACHE.read_text(encoding="utf-8"))
    return str(data.get("date") or ""), list(data.get("rows") or [])


def _save_file(date: str, rows: list[dict]) -> None:
    FILE_CACHE.write_text(
        json.dumps({"date": date, "source": CBR_URL, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch_live() -> tuple[str, list[dict]]:
    ctx = ssl._create_unverified_context()
    old_to = socket.getdefaulttimeout()
    socket.setdefaulttimeout(8)
    last_err: Exception | None = None
    raw = b""
    try:
        for url in CBR_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                kw: dict = {"timeout": 8}
                if url.startswith("https"):
                    kw["context"] = ctx
                with urllib.request.urlopen(req, **kw) as resp:
                    raw = resp.read()
                last_err = None
                break
            except Exception as exc:
                last_err = exc
    finally:
        socket.setdefaulttimeout(old_to)
    if last_err and not raw:
        raise last_err
    return _parse(raw)


def daily(force: bool = False) -> tuple[str, list[dict]]:
    now = time.time()
    with _LOCK:
        if not force and _CACHE["rows"] and now - float(_CACHE["ts"]) < 600:
            return str(_CACHE["date"]), list(_CACHE["rows"])
    try:
        date, rows = _fetch_live()
        source = "cbr.ru live"
        try:
            _save_file(date, rows)
        except OSError:
            pass
    except Exception:
        date, rows = _load_file()
        source = "кэш официального XML ЦБ"
    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["date"] = date
        _CACHE["rows"] = rows
        _CACHE["source"] = source
        return date, list(rows)


def find_code(text: str) -> str | None:
    t = (text or "").lower()
    date, rows = daily()
    _ = date
    for hint, code in _HINTS:
        if hint in t:
            return code
    for row in rows:
        code = row["char_code"]
        if code.lower() in t.split() or f" {code.lower()} " in f" {t} ":
            return code
        name = str(row["name"]).lower()
        if len(name) >= 4 and name in t:
            return code
    return None


def format_rates(text: str) -> str:
    date, rows = daily()
    src = str(_CACHE.get("source") or "cbr.ru")
    want = find_code(text)
    all_ask = any(x in (text or "").lower() for x in ("все", "валют", "список курс", "какие валют"))
    if want and not all_ask:
        row = next((r for r in rows if r["char_code"] == want), None)
        if not row:
            return f"ЦБ не публикует код {want} на {date}."
        return (
            f"Курс ЦБ РФ на {date} ({src}):\n"
            f"- {row['name']} ({row['char_code']}): {row['value_rub']} ₽ "
            f"за {row['nominal']} ед."
        )
    lines = [f"Курсы ЦБ РФ на {date} ({src}), {len(rows)} валют:"]
    for row in rows:
        lines.append(
            f"- {row['char_code']} {row['name']}: {row['value_rub']} ₽ за {row['nominal']}"
        )
    return "\n".join(lines)
