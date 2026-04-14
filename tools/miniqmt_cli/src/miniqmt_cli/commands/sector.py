import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.group()
def sector():
    """Sector commands."""


@sector.command("list")
@click.pass_context
def list_cmd(ctx):
    """List sectors."""
    t = make_transport(ctx)
    resp = t.get("/data/sectors")
    sectors = resp.get("sectors", [])
    click.echo(format_output([{"sector": s} for s in sectors], ctx.obj["fmt"]))
