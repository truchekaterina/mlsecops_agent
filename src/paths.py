"""Корни каталогов: код в src, страницы в static, базы в data.

Файлы в src лежат рядом, не в пакетах. Три процесса стартуют по имени файла:
Страж — `uvicorn web_app:app`, почта — `uvicorn mail_app:app`,
сделки — отдельный процесс `python mcp_strazh.py`.
Пакет сломал бы эти точки входа и импорты внутри дочернего MCP.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent
STAND = SRC.parent
DATA = STAND / "data"
STATIC = STAND / "static"
DATA.mkdir(parents=True, exist_ok=True)
