#requires -Version 5.1
<#
.SYNOPSIS
    One-time bootstrap for the miniqmt-cli daemon host.

.DESCRIPTION
    Run ONCE on the Windows host. Registers a user-scope Scheduled Task
    that launches the miniqmt-cli daemon at logon, with automatic restart
    on failure. Requires NO Administrator privileges and NO external
    package manager — only Python and the tools built into Windows.

    After this script runs, the Mac-driven scripts/deploy.sh restarts the
    task via Stop-ScheduledTask / Start-ScheduledTask on every deploy.

.PARAMETER WinRepo
    Directory where Mac will deploy the tree. Default: C:\apps\trading-skills

.PARAMETER WinPython
    Python interpreter to bake into the task action. Default: python

.PARAMETER TaskName
    Scheduled Task name. Default: MiniqmtDaemon

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\bootstrap.ps1
#>

[CmdletBinding()]
param(
    [string]$WinRepo = "C:\apps\trading-skills",
    [string]$WinPython = "python",
    [string]$TaskName = "MiniqmtDaemon"
)

$ErrorActionPreference = "Stop"

function Log($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# 1. Python check
Log "checking Python"
$pyVersion = & $WinPython --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python not found: $WinPython. Install Python 3.11+ first."
}
Write-Host "  $pyVersion"

# 2. Ensure working directory exists
if (-not (Test-Path $WinRepo)) {
    Log "creating $WinRepo"
    New-Item -ItemType Directory -Path $WinRepo -Force | Out-Null
}

$logPath = Join-Path $WinRepo "daemon.log"
Log "daemon log → $logPath"

# 3. Build the Scheduled Task
# cmd.exe wraps `python -m miniqmt_cli.main serve` and redirects stdout+stderr
# to daemon.log, relative to the task's working directory.
$cmdArgs = "/c `"python -m miniqmt_cli.main serve >> `"`"$logPath`"`" 2>&1`""

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $cmdArgs `
    -WorkingDirectory $WinRepo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Log "registering Scheduled Task $TaskName"
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "miniqmt-cli daemon (managed by scripts/deploy.sh)" `
        -Force | Out-Null
} catch {
    throw "Register-ScheduledTask failed: $_"
}

# 4. Quick sanity probe — task exists and can be queried
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "task registration succeeded but Get-ScheduledTask cannot find $TaskName"
}
Log "$TaskName registered (state: $($task.State))"

# 5. Config sanity
$serverCfg = Join-Path $env:USERPROFILE ".miniqmt_cli\server.toml"
if (-not (Test-Path $serverCfg)) {
    Log "no server.toml found at $serverCfg"
    Write-Host "    Before starting the task, run:" -ForegroundColor Yellow
    Write-Host "      $WinPython -m miniqmt_cli.main config server init"
    Write-Host "    Then edit $serverCfg (qmt_path, accounts)."
    Log "task is registered but NOT started. Start it manually after configuring."
    exit 0
}

Log "starting $TaskName"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 1
$task = Get-ScheduledTask -TaskName $TaskName
Log "task state: $($task.State)"

Log "bootstrap done. Day-to-day deploys: ./scripts/deploy.sh from Mac."
