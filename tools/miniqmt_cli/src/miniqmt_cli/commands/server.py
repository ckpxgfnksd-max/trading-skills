"""serve / version / health commands."""
from __future__ import annotations

import click

from miniqmt_cli import __version__
from miniqmt_cli.client.transport import make_transport


@click.command()
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--server-config", "server_config_path", default=None, help="Override server.toml path")
@click.pass_context
def serve(ctx, host, port, dry_run, server_config_path):
    """Start the FastAPI daemon (Windows)."""
    import uvicorn

    from miniqmt_cli.server.app import create_app
    from miniqmt_cli.server_config import load_server_config

    cfg = load_server_config(server_config_path)
    if host:
        cfg.host = host
    if port:
        cfg.port = port

    app = create_app(cfg, dry_run=dry_run)
    click.echo(f"miniqmt-cli daemon listening on http://{cfg.host}:{cfg.port} (dry_run={dry_run})")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


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
