"""miniqmt-cli root click group."""
from __future__ import annotations

import click

from miniqmt_cli.client_config import load_client_config
from miniqmt_cli.commands.account import account
from miniqmt_cli.commands.config_cmd import config_group
from miniqmt_cli.commands.instrument import instrument
from miniqmt_cli.commands.kline import kline
from miniqmt_cli.commands.order import order
from miniqmt_cli.commands.sector import sector
from miniqmt_cli.commands.server import health, serve, version
from miniqmt_cli.commands.setup import setup
from miniqmt_cli.commands.stream import stream
from miniqmt_cli.commands.tick import tick
from miniqmt_cli.commands.ticks import ticks


@click.group()
@click.option(
    "--format", "fmt", default=None,
    type=click.Choice(["table", "json", "csv"]),
    help="Output format (default: from config, else table)",
)
@click.option("--config", "config_path", default=None, help="Override client config path")
@click.pass_context
def cli(ctx, fmt, config_path):
    """miniqmt-cli: drive miniQMT / xtquant via a persistent HTTP daemon."""
    ctx.ensure_object(dict)
    cfg = load_client_config(config_path)
    if fmt:
        cfg.output_format = fmt
    ctx.obj["client_cfg"] = cfg
    ctx.obj["fmt"] = cfg.output_format


cli.add_command(config_group)
cli.add_command(setup)
cli.add_command(version)
cli.add_command(health)
cli.add_command(serve)
cli.add_command(instrument)
cli.add_command(sector)
cli.add_command(kline)
cli.add_command(tick)
cli.add_command(ticks)
cli.add_command(stream)
cli.add_command(account)
cli.add_command(order)


if __name__ == "__main__":
    cli()
