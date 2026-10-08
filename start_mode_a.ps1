# Режим A: Ollama на CPU, MailHog, веб-сервис LangChain.
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

$mh = Join-Path $stand "MailHog_windows_amd64.exe"
if (Test-Path $mh) {
    Get-Process MailHog_windows_amd64 -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Process -FilePath $mh -WindowStyle Hidden
    Write-Host "MailHog: http://127.0.0.1:8025  SMTP :1025"
}

if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$ollamaDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Write-Host "Сервис Стража (LangChain create_agent)."
Write-Host "Откройте в браузере:  http://127.0.0.1:8080"
Write-Host "Регистрация открыта для любого человека. Письма — на его почту (MailHog)."
Set-Location $stand
& "$root\.venv\Scripts\python.exe" -m uvicorn web_app:app --host 0.0.0.0 --port 8080
