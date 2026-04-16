"""CLI entry point for trading-analysis."""
from __future__ import annotations

import datetime

import click

from miniqmt_cli.client.transport import Transport
from miniqmt_cli.client_config import load_client_config

from trading_analysis.datasource import fetch_ticks
from trading_analysis.moneyflow import aggregate_moneyflow, compute_deltas
from trading_analysis.output import format_json, format_ranking, format_table


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
def moneyflow(codes, date, start, end, fmt, thresholds, config_path):
    """Compute tick-level money flow by tier."""
    if date is None:
        date = datetime.date.today().strftime("%Y%m%d")
    thresh = _parse_thresholds(thresholds)
    cfg = load_client_config(config_path)
    transport = Transport(cfg)

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
