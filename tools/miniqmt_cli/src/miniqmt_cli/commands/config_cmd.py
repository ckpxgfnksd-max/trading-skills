"""Config management subcommands."""
from __future__ import annotations

import json
from pathlib import Path

import click

from miniqmt_cli import client_config, server_config


@click.group("config")
def config_group():
    """Manage miniqmt-cli config files."""


@config_group.group("client")
def client_group():
    """Client config (~/.miniqmt_cli/client.toml)."""


@client_group.command("init")
def client_init():
    """Write a template client.toml if it does not exist."""
    path = client_config.write_template()
    click.echo(f"client config at: {path}")


@client_group.command("show")
@click.pass_context
def client_show(ctx):
    """Print the resolved client config."""
    cfg = ctx.obj["client_cfg"]
    click.echo(json.dumps({
        "mode": cfg.mode,
        "server_url": cfg.server_url,
        "resolved_url": cfg.resolve_url(),
        "output_format": cfg.output_format,
    }, indent=2))


@client_group.command("set-server-url")
@click.argument("url")
def client_set_server_url(url):
    """Write client.server_url into ~/.miniqmt_cli/client.toml."""
    path = Path.home() / ".miniqmt_cli" / "client.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        text = path.read_text()
    else:
        text = client_config.TEMPLATE
    lines = text.splitlines()
    new_lines = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("server_url"):
            new_lines.append(f'server_url = "{url}"')
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f'server_url = "{url}"')
    path.write_text("\n".join(new_lines) + "\n")
    click.echo(f"set server_url={url} in {path}")


@config_group.group("server")
def server_group():
    """Server config (~/.miniqmt_cli/server.toml)."""


@server_group.command("init")
def server_init():
    """Write a template server.toml if it does not exist."""
    path = server_config.write_template()
    click.echo(f"server config at: {path}")


@server_group.command("show")
@click.option("--server-config", "path_override", default=None)
def server_show(path_override):
    """Print the resolved server config (account_id masked)."""
    cfg = server_config.load_server_config(path_override)
    payload = {
        "host": cfg.host,
        "port": cfg.port,
        "qmt_path": cfg.qmt_path,
        "session_id": cfg.resolved_session_id(),
        "accounts": {
            name: {
                "account_id_masked": acc.masked_id(),
                "account_type": acc.account_type,
                "requires_confirm_live": acc.requires_confirm_live,
            }
            for name, acc in cfg.accounts.items()
        },
        "audit_log_path": cfg.audit_log_path,
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
