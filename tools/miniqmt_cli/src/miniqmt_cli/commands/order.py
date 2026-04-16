"""Order placement and cancellation with three-layer safety (CLI layer)."""
from __future__ import annotations

import uuid

import click

from miniqmt_cli.client.errors import BrokerReject, GuardExit
from miniqmt_cli.client.transport import make_transport
from miniqmt_cli.output import format_output


@click.group()
def order():
    """Trading commands. All guards fire CLI-side and daemon-side independently."""


def _common_opts(f):
    f = click.option("--account", required=True)(f)
    f = click.option("--code", required=True)(f)
    f = click.option("--volume", required=True, type=int)(f)
    f = click.option("--price", required=True, type=float)(f)
    f = click.option("--type", "order_type", default="limit", type=click.Choice(["limit", "market"]))(f)
    f = click.option("--dry-run", is_flag=True, default=False)(f)
    f = click.option("--yes", is_flag=True, default=False)(f)
    f = click.option("--confirm-live", default=None, help="Last 4 digits of live account_id")(f)
    f = click.option("--wait", "wait_secs", default=None, type=int, help="Wait N seconds for terminal order status")(f)
    return f


@order.command()
@_common_opts
@click.pass_context
def buy(ctx, account, code, volume, price, order_type, dry_run, yes, confirm_live, wait_secs):
    """Buy order."""
    _place(ctx, "buy", account, code, volume, price, order_type, dry_run, yes, confirm_live, wait_secs)


@order.command()
@_common_opts
@click.pass_context
def sell(ctx, account, code, volume, price, order_type, dry_run, yes, confirm_live, wait_secs):
    """Sell order."""
    _place(ctx, "sell", account, code, volume, price, order_type, dry_run, yes, confirm_live, wait_secs)


@order.command("cancel")
@click.option("--account", required=True)
@click.option("--order-id", "order_id", required=True, type=int)
@click.option("--yes", is_flag=True, default=False)
@click.pass_context
def cancel_cmd(ctx, account, order_id, yes):
    """Cancel an outstanding order."""
    t = make_transport(ctx)
    click.echo(f"Account:   {account}")
    click.echo(f"Order id:  {order_id}")
    click.echo("-" * 31)
    if not yes:
        confirmation = click.prompt('Type "yes" to confirm', default="", show_default=False)
        if confirmation.strip().lower() != "yes":
            raise GuardExit("cancel declined by user")
    req_id = str(uuid.uuid4())
    resp = t.post(
        "/trade/cancel",
        body={"account": account, "order_id": order_id, "client_req_id": req_id},
    )
    if resp.get("status") != "ok":
        raise BrokerReject(f"cancel rejected: {resp}")
    click.echo(format_output(resp, ctx.obj["fmt"]))


TERMINAL_STATUSES = {"filled", "cancelled", "rejected"}


def _place(ctx, side, account, code, volume, price, order_type, dry_run, yes, confirm_live, wait_secs):
    t = make_transport(ctx)
    meta = t.get("/trade/account/meta", params={"name": account})
    if meta.get("requires_confirm_live"):
        if not confirm_live:
            raise GuardExit(
                f"account {account!r} requires --confirm-live <last4 digits of account_id>"
            )
        if not (isinstance(confirm_live, str) and len(confirm_live) == 4 and confirm_live.isdigit()):
            raise GuardExit("--confirm-live value must be exactly 4 digits")

    preview = t.get(
        "/trade/preview",
        params={
            "account": account,
            "code": code,
            "side": side,
            "volume": volume,
            "price": price,
        },
    )
    click.echo(f"Account:   {account} ({preview.get('account_id_masked')})")
    click.echo(f"Code:      {code}")
    click.echo(f"Side:      {side.upper()}")
    click.echo(f"Volume:    {volume}")
    click.echo(f"Price:     {price}  ({order_type})")
    last_price = preview.get("last_price")
    if last_price is not None:
        click.echo(f"Last:      {last_price}")
    click.echo(f"Est.Cost:  {preview.get('est_cost')}")
    click.echo("-" * 31)

    if dry_run:
        raise GuardExit("dry-run: order not sent")

    if not yes:
        confirmation = click.prompt('Type "yes" to confirm', default="", show_default=False)
        if confirmation.strip().lower() != "yes":
            raise GuardExit("order declined by user")

    body = {
        "account": account,
        "code": code,
        "side": side,
        "volume": volume,
        "price": price,
        "type": order_type,
        "client_req_id": str(uuid.uuid4()),
        "confirm_live_last4": confirm_live,
    }
    resp = t.post("/trade/order", body=body)
    if resp.get("status") == "rejected":
        raise BrokerReject(f"broker rejected: seq={resp.get('seq')}")
    click.echo(format_output(resp, ctx.obj["fmt"]))

    if wait_secs and resp.get("seq"):
        _wait_for_terminal(t, account, resp["seq"], wait_secs)


def _wait_for_terminal(t, account, seq, timeout):
    """Stream order events and wait for a terminal status matching seq."""
    import time

    click.echo(f"等待成交... (最多{timeout}秒)")
    deadline = time.time() + timeout
    try:
        for event in t.stream("/stream/order", params={"account": account}):
            if time.time() > deadline:
                break
            evt_type = event.get("type")
            if evt_type == "order_status" and event.get("order_id") == seq:
                status = event.get("status", "")
                filled = event.get("filled_volume", 0)
                avg_px = event.get("avg_price", 0)
                click.echo(f"  [{status}] {event.get('code')} {event.get('side', '').upper()} "
                           f"{filled}股 @ {avg_px}")
                if status in TERMINAL_STATUSES:
                    return
            elif evt_type == "trade" and event.get("order_id") == seq:
                click.echo(f"  [成交] {event.get('code')} {event.get('volume')}股 @ {event.get('price')}")
    except KeyboardInterrupt:
        pass
    except Exception:
        pass
    click.echo(f"等待超时 ({timeout}秒)")
    ctx = click.get_current_context()
    ctx.exit(1)
