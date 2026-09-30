<#
.SYNOPSIS
    Starts everything Interlock needs for daily use, each in its own window:
    the WhatsApp agent, the API (which also serves the UI), and the worker.
    Then opens the UI in the browser.

.DESCRIPTION
    Each process gets its own PowerShell window so its log stays readable and
    it can be stopped on its own with Ctrl+C. Nothing here runs hidden.

    Build the UI once first (see docs/web-ui.md):
        cd apps\web; npm install; npm run build

.PARAMETER NoAgent
    Skip the WhatsApp agent, e.g. if it's already running.
#>
param([switch]$NoAgent)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python -- see HANDOVER.md section 2."
}
if (-not (Test-Path (Join-Path $root "apps\web\dist\index.html"))) {
    Write-Warning "The UI isn't built yet, so http://127.0.0.1:8000/ will open /docs. Build it with: cd apps\web; npm install; npm run build"
}

function Start-Window([string]$title, [string]$workdir, [string]$command) {
    $script = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workdir'; $command"
    Start-Process powershell -ArgumentList "-NoExit", "-NoProfile", "-Command", $script | Out-Null
    Write-Host "Started: $title"
}

if (-not $NoAgent) {
    Start-Window "Interlock - WhatsApp agent" (Join-Path $root "apps\agent") "npm start"
}
Start-Window "Interlock - API + UI" $root "& '$python' -m uvicorn interlock.api.main:app --host 127.0.0.1 --port 8000"
Start-Window "Interlock - worker" $root "& '$python' -m interlock.workers.loop"

# Give uvicorn a moment before opening the browser.
$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 | Out-Null
        $up = $true
    } catch { $up = $false }
} until ($up -or (Get-Date) -gt $deadline)

if ($up) {
    Start-Process "http://127.0.0.1:8000/"
    Write-Host "Interlock is running: http://127.0.0.1:8000/"
} else {
    Write-Warning "The API didn't answer within 20 s -- check the 'Interlock - API + UI' window."
}
