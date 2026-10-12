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

$python = Join-Path $root ".venv\Scripts\python.exe"
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

Start-Process -FilePath $python -ArgumentList @(
    "-m", "uvicorn", "mail_app:app", "--host", "127.0.0.1", "--port", "8090"
) -WorkingDirectory $src -WindowStyle Hidden
Write-Host "Почта:   http://127.0.0.1:8090"

if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$ollamaDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

$bind = "127.0.0.1"
if ($env:STRAZH_BIND) { $bind = $env:STRAZH_BIND.Trim() }
$webArgs = @("-m", "uvicorn", "web_app:app", "--host", $bind, "--port", "8080")
if (@("127.0.0.1", "localhost", "::1") -notcontains $bind) {
    if (-not $env:STRAZH_SSL_CERTFILE -or -not $env:STRAZH_SSL_KEYFILE) {
        Write-Host "Для показа не на этом компьютере задайте STRAZH_SSL_CERTFILE и STRAZH_SSL_KEYFILE."
        exit 1
    }
    $webArgs += @("--ssl-certfile", $env:STRAZH_SSL_CERTFILE, "--ssl-keyfile", $env:STRAZH_SSL_KEYFILE)
}

Write-Host "Сервис Стража (LangChain create_agent)."
Write-Host "Откройте в браузере:  http://127.0.0.1:8080"
Write-Host "Сначала ящик на :8090, потом регистрация в Страже на эту почту."
Set-Location $src
& $python @webArgs
