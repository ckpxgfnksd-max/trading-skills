import json

import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_row


@click.group()
def stream():
    """Real-time streaming commands (Ctrl+C to stop)."""


@stream.command("tick")
@click.option("--code", "codes", required=True, multiple=True)
@click.pass_context
def stream_tick(ctx, codes):
    """Stream live ticks."""
    t = make_transport(ctx)
    try:
        for event in t.stream("/stream/tick", params=[("code", c) for c in codes]):
            click.echo(format_row(event, ctx.obj["fmt"]))
    except KeyboardInterrupt:
        pass


@stream.command("kline")
@click.option("--code", "codes", required=True, multiple=True)
@click.option("--period", default="1m", type=click.Choice(["1m", "5m"]))
@click.pass_context
def stream_kline(ctx, codes, period):
    """Stream live klines."""
    t = make_transport(ctx)
    params = [("code", c) for c in codes] + [("period", period)]
    try:
        for event in t.stream("/stream/kline", params=params):
            click.echo(format_row(event, ctx.obj["fmt"]))
    except KeyboardInterrupt:
        pass


@stream.command("order")
@click.option("--account", default=None, help="Filter by account name")
@click.pass_context
def stream_order(ctx, account):
    """Stream order status events (Ctrl+C to stop)."""
    t = make_transport(ctx)
    params = {"account": account} if account else None
    try:
        for event in t.stream("/stream/order", params=params):
            click.echo(format_row(event, ctx.obj["fmt"]))
    except KeyboardInterrupt:
        pass
