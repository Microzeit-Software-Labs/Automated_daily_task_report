<#
.SYNOPSIS
    Runs Interlock in the background -- the API and web UI, the worker, and the
    WhatsApp agent -- or stops it, restarts it, or says how it is doing.

.DESCRIPTION
    Everything runs under one hidden "supervisor" process (src\interlock\supervisor.py)
    that restarts anything that stops. There are no windows to keep open. Logs are
    in the logs\ folder (api.log, worker.log, agent.log, supervisor.log).

    Safe to run any time: if Interlock is already running it just says so, and a
    second copy can never start (the supervisor takes a lock).

    To have it start by itself when you log in to Windows:
        .\scripts\register-autostart.ps1

    Build the UI once first (see docs\web-ui.md):
        cd apps\web; npm install; npm run build

.PARAMETER Stop
    Shut Interlock down (all three parts).
.PARAMETER Restart
    Stop, then start. Use this after changing any backend code.
.PARAMETER Status
    Show what is running and whether WhatsApp is connected. Starts nothing.
.PARAMETER Console
    Run in this window instead of the background, printing every log line. For
    debugging; Ctrl+C stops it.
.PARAMETER NoAgent
    Leave the WhatsApp agent out.
.PARAMETER NoBrowser
    Don't open the web UI afterwards.
#>
param(
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [switch]$Console,
    [switch]$NoAgent,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$logs = Join-Path $root "logs"
$statusFile = Join-Path $logs "supervisor-status.json"
$stopFile = Join-Path $logs "supervisor.stop"
$url = "http://127.0.0.1:8000"

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python -- see HANDOVER.md section 2."
}

# The supervisor's own report of itself, or $null if it isn't running. The pid
# check and the freshness check together stop a stale file (after a crash or a
# reboot) from looking like a live service.
function Get-Supervisor {
    if (-not (Test-Path $statusFile)) { return $null }
    try { $s = Get-Content $statusFile -Raw | ConvertFrom-Json } catch { return $null }
    if ($s.stopped) { return $null }
    if (-not (Get-Process -Id $s.pid -ErrorAction SilentlyContinue)) { return $null }
    $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$s.updated_at
    if ($age -gt 15) { return $null }
    return $s
}

function Test-Api {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$url/health" -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

function Show-Status {
    $s = Get-Supervisor
    if (-not $s) {
        Write-Host "Interlock is not running."
        Write-Host "  Start it:   .\scripts\start-interlock.ps1"
    } else {
        $up = [TimeSpan]::FromSeconds([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$s.started_at)
        Write-Host ("Interlock is running (supervisor pid {0}, up {1}d {2}h {3}m)." -f $s.pid, $up.Days, $up.Hours, $up.Minutes)
        foreach ($p in $s.children.PSObject.Properties) {
            $c = $p.Value
            $line = "  {0,-7} {1,-9}" -f $p.Name, $c.state
            if ($c.pid) { $line += " pid $($c.pid)" }
            if ($c.restarts -gt 0) { $line += "  restarted $($c.restarts)x (last exit code $($c.last_exit_code))" }
            Write-Host $line
        }
        if (Test-Api) {
            Write-Host "  web UI  $url/"
            try {
                $w = Invoke-RestMethod -Uri "$url/whatsapp/status" -TimeoutSec 3
                Write-Host ("  WhatsApp: {0}{1}" -f $w.state, $(if ($w.account_number) { " as $($w.account_number)" } else { "" }))
            } catch { }
        } else {
            Write-Host "  The API isn't answering yet (it may still be starting)."
        }
    }
    $task = Get-ScheduledTask -TaskName "Interlock" -ErrorAction SilentlyContinue
    $shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Interlock.lnk"
    if ($task -or (Test-Path $shortcut)) {
        Write-Host "  Starts automatically when you log in: yes"
    } else {
        Write-Host "  Starts automatically when you log in: no  (.\scripts\register-autostart.ps1)"
    }
}

function Stop-Interlock {
    $s = Get-Supervisor
    if (-not $s) {
        Write-Host "Interlock isn't running."
        return
    }
    New-Item -ItemType Directory -Force $logs | Out-Null
    Set-Content -Path $stopFile -Value "stop" -Encoding ASCII
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Process -Id $s.pid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
    }
    if (Get-Process -Id $s.pid -ErrorAction SilentlyContinue) {
        # It didn't shut down by itself. Ending it ends its children too (job object).
        Write-Warning "Interlock didn't stop in 30 s; ending it."
        Stop-Process -Id $s.pid -Force
    }
    Write-Host "Interlock stopped."
}

function Start-Interlock {
    if (Get-Supervisor) {
        Write-Host "Interlock is already running: $url/"
        if (-not $NoBrowser) { Start-Process "$url/" }
        return
    }
    if (-not (Test-Path (Join-Path $root "apps\web\dist\index.html"))) {
        Write-Warning "The UI isn't built yet, so $url/ will open /docs. Build it with: cd apps\web; npm install; npm run build"
    }

    # Things started by hand (the old per-window launcher) would collide with the service.
    $stray = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -match 'dist[\\/]src[\\/]index\.js' })
    if ($stray.Count -gt 0 -and -not $NoAgent) {
        Write-Warning "A WhatsApp agent is already running outside the service (pid $($stray[0].ProcessId)). Close it first -- two agents on one WhatsApp link is the one thing that must not happen. The service's own agent will keep waiting until it's gone."
    }
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        Write-Warning "Something is already listening on port 8000 (an old 'Interlock - API + UI' window?). Close it, or the service's API can't start."
    }

    $extra = @()
    if ($NoAgent) { $extra += "--no-agent" }

    if ($Console) {
        Write-Host "Running in this window. Press Ctrl+C to stop."
        Push-Location $root
        try { & $python -m interlock.supervisor --echo @extra } finally { Pop-Location }
        return
    }

    Start-Process -FilePath $pythonw -ArgumentList (@("-m", "interlock.supervisor") + $extra) `
        -WorkingDirectory $root -WindowStyle Hidden
    Write-Host "Starting Interlock in the background..."

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        $up = Test-Api
    } until ($up -or (Get-Date) -gt $deadline)

    if ($up) {
        Write-Host "Interlock is running: $url/   (logs: $logs)"
        if (-not $NoBrowser) { Start-Process "$url/" }
    } else {
        Write-Warning "The API didn't answer within 45 s. Look at $logs\api.log and $logs\supervisor.log."
        exit 1
    }
}

if ($Status) { Show-Status; return }
if ($Stop) { Stop-Interlock; return }
if ($Restart) { Stop-Interlock }
Start-Interlock
