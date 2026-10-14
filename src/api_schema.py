"""Выключает /docs, /redoc и /openapi.json, пока нет STRAZH_OPENAPI=1.

Кто вызывает: web_app и mail_app при создании FastAPI.
Почему отдельно: оба HTTP-процесса должны закрывать схему одним способом.
"""

from __future__ import annotations

import os


def fastapi_kwargs() -> dict:
    flag = os.environ.get("STRAZH_OPENAPI", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}
