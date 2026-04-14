import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.command()
@click.option("--code", required=True)
@click.option("--start", required=True, help="Start datetime")
@click.option("--end", required=True, help="End datetime")
@click.pass_context
def ticks(ctx, code, start, end):
    """Historical tick sequence (not OHLCV; use 'kline' for bars)."""
    t = make_transport(ctx)
    rows = t.get(
        "/data/ticks", params={"code": code, "start": start, "end": end}
    )
    click.echo(format_output(rows, ctx.obj["fmt"]))
