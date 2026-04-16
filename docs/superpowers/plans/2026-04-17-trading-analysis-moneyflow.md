# trading-analysis Moneyflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI tool that computes per-stock money flow breakdown from miniqmt-cli tick snapshots using hybrid tier classification.

**Architecture:** Separate `tools/trading_analysis/` package imports `miniqmt_cli.client.transport.Transport` for HTTP data fetching. Core logic in `moneyflow.py` diffs adjacent 3-second snapshots, classifies direction via bid/ask comparison, assigns tiers by average-per-trade amount, and aggregates buy/sell/net per tier.

**Tech Stack:** Python 3.11+, Click, Rich, pandas, miniqmt-cli (editable dep)

**Spec:** `docs/superpowers/specs/2026-04-17-trading-analysis-moneyflow-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| `tools/trading_analysis/pyproject.toml` | Package metadata, deps, entry point |
| `tools/trading_analysis/src/trading_analysis/__init__.py` | Version string |
| `tools/trading_analysis/src/trading_analysis/moneyflow.py` | Core: delta, direction, tier, aggregation |
| `tools/trading_analysis/src/trading_analysis/datasource.py` | Wrap Transport to fetch ticks |
| `tools/trading_analysis/src/trading_analysis/output.py` | Rich table formatting |
| `tools/trading_analysis/src/trading_analysis/cli.py` | Click CLI entry point |
| `tests/trading_analysis/__init__.py` | Test package |
| `tests/trading_analysis/test_moneyflow.py` | Unit tests for core algorithm |
| `tests/trading_analysis/test_datasource.py` | Integration tests with mock transport |

---

### Task 1: Package Scaffold

**Files:**
- Create: `tools/trading_analysis/pyproject.toml`
- Create: `tools/trading_analysis/src/trading_analysis/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "trading-analysis"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "rich>=13.0",
    "pandas>=2.0",
    "miniqmt-cli",
]

[project.scripts]
trading-analysis = "trading_analysis.cli:cli"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Create __init__.py**

```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Install editable**

Run: `pip install -e tools/trading_analysis`

Expected: `Successfully installed trading-analysis-0.1.0`

- [ ] **Step 4: Commit**

```bash
git add tools/trading_analysis/pyproject.toml tools/trading_analysis/src/trading_analysis/__init__.py
git commit -m "feat(trading-analysis): scaffold package with pyproject.toml"
```

---

### Task 2: Core Algorithm -- Delta Computation

**Files:**
- Create: `tests/trading_analysis/__init__.py`
- Create: `tests/trading_analysis/test_moneyflow.py`
- Create: `tools/trading_analysis/src/trading_analysis/moneyflow.py`

- [ ] **Step 1: Create test file with delta computation tests**

`tests/trading_analysis/test_moneyflow.py`:

```python
from trading_analysis.moneyflow import compute_deltas


def _snap(stime, amount, volume, txn, last_price, ask0, bid0):
    """Build a minimal snapshot dict matching xtquant tick format."""
    return {
        "stime": stime,
        "lastPrice": last_price,
        "amount": amount,
        "volume": volume,
        "transactionNum": txn,
        "askPrice": [ask0, 0, 0, 0, 0],
        "bidPrice": [bid0, 0, 0, 0, 0],
    }


class TestComputeDeltas:
    def test_basic_two_snapshots(self):
        snaps = [
            _snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9),
            _snap("093003", 1_500_000, 150, 80, 10.2, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert len(deltas) == 1
        d = deltas[0]
        assert d["delta_amount"] == 500_000
        assert d["delta_volume"] == 50
        assert d["delta_txn"] == 30
        assert d["last_price"] == 10.2
        assert d["ask0"] == 10.1
        assert d["bid0"] == 9.9

    def test_first_snapshot_skipped(self):
        snaps = [
            _snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert len(deltas) == 0

    def test_negative_delta_skipped(self):
        snaps = [
            _snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9),
            _snap("093003", 500_000, 50, 20, 10.0, 10.1, 9.9),  # reset
            _snap("093006", 800_000, 80, 40, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        # first delta is negative (skipped), second is positive
        assert len(deltas) == 1
        assert deltas[0]["delta_amount"] == 300_000

    def test_avg_amount_computed(self):
        snaps = [
            _snap("093000", 0, 0, 0, 10.0, 10.1, 9.9),
            _snap("093003", 600_000, 60, 10, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert deltas[0]["avg_amount"] == 60_000  # 600k / 10

    def test_zero_txn_delta_uses_one(self):
        snaps = [
            _snap("093000", 0, 0, 0, 10.0, 10.1, 9.9),
            _snap("093003", 100_000, 10, 0, 10.0, 10.1, 9.9),  # txn stays 0
        ]
        deltas = compute_deltas(snaps)
        assert deltas[0]["avg_amount"] == 100_000  # 100k / max(0,1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_analysis.moneyflow'`

- [ ] **Step 3: Implement compute_deltas**

`tools/trading_analysis/src/trading_analysis/moneyflow.py`:

```python
"""Core money flow computation: delta, direction, tier, aggregation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


def compute_deltas(snapshots: list[dict]) -> list[dict]:
    """Diff adjacent snapshots. Skip first and any with negative delta."""
    deltas = []
    for i in range(1, len(snapshots)):
        prev, curr = snapshots[i - 1], snapshots[i]
        d_amount = curr["amount"] - prev["amount"]
        d_volume = curr["volume"] - prev["volume"]
        d_txn = curr["transactionNum"] - prev["transactionNum"]
        if d_amount < 0 or d_volume < 0:
            continue
        deltas.append({
            "stime": curr["stime"],
            "delta_amount": d_amount,
            "delta_volume": d_volume,
            "delta_txn": d_txn,
            "avg_amount": d_amount / max(d_txn, 1),
            "last_price": curr["lastPrice"],
            "ask0": curr["askPrice"][0],
            "bid0": curr["bidPrice"][0],
        })
    return deltas
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py -v`

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add tests/trading_analysis/ tools/trading_analysis/src/trading_analysis/moneyflow.py
git commit -m "feat(trading-analysis): compute_deltas with delta/avg_amount"
```

---

### Task 3: Core Algorithm -- Direction Classification

**Files:**
- Modify: `tests/trading_analysis/test_moneyflow.py`
- Modify: `tools/trading_analysis/src/trading_analysis/moneyflow.py`

- [ ] **Step 1: Add direction classification tests**

Append to `tests/trading_analysis/test_moneyflow.py`:

```python
from trading_analysis.moneyflow import classify_direction


class TestClassifyDirection:
    def test_active_buy(self):
        delta = {"last_price": 10.2, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "buy"

    def test_active_sell(self):
        delta = {"last_price": 9.8, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "sell"

    def test_neutral(self):
        delta = {"last_price": 10.0, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "neutral"

    def test_equal_to_ask_is_buy(self):
        delta = {"last_price": 10.1, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "buy"

    def test_equal_to_bid_is_sell(self):
        delta = {"last_price": 9.9, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "sell"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py::TestClassifyDirection -v`

Expected: FAIL with `cannot import name 'classify_direction'`

- [ ] **Step 3: Implement classify_direction**

Append to `moneyflow.py`:

```python
def classify_direction(delta: dict) -> str:
    """Classify a delta interval as buy, sell, or neutral."""
    price = delta["last_price"]
    if price >= delta["ask0"]:
        return "buy"
    if price <= delta["bid0"]:
        return "sell"
    return "neutral"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py -v`

Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add tests/trading_analysis/test_moneyflow.py tools/trading_analysis/src/trading_analysis/moneyflow.py
git commit -m "feat(trading-analysis): classify_direction via bid/ask comparison"
```

---

### Task 4: Core Algorithm -- Tier Classification and Aggregation

**Files:**
- Modify: `tests/trading_analysis/test_moneyflow.py`
- Modify: `tools/trading_analysis/src/trading_analysis/moneyflow.py`

- [ ] **Step 1: Add tier and aggregation tests**

Append to `tests/trading_analysis/test_moneyflow.py`:

```python
from trading_analysis.moneyflow import (
    classify_tier,
    aggregate_moneyflow,
    DEFAULT_THRESHOLDS,
    MoneyFlowSummary,
)


class TestClassifyTier:
    def test_small(self):
        assert classify_tier(30_000) == "small"

    def test_medium(self):
        assert classify_tier(100_000) == "medium"

    def test_large(self):
        assert classify_tier(500_000) == "large"

    def test_extra_large(self):
        assert classify_tier(1_500_000) == "xlarge"

    def test_boundary_medium(self):
        assert classify_tier(40_000) == "medium"

    def test_boundary_large(self):
        assert classify_tier(200_000) == "large"

    def test_boundary_xlarge(self):
        assert classify_tier(1_000_000) == "xlarge"

    def test_custom_thresholds(self):
        assert classify_tier(50_000, thresholds=(50_000, 200_000, 1_000_000)) == "medium"
        assert classify_tier(49_999, thresholds=(50_000, 200_000, 1_000_000)) == "small"


class TestAggregateMoneyflow:
    def test_single_buy_xlarge(self):
        deltas = [{
            "delta_amount": 2_000_000,
            "delta_volume": 100,
            "delta_txn": 1,
            "avg_amount": 2_000_000,
            "last_price": 10.2,
            "ask0": 10.1,
            "bid0": 9.9,
            "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["xlarge"].buy == 2_000_000
        assert result.tiers["xlarge"].sell == 0
        assert result.tiers["xlarge"].net == 2_000_000

    def test_single_sell_small(self):
        deltas = [{
            "delta_amount": 10_000,
            "delta_volume": 10,
            "delta_txn": 5,
            "avg_amount": 2_000,
            "last_price": 9.8,
            "ask0": 10.1,
            "bid0": 9.9,
            "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["small"].sell == 10_000
        assert result.tiers["small"].buy == 0

    def test_neutral_split_half(self):
        deltas = [{
            "delta_amount": 100_000,
            "delta_volume": 50,
            "delta_txn": 2,
            "avg_amount": 50_000,
            "last_price": 10.0,
            "ask0": 10.1,
            "bid0": 9.9,
            "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["medium"].buy == 50_000
        assert result.tiers["medium"].sell == 50_000
        assert result.tiers["medium"].net == 0

    def test_main_force_net(self):
        deltas = [
            {
                "delta_amount": 2_000_000, "delta_volume": 100, "delta_txn": 1,
                "avg_amount": 2_000_000, "last_price": 10.2,
                "ask0": 10.1, "bid0": 9.9, "stime": "093003",
            },
            {
                "delta_amount": 500_000, "delta_volume": 50, "delta_txn": 1,
                "avg_amount": 500_000, "last_price": 9.8,
                "ask0": 10.1, "bid0": 9.9, "stime": "093006",
            },
        ]
        result = aggregate_moneyflow(deltas)
        assert result.main_force_net == 2_000_000 - 500_000

    def test_stats_counts(self):
        deltas = [
            {
                "delta_amount": 100_000, "delta_volume": 10, "delta_txn": 5,
                "avg_amount": 20_000, "last_price": 10.2,
                "ask0": 10.1, "bid0": 9.9, "stime": "093003",
            },
            {
                "delta_amount": 50_000, "delta_volume": 5, "delta_txn": 3,
                "avg_amount": 16_667, "last_price": 9.8,
                "ask0": 10.1, "bid0": 9.9, "stime": "093006",
            },
            {
                "delta_amount": 80_000, "delta_volume": 8, "delta_txn": 4,
                "avg_amount": 20_000, "last_price": 10.0,
                "ask0": 10.1, "bid0": 9.9, "stime": "093009",
            },
        ]
        result = aggregate_moneyflow(deltas)
        assert result.stats["buy_count"] == 1
        assert result.stats["sell_count"] == 1
        assert result.stats["neutral_count"] == 1
        assert result.stats["total_intervals"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py::TestClassifyTier -v`

Expected: FAIL with `cannot import name 'classify_tier'`

- [ ] **Step 3: Implement tier classification and aggregation**

Append to `moneyflow.py`:

```python
DEFAULT_THRESHOLDS = (40_000, 200_000, 1_000_000)  # small/medium, medium/large, large/xlarge

TIER_NAMES = ("small", "medium", "large", "xlarge")


def classify_tier(
    avg_amount: float,
    thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS,
) -> str:
    """Classify by average per-trade amount into four tiers."""
    if avg_amount >= thresholds[2]:
        return "xlarge"
    if avg_amount >= thresholds[1]:
        return "large"
    if avg_amount >= thresholds[0]:
        return "medium"
    return "small"


@dataclass
class TierBucket:
    buy: float = 0.0
    sell: float = 0.0

    @property
    def net(self) -> float:
        return self.buy - self.sell


@dataclass
class MoneyFlowSummary:
    tiers: dict[str, TierBucket] = field(
        default_factory=lambda: {name: TierBucket() for name in TIER_NAMES}
    )
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "total_intervals": 0,
            "buy_count": 0,
            "sell_count": 0,
            "neutral_count": 0,
        }
    )

    @property
    def main_force_net(self) -> float:
        return self.tiers["xlarge"].net + self.tiers["large"].net

    @property
    def retail_net(self) -> float:
        return self.tiers["medium"].net + self.tiers["small"].net


def aggregate_moneyflow(
    deltas: list[dict],
    thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS,
) -> MoneyFlowSummary:
    """Classify and aggregate all delta intervals into a summary."""
    summary = MoneyFlowSummary()
    for d in deltas:
        direction = classify_direction(d)
        tier = classify_tier(d["avg_amount"], thresholds)
        bucket = summary.tiers[tier]
        amount = d["delta_amount"]

        if direction == "buy":
            bucket.buy += amount
            summary.stats["buy_count"] += 1
        elif direction == "sell":
            bucket.sell += amount
            summary.stats["sell_count"] += 1
        else:
            bucket.buy += amount / 2
            bucket.sell += amount / 2
            summary.stats["neutral_count"] += 1

        summary.stats["total_intervals"] += 1
    return summary
```

- [ ] **Step 4: Run all tests to verify they pass**

Run: `python3 -m pytest tests/trading_analysis/test_moneyflow.py -v`

Expected: 23 passed

- [ ] **Step 5: Commit**

```bash
git add tests/trading_analysis/test_moneyflow.py tools/trading_analysis/src/trading_analysis/moneyflow.py
git commit -m "feat(trading-analysis): tier classification and aggregation"
```

---

### Task 5: Datasource -- Fetch Ticks via Transport

**Files:**
- Create: `tools/trading_analysis/src/trading_analysis/datasource.py`
- Create: `tests/trading_analysis/test_datasource.py`

- [ ] **Step 1: Write datasource tests with mock transport**

`tests/trading_analysis/test_datasource.py`:

```python
from unittest.mock import MagicMock

from trading_analysis.datasource import fetch_ticks


def _make_transport(return_value):
    t = MagicMock()
    t.get.return_value = return_value
    return t


class TestFetchTicks:
    def test_returns_list_of_dicts(self):
        data = [{"stime": "093000", "amount": 100}]
        t = _make_transport(data)
        result = fetch_ticks(t, "002028.SZ", "20260416", "093000", "150000")
        assert result == data
        t.get.assert_called_once_with(
            "/data/ticks",
            params={"code": "002028.SZ", "start": "20260416093000", "end": "20260416150000"},
        )

    def test_empty_list_returned(self):
        t = _make_transport([])
        result = fetch_ticks(t, "002028.SZ", "20260416", "093000", "150000")
        assert result == []

    def test_date_time_concatenation(self):
        t = _make_transport([])
        fetch_ticks(t, "000001.SZ", "20260415", "100000", "113000")
        t.get.assert_called_once_with(
            "/data/ticks",
            params={"code": "000001.SZ", "start": "20260415100000", "end": "20260415113000"},
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/trading_analysis/test_datasource.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'trading_analysis.datasource'`

- [ ] **Step 3: Implement datasource**

`tools/trading_analysis/src/trading_analysis/datasource.py`:

```python
"""Fetch tick snapshots from the miniqmt-cli daemon."""
from __future__ import annotations

from miniqmt_cli.client.transport import Transport


def fetch_ticks(
    transport: Transport,
    code: str,
    date: str,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch tick snapshots for a single stock.

    Args:
        transport: miniqmt_cli Transport instance
        code: stock code, e.g. "002028.SZ"
        date: YYYYMMDD
        start: HHMMSS
        end: HHMMSS

    Returns:
        List of snapshot dicts from the daemon.
    """
    return transport.get(
        "/data/ticks",
        params={"code": code, "start": f"{date}{start}", "end": f"{date}{end}"},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/trading_analysis/test_datasource.py -v`

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add tools/trading_analysis/src/trading_analysis/datasource.py tests/trading_analysis/test_datasource.py
git commit -m "feat(trading-analysis): datasource wrapping Transport.get"
```

---

### Task 6: Output Formatting

**Files:**
- Create: `tools/trading_analysis/src/trading_analysis/output.py`

- [ ] **Step 1: Implement output formatting**

`tools/trading_analysis/src/trading_analysis/output.py`:

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add tools/trading_analysis/src/trading_analysis/output.py
git commit -m "feat(trading-analysis): Rich table and JSON output formatting"
```

---

### Task 7: CLI Entry Point

**Files:**
- Create: `tools/trading_analysis/src/trading_analysis/cli.py`

- [ ] **Step 1: Implement CLI**

`tools/trading_analysis/src/trading_analysis/cli.py`:

```python
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
```

- [ ] **Step 2: Verify CLI runs**

Run: `trading-analysis moneyflow --help`

Expected: help text showing all options

- [ ] **Step 3: Commit**

```bash
git add tools/trading_analysis/src/trading_analysis/cli.py
git commit -m "feat(trading-analysis): CLI entry point for moneyflow command"
```

---

### Task 8: End-to-End Smoke Test

**Files:** none new (uses existing)

- [ ] **Step 1: Reinstall the package**

Run: `pip install -e tools/trading_analysis`

- [ ] **Step 2: Run all unit tests**

Run: `python3 -m pytest tests/trading_analysis/ -v`

Expected: all tests pass

- [ ] **Step 3: Live smoke test (requires daemon running)**

Run: `trading-analysis moneyflow --code 002028.SZ --date 20260416 --start 093000 --end 093500`

Expected: Rich table output with four tiers, buy/sell/net columns, stats line.

- [ ] **Step 4: Multi-stock smoke test**

Run: `trading-analysis moneyflow --code 002028.SZ --code 000859.SZ --date 20260416`

Expected: two tables followed by a ranking summary.

- [ ] **Step 5: JSON format smoke test**

Run: `trading-analysis moneyflow --code 002028.SZ --date 20260416 --start 093000 --end 093500 --format json`

Expected: valid JSON with tiers, main_force_net, stats.

- [ ] **Step 6: Final commit (if any fixes were needed)**

```bash
git add -A
git commit -m "fix(trading-analysis): smoke test fixes"
```
