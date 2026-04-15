#requires -Version 5.1
<#
.SYNOPSIS
    One-time bootstrap for the miniqmt-cli daemon host.

.DESCRIPTION
    Run ONCE on the Windows host, as Administrator. Installs nssm if missing,
    registers the MiniqmtDaemon Windows service pointing at the deployed
    miniqmt-cli entry point, and starts it. After this, scripts/deploy.sh on
    Mac handles every subsequent deploy.

.PARAMETER WinRepo
    Directory where Mac will deploy the tree. Default: C:\apps\trading-skills

.PARAMETER WinPython
    Python interpreter to bake into the service. Default: python

.PARAMETER ServiceName
    nssm service name. Default: MiniqmtDaemon

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\bootstrap.ps1
#>

[CmdletBinding()]
param(
    [string]$WinRepo = "C:\apps\trading-skills",
    [string]$WinPython = "python",
    [string]$ServiceName = "MiniqmtDaemon"
)

$ErrorActionPreference = "Stop"

function Log($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# 1. Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "bootstrap.ps1 must run as Administrator"
}

# 2. Check Python
Log "checking Python"
$pyVersion = & $WinPython --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python not found: $WinPython. Install Python 3.11+ first."
}
Write-Host "  $pyVersion"

# 3. Ensure target dir exists
if (-not (Test-Path $WinRepo)) {
    Log "creating $WinRepo"
    New-Item -ItemType Directory -Path $WinRepo | Out-Null
}

# 4. Install nssm if missing
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    $choco = Get-Command choco -ErrorAction SilentlyContinue
    $scoop = Get-Command scoop -ErrorAction SilentlyContinue
    if ($choco) {
        Log "installing nssm via choco"
        & choco install -y nssm
    } elseif ($scoop) {
        Log "installing nssm via scoop"
        & scoop install nssm
    } else {
        throw @"
nssm is not installed and neither choco nor scoop is available.
Install nssm manually from https://nssm.cc and re-run this script.
"@
    }
    $nssm = Get-Command nssm -ErrorAction Stop
}
Log "nssm at $($nssm.Path)"

# 5. Register or reconfigure the service
$pyFull = (Get-Command $WinPython -ErrorAction Stop).Source
Log "python at $pyFull"

$existing = & nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Log "service $ServiceName exists, reconfiguring"
    & nssm set $ServiceName Application $pyFull | Out-Host
    & nssm set $ServiceName AppParameters "-m miniqmt_cli.main serve" | Out-Host
    & nssm set $ServiceName AppDirectory $WinRepo | Out-Host
} else {
    Log "registering service $ServiceName"
    & nssm install $ServiceName $pyFull "-m" "miniqmt_cli.main" "serve" | Out-Host
    & nssm set $ServiceName AppDirectory $WinRepo | Out-Host
}
& nssm set $ServiceName AppStdout  (Join-Path $WinRepo "daemon.log") | Out-Host
& nssm set $ServiceName AppStderr  (Join-Path $WinRepo "daemon.log") | Out-Host
& nssm set $ServiceName AppRotateFiles 1 | Out-Host
& nssm set $ServiceName AppRotateBytes 10485760 | Out-Host
& nssm set $ServiceName Start SERVICE_AUTO_START | Out-Host

# 6. Config sanity
$serverCfg = Join-Path $env:USERPROFILE ".miniqmt_cli\server.toml"
if (-not (Test-Path $serverCfg)) {
    Log "no server.toml found at $serverCfg"
    Write-Host "    Before starting the service, run:" -ForegroundColor Yellow
    Write-Host "      $WinPython -m miniqmt_cli.main config server init"
    Write-Host "    Then edit $serverCfg (qmt_path, accounts)."
    Log "service is registered but NOT started. Start it manually after configuring."
    exit 0
}

Log "starting $ServiceName"
& nssm start $ServiceName | Out-Host

Log "bootstrap done. Next step: run scripts/deploy.sh from Mac on every change."
