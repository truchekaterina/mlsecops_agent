"""Корни стенда. Код лежит в src, страницы в static, базы и логи в data."""

from pathlib import Path

SRC = Path(__file__).resolve().parent
STAND = SRC.parent
DATA = STAND / "data"
STATIC = STAND / "static"
DATA.mkdir(parents=True, exist_ok=True)
