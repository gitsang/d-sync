<#
.SYNOPSIS
    Register D-Sync to start automatically on Windows (at the current user's login).

.DESCRIPTION
    Creates the scheduled task "D-Sync"; the trigger is user logon and the action starts D-Sync.exe.
    A scheduled task is more reliable than a shortcut in the Startup folder and can report its run state.

.PARAMETER ExePath
    Full path to D-Sync.exe. Defaults to D-Sync.exe next to this script.

.PARAMETER Port
    Dashboard port, default 8765.

.PARAMETER Remove
    Pass this parameter to unregister the task.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_autostart.ps1
    powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#>
param(
    [string]$ExePath = "",
    [int]$Port = 8765,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$TaskName = "D-Sync"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered scheduled task: $TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "Scheduled task not found: $TaskName"
    }
    exit 0
}

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "D-Sync.exe"
}
if (-not (Test-Path $ExePath)) {
    Write-Host "Executable not found: $ExePath" -ForegroundColor Red
    Write-Host "Run python scripts/build_exe.py to build the EXE first, or pass -ExePath." -ForegroundColor Red
    exit 1
}

$ExePath = (Resolve-Path $ExePath).Path
$arguments = "--no-browser --port $Port"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $ExePath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "SD card auto-sync daemon" | Out-Null

Write-Host "Registered autostart task: $TaskName" -ForegroundColor Green
Write-Host "  Program path: $ExePath"
Write-Host "  Arguments: $arguments"
Write-Host "  Dashboard URL: http://127.0.0.1:$Port/"
