#!/usr/bin/env bash
# Весь стенд: Ollama + модель + Страж + почта + учебные MCP (9100–9103).
# ./start-docker.sh       — поднять и напечатать ссылки
# ./start-docker.sh urls  — только текущие ссылки (стенд уже запущен)
set -euo pipefail
cd "$(dirname "$0")"

health_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf http://127.0.0.1:38480/api/health >/dev/null 2>&1
  else
    python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:38480/api/health', timeout=2)" >/dev/null 2>&1
  fi
}

# IPv4 Wi‑Fi / Ethernet, без docker / VM / VPN.
# Строка ip: «2: wlp4s0    inet 192.168.4.142/24 brd …»
lan_ipv4() {
  if ! command -v ip >/dev/null 2>&1; then
    return 0
  fi
  ip -4 -o addr show scope global | awk '{
    iface = $2
    if (iface ~ /^(lo|docker|br-|virbr|tun|veth|cni|tailscale|wg)/) next
    split($4, a, "/")
    if (a[1] ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) print a[1]
  }'
}

print_urls() {
  echo
  echo "У этого компьютера:"
  echo "  Страж:  http://127.0.0.1:38480"
  echo "  Почта:  http://127.0.0.1:38490"
  echo
  ips="$(lan_ipv4 || true)"
  if [ -n "${ips}" ]; then
    echo "Одногруппникам в той же Wi‑Fi (скопируйте как есть):"
    while read -r ip; do
      [ -n "$ip" ] || continue
      echo "  Страж:  http://${ip}:38480"
      echo "  Почта:  http://${ip}:38490"
    done <<< "${ips}"
    echo
    echo "IP выдаёт Wi‑Fi (DHCP), порты всегда 38480 и 38490."
    echo "Если сеть сменилась: ./start-docker.sh urls"
  else
    echo "LAN-адрес не найден. С этого компьютера открывайте 127.0.0.1."
  fi
  echo
  echo "1) Почта — регистрация ящика"
  echo "2) Страж — регистрация на ту же почту (пароль может быть другим)"
  echo "MCP: docs/КАК_РАБОТАТЬ_С_MCP.md"
  echo "Стоп:  docker compose down"
  echo "Логи:  docker compose logs -f"
}

if [ "${1:-}" = "urls" ]; then
  if ! health_ok; then
    echo "Стенд не отвечает на http://127.0.0.1:38480/api/health" >&2
    echo "Сначала: ./start-docker.sh" >&2
    exit 1
  fi
  echo "Стенд уже работает."
  print_urls
  exit 0
fi

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
print_urls
