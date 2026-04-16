# trading-analysis: Tick-Level Money Flow Analysis

**Date**: 2026-04-17
**Status**: Phase 1 design approved
**Package**: `tools/trading_analysis/`

---

## Goal

Compute per-stock money flow breakdown (equivalent to tushare moneyflow) from
miniqmt-cli tick snapshots. Classify 3-second interval deltas into four tiers
using a hybrid approach (average-per-trade amount determines tier, interval total
determines magnitude), and report buy/sell/net per tier.

---

## Architecture

```
trading-analysis CLI (Click)
        |
        v
  datasource.py  --import-->  miniqmt_cli.client.transport
        |                              |
        v                              v
  moneyflow.py (core)           daemon HTTP API
        |                      (127.0.0.1:8765)
        v
   output.py (Rich tables)
```

Reuses miniqmt-cli's `Transport` class for HTTP communication with the daemon.
No subprocess calls, no code duplication.

---

## Package Structure

```
tools/trading_analysis/
├── pyproject.toml
├── src/trading_analysis/
│   ├── __init__.py
│   ├── cli.py              # Click CLI entry point
│   ├── moneyflow.py        # Core: diff -> classify -> aggregate
│   ├── datasource.py       # Wraps miniqmt_cli.client.transport
│   └── output.py           # Rich table formatting
```

Entry point: `trading-analysis moneyflow`

Dependencies: click, rich, pandas, miniqmt-cli (editable, same repo)

---

## Data Source

xtquant's tick data via `GET /data/ticks` returns **3-second snapshots**, not
individual trades. Each snapshot contains:

- `lastPrice`, `volume` (cumulative), `amount` (cumulative), `transactionNum` (cumulative)
- `askPrice[5]`, `bidPrice[5]` -- full order book depth
- ~4,800 snapshots per stock per day

---

## Core Algorithm

### Step 1: Delta Computation

Adjacent snapshots are differenced:

```
delta_amount = snap[i].amount - snap[i-1].amount
delta_volume = snap[i].volume - snap[i-1].volume
delta_txn    = snap[i].transactionNum - snap[i-1].transactionNum
avg_amount   = delta_amount / max(delta_txn, 1)
```

First snapshot is skipped (no predecessor). Intervals with delta < 0 (cross-day
reset) are skipped and counted in stats.

### Step 2: Direction Classification

Per 3-second interval, compare `lastPrice` against the order book:

```
lastPrice >= askPrice[0]  ->  active buy
lastPrice <= bidPrice[0]  ->  active sell
otherwise                 ->  neutral (split 50/50 into buy and sell)
```

### Step 3: Hybrid Tier Classification

The `avg_amount` (per-trade average) determines the tier. The `delta_amount`
(interval total) is accumulated into that tier.

| Tier       | avg_amount Condition     | tushare Equivalent |
|------------|-------------------------|--------------------|
| Extra-large | >= 1,000,000 (100 wan)  | Institutional      |
| Large       | 200,000 ~ 1,000,000    | Main force         |
| Medium      | 40,000 ~ 200,000       | Mid-tier           |
| Small       | < 40,000               | Retail             |

Thresholds are configurable via `--thresholds` (in wan: `4,20,100`).

### Step 4: Aggregation

Each tier accumulates `buy_amount`, `sell_amount`, `neutral_amount`.
Neutral is split 50/50 into buy and sell.

```
net = buy - sell
main_force_net = extra_large.net + large.net
retail_net = medium.net + small.net
```

---

## CLI Interface

```bash
# Single stock, today, full day
trading-analysis moneyflow --code 002028.SZ

# Custom time range
trading-analysis moneyflow --code 002028.SZ --start 093000 --end 110000

# Historical date
trading-analysis moneyflow --code 002028.SZ --date 20260415

# Multiple stocks with ranking
trading-analysis moneyflow --code 002028.SZ --code 000859.SZ --code 300618.SZ

# Output format
trading-analysis moneyflow --code 002028.SZ --format json

# Custom thresholds (wan)
trading-analysis moneyflow --code 002028.SZ --thresholds 4,20,100
```

### Parameter Defaults

| Parameter      | Default     | Format              |
|---------------|-------------|---------------------|
| `--date`      | today       | `YYYYMMDD`          |
| `--start`     | `093000`    | `HHMMSS`            |
| `--end`       | `150000`    | `HHMMSS`            |
| `--format`    | `table`     | `table/json/csv`    |
| `--thresholds`| `4,20,100`  | comma-separated wan |
| `--config`    | (from miniqmt-cli client.toml) | path     |

### Output Format

```
002028.SZ  2026-04-16 09:30 ~ 15:00
──────────────────────────────────────────────────
档位       买入(万)    卖出(万)    净流入(万)   方向
超大单      1,230.5      480.2      +750.3    净流入
大单          860.1      920.3       -60.2    小幅流出
中单          340.7      290.1       +50.6
小单          180.3      210.8       -30.5
──────────────────────────────────────────────────
主力合计(超大+大)                   +690.1    净流入
散户合计(中+小)                      +20.1

统计: 快照 4,800 条 | 有效区间 4,799 | 买入 2,103 | 卖出 2,288 | 中性 408
```

Multiple stocks: one table per stock, plus a ranking summary:

```
── 主力净流入排名 ──
#1  002028.SZ  +690.1万
#2  300618.SZ  +120.3万
#3  000859.SZ   -45.2万
```

---

## Error Handling

| Scenario                    | Behavior                                     |
|-----------------------------|----------------------------------------------|
| Daemon unreachable          | Transport error propagates (existing handling)|
| Ticks return empty          | Print "no data" message and exit              |
| Delta < 0 (cross-day reset) | Skip interval, count in stats                |

---

## Testing

- **Unit tests** for `moneyflow.py`: fixed snapshot data -> verify diff, direction,
  tier classification, aggregation
- **Integration tests** for `datasource.py`: mock transport, verify end-to-end
- CLI layer is thin; not tested directly

---

## TODO: Future Phases

### Phase 2: Real-time Mode + Multi-stock

- Use `miniqmt_cli.client.transport.stream()` with SSE `/stream/tick`
- Maintain in-memory cumulative state, refresh output every N minutes
- Parallel subscriptions for multiple stocks

### Phase 3: Buy Signal Integration

- Fetch kline data to compute MA20
- Trigger signal when: main force net inflow + price above MA20
- Optional notification (terminal alert / webhook)

### Phase 4: Historical Backtesting

- Retrieve historical ticks for past dates
- Cross-validate against tushare moneyflow data for accuracy assessment
- Generate accuracy report comparing hybrid classification vs tushare
