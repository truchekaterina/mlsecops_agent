# Режим A: Ollama на CPU, почтовый сервис, веб-сервис LangChain.
$root = Split-Path -Parent $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
$stand = $PSScriptRoot
$ollamaDir = "$env:LOCALAPPDATA\Programs\Ollama"
$env:PATH = "$ollamaDir;$env:USERPROFILE\.local\bin;" + $env:PATH
$env:CUDA_VISIBLE_DEVICES = "-1"
$env:OLLAMA_NUM_GPU = "0"
$env:OLLAMA_LLM_LIBRARY = "cpu_avx2"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 | Out-Null } catch {}
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$python = Join-Path $stand ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python -or -not (Test-Path $python)) {
    Write-Host "Python не найден: нет .venv и нет python в PATH."
    exit 1
}
$src = Join-Path $stand "src"
$dataDir = Join-Path $stand "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
$secretFile = Join-Path $dataDir "jwt_secret"
$demoSecret = "strazh-demo-hs256"
$existing = ""
if (Test-Path $secretFile) { $existing = (Get-Content -Raw -Path $secretFile).Trim() }
if ($existing.Length -lt 16 -or $existing -eq $demoSecret) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $existing = [Convert]::ToBase64String($bytes)
    Set-Content -Path $secretFile -Value $existing -NoNewline -Encoding ascii
}
$env:STRAZH_JWT_SECRET = $existing
$mailSecretFile = Join-Path $dataDir "mail_secret"
$mailSecret = ""
if ($env:STRAZH_MAIL_SECRET) { $mailSecret = $env:STRAZH_MAIL_SECRET.Trim() }
if (-not $mailSecret -and (Test-Path $mailSecretFile)) {
    $mailSecret = (Get-Content -Raw -Path $mailSecretFile).Trim()
}
if (-not $mailSecret) {
    $mailBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($mailBytes)
    $mailSecret = [Convert]::ToBase64String($mailBytes)
    Set-Content -Path $mailSecretFile -Value $mailSecret -NoNewline -Encoding ascii
}
if ($mailSecret.Length -lt 16) {
    Write-Host "STRAZH_MAIL_SECRET короче 16 символов. Почта не стартует."
    exit 1
}
$env:STRAZH_MAIL_SECRET = $mailSecret
$dbSecretFile = Join-Path $dataDir "db_secret"
$dbSecret = ""
if ($env:STRAZH_DB_SECRET) { $dbSecret = $env:STRAZH_DB_SECRET.Trim() }
if (-not $dbSecret -and (Test-Path $dbSecretFile)) {
    $dbSecret = (Get-Content -Raw -Path $dbSecretFile).Trim()
}
if (-not $dbSecret) {
    $dbBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($dbBytes)
    $dbSecret = [Convert]::ToBase64String($dbBytes)
    Set-Content -Path $dbSecretFile -Value $dbSecret -NoNewline -Encoding ascii
}
if ($dbSecret.Length -lt 16) {
    Write-Host "STRAZH_DB_SECRET короче 16 символов. База не стартует."
    exit 1
}
$env:STRAZH_DB_SECRET = $dbSecret
$auditSecretFile = Join-Path $dataDir "audit_secret"
$auditSecret = ""
if ($env:STRAZH_AUDIT_SECRET) { $auditSecret = $env:STRAZH_AUDIT_SECRET.Trim() }
if (-not $auditSecret -and (Test-Path $auditSecretFile)) {
    $auditSecret = (Get-Content -Raw -Path $auditSecretFile).Trim()
}
if (-not $auditSecret) {
    $auditBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($auditBytes)
    $auditSecret = [Convert]::ToBase64String($auditBytes)
    Set-Content -Path $auditSecretFile -Value $auditSecret -NoNewline -Encoding ascii
}
if ($auditSecret.Length -lt 16) {
    Write-Host "STRAZH_AUDIT_SECRET короче 16 символов. Журнал не стартует."
    exit 1
}
$env:STRAZH_AUDIT_SECRET = $auditSecret
$env:STRAZH_MAIL_URL = "https://127.0.0.1:8090"
$sslLine = & $python -c "import sys; sys.path.insert(0, r'$src'); from tls_cert import uvicorn_ssl_args; print(' '.join(uvicorn_ssl_args()))"
$sslArgs = $sslLine -split ' '

Start-Process -FilePath $python -ArgumentList (@(
    "-m", "uvicorn", "mail_app:app", "--host", "127.0.0.1", "--port", "8090"
) + $sslArgs) -WorkingDirectory $src -WindowStyle Hidden
Write-Host "Почта:   https://127.0.0.1:8090"

if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$ollamaDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

$bind = "127.0.0.1"
if ($env:STRAZH_BIND) { $bind = $env:STRAZH_BIND.Trim() }
$webArgs = @("-m", "uvicorn", "web_app:app", "--host", $bind, "--port", "8080") + $sslArgs

Write-Host "Сервис Стража (LangChain create_agent)."
Write-Host "Откройте в браузере:  https://127.0.0.1:8080"
Write-Host "Сертификат самоподписанный: браузер один раз спросит доверие."
Write-Host "Сначала ящик на :8090, потом регистрация в Страже на эту почту."
Set-Location $src
& $python @webArgs
