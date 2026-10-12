"""Публичная схема API выключена. Для локальной разработки её включает STRAZH_OPENAPI."""

from __future__ import annotations

import os


def fastapi_kwargs() -> dict:
    flag = os.environ.get("STRAZH_OPENAPI", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}
