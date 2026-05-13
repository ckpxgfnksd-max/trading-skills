"""Interactive end-to-end setup wizard for miniqmt-cli.

Drives the whole deployment from Mac: collects parameters, checks SSH,
bootstraps Windows (via user-run PowerShell), lays down server config,
runs scripts/deploy.sh, prints tunnel instructions, and runs a final
health smoke test.

Idempotent: each run reads ~/.miniqmt_cli/wizard.json, skips steps that
already completed (with confirmation), and is safe to re-run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import click

from miniqmt_cli import __version__, client_config
from miniqmt_cli.client.transport import make_transport

WIZARD_STATE_PATH = Path.home() / ".miniqmt_cli" / "wizard.json"
CLIENT_CONFIG_PATH = Path.home() / ".miniqmt_cli" / "client.toml"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class WizardState:
    win_host: Optional[str] = None
    win_repo: str = "C:/apps/trading-skills"
    win_python: str = "python"
    win_service: str = "MiniqmtDaemon"
    completed: List[str] = field(default_factory=list)

    def mark(self, step: str) -> None:
        if step not in self.completed:
            self.completed.append(step)
            self.save()

    def is_done(self, step: str) -> bool:
        return step in self.completed

    def save(self) -> None:
        WIZARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WIZARD_STATE_PATH.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "WizardState":
        if not WIZARD_STATE_PATH.exists():
            return cls()
        try:
            data = json.loads(WIZARD_STATE_PATH.read_text())
        except Exception:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def hdr(title: str) -> None:
    click.echo()
    click.secho(f"━━ {title} ━━", fg="cyan", bold=True)


def ok(msg: str) -> None:
    click.secho(f"  ✓ {msg}", fg="green")


def info(msg: str) -> None:
    click.secho(f"  • {msg}", fg="white")


def warn(msg: str) -> None:
    click.secho(f"  ! {msg}", fg="yellow")


def err(msg: str) -> None:
    click.secho(f"  ✗ {msg}", fg="red")


# Emoji-free terminal equivalents — some shells mangle the unicode.
# Swap in ASCII if the user's LANG is not UTF-8.
if (os.environ.get("LANG") or "").lower().find("utf-8") < 0 and \
   (os.environ.get("LC_ALL") or "").lower().find("utf-8") < 0:
    def ok(msg: str) -> None:  # type: ignore
        click.secho(f"  [OK] {msg}", fg="green")
    def warn(msg: str) -> None:  # type: ignore
        click.secho(f"  [!]  {msg}", fg="yellow")
    def err(msg: str) -> None:  # type: ignore
        click.secho(f"  [X]  {msg}", fg="red")


def repo_root() -> Path:
    """Find the checkout root (containing scripts/deploy.sh) from the
    installed miniqmt_cli package. Assumes an editable install under
    tools/miniqmt_cli/src/miniqmt_cli/."""
    import miniqmt_cli
    here = Path(miniqmt_cli.__file__).resolve()
    # .../tools/miniqmt_cli/src/miniqmt_cli/__init__.py → parents[4] = repo root
    for candidate in [here.parents[4], here.parents[3], here.parents[2]]:
        if (candidate / "scripts" / "deploy.sh").exists():
            return candidate
    raise RuntimeError(
        "cannot locate scripts/deploy.sh; the wizard expects an editable "
        "install from the checkout (pip install -e tools/miniqmt_cli)"
    )


def ssh(host: str, cmd: str, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", host, cmd],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def scp(local: Path, host: str, remote: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", str(local), f"{host}:{remote}"],
        capture_output=True, text=True, timeout=timeout,
    )


def should_rerun(step: str, state: WizardState) -> bool:
    """True if the step should execute. Already-done steps ask for confirmation."""
    if not state.is_done(step):
        return True
    return click.confirm(f"  step {step!r} already done — re-run?", default=False)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_params(state: WizardState) -> None:
    hdr("Step 1 / 9 — Parameters")
    state.win_host = click.prompt(
        "  Windows host (ssh alias or user@host)",
        default=state.win_host or "",
        show_default=bool(state.win_host),
    ).strip()
    if not state.win_host:
        raise click.ClickException("WIN_HOST is required")
    state.win_repo = click.prompt(
        "  Remote repo dir", default=state.win_repo
    ).strip()
    state.win_python = click.prompt(
        "  Remote python interpreter", default=state.win_python
    ).strip()
    state.win_service = click.prompt(
        "  Windows service name", default=state.win_service
    ).strip()
    state.mark("params")
    ok(f"host={state.win_host}  repo={state.win_repo}  service={state.win_service}")


def step_local_config(state: WizardState) -> None:
    hdr("Step 2 / 9 — Local client.toml")
    if CLIENT_CONFIG_PATH.exists():
        ok(f"already exists at {CLIENT_CONFIG_PATH}")
    else:
        CLIENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CLIENT_CONFIG_PATH.write_text(client_config.TEMPLATE, encoding="utf-8")
        ok(f"created {CLIENT_CONFIG_PATH}")
    info("Mac CLI will reach the daemon at http://127.0.0.1:8765 via SSH tunnel (step 8).")
    state.mark("local_config")


def step_ssh(state: WizardState) -> None:
    hdr("Step 3 / 9 — SSH connectivity")
    assert state.win_host
    r = ssh(state.win_host, "echo ok", timeout=10)
    if r.returncode != 0 or "ok" not in r.stdout.strip():
        err(f"ssh {state.win_host} failed")
        if r.stderr:
            for line in r.stderr.strip().splitlines():
                click.echo(f"      {line}")
        info("Check: (a) ssh alias in ~/.ssh/config, (b) public key on Windows, (c) OpenSSH Server running.")
        raise click.ClickException("ssh connectivity failed")
    ok(f"ssh {state.win_host} works")
    state.mark("ssh_ok")


def step_remote_python(state: WizardState) -> None:
    hdr("Step 4 / 9 — Remote Python")
    assert state.win_host
    r = ssh(state.win_host, f"{state.win_python} --version", timeout=15)
    if r.returncode != 0:
        err(f"{state.win_python} not found on {state.win_host}")
        if r.stderr:
            click.echo(f"      {r.stderr.strip()}")
        info("Install Python 3.11+ on Windows and put it on PATH, then re-run this step.")
        raise click.ClickException("remote python missing")
    version_line = (r.stdout or r.stderr).strip()
    ok(f"remote: {version_line}")
    if "3.11" not in version_line and "3.12" not in version_line and "3.13" not in version_line:
        warn(f"version does not look like 3.11+; miniqmt-cli requires >=3.11")
    state.mark("remote_python")


def step_bootstrap(state: WizardState) -> None:
    hdr("Step 5 / 9 — Windows service (nssm bootstrap)")
    assert state.win_host
    # Check whether the service already exists.
    r = ssh(state.win_host, f"sc query {state.win_service}", timeout=15)
    if r.returncode == 0 and state.win_service in r.stdout:
        ok(f"service {state.win_service} already registered")
        state.mark("bootstrap")
        return

    warn(f"service {state.win_service} not found on {state.win_host}")
    click.echo()
    click.echo("  The one-time bootstrap must run as Administrator on Windows.")
    click.echo("  It installs nssm (via choco/scoop) and registers the service.")
    click.echo()

    do_upload = click.confirm(
        "  Upload bootstrap.ps1 to the Windows host now?", default=True
    )
    if not do_upload:
        info("Skipping upload. If you already copied bootstrap.ps1, run it as Admin and re-run this step.")
        return

    bootstrap_local = repo_root() / "scripts" / "windows" / "bootstrap.ps1"
    bootstrap_remote = "C:/Users/Public/miniqmt-bootstrap.ps1"
    r = scp(bootstrap_local, state.win_host, bootstrap_remote)
    if r.returncode != 0:
        err(f"scp failed: {r.stderr.strip()}")
        raise click.ClickException("upload failed")
    ok(f"uploaded → {state.win_host}:{bootstrap_remote}")

    click.echo()
    click.secho("  ACTION REQUIRED on Windows:", fg="yellow", bold=True)
    click.echo("    1. Open an Administrator PowerShell on the Windows host.")
    click.echo("    2. Run:")
    click.secho(
        f'       powershell -ExecutionPolicy Bypass -File "{bootstrap_remote.replace("/", chr(92))}"',
        fg="white",
    )
    click.echo("    3. Wait for it to finish (installs nssm, registers service).")
    click.echo()

    if not click.confirm("  Have you completed the bootstrap on Windows?", default=False):
        raise click.ClickException(
            "bootstrap not confirmed; re-run the wizard when ready"
        )

    # Verify.
    r = ssh(state.win_host, f"sc query {state.win_service}", timeout=15)
    if r.returncode != 0 or state.win_service not in r.stdout:
        err(f"service {state.win_service} still not found after bootstrap")
        info("Check the Administrator PowerShell output for errors.")
        raise click.ClickException("bootstrap verification failed")
    ok(f"service {state.win_service} now registered")
    state.mark("bootstrap")


def step_server_config(state: WizardState) -> None:
    hdr("Step 6 / 9 — Remote server.toml")
    assert state.win_host
    # Check whether %USERPROFILE%\.miniqmt_cli\server.toml exists.
    check_cmd = (
        f'{state.win_python} -c '
        '"import os, pathlib; p = pathlib.Path.home() / \'.miniqmt_cli\' / \'server.toml\'; '
        'print(\'EXISTS\' if p.exists() else \'MISSING\')"'
    )
    r = ssh(state.win_host, check_cmd, timeout=20)
    status = (r.stdout or "").strip()
    if "EXISTS" in status:
        ok("server.toml already present on the Windows host")
        info("If you need to reconfigure accounts, edit it on Windows directly.")
        state.mark("server_config")
        return

    warn("no server.toml on the Windows host")
    do_create = click.confirm("  Create the template now?", default=True)
    if not do_create:
        info("Skipping. Create it manually then re-run this step.")
        return

    init_cmd = f"{state.win_python} -m miniqmt_cli.main config server init"
    r = ssh(state.win_host, init_cmd, timeout=30)
    if r.returncode != 0:
        err("config server init failed")
        click.echo(f"      {r.stderr.strip()}")
        info("The CLI may not be installed on Windows yet — that's fine. Run step 7 (deploy) first, then re-run this step.")
        raise click.ClickException("config init failed")
    ok("server.toml template created on Windows")
    click.echo()
    click.secho("  ACTION REQUIRED on Windows:", fg="yellow", bold=True)
    click.echo("    1. Edit %USERPROFILE%\\.miniqmt_cli\\server.toml:")
    click.echo("       - qmt_path  → miniQMT client install directory")
    click.echo("       - [accounts.sim] account_id")
    click.echo("       - [accounts.live] if you trade live (requires_confirm_live = true)")
    click.echo("    2. Save and close the file.")
    click.echo()
    if not click.confirm("  Have you finished editing server.toml?", default=False):
        raise click.ClickException("server.toml not confirmed; re-run when ready")
    state.mark("server_config")


def step_deploy(state: WizardState) -> None:
    hdr("Step 7 / 9 — Deploy code + restart daemon")
    assert state.win_host
    script = repo_root() / "scripts" / "deploy.sh"
    if not script.exists():
        raise click.ClickException(f"deploy.sh not found at {script}")

    env = os.environ.copy()
    env["WIN_HOST"] = state.win_host
    env["WIN_REPO"] = state.win_repo
    env["WIN_PYTHON"] = state.win_python
    env["WIN_SERVICE"] = state.win_service

    info(f"running {script} (streaming output below)")
    click.echo()
    # Stream deploy.sh output live so the user sees progress.
    proc = subprocess.run(["bash", str(script)], env=env)
    click.echo()
    if proc.returncode != 0:
        err(f"deploy.sh exited {proc.returncode}")
        raise click.ClickException("deploy failed")
    ok("deploy.sh finished cleanly")
    state.mark("deploy")


def step_tunnel(state: WizardState) -> None:
    hdr("Step 8 / 9 — SSH tunnel")
    assert state.win_host
    tunnel_cmd = (
        f"ssh -N -L 8765:127.0.0.1:8765 {state.win_host}"
    )
    click.echo("  The CLI talks to the daemon via http://127.0.0.1:8765.")
    click.echo("  Keep a tunnel open in a separate terminal:")
    click.echo()
    click.secho(f"      {tunnel_cmd}", fg="white", bold=True)
    click.echo()

    offer_autossh = click.confirm(
        "  Generate a persistent autossh + launchd configuration for this tunnel?",
        default=False,
    )
    if offer_autossh:
        _emit_autossh_launchd(state.win_host)

    info("When the tunnel is up, the next step will talk through it.")
    if not click.confirm("  Is the tunnel running right now?", default=True):
        warn("Skipping final smoke test; start the tunnel then run 'miniqmt-cli setup --step 9'.")
        return
    state.mark("tunnel")


def _emit_autossh_launchd(host: str) -> None:
    if shutil.which("autossh") is None:
        warn("autossh not installed. Run: brew install autossh")
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.miniqmt.tunnel.plist"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.miniqmt.tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/autossh</string>
        <string>-M</string>
        <string>0</string>
        <string>-N</string>
        <string>-o</string>
        <string>ServerAliveInterval=30</string>
        <string>-o</string>
        <string>ServerAliveCountMax=3</string>
        <string>-o</string>
        <string>ExitOnForwardFailure=yes</string>
        <string>-L</string>
        <string>8765:127.0.0.1:8765</string>
        <string>{host}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/miniqmt-tunnel.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/miniqmt-tunnel.log</string>
</dict>
</plist>
"""
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    ok(f"wrote {plist_path}")
    click.echo("  To load it:")
    click.secho(f"      launchctl unload {plist_path} 2>/dev/null; launchctl load {plist_path}", fg="white")


def step_smoke_test(state: WizardState) -> None:
    hdr("Step 9 / 9 — Smoke test")
    cfg = client_config.load_client_config()
    ctx_obj = {"client_cfg": cfg, "fmt": cfg.output_format}

    class _FakeCtx:
        def __init__(self, obj):
            self.obj = obj

    fake_ctx = _FakeCtx(ctx_obj)
    try:
        t = make_transport(fake_ctx)
    except Exception as e:
        err(f"cannot construct transport: {e}")
        raise click.ClickException("smoke test failed")

    info("GET /version")
    try:
        v = t.get("/version")
        ok(f"daemon version: tag={v.get('tag')} version={v.get('version')}")
    except click.ClickException as e:
        err(f"version probe: {e.message}")
        info("Is the SSH tunnel running? (ssh -N -L 8765:127.0.0.1:8765 <host>)")
        raise

    info("GET /health")
    try:
        h = t.get("/health")
        daemon = h.get("daemon") or {}
        accounts = h.get("accounts") or {}
        dstate = daemon.get("state", "unknown")
        xt_loaded = daemon.get("xtquant_loaded", False)
        if dstate != "up":
            warn(f"health: daemon {dstate} (xtquant_loaded={xt_loaded}) — check server.toml qmt_path")
        else:
            problems = []
            for name, sub in accounts.items():
                tstate = (sub.get("trader") or {}).get("state", "unknown")
                if tstate == "lost":
                    problems.append(f"{name}: trader lost")
                if sub.get("risk_breaker") == "tripped":
                    problems.append(f"{name}: risk breaker tripped")
            if problems:
                warn("health: " + "; ".join(problems))
            else:
                ok(f"health: daemon up, xtquant_loaded={xt_loaded}, {len(accounts)} account(s)")
    except click.ClickException as e:
        err(f"health probe: {e.message}")
        raise

    state.mark("smoke_test")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

STEPS: List[Tuple[str, Callable[[WizardState], None]]] = [
    ("params", step_params),
    ("local_config", step_local_config),
    ("ssh_ok", step_ssh),
    ("remote_python", step_remote_python),
    ("bootstrap", step_bootstrap),
    ("server_config", step_server_config),
    ("deploy", step_deploy),
    ("tunnel", step_tunnel),
    ("smoke_test", step_smoke_test),
]


@click.command("setup")
@click.option("--step", "step_num", type=int, default=None,
              help="Run a single step (1-9) instead of the full wizard")
@click.option("--reset", is_flag=True, default=False,
              help="Delete saved wizard state and start fresh")
def setup(step_num, reset):
    """Interactive end-to-end deployment wizard.

    Walks you from a blank Mac + blank Windows host to a running daemon
    reachable through an SSH tunnel. Re-run safely — the wizard remembers
    which steps are done and asks before re-executing them.
    """
    if reset:
        if WIZARD_STATE_PATH.exists():
            WIZARD_STATE_PATH.unlink()
            click.echo(f"removed {WIZARD_STATE_PATH}")
        return

    click.secho(f"miniqmt-cli setup wizard  (v{__version__})", fg="cyan", bold=True)
    click.echo("=" * 50)
    click.echo("This walks you through every step needed to deploy miniqmt-cli")
    click.echo("from this Mac to a Windows host over SSH.")
    click.echo()

    state = WizardState.load()
    if state.completed:
        info(f"previous state loaded from {WIZARD_STATE_PATH}")
        info(f"already completed: {', '.join(state.completed)}")

    if step_num is not None:
        if not (1 <= step_num <= len(STEPS)):
            raise click.ClickException(f"--step must be 1..{len(STEPS)}")
        name, fn = STEPS[step_num - 1]
        # Force re-run of requested step.
        if name in state.completed:
            state.completed.remove(name)
            state.save()
        fn(state)
        click.echo()
        ok(f"step {step_num} ({name}) done")
        return

    # Full wizard: iterate, skipping completed steps unless user opts in.
    for idx, (name, fn) in enumerate(STEPS, start=1):
        if state.is_done(name):
            hdr(f"Step {idx} / {len(STEPS)} — {name}  (already done)")
            if not click.confirm("  re-run this step?", default=False):
                ok("skipped")
                continue
            state.completed.remove(name)
            state.save()
        fn(state)

    click.echo()
    click.secho("All steps complete. Happy trading.", fg="green", bold=True)
    click.echo(f"State saved at: {WIZARD_STATE_PATH}")
    click.echo("Day-to-day updates: ./scripts/deploy.sh")
