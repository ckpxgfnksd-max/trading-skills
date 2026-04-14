import click

from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.command()
@click.option("--code", "codes", required=True, multiple=True)
@click.pass_context
def tick(ctx, codes):
    """Latest tick snapshot for one or more codes."""
    t = make_transport(ctx)
    raw = t.get("/data/tick", params=[("code", c) for c in codes])
    rows = []
    if isinstance(raw, dict):
        for code, snap in raw.items():
            row = {"code": code}
            if isinstance(snap, dict):
                row.update(snap)
            rows.append(row)
    click.echo(format_output(rows, ctx.obj["fmt"]))
