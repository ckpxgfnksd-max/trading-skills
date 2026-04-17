"""CLI: miniqmt-cli risk status / reset."""
from __future__ import annotations

import json

import click

from miniqmt_cli.client.errors import GuardExit, RiskReject
from miniqmt_cli.client.transport import make_transport


@click.group()
def risk():
    """Risk control: view status, reset circuit breaker."""


@risk.command("status")
@click.option("--account", default=None, help="Specific account; otherwise list all.")
@click.pass_context
def status_cmd(ctx, account):
    t = make_transport(ctx)
    params = {"account": account} if account else None
    body = t.get("/risk/status", params=params)
    fmt = ctx.obj.get("fmt", "table")
    if fmt == "json":
        click.echo(json.dumps(body, ensure_ascii=False, indent=2))
        return
    if account:
        _render_account_status(account, body)
    else:
        for name, data in (body.get("accounts") or {}).items():
            _render_account_status(name, data)
            click.echo("")


def _render_account_status(name: str, data: dict) -> None:
    click.echo(f"Account: {name}")
    click.echo(f"  Trade date:      {data.get('trade_date') or '(not captured)'}")
    base = data.get("baseline_total_asset")
    cap = data.get("baseline_captured_at")
    imprecise = data.get("baseline_imprecise")
    if base is not None:
        flag = " (imprecise)" if imprecise else ""
        click.echo(f"  Baseline asset:  {base:,.2f}  (captured {cap}{flag})")
    curr = data.get("current_total_asset")
    if curr is not None:
        click.echo(f"  Current asset:   {curr:,.2f}")
    pnl = data.get("daily_pnl")
    if pnl is not None:
        click.echo(f"  Daily PnL:       {pnl:+,.2f}")
    tripped = data.get("breaker_tripped")
    if tripped:
        click.echo(
            f"  Breaker:         TRIPPED at {data.get('breaker_tripped_at')} "
            f"-- \"{data.get('breaker_reason')}\""
        )
    else:
        click.echo("  Breaker:         OK")
    eff = data.get("effective_config") or {}
    click.echo(
        f"  Config:          max_loss={eff.get('max_daily_loss')} "
        f"max_pos_pct={eff.get('max_position_pct')}% "
        f"max_freq={eff.get('max_orders_per_minute')}/min "
        f"max_positions={eff.get('max_positions')}"
    )
    click.echo(f"  Orders in 60s:   {data.get('orders_in_window')}")
    pending = data.get("pending_orders") or {}
    if pending:
        for code, e in pending.items():
            click.echo(
                f"  Pending:         {code}: +{e['buy_volume']} ({e['buy_amount']:,.2f})"
            )
    click.echo(f"  Resets today:    {data.get('reset_count_today', 0)}")


@risk.command("reset")
@click.option("--account", required=True)
@click.option("--note", required=True, help="Operator justification (required for audit).")
@click.option("--confirm-live", default=None, help="Last 4 digits of live account_id")
@click.option("--yes", is_flag=True, default=False)
@click.pass_context
def reset_cmd(ctx, account, note, confirm_live, yes):
    t = make_transport(ctx)
    try:
        status_body = t.get("/risk/status", params={"account": account})
    except click.ClickException:
        # preserve daemon-side error messages (e.g. 400 confirm_live_last4); do not wrap
        raise
    except Exception as e:
        raise RiskReject("STATUS_FAILED", str(e))
    if not status_body.get("breaker_tripped"):
        raise RiskReject("NOT_TRIPPED", f"breaker for {account} is not tripped")
    reason = status_body.get("breaker_reason")
    tripped_at = status_body.get("breaker_tripped_at")
    click.echo(f"Account:       {account}")
    click.echo(f"Tripped at:    {tripped_at}")
    click.echo(f"Reason:        {reason}")
    click.echo(f"Note:          {note}")
    click.echo("-" * 31)
    if not yes:
        confirmation = click.prompt('Type "yes" to confirm', default="", show_default=False)
        if confirmation.strip().lower() != "yes":
            raise GuardExit("reset declined by user")
    body = {"account": account, "operator_note": note}
    if confirm_live:
        body["confirm_live_last4"] = confirm_live
    try:
        resp = t.post("/risk/reset", body=body)
    except click.ClickException:
        # preserve daemon-side error messages (e.g. 400 confirm_live_last4); do not wrap
        raise
    except Exception as e:
        raise RiskReject("RESET_FAILED", str(e))
    click.echo(f"Reset OK. Previous reason: {resp.get('previous_reason')}")
    click.echo("Baseline unchanged. Next order crossing threshold will re-trip.")
