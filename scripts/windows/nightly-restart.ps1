#requires -Version 5.1
<#
.SYNOPSIS
    Stops and restarts the miniqmt-cli daemon Scheduled Task.

.DESCRIPTION
    Invoked by the MiniqmtDaemonNightlyRestart Scheduled Task on
    trading-day mornings. One known-clean xtquant process per day —
    daemon uptime spanning a market-close boundary correlates with the
    xtquant snapshot cache wedging its `timetag` field (incident
    2026-05-18; see skills/miniqmt-cli/SKILL.md).

    Same stop sequence as post-deploy.ps1: stop the task, kill any
    orphan python still holding the daemon port, then start the task.

.PARAMETER WinService
    Scheduled Task name to restart. Default: MiniqmtDaemon.

.PARAMETER Port
    Port the daemon listens on, used to find orphan python processes
    after Stop-ScheduledTask. Default: 8765.
#>

[CmdletBinding()]
param(
    [string]$WinService = "MiniqmtDaemon",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

function Log($msg) {
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

Log "nightly restart: stopping $WinService"
try {
    Stop-ScheduledTask -TaskName $WinService -ErrorAction SilentlyContinue
} catch { }

# Stop-ScheduledTask only kills the action's direct process (the cmd.exe
# wrapper); the child python.exe can outlive it and keep holding the
# port. Mirror the kill-orphan loop used in post-deploy.ps1.
for ($i = 0; $i -lt 10; $i++) {
    $owners = @()
    try {
        $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { $_.OwningProcess } |
            Where-Object { $_ -and $_ -gt 0 } |
            Select-Object -Unique
    } catch { }
    if ($owners.Count -eq 0) { break }
    foreach ($procId in $owners) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    Start-Sleep -Milliseconds 500
}
Start-Sleep -Seconds 1

Log "nightly restart: starting $WinService"
Start-ScheduledTask -TaskName $WinService
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $WinService
Log "task state: $($task.State)"
