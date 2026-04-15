#!/usr/bin/env bash
# Mac-driven deploy for miniqmt-cli.
#
# Packages the local working tree (including uncommitted changes), ships it to
# a Windows host over SSH, extracts it, runs pip install, and restarts the
# daemon Windows service. Fully driven from macOS — Windows never touches git.
#
# Prerequisites (one-time, on Windows): run scripts/windows/bootstrap.ps1.
# See scripts/windows/README.md.
#
# Usage:
#   WIN_HOST=my-win ./scripts/deploy.sh
#
# Optional env vars (defaults in parens):
#   WIN_HOST        ssh alias or user@host (required)
#   WIN_REPO        remote install directory  (C:/apps/trading-skills)
#   WIN_PYTHON      remote python interpreter (python)
#   WIN_SERVICE     nssm service name         (MiniqmtDaemon)
#   SKIP_RESTART    set to 1 to skip service restart
#   SKIP_HEALTH     set to 1 to skip health check

set -euo pipefail

WIN_HOST="${WIN_HOST:?set WIN_HOST (ssh alias or user@host)}"
WIN_REPO="${WIN_REPO:-C:/apps/trading-skills}"
WIN_PYTHON="${WIN_PYTHON:-python}"
WIN_SERVICE="${WIN_SERVICE:-MiniqmtDaemon}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARBALL="/tmp/miniqmt-deploy-${STAMP}.tar.gz"
# Stage the tarball inside $WIN_REPO itself — avoids Unix-style /tmp
# assumptions on Windows and works as long as $WIN_REPO already exists
# (which bootstrap.ps1 guarantees).
REMOTE_TARBALL_REL="_deploy-${STAMP}.tar.gz"
REMOTE_TARBALL="${WIN_REPO}/${REMOTE_TARBALL_REL}"

cleanup() {
  rm -f "$TARBALL"
}
trap cleanup EXIT

log() { printf "\033[36m==>\033[0m %s\n" "$*"; }

log "packaging working tree into $TARBALL"
tar \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./venv' \
  --exclude='./node_modules' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='./tmp' \
  -czf "$TARBALL" \
  tools/miniqmt_cli \
  scripts/windows

SIZE="$(du -h "$TARBALL" | awk '{print $1}')"
log "tarball size: $SIZE"

log "scp → $WIN_HOST:$REMOTE_TARBALL"
scp -q "$TARBALL" "$WIN_HOST:$REMOTE_TARBALL"

log "extracting on $WIN_HOST into $WIN_REPO"
# Windows 10+ ships tar.exe; this works under cmd.exe default shell.
WIN_REPO_BACK="${WIN_REPO//\//\\}"
ssh "$WIN_HOST" "if not exist \"$WIN_REPO_BACK\" mkdir \"$WIN_REPO_BACK\"" || true
ssh "$WIN_HOST" "cd /d \"$WIN_REPO_BACK\" && tar -xzf \"$REMOTE_TARBALL_REL\" && del \"$REMOTE_TARBALL_REL\""

log "running post-deploy script on $WIN_HOST"
# Pass arguments without outer quotes. cmd.exe does not strip single
# quotes, so they would leak into the PowerShell parameter values.
# WIN_REPO, WIN_PYTHON, WIN_SERVICE must not contain spaces.
ARGS="-WinRepo $WIN_REPO -WinPython $WIN_PYTHON -WinService $WIN_SERVICE"
if [[ "${SKIP_RESTART:-0}" == "1" ]]; then
  ARGS="$ARGS -SkipRestart"
fi
if [[ "${SKIP_HEALTH:-0}" == "1" ]]; then
  ARGS="$ARGS -SkipHealth"
fi
ssh "$WIN_HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File \"${WIN_REPO_BACK}\\scripts\\windows\\post-deploy.ps1\" $ARGS"

log "done."
