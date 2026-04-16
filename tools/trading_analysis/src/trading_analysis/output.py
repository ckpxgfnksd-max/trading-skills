"""Rich table output for money flow summary."""
from __future__ import annotations

import io
import json
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

from trading_analysis.moneyflow import MoneyFlowSummary, TIER_NAMES

TIER_LABELS = {
    "xlarge": "超大单",
    "large": "大单",
    "medium": "中单",
    "small": "小单",
}


def _wan(v: float) -> str:
    """Format amount in wan (10,000) with comma grouping."""
    return f"{v / 10_000:,.1f}"


def _net_label(net: float) -> str:
    if net > 0:
        return "净流入"
    if net < 0:
        return "净流出"
    return ""


def format_table(
    code: str,
    date: str,
    start: str,
    end: str,
    summary: MoneyFlowSummary,
    snapshot_count: int,
) -> str:
    """Format a single stock's moneyflow as a Rich table string."""
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, width=72)

    s_start = f"{start[:2]}:{start[2:4]}"
    s_end = f"{end[:2]}:{end[2:4]}"
    d_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    console.print(f"\n{code}  {d_fmt} {s_start} ~ {s_end}")

    table = Table(show_header=True, header_style="bold cyan", width=72)
    table.add_column("档位", width=10)
    table.add_column("买入(万)", justify="right", width=12)
    table.add_column("卖出(万)", justify="right", width=12)
    table.add_column("净流入(万)", justify="right", width=12)
    table.add_column("方向", width=10)

    for name in TIER_NAMES[::-1]:  # xlarge first
        b = summary.tiers[name]
        table.add_row(
            TIER_LABELS[name],
            _wan(b.buy),
            _wan(b.sell),
            f"{'+' if b.net >= 0 else ''}{_wan(b.net)}",
            _net_label(b.net),
        )

    table.add_section()
    table.add_row(
        "主力合计", "", "",
        f"{'+' if summary.main_force_net >= 0 else ''}{_wan(summary.main_force_net)}",
        _net_label(summary.main_force_net),
    )
    table.add_row(
        "散户合计", "", "",
        f"{'+' if summary.retail_net >= 0 else ''}{_wan(summary.retail_net)}",
        _net_label(summary.retail_net),
    )
    console.print(table)

    st = summary.stats
    console.print(
        f"统计: 快照 {snapshot_count} 条 | "
        f"有效区间 {st['total_intervals']} | "
        f"买入 {st['buy_count']} | "
        f"卖出 {st['sell_count']} | "
        f"中性 {st['neutral_count']}"
    )
    return buf.getvalue()


def format_ranking(results: list[tuple[str, float]]) -> str:
    """Format multi-stock ranking by main force net."""
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, width=72)
    console.print("\n── 主力净流入排名 ──")
    ranked = sorted(results, key=lambda x: x[1], reverse=True)
    for i, (code, net) in enumerate(ranked, 1):
        sign = "+" if net >= 0 else ""
        console.print(f"#{i}  {code}  {sign}{_wan(net)}万")
    return buf.getvalue()


def format_json(
    code: str,
    summary: MoneyFlowSummary,
    snapshot_count: int,
) -> str:
    """Format summary as JSON."""
    data = {
        "code": code,
        "tiers": {
            name: {
                "buy": summary.tiers[name].buy,
                "sell": summary.tiers[name].sell,
                "net": summary.tiers[name].net,
            }
            for name in TIER_NAMES
        },
        "main_force_net": summary.main_force_net,
        "retail_net": summary.retail_net,
        "stats": summary.stats,
        "snapshot_count": snapshot_count,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
