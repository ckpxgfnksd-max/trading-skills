#requires -Version 5.1
<#
.SYNOPSIS
    Runs on Windows via ssh, triggered from Mac-side scripts/deploy.sh.

.DESCRIPTION
    Installs (editable) the miniqmt_cli package from the deployed tree,
    optionally restarts the MiniqmtDaemon Scheduled Task, and probes
    /health. Idempotent.

.PARAMETER WinRepo
    Install directory containing tools\miniqmt_cli (forward slashes ok).

.PARAMETER WinPython
    Python interpreter to invoke. Typically 'python' or a full path.

.PARAMETER WinService
    Scheduled Task name to restart. Default: MiniqmtDaemon.

.PARAMETER SkipRestart
    Skip the task restart step.

.PARAMETER SkipHealth
    Skip the post-restart health probe.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$WinRepo,
    [string]$WinPython = "python",
    [string]$WinService = "MiniqmtDaemon",
    [switch]$SkipRestart,
    [switch]$SkipHealth
)

$ErrorActionPreference = "Stop"

function Log($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

$RepoPath = $WinRepo.Replace("/", "\")
if (-not (Test-Path $RepoPath)) {
    throw "repo path not found: $RepoPath"
}
Set-Location $RepoPath

Log "pip install -e tools\miniqmt_cli (editable, quiet)"
& $WinPython -m pip install -e "tools\miniqmt_cli" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Log "import smoke test"
& $WinPython -c "import miniqmt_cli; print('miniqmt_cli', miniqmt_cli.__version__)"
if ($LASTEXITCODE -ne 0) { throw "import failed" }

if (-not $SkipRestart) {
    $task = Get-ScheduledTask -TaskName $WinService -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Warning "Scheduled Task $WinService not registered; skipping restart. Run bootstrap.ps1 first."
    } else {
        Log "restarting Scheduled Task $WinService"
        try {
            Stop-ScheduledTask -TaskName $WinService -ErrorAction SilentlyContinue
        } catch {
            # Task may not be running; that's fine.
        }
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $WinService
        Start-Sleep -Seconds 1
        $task = Get-ScheduledTask -TaskName $WinService
        Log "task state: $($task.State)"
    }
}

if (-not $SkipHealth) {
    Log "health probe http://127.0.0.1:8765/health"
    $ok = $false
    for ($i = 1; $i -le 10; $i++) {
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
            $state = $resp.state
            Log "  health: $state"
            if ($state -in @("ready", "daemon_up_no_trader")) {
                $ok = $true
                break
            }
            if ($state -eq "daemon_up_xtquant_missing") {
                Write-Warning "xtquant failed to load — check server.toml qmt_path"
                $ok = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $ok) {
        throw "health probe timed out after 10 tries"
    }

    Log "version probe"
    try {
        $v = Invoke-RestMethod -Uri "http://127.0.0.1:8765/version" -TimeoutSec 2
        Write-Host "  remote: tag=$($v.tag) version=$($v.version)"
    } catch {
        Write-Warning "version probe failed: $_"
    }
}

Log "post-deploy done."
