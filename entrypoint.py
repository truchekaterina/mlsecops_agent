"""Запуск почты и Стража внутри контейнера. Локальный start_mode_a.ps1 не заменяет."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path("/app/data")
SECRET_FILE = DATA / "jwt_secret"
MAIL_SECRET_FILE = DATA / "mail_secret"
DEMO_SECRET = "strazh-demo-hs256"


def ensure_jwt() -> str:
    DATA.mkdir(parents=True, exist_ok=True)
    existing = SECRET_FILE.read_text(encoding="ascii").strip() if SECRET_FILE.exists() else ""
    if len(existing) < 16 or existing == DEMO_SECRET:
        existing = secrets.token_urlsafe(32)
        SECRET_FILE.write_text(existing, encoding="ascii")
    return existing


def ensure_mail_secret() -> str:
    """Секрет почты отдельно от JWT. В репозиторий не кладётся."""
    DATA.mkdir(parents=True, exist_ok=True)
    current = os.environ.get("STRAZH_MAIL_SECRET", "").strip()
    if len(current) >= 16:
        return current
    if current:
        raise SystemExit("STRAZH_MAIL_SECRET короче 16 символов. Почта не стартует.")
    existing = MAIL_SECRET_FILE.read_text(encoding="ascii").strip() if MAIL_SECRET_FILE.exists() else ""
    if len(existing) < 16:
        existing = secrets.token_urlsafe(32)
        MAIL_SECRET_FILE.write_text(existing, encoding="ascii")
    return existing


def wait_ollama(url: str, tries: int = 90) -> None:
    tags = url.rstrip("/") + "/api/tags"
    last: Exception | None = None
    for _ in range(tries):
        try:
            urllib.request.urlopen(tags, timeout=2).read()
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(2)
    raise SystemExit(f"Ollama не ответила на {url}: {last}")


def _has_model(names: list[str], model: str) -> bool:
    return any(name == model or name.startswith(model) for name in names)


def ensure_model(url: str, model: str) -> None:
    tags = json.load(urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=15))
    names = [str(item.get("name") or "") for item in tags.get("models") or []]
    if _has_model(names, model):
        print(f"Модель {model} уже есть.", flush=True)
        return
    print(f"Качаю модель {model}. Первый запуск может занять несколько минут...", flush=True)
    req = urllib.request.Request(
        url.rstrip("/") + "/api/pull",
        data=json.dumps({"name": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        for raw in resp:
            try:
                row = json.loads(raw.decode())
            except json.JSONDecodeError:
                continue
            status = str(row.get("status") or "")
            if status:
                print(status, flush=True)
    print("Модель готова.", flush=True)


def main() -> None:
    os.environ["STRAZH_JWT_SECRET"] = ensure_jwt()
    os.environ["STRAZH_MAIL_SECRET"] = ensure_mail_secret()
    os.environ.setdefault("STRAZH_MAIL_URL", "http://127.0.0.1:8090")
    ollama = os.environ.get("STRAZH_OLLAMA_URL", "http://127.0.0.1:11434")
    model = os.environ.get("STRAZH_MODEL", "qwen2.5:3b")
    print("Жду Ollama...", flush=True)
    wait_ollama(ollama)
    ensure_model(ollama, model)
    src = Path("/app/src")
    os.chdir(src)
    env = os.environ.copy()
    # 0.0.0.0 — интерфейсы контейнера, иначе проброс Docker не доходит до почты.
    # На хосте порт опубликован только как 127.0.0.1:8090.
    mail = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mail_app:app", "--host", "0.0.0.0", "--port", "8090"],
        cwd=src,
        env=env,
    )
    try:
        web = subprocess.run(
            [sys.executable, "-m", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8080"],
            cwd=src,
            env=env,
            check=False,
        )
    finally:
        mail.terminate()
        try:
            mail.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mail.kill()
    raise SystemExit(web.returncode)


if __name__ == "__main__":
    main()
