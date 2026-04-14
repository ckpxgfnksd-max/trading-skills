import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.group()
def account():
    """Account and portfolio commands."""


@account.command("list")
@click.pass_context
def list_cmd(ctx):
    """List configured accounts."""
    t = make_transport(ctx)
    resp = t.get("/trade/accounts")
    click.echo(format_output(resp.get("accounts", []), ctx.obj["fmt"]))


@account.command()
@click.option("--account", "name", required=True)
@click.pass_context
def asset(ctx, name):
    """Query account asset."""
    t = make_transport(ctx)
    data = t.get("/trade/asset", params={"account": name})
    click.echo(format_output(data, ctx.obj["fmt"]))


@account.command()
@click.option("--account", "name", required=True)
@click.pass_context
def position(ctx, name):
    """Query positions."""
    t = make_transport(ctx)
    rows = t.get("/trade/positions", params={"account": name})
    click.echo(format_output(rows, ctx.obj["fmt"]))


@account.command()
@click.option("--account", "name", required=True)
@click.pass_context
def orders(ctx, name):
    """Query today's orders."""
    t = make_transport(ctx)
    rows = t.get("/trade/orders", params={"account": name})
    click.echo(format_output(rows, ctx.obj["fmt"]))


@account.command()
@click.option("--account", "name", required=True)
@click.pass_context
def trades(ctx, name):
    """Query today's trades."""
    t = make_transport(ctx)
    rows = t.get("/trade/trades", params={"account": name})
    click.echo(format_output(rows, ctx.obj["fmt"]))
