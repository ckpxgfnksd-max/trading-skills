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

.PARAMETER LogPath
    File where the daemon's stdout+stderr are appended. Default:
    %USERPROFILE%\miniqmt-daemon.log (kept outside $WinRepo so a clean
    redeploy does not nuke historical logs).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\bootstrap.ps1
#>

[CmdletBinding()]
param(
    [string]$WinRepo = "C:\apps\trading-skills",
    [string]$WinPython = "python",
    [string]$TaskName = "MiniqmtDaemon",
    [string]$LogPath = (Join-Path $env:USERPROFILE "miniqmt-daemon.log")
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

Log "daemon log → $LogPath"

# 3. Write a small wrapper .cmd file to avoid Task Scheduler quoting hell.
# The wrapper sets cwd, runs the daemon, and appends stdout+stderr to $LogPath.
$wrapperPath = Join-Path $WinRepo "run-daemon.cmd"
$wrapperBody = @"
@echo off
cd /d "$WinRepo"
"$WinPython" -m miniqmt_cli.main serve >> "$LogPath" 2>&1
"@
Set-Content -Path $wrapperPath -Value $wrapperBody -Encoding ASCII
Log "wrapper script → $wrapperPath"

# 4. Build the Scheduled Task. -Execute points at the .cmd file, so there
# are no nested-quote gymnastics to worry about.
$action = New-ScheduledTaskAction `
    -Execute $wrapperPath `
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

# 4b. Register the nightly-restart task. Runs at 08:50 Mon-Fri so xtquant
# starts the day with a clean snapshot cache; daemon uptime crossing a
# market-close boundary is the empirically supported trigger for the
# `timetag` cache wedge (incident 2026-05-18; see skills/miniqmt-cli/SKILL.md).
$restartTaskName = "${TaskName}NightlyRestart"
$restartScriptPath = Join-Path $WinRepo "scripts\windows\nightly-restart.ps1"

$restartAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$restartScriptPath`" -WinService `"$TaskName`""

$restartTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "08:50"

$restartSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$restartPrincipal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Log "registering Scheduled Task $restartTaskName (Mon-Fri 08:50)"
try {
    Register-ScheduledTask `
        -TaskName $restartTaskName `
        -Action $restartAction `
        -Trigger $restartTrigger `
        -Settings $restartSettings `
        -Principal $restartPrincipal `
        -Description "Nightly restart of $TaskName for clean xtquant cache" `
        -Force | Out-Null
} catch {
    throw "Register-ScheduledTask failed for ${restartTaskName}: $_"
}
$restartTask = Get-ScheduledTask -TaskName $restartTaskName -ErrorAction SilentlyContinue
if (-not $restartTask) {
    throw "task registration succeeded but Get-ScheduledTask cannot find $restartTaskName"
}
Log "$restartTaskName registered (state: $($restartTask.State))"

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
