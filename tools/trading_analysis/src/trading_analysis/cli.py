"""CLI entry point for trading-analysis."""
from __future__ import annotations

import datetime
import time

import click

from miniqmt_cli.client.transport import Transport
from miniqmt_cli.client_config import load_client_config

from trading_analysis.datasource import fetch_kline, fetch_tick_snapshot, fetch_ticks
from trading_analysis.moneyflow import (
    MoneyFlowSummary,
    aggregate_moneyflow,
    classify_direction,
    classify_tier,
    compute_deltas,
)
from trading_analysis.output import (
    build_live_multi_table,
    build_live_table,
    format_json,
    format_ranking,
    format_table,
)


def _parse_thresholds(raw: str) -> tuple[float, float, float]:
    parts = [float(x.strip()) * 10_000 for x in raw.split(",")]
    if len(parts) != 3:
        raise click.BadParameter("thresholds must be 3 comma-separated numbers (wan)")
    return (parts[0], parts[1], parts[2])


@click.group()
def cli():
    """trading-analysis: quantitative analysis toolkit."""


@cli.command()
@click.option("--code", "codes", required=True, multiple=True, help="Stock code(s)")
@click.option("--date", default=None, help="Date YYYYMMDD (default: today)")
@click.option("--start", default="093000", help="Start time HHMMSS")
@click.option("--end", default="150000", help="End time HHMMSS")
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json", "csv"]))
@click.option("--thresholds", default="4,20,100", help="Tier thresholds in wan (small,medium,large)")
@click.option("--config", "config_path", default=None, help="Override client config path")
@click.option("--live", is_flag=True, default=False, help="Real-time mode with Rich Live display")
@click.option("--interval", default=10, type=int, help="Live refresh interval in seconds (default: 10)")
@click.option("--signal", default=None, help='Signal expression, e.g. "main_net > 0 and price > ma20"')
def moneyflow(codes, date, start, end, fmt, thresholds, config_path, live, interval, signal):
    """Compute tick-level money flow by tier."""
    if date is None:
        date = datetime.date.today().strftime("%Y%m%d")
    thresh = _parse_thresholds(thresholds)
    cfg = load_client_config(config_path)
    transport = Transport(cfg)

    if live:
        _run_live(transport, list(codes), thresh, interval, signal)
        return

    ranking = []
    for code in codes:
        snapshots = fetch_ticks(transport, code, date, start, end)
        if not snapshots:
            click.echo(f"{code}: 无数据（非交易时间或代码无效）")
            continue

        deltas = compute_deltas(snapshots)
        summary = aggregate_moneyflow(deltas, thresh)

        if fmt == "json":
            click.echo(format_json(code, summary, len(snapshots)))
        else:
            click.echo(format_table(code, date, start, end, summary, len(snapshots)))

        ranking.append((code, summary.main_force_net))

    if len(ranking) > 1 and fmt == "table":
        click.echo(format_ranking(ranking))


def _load_ma_values(transport: Transport, code: str, periods: set[int]) -> dict[str, float | None]:
    """Fetch daily kline and compute required MAs for a code."""
    if not periods:
        return {}
    max_period = max(periods)
    # Fetch enough bars: max_period + some buffer
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=max_period * 2)
    bars = fetch_kline(
        transport, code, "1d",
        start_date.strftime("%Y%m%d"),
        today.strftime("%Y%m%d"),
    )
    closes = [b.get("close") for b in bars if b.get("close") is not None]

    from trading_analysis.signals import compute_ma
    result = {}
    for p in periods:
        ma_val = compute_ma(closes, p)
        result[f"ma{p}"] = ma_val
    return result


def _run_live(
    transport: Transport,
    codes: list[str],
    thresholds: tuple[float, float, float],
    interval: int,
    signal_expr: str | None,
) -> None:
    """Real-time polling mode using Rich Live display."""
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text

    from trading_analysis.signals import evaluate, parse_required_mas

    console = Console()

    # Parse signal expression and preload MAs
    ma_cache: dict[str, dict[str, float | None]] = {}
    required_mas: set[int] = set()
    if signal_expr:
        required_mas = parse_required_mas(signal_expr)
        console.print(f"[bold]信号表达式:[/bold] {signal_expr}")
        if required_mas:
            console.print(f"[dim]加载均线: MA{', MA'.join(str(p) for p in sorted(required_mas))}...[/dim]")
        for code in codes:
            ma_cache[code] = _load_ma_values(transport, code, required_mas)
            ma_parts = []
            for p in sorted(required_mas):
                key = f"ma{p}"
                val = ma_cache[code].get(key)
                ma_parts.append(f"MA{p}={'N/A' if val is None else f'{val:.2f}'}")
            ma_str = ", ".join(ma_parts)
            if ma_str:
                console.print(f"  {code}: {ma_str}")
        console.print()

    # Per-code state
    state: dict[str, dict] = {}
    for code in codes:
        state[code] = {
            "summary": MoneyFlowSummary(),
            "last_snap": None,
            "snap_count": 0,
            "last_price": None,
            "signal_triggered": False,
        }

    multi = len(codes) > 1

    def _poll_and_update():
        """Fetch latest snapshots and accumulate deltas."""
        try:
            all_snaps = fetch_tick_snapshot(transport, codes)
        except Exception:
            return

        for code in codes:
            snap = all_snaps.get(code)
            if not snap or not isinstance(snap, dict):
                continue

            s = state[code]
            s["snap_count"] += 1
            s["last_price"] = snap.get("lastPrice")
            prev = s["last_snap"]
            s["last_snap"] = snap

            if prev is None:
                continue

            d_amount = snap.get("amount", 0) - prev.get("amount", 0)
            d_volume = snap.get("volume", 0) - prev.get("volume", 0)
            d_txn = snap.get("transactionNum", 0) - prev.get("transactionNum", 0)

            if d_amount <= 0 or d_volume < 0:
                continue

            delta = {
                "delta_amount": d_amount,
                "delta_volume": d_volume,
                "delta_txn": d_txn,
                "avg_amount": d_amount / max(d_txn, 1),
                "last_price": snap.get("lastPrice", 0),
                "ask0": (snap.get("askPrice") or [0])[0],
                "bid0": (snap.get("bidPrice") or [0])[0],
            }

            direction = classify_direction(delta)
            tier = classify_tier(delta["avg_amount"], thresholds)
            bucket = s["summary"].tiers[tier]
            amount = delta["delta_amount"]

            if direction == "buy":
                bucket.buy += amount
                s["summary"].stats["buy_count"] += 1
            elif direction == "sell":
                bucket.sell += amount
                s["summary"].stats["sell_count"] += 1
            else:
                bucket.buy += amount / 2
                bucket.sell += amount / 2
                s["summary"].stats["neutral_count"] += 1

            s["summary"].stats["total_intervals"] += 1

    def _check_signals() -> list[str]:
        """Evaluate signal expression for each code. Returns list of alert messages."""
        if not signal_expr:
            return []
        alerts = []
        for code in codes:
            s = state[code]
            if s["last_price"] is None:
                continue
            variables: dict[str, float | None] = {
                "main_net": s["summary"].main_force_net,
                "retail_net": s["summary"].retail_net,
                "price": s["last_price"],
            }
            variables.update(ma_cache.get(code, {}))
            result = evaluate(signal_expr, variables)
            was_triggered = s["signal_triggered"]
            s["signal_triggered"] = result.triggered
            if result.triggered and not was_triggered:
                alerts.append(
                    f"[bold red on white] >>> {code} 信号触发: {signal_expr} <<< [/bold red on white]"
                )
        return alerts

    def _build_display():
        parts = []
        if multi:
            summaries = {
                code: (state[code]["summary"], state[code]["snap_count"])
                for code in codes
            }
            parts.append(build_live_multi_table(summaries, interval))
        else:
            code = codes[0]
            s = state[code]
            parts.append(build_live_table(code, s["summary"], s["snap_count"], interval))

        # Signal status line
        if signal_expr:
            triggered_codes = [c for c in codes if state[c]["signal_triggered"]]
            if triggered_codes:
                sig_text = Text(f"信号触发: {', '.join(triggered_codes)}", style="bold red")
            else:
                sig_text = Text(f"信号监控中: {signal_expr}", style="dim")
            parts.append(sig_text)

        if len(parts) == 1:
            return parts[0]

        from rich.console import Group
        return Group(*parts)

    # Initial poll
    _poll_and_update()

    try:
        with Live(_build_display(), console=console, refresh_per_second=1) as live:
            while True:
                time.sleep(interval)
                _poll_and_update()
                alerts = _check_signals()
                live.update(_build_display())
                # Print alerts outside Live so they persist in scrollback
                for alert in alerts:
                    live.console.print(alert)
    except KeyboardInterrupt:
        pass

    # Final summary
    console.print("\n[bold]-- 最终统计 --[/bold]")
    for code in codes:
        s = state[code]
        console.print(
            format_table(
                code,
                datetime.date.today().strftime("%Y%m%d"),
                "开盘",
                "当前",
                s["summary"],
                s["snap_count"],
            )
        )
    if signal_expr:
        triggered = [c for c in codes if state[c]["signal_triggered"]]
        if triggered:
            console.print(f"\n[bold red]信号已触发: {', '.join(triggered)}[/bold red]")
        else:
            console.print(f"\n[dim]信号未触发: {signal_expr}[/dim]")
