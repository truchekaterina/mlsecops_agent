# Весь стенд: Ollama + модель + Страж + почта + учебные MCP (9100–9103).
# .\start-docker.ps1       — поднять и напечатать ссылки
# .\start-docker.ps1 urls  — только текущие ссылки (стенд уже запущен)
param(
    [Parameter(Position = 0)]
    [string]$Command = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-StrazhHealth {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:38480/api/health" -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-LanIPv4 {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -ne "127.0.0.1" -and
            $_.PrefixOrigin -ne "WellKnown" -and
            $_.InterfaceAlias -notmatch "vEthernet|Docker|WSL|Loopback|Virtual|VPN|Tailscale|Hyper-V"
        } |
        Select-Object -ExpandProperty IPAddress -Unique
}

function Write-Urls {
    Write-Host ""
    Write-Host "У этого компьютера:"
    Write-Host "  Страж:  http://127.0.0.1:38480"
    Write-Host "  Почта:  http://127.0.0.1:38490"
    Write-Host ""
    $ips = @(Get-LanIPv4)
    if ($ips.Count -gt 0) {
        Write-Host "Одногруппникам в той же Wi‑Fi (скопируйте как есть):"
        foreach ($ip in $ips) {
            Write-Host "  Страж:  http://${ip}:38480"
            Write-Host "  Почта:  http://${ip}:38490"
        }
        Write-Host ""
        Write-Host "IP выдаёт Wi‑Fi (DHCP), порты всегда 38480 и 38490."
        Write-Host "Если сеть сменилась: .\start-docker.ps1 urls"
    } else {
        Write-Host "LAN-адрес не найден. С этого компьютера открывайте 127.0.0.1."
    }
    Write-Host ""
    Write-Host "1) Почта — регистрация ящика"
    Write-Host "2) Страж — регистрация на ту же почту (пароль может быть другим)"
    Write-Host "MCP: docs/КАК_РАБОТАТЬ_С_MCP.md"
    Write-Host "Стоп:  docker compose down"
    Write-Host "Логи:  docker compose logs -f"
}

if ($Command -eq "urls") {
    if (-not (Test-StrazhHealth)) {
        Write-Error "Стенд не отвечает на http://127.0.0.1:38480/api/health. Сначала: .\start-docker.ps1"
    }
    Write-Host "Стенд уже работает."
    Write-Urls
    exit 0
}

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
docker compose up -d --build

Write-Host "Жду http://127.0.0.1:38480/api/health ..."
$ok = $false
for ($i = 0; $i -lt 300; $i++) {
    if (Test-StrazhHealth) { $ok = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ok) {
    Write-Error "Страж не ответил за ~10 минут. Смотрите: docker compose logs -f"
}

Write-Host ""
Write-Host "Готово."
Write-Urls
