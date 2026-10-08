# Режим A: Ollama на CPU, Mailpit, веб-сервис LangChain.
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

Get-Process MailHog_windows_amd64, mailpit -ErrorAction SilentlyContinue | Stop-Process -Force
$mp = Join-Path $stand "mailpit.exe"
if (Test-Path $mp) {
    Start-Process -FilePath $mp -ArgumentList @(
        "--listen", "127.0.0.1:8025",
        "--smtp", "127.0.0.1:1025",
        "--disable-version-check",
        "--quiet"
    ) -WindowStyle Hidden
    Write-Host "Mailpit: http://127.0.0.1:8025  SMTP :1025"
}

if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$ollamaDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Write-Host "Сервис Стража (LangChain create_agent)."
Write-Host "Откройте в браузере:  http://127.0.0.1:8080"
Write-Host "Письма — на почту из регистрации (смотрите Mailpit)."
Set-Location $stand
& "$root\.venv\Scripts\python.exe" -m uvicorn web_app:app --host 0.0.0.0 --port 8080
