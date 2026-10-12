#!/usr/bin/env bash
# Весь стенд: Ollama + модель + Страж + почта + учебные MCP (9100–9103).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Нужен Docker. Установите Docker Desktop или Docker Engine." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Нужен Docker Compose v2 (команда: docker compose)." >&2
  exit 1
fi

echo "Собираю образ и поднимаю стек."
echo "Первый раз качает qwen2.5:3b (~2 ГБ) — это может занять 5–15 минут."
# Без --wait: у compose по умолчанию 60 с, а pull модели длиннее.
# depends_on ollama-pull (completed) всё равно удерживает up, пока модель не скачается.
docker compose up -d --build

health_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf http://127.0.0.1:38480/api/health >/dev/null 2>&1
  else
    python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:38480/api/health', timeout=2)" >/dev/null 2>&1
  fi
}

echo "Жду http://127.0.0.1:38480/api/health ..."
ok=0
for _ in $(seq 1 300); do
  if health_ok; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  echo "Страж не ответил за ~10 минут. Смотрите: docker compose logs -f" >&2
  exit 1
fi

echo
echo "Готово."
echo "  Страж:  http://127.0.0.1:38480"
echo "  Почта:  http://127.0.0.1:38490"
echo
echo "1) Почта — регистрация ящика"
echo "2) Страж — регистрация на ту же почту (пароль может быть другим)"
echo "MCP: docs/КАК_РАБОТАТЬ_С_MCP.md"
echo "Стоп:  docker compose down"
echo "Логи:  docker compose logs -f"
