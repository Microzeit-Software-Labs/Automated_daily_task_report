<#
.SYNOPSIS
    Makes Interlock start by itself, in the background, whenever you log in to
    Windows -- and (with -Remove) undoes that.

.DESCRIPTION
    Registers a Scheduled Task named "Interlock" for your own account that runs
    the supervisor (src\interlock\supervisor.py) at logon, with no window. It
    needs no administrator rights. If Windows refuses a Scheduled Task, a
    shortcut in your Startup folder does the same job.

    The supervisor takes a lock, so this can never start a second copy -- not
    even if you also run start-interlock.ps1 by hand.

    This replaces the separate "Interlock WhatsApp Agent" task that
    docs\whatsapp-agent-setup.md used to describe: the supervisor runs the agent
    too, and two agents on one WhatsApp link must never happen, so that older task
    is removed if it is there.

.PARAMETER Remove
    Stop Interlock starting at login. Doesn't stop it if it is running now.
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$taskName = "Interlock"
$oldAgentTask = "Interlock WhatsApp Agent"
$shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Interlock.lnk"
$me = "$env:USERDOMAIN\$env:USERNAME"

function Remove-Autostart {
    $removed = $false
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        $removed = $true
    }
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Force
        $removed = $true
    }
    return $removed
}

if ($Remove) {
    if (Remove-Autostart) { Write-Host "Interlock will no longer start at login." }
    else { Write-Host "Interlock wasn't set to start at login." }
    return
}

if (-not (Test-Path $pythonw)) {
    throw "No virtualenv at $pythonw -- see HANDOVER.md section 2."
}

if (Get-ScheduledTask -TaskName $oldAgentTask -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $oldAgentTask -Confirm:$false
    Write-Host "Removed the old '$oldAgentTask' task: the service runs the agent now."
}

[void](Remove-Autostart)   # re-running replaces what was there

try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m interlock.supervisor" -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "Runs Interlock (API, worker, WhatsApp agent) in the background." | Out-Null
    Write-Host "Registered the '$taskName' Scheduled Task: Interlock will start in the background when you log in."
} catch {
    Write-Warning "Windows wouldn't register a Scheduled Task ($($_.Exception.Message)). Using your Startup folder instead."
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $pythonw
    $link.Arguments = "-m interlock.supervisor"
    $link.WorkingDirectory = $root
    $link.WindowStyle = 7
    $link.Description = "Runs Interlock in the background."
    $link.Save()
    Write-Host "Added a Startup shortcut: Interlock will start in the background when you log in."
}

Write-Host ""
Write-Host "Start it now:   .\scripts\start-interlock.ps1"
Write-Host "Check on it:    .\scripts\start-interlock.ps1 -Status"
Write-Host "Undo this:      .\scripts\register-autostart.ps1 -Remove"
