<#
.SYNOPSIS
    将 D-Sync 注册为 Windows 开机自启（当前用户登录时启动）。

.DESCRIPTION
    创建计划任务 "D-Sync"，触发器为用户登录时，操作为启动 D-Sync.exe。
    相比写入「启动」文件夹，计划任务方式更稳定，且可显示运行状态。

.PARAMETER ExePath
    D-Sync.exe 的完整路径。默认为脚本同目录下的 D-Sync.exe。

.PARAMETER Port
    看板端口，默认 8765。

.PARAMETER Remove
    传入此参数则卸载已注册的任务。

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
        Write-Host "已卸载计划任务: $TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "未找到计划任务: $TaskName"
    }
    exit 0
}

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "D-Sync.exe"
}
if (-not (Test-Path $ExePath)) {
    Write-Host "未找到可执行文件: $ExePath" -ForegroundColor Red
    Write-Host "请先执行 python scripts/build_exe.py 生成 EXE，或用 -ExePath 指定路径。" -ForegroundColor Red
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
    -Settings $settings -Principal $principal -Description "SD 卡自动同步守护程序" | Out-Null

Write-Host "已注册开机自启任务: $TaskName" -ForegroundColor Green
Write-Host "  程序路径: $ExePath"
Write-Host "  启动参数: $arguments"
Write-Host "  看板地址: http://127.0.0.1:$Port/"
