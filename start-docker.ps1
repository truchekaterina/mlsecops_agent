# Весь стенд: Ollama + модель + Страж + почта + учебные MCP (9100–9103).
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Нужен Docker. Установите Docker Desktop."
}
try {
    docker compose version | Out-Null
} catch {
    Write-Error "Нужен Docker Compose v2 (команда: docker compose)."
}

Write-Host "Собираю образ и поднимаю стек."
Write-Host "Первый раз качает qwen2.5:3b (~2 ГБ) — это может занять 5–15 минут."
# Без --wait: у compose по умолчанию 60 с, а pull модели длиннее.
docker compose up -d --build

Write-Host "Жду http://127.0.0.1:38480/api/health ..."
$ok = $false
for ($i = 0; $i -lt 300; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:38480/api/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ok) {
    Write-Error "Страж не ответил за ~10 минут. Смотрите: docker compose logs -f"
}

Write-Host ""
Write-Host "Готово."
Write-Host "  Страж:  http://127.0.0.1:38480"
Write-Host "  Почта:  http://127.0.0.1:38490"
Write-Host ""
Write-Host "1) Почта — регистрация ящика"
Write-Host "2) Страж — регистрация на ту же почту (пароль может быть другим)"
Write-Host "MCP: docs/КАК_РАБОТАТЬ_С_MCP.md"
Write-Host "Стоп:  docker compose down"
Write-Host "Логи:  docker compose logs -f"
