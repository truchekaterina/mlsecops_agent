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
Start-Process -FilePath $python -ArgumentList @(
    "-m", "uvicorn", "mail_app:app", "--host", "127.0.0.1", "--port", "8090"
) -WorkingDirectory $src -WindowStyle Hidden
Write-Host "Почта:   http://127.0.0.1:8090"

if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath "$ollamaDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Write-Host "Сервис Стража (LangChain create_agent)."
Write-Host "Откройте в браузере:  http://127.0.0.1:8080"
Write-Host "Сначала ящик на :8090, потом регистрация в Страже на эту почту."
Set-Location $src
& $python -m uvicorn web_app:app --host 0.0.0.0 --port 8080
