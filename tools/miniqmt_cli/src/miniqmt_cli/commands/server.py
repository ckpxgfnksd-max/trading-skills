"""serve / version / health commands."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import click

from miniqmt_cli import __version__
from miniqmt_cli.client.transport import make_transport


DEFAULT_LOG_PATH = "~/.miniqmt_cli/daemon.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
LOG_BACKUPS = 7


def _uvicorn_log_config(log_file: str) -> dict:
    """Build a uvicorn-compatible dictConfig that emits timestamped lines to
    both stdout and a rotating file. Prior daemons logged via the bare
    uvicorn default config — no timestamps, no file — so we lost the entire
    forensic record across deadlock restarts. This fixes that."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "stamped": {
                "format": LOG_FORMAT,
                "datefmt": LOG_DATEFMT,
            },
            "stamped_access": {
                # Uvicorn's access logger passes (host, request, status_code)
                # as %(message)s -- so the stamped formatter works as-is.
                "format": LOG_FORMAT,
                "datefmt": LOG_DATEFMT,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "stamped",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "stamped",
                "filename": log_file,
                "maxBytes": LOG_MAX_BYTES,
                "backupCount": LOG_BACKUPS,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "": {  # root — catches miniqmt_cli.* and everything else
                "handlers": ["console", "file"],
                "level": "INFO",
            },
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


@click.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--server-config", "server_config_path", default=None, help="Override server.toml path")
@click.option(
    "--log-file", default=None,
    help=f"Path for rotating daemon log (default: {DEFAULT_LOG_PATH}). Set to '-' to disable file logging.",
)
@click.pass_context
def serve(ctx, host, port, dry_run, server_config_path, log_file):
    """Start the FastAPI daemon (Windows)."""
    import uvicorn

    from miniqmt_cli.server.app import create_app
    from miniqmt_cli.server_config import load_server_config

    cfg = load_server_config(server_config_path)
    if host:
        cfg.host = host
    if port:
        cfg.port = port

    log_path = log_file if log_file else DEFAULT_LOG_PATH
    if log_path == "-":
        log_config = None  # uvicorn falls back to its default console-only config
    else:
        resolved = Path(log_path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        log_config = _uvicorn_log_config(str(resolved))

    app = create_app(cfg, dry_run=dry_run)
    click.echo(
        f"miniqmt-cli daemon listening on http://{cfg.host}:{cfg.port} "
        f"(dry_run={dry_run}, log={log_path})"
    )
    uvicorn.run(
        app, host=cfg.host, port=cfg.port, log_level="info",
        log_config=log_config,
    )


@click.command()
@click.pass_context
def version(ctx):
    """Show local version, and remote daemon version if reachable."""
    click.echo(f"miniqmt-cli {__version__} (local)")
    try:
        t = make_transport(ctx)
        data = t.get("/version")
        click.echo(f"remote: tag={data.get('tag')} version={data.get('version')}")
    except Exception:
        click.echo(f"remote: unreachable ({ctx.obj['client_cfg'].resolve_url()})")


@click.command()
@click.pass_context
def health(ctx):
    """Health check against the daemon."""
    t = make_transport(ctx)
    try:
        data = t.get("/health")
    except click.ClickException as e:
        click.echo(f"state: daemon_down ({e.message})")
        ctx.exit(1)
    state = data.get("state", "unknown")
    click.echo(f"state: {state}")
    if state in ("ready", "daemon_up_no_trader"):
        ctx.exit(0)
    ctx.exit(1)
