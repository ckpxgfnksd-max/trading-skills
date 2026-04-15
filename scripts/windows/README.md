# Windows deployment scripts

All day-to-day deploys are triggered from macOS via `scripts/deploy.sh`.
The Windows host never runs git, never clones anything. You only touch
Windows once, to run `bootstrap.ps1`.

## One-time setup on Windows

1. Install **Python 3.11+** (python.org installer or Microsoft Store).
   Make sure `python` resolves on PATH for your user.
2. Install and start **miniQMT client** (国金 QMT). Log in and confirm
   the trading account has API authorization enabled. Note the install
   path — you need it for `server.toml` `qmt_path`.
3. Enable **OpenSSH Server** (Settings → Apps → Optional features →
   OpenSSH Server → `Start-Service sshd` →
   `Set-Service -Name sshd -StartupType Automatic`). Put your Mac
   public key into `%USERPROFILE%\.ssh\authorized_keys`.
4. Bootstrap the Scheduled Task. **No Administrator required** — runs
   entirely in user scope:
   ```powershell
   # From Mac:
   scp scripts/windows/bootstrap.ps1 my-win:C:/Users/you/bootstrap.ps1
   # On Windows (ordinary PowerShell, not elevated):
   powershell -ExecutionPolicy Bypass -File C:\Users\you\bootstrap.ps1
   ```
   This uses `Register-ScheduledTask` to create a user-scope
   `MiniqmtDaemon` task that runs `python -m miniqmt_cli.main serve` at
   logon, restarts on failure, and writes stdout+stderr to
   `C:\apps\trading-skills\daemon.log`.
5. Initialise the server config (still on Windows, in any shell):
   ```powershell
   python -m miniqmt_cli.main config server init
   notepad %USERPROFILE%\.miniqmt_cli\server.toml
   ```
   Edit `qmt_path`, `[accounts.sim]`, optionally `[accounts.live]`.
6. Start the task once the config is in place:
   ```powershell
   Start-ScheduledTask -TaskName MiniqmtDaemon
   ```

Done. You will not touch Windows again for routine deploys.

### Why Scheduled Task and not a Windows service

A proper Windows service (via nssm or `sc.exe`) runs continuously even
when no user is logged in, but registering one requires Administrator.
A user-scope Scheduled Task requires no elevation and runs whenever the
user is logged in — which is the case whenever you use the QMT client
anyway, so this is not a practical limitation for this tool.

## Day-to-day deploy (from Mac)

```bash
export WIN_HOST=my-win             # ssh alias or user@host
./scripts/deploy.sh
```

Optional overrides:

```bash
WIN_REPO="C:/apps/trading-skills" \
WIN_PYTHON="python"                 \
WIN_SERVICE="MiniqmtDaemon"         \
./scripts/deploy.sh
```

What it does, in order:

1. Tars the working tree (`tools/miniqmt_cli` + `scripts/windows`),
   excluding `.git`, caches, and venvs. Includes uncommitted changes.
2. `scp` the tarball to `/tmp` on Windows.
3. `ssh` → `tar -xzf` into `$WIN_REPO`.
4. `ssh` → `powershell post-deploy.ps1`, which:
   - `pip install -e tools\miniqmt_cli --quiet` (picks up new deps)
   - import smoke test
   - `Stop-ScheduledTask` + `Start-ScheduledTask MiniqmtDaemon`
   - polls `http://127.0.0.1:8765/health` until `ready` /
     `daemon_up_no_trader` (or explicit xtquant-missing error)
   - prints remote `/version`

Any step that fails aborts the deploy with a clear error message.

## Tunnel for CLI usage (separate from deploy)

Deploy itself only needs plain ssh to the Windows host. The CLI, though,
talks to the daemon via HTTP on `127.0.0.1:8765`. Keep that bound to
loopback and reach it through an SSH local-forward:

```bash
ssh -N -L 8765:127.0.0.1:8765 $WIN_HOST
# in another terminal:
miniqmt-cli health
miniqmt-cli tick --code 000001.SZ
```

Or run both tunnels permanently via `autossh` + `launchd` — see the
deployment notes in the project README.

## Escape hatches

```bash
# Deploy code only, don't touch the service
SKIP_RESTART=1 ./scripts/deploy.sh

# Restart the service but don't wait for /health to come back
SKIP_HEALTH=1 ./scripts/deploy.sh

# Deploy to a different service name (e.g. staging)
WIN_SERVICE=MiniqmtDaemonStaging ./scripts/deploy.sh
```

## Troubleshooting

| Symptom                                          | Fix                                                                 |
|--------------------------------------------------|---------------------------------------------------------------------|
| `tar -xzf` fails with "command not found"        | Windows older than Win10 1803. Upgrade, or install GNU tar manually |
| `Scheduled Task MiniqmtDaemon not registered`     | Re-run `bootstrap.ps1` on Windows                                   |
| `health: daemon_up_xtquant_missing`              | `server.toml` `qmt_path` is wrong or miniQMT not installed there   |
| `pip install` ImportError for `fastapi`          | Python interpreter in `$WIN_PYTHON` is not the one with our deps   |
| `scp` succeeds but files not updated             | `$WIN_REPO` disagreement; check with `ssh $WIN_HOST dir $WIN_REPO` |
| Task restarts but `/health` never becomes ready  | Check `daemon.log` under `$WIN_REPO`; likely xttrader login issue  |
| Task shows state `Ready` but not `Running`        | `Start-ScheduledTask -TaskName MiniqmtDaemon`, then check daemon.log|
