---
name: trading-analysis
description: Analyze A-share money flow from tick-level data using the trading-analysis CLI, with both historical and real-time modes. Use this skill whenever the user mentions money flow, capital flow, main force inflow/outflow, institutional buying, tick-level analysis, order size classification (super-large/large/medium/small orders), real-time monitoring of capital flow, or wants to know whether smart money is buying or selling a stock. Also use when the user asks about 资金流向, 主力资金, 大单, 超大单, 散户, 逐笔分析, or 实时监控.
---

# trading-analysis: Tick-Level Money Flow Analysis

## Architecture

```
trading-analysis CLI
        |
        v
  miniqmt_cli.client.transport (HTTP)
        |
        v
  miniqmt-cli daemon (Windows, port 8765)
        |
        v
  xtquant tick snapshots (3-second intervals)
```

Requires: miniqmt-cli daemon running + SSH tunnel active.
Verify with `miniqmt-cli health` before use.

## How It Works

1. Fetches 3-second tick snapshots from the daemon (`/data/ticks`)
2. Diffs adjacent snapshots to get per-interval delta (amount, volume, trade count)
3. Classifies direction: `lastPrice >= ask1` = active buy, `<= bid1` = active sell, else neutral (split 50/50)
4. Hybrid tier assignment: `avg_amount = delta_amount / delta_trades` determines tier, `delta_amount` is accumulated
5. Aggregates buy/sell/net per tier, computes main force net (xlarge + large) and retail net (medium + small)

## Tier Thresholds (Default)

| Tier | Average Per-Trade Amount | Label |
|------|-------------------------|-------|
| Extra-large | >= 100 wan (1,000,000) | 超大单 |
| Large | 20 ~ 100 wan | 大单 |
| Medium | 4 ~ 20 wan | 中单 |
| Small | < 4 wan | 小单 |

Thresholds are configurable via `--thresholds`.

## Commands

```bash
# Single stock, today, full trading day
trading-analysis moneyflow --code 002028.SZ

# Specify date (historical)
trading-analysis moneyflow --code 002028.SZ --date 20260416

# Custom time range
trading-analysis moneyflow --code 002028.SZ --start 093000 --end 110000

# Multiple stocks (outputs per-stock tables + ranking)
trading-analysis moneyflow --code 002028.SZ --code 000859.SZ --code 300618.SZ

# JSON output
trading-analysis moneyflow --code 002028.SZ --format json

# Custom thresholds (wan): small/medium boundary, medium/large, large/xlarge
trading-analysis moneyflow --code 002028.SZ --thresholds 4,20,100

# Use specific miniqmt-cli client config
trading-analysis moneyflow --code 002028.SZ --config ~/.miniqmt_cli/client.toml
```

### Real-time Mode (--live)

Polls the daemon for latest tick snapshots every N seconds, accumulates deltas into a running summary, and displays via Rich Live (in-place terminal refresh). Ctrl+C to stop; prints final summary on exit.

```bash
# Single stock real-time (default 10s refresh)
trading-analysis moneyflow --code 002028.SZ --live

# Multiple stocks real-time ranking
trading-analysis moneyflow --code 002028.SZ --code 000859.SZ --code 300618.SZ --live

# Custom refresh interval (30 seconds)
trading-analysis moneyflow --code 002028.SZ --live --interval 30
```

- Single stock: full four-tier table, updated in-place
- Multiple stocks: compact ranking table sorted by main force net inflow
- Requires market hours for meaningful data; outside trading hours the display will show zeros
- Combine with `--signal` to get alert-on-trigger behavior; see **Signal Expressions** below.

## Signal Expressions (`--signal`)

Live mode supports a minimal expression language for triggering alerts when conditions are met. The expression is evaluated every refresh interval against the running per-stock state.

```bash
# Alert when main force is net buying AND price is above MA20
trading-analysis moneyflow --code 002028.SZ --live \
  --signal "main_net > 0 and price > ma20"

# Alert on a reversal signal
trading-analysis moneyflow --code 002028.SZ --live \
  --signal "main_net > 500000 and ma5 > ma20"

# Multiple stocks — signal is evaluated per code independently
trading-analysis moneyflow --code 002028.SZ --code 000859.SZ --live \
  --signal "main_net > 0"
```

**Variables** (all in yuan unless noted):

| Variable | Meaning |
|----------|---------|
| `main_net` | Main force net inflow (xlarge + large tiers) |
| `retail_net` | Retail net inflow (medium + small tiers) |
| `price` | Latest tick `lastPrice` |
| `ma5` / `ma10` / `ma20` / `ma60` | Simple moving average of that many 1-minute closes |

**Operators**: `>`, `<`, `>=`, `<=`, `==`, `and`, `or` (lowercase only).

**Literals**: integers and floats; numbers are in yuan (e.g. `500000` = 50 wan).

**Semantics**:

- If any referenced variable is `None` (e.g. MA window not yet filled), the signal is **not triggered** — no false positives during warm-up.
- MA windows are auto-detected from the expression; `ma20` triggers a preload of 20 1-minute klines before the live loop starts.
- Trigger flips edge-sensitive: the alert fires once when `False → True`. It re-arms when the expression goes back to `False`.

**Output**: when triggered, the live display prints a red banner `>>> 002028.SZ 信号触发: <expr> <<<` and the footer shows "已触发: [codes]". On Ctrl+C exit, a final summary lists which codes ever triggered.

**Limitations**:

- No parentheses — precedence is strictly `cmp → and → or` with left-to-right evaluation.
- No arithmetic (`+`, `-`, `*`, `/`) inside expressions — compare variables to literal thresholds only.
- No historical lookback beyond the MA window (no "price N minutes ago").

## Parameter Reference

| Parameter | Default | Format |
|-----------|---------|--------|
| `--code` | (required, multiple) | `XXXXXX.SZ` / `XXXXXX.SH` |
| `--date` | today | `YYYYMMDD` |
| `--start` | `093000` | `HHMMSS` |
| `--end` | `150000` | `HHMMSS` |
| `--format` | `table` | `table` / `json` / `csv` |
| `--thresholds` | `4,20,100` | comma-separated wan |
| `--config` | from miniqmt-cli client.toml | path |
| `--live` | off | flag |
| `--interval` | `10` | seconds |

## Output Example

```
002028.SZ  2026-04-16 09:30 ~ 15:00
──────────────────────────────────────────────────
档位       买入(万)    卖出(万)    净流入(万)   方向
超大单      1,230.5      480.2      +750.3    净流入
大单          860.1      920.3       -60.2    净流出
中单          340.7      290.1       +50.6    净流入
小单          180.3      210.8       -30.5    净流出
──────────────────────────────────────────────────
主力合计                             +690.1    净流入
散户合计                              +20.1    净流入

统计: 快照 4,800 条 | 有效区间 4,799 | 买入 2,103 | 卖出 2,288 | 中性 408
```

Multiple stocks append a ranking:

```
── 主力净流入排名 ──
#1  002028.SZ  +690.1万
#2  300618.SZ  +120.3万
#3  000859.SZ   -45.2万
```

## Interpreting Results

- **主力合计 > 0**: Main force (institutions/large traders) net buying -- bullish signal
- **主力合计 < 0**: Main force net selling -- bearish signal
- **超大单 dominant**: Likely institutional activity
- **大单 dominant without 超大单**: Could be large retail or small institutional
- **All activity in 小单/中单**: Retail-driven, no clear institutional signal

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "cannot reach daemon" | SSH tunnel or daemon down | `miniqmt-cli health`; restart tunnel/daemon |
| "无数据" | Non-trading hours, invalid code, or no cached data | Check code format, try during market hours |
| All tiers show 0 | No trading activity in the time range | Widen the time range |
| 大单/超大单 always 0 | 3-second avg too small to hit threshold | Lower thresholds: `--thresholds 2,10,50` |
| Live mode shows all zeros | Outside trading hours, no new ticks | Run during market hours (09:30-15:00) |
| Live mode not updating | Daemon not returning fresh snapshots | Check `miniqmt-cli health`; ensure miniQMT client is open |

## Related Skills

- **miniqmt-cli** — The daemon and data source underneath
- **miniqmt-http-api** — HTTP/SSE endpoints used by this tool
- **auto-trading-loop** — How to compose analysis + orders into a trading loop
