import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.client.errors import GuardExit
from miniqmt_cli.output import format_output


@click.group()
def instrument():
    """Instrument metadata commands."""


@instrument.command("list")
@click.option("--sector", default=None, help="Sector name (e.g. '沪深A股')")
@click.option("--limit", default=None, type=int, help="Cap the number of rows")
@click.pass_context
def list_cmd(ctx, sector, limit):
    """List instruments. One of --sector or --limit is required."""
    if not sector and not limit:
        raise GuardExit("one of --sector or --limit is required")
    t = make_transport(ctx)
    resp = t.get(
        "/data/instruments",
        params={k: v for k, v in {"sector": sector, "limit": limit}.items() if v is not None},
    )
    codes = resp.get("codes", [])
    click.echo(format_output([{"code": c} for c in codes], ctx.obj["fmt"]))


@instrument.command()
@click.option("--code", required=True)
@click.pass_context
def info(ctx, code):
    """Get instrument detail."""
    t = make_transport(ctx)
    data = t.get("/data/instrument", params={"code": code})
    click.echo(format_output(data, ctx.obj["fmt"]))
