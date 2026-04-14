import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.client.errors import GuardExit
from miniqmt_cli.output import format_output


@click.command()
@click.option("--code", required=True)
@click.option("--period", required=True, type=click.Choice(["1d", "1m", "5m"]))
@click.option("--start", required=True, help="Start time (YYYYMMDD or YYYYMMDDHHMMSS)")
@click.option("--end", required=True, help="End time")
@click.pass_context
def kline(ctx, code, period, start, end):
    """Fetch historical OHLCV bars (not ticks; use 'ticks' for raw ticks)."""
    if period == "tick":
        raise GuardExit("period=tick is not a kline period; use 'miniqmt-cli ticks'")
    t = make_transport(ctx)
    rows = t.get(
        "/data/kline",
        params={"code": code, "period": period, "start": start, "end": end},
    )
    click.echo(format_output(rows, ctx.obj["fmt"]))
