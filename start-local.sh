#!/usr/bin/env bash
# Локальный запуск без Docker: почта 8090, Страж 8080, учебные MCP 9100–9103.
# Контейнер и порты 38480/38490 не использует.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
umask 077

ensure_secret() {
  local file="$1" name="$2" current="${!2:-}"
  if [[ -z "$current" && -f "$file" ]]; then
    current="$(tr -d '[:space:]' < "$file")"
  fi
  if [[ "$name" == "STRAZH_JWT_SECRET" && "$current" == "strazh-demo-hs256" ]]; then
    current=""
  fi
  if [[ ${#current} -lt 16 ]]; then
    current="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf '%s' "$current" > "$file"
    chmod 600 "$file"
  fi
  export "$name=$current"
}

port_free() {
  local port="$1"
  if ss -ltn 2>/dev/null | grep -q ":${port} "; then
    echo "Порт ${port} уже занят. Остановите тот процесс и запустите скрипт снова."
    exit 1
  fi
}

ensure_secret data/jwt_secret STRAZH_JWT_SECRET
ensure_secret data/mail_secret STRAZH_MAIL_SECRET
ensure_secret data/db_secret STRAZH_DB_SECRET
ensure_secret data/audit_secret STRAZH_AUDIT_SECRET
export STRAZH_MAIL_URL="https://127.0.0.1:8090"
export OLLAMA_NUM_GPU=0
export STRAZH_MODEL="${STRAZH_MODEL:-qwen2.5:3b}"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt "uv==0.13.0"

if ! curl -sf --max-time 2 "http://127.0.0.1:11434/api/tags" >/dev/null; then
  if ! command -v ollama >/dev/null; then
    echo "Ollama не найдена. Поставьте её и повторите: https://ollama.com"
    exit 1
  fi
  echo "Запускаю ollama serve..."
  ollama serve >/tmp/strazh-ollama.log 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf --max-time 2 "http://127.0.0.1:11434/api/tags" >/dev/null; then
      break
    fi
    sleep 1
  done
fi
if ! curl -sf --max-time 2 "http://127.0.0.1:11434/api/tags" >/dev/null; then
  echo "Ollama не ответила на http://127.0.0.1:11434. Лог: /tmp/strazh-ollama.log"
  exit 1
fi
if ! curl -sf "http://127.0.0.1:11434/api/tags" | grep -q "qwen2.5:3b"; then
  echo "Скачиваю модель qwen2.5:3b (около 2 ГБ, один раз)..."
  ollama pull qwen2.5:3b
fi

for port in 8080 8090 9100 9101 9102 9103; do
  port_free "$port"
done

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

mapfile -t SSL_ARGS < <(python -c 'import sys; sys.path.insert(0, "src"); from tls_cert import uvicorn_ssl_args; print("\n".join(uvicorn_ssl_args()))')

python -m uvicorn mail_app:app --app-dir src --host 127.0.0.1 --port 8090 "${SSL_ARGS[@]}" &
pids+=("$!")

start_mcp() {
  env -u STRAZH_JWT_SECRET -u STRAZH_DB_SECRET -u STRAZH_MAIL_SECRET -u STRAZH_AUDIT_SECRET \
    STRAZH_MAIL_URL="https://127.0.0.1:8090" \
    python "$1" &
  pids+=("$!")
}
start_mcp examples/mcp_stub/server.py
start_mcp examples/evil_mcp/portfolio_backup/server.py
start_mcp examples/evil_mcp/tax_helper/server.py
start_mcp examples/evil_mcp/stock_wiki/server.py

echo "Почта:  https://127.0.0.1:8090"
echo "Страж:  https://127.0.0.1:8080"
echo "Сертификат самоподписанный: браузер спросит, можно ли ему доверять."
echo "Сначала ящик на почте, потом регистрация в Страже на ту же почту."
echo "Учебный MCP: http://127.0.0.1:9100/mcp"
python -m uvicorn web_app:app --app-dir src --host 127.0.0.1 --port 8080 "${SSL_ARGS[@]}"
