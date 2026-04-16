---
name: miniqmt-cli
description: Operate miniQMT/xtquant via the miniqmt-cli tool -- market data queries, real-time streaming, account management, order placement with safety guards, daemon health checks, SSH tunnel management, and deployment to Windows. Use this skill whenever the user mentions miniqmt, miniQMT, xtquant, QMT trading, placing stock orders via CLI, checking positions or assets on a QMT account, streaming ticks or klines from QMT, deploying the trading daemon, or troubleshooting the miniqmt-cli daemon. Also use when the user asks about A-share programmatic trading via a CLI daemon architecture.
---

# miniqmt-cli: Drive miniQMT / xtquant from the Command Line

## Architecture

Mac CLI (Click) --> SSH tunnel --> Windows FastAPI daemon (port 8765) --> xtquant/miniQMT

- **Client** (Mac): `~/.miniqmt_cli/client.toml` -- sends HTTP requests through an SSH tunnel
- **Server** (Windows): `~/.miniqmt_cli/server.toml` -- FastAPI daemon wrapping xtquant, runs as a Scheduled Task
- Communication: all commands hit `http://127.0.0.1:8765` via `ssh -N -L 8765:127.0.0.1:8765 <host>`

## Prerequisites

Before running any command, verify:

1. **SSH tunnel is up** -- without it, all commands fail with "cannot reach daemon"
2. **Daemon is running** on Windows -- check with `miniqmt-cli health`
3. **miniQMT client is open** on Windows -- required for xtquant to connect to the broker

Health states and what they mean:

| State | Meaning | Action |
|-------|---------|--------|
| `ready` | Daemon + xtquant + trader all connected | Good to go |
| `daemon_up_no_trader` | Daemon runs but no account logged in yet | First trade/account command will trigger login |
| `daemon_up_xtquant_missing` | xtquant not found | Check `qmt_path` in server.toml |
| Connection refused | Daemon not running or tunnel down | Check tunnel, then restart daemon |

## Global Options

```bash
miniqmt-cli [--format table|json|csv] [--config <path>] <command>
```

Default format comes from `client.toml`; override per-call with `--format`.

## Commands Reference

### Market Data

```bash
# List sectors (e.g. 沪深A股, 上证50)
miniqmt-cli sector list

# List instruments in a sector
miniqmt-cli instrument list --sector "沪深A股"
miniqmt-cli instrument list --limit 10

# Instrument detail
miniqmt-cli instrument info --code 000001.SZ

# Latest tick snapshot (supports multiple codes)
miniqmt-cli tick --code 000001.SZ --code 600519.SH

# Historical K-line (periods: 1d, 1m, 5m)
miniqmt-cli kline --code 000001.SZ --period 1d --start 20260101 --end 20260415

# Historical ticks (raw tick-by-tick data)
miniqmt-cli ticks --code 000001.SZ --start 20260415093000 --end 20260415100000
```

### Real-time Streaming (SSE)

Streams run until Ctrl+C. Output is one event per line.

```bash
# Stream live ticks
miniqmt-cli stream tick --code 000001.SZ --code 600519.SH

# Stream live klines (1m or 5m)
miniqmt-cli stream kline --code 000001.SZ --period 1m
```

### Account & Portfolio

```bash
# List configured accounts (names + masked IDs)
miniqmt-cli account list

# Query account asset (cash, total, frozen)
miniqmt-cli account asset --account sim

# Query positions
miniqmt-cli account position --account sim

# Today's orders
miniqmt-cli account orders --account sim

# Today's trades (fills)
miniqmt-cli account trades --account sim
```

### Trading (Three-Layer Safety)

Orders go through three independent safety checks:

1. **CLI layer**: `--dry-run` preview, interactive "yes" confirmation, `--confirm-live` digit match
2. **Daemon layer**: account whitelist, live-gate re-verification, idempotency dedup
3. **Audit layer**: every order logged to `~/.miniqmt_cli/orders.jsonl` with pre/post phases

```bash
# Buy -- preview only (no order sent)
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 10.50 --dry-run

# Buy -- with interactive confirmation
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 10.50

# Buy -- skip confirmation (scripting)
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 10.50 --yes

# Sell
miniqmt-cli order sell --account sim --code 000001.SZ --volume 100 --price 11.00 --yes

# Live account requires last-4-digit verification
miniqmt-cli order buy --account live --code 000001.SZ --volume 100 --price 10.50 --confirm-live 1234

# Cancel an order
miniqmt-cli order cancel --account sim --order-id 12345 --yes
```

Order types: `--type limit` (default) or `--type market`.

### Daemon Management

```bash
# Check versions (local + remote)
miniqmt-cli version

# Health check
miniqmt-cli health

# Start daemon locally on Windows (normally managed by Scheduled Task)
miniqmt-cli serve [--host 127.0.0.1] [--port 8765] [--dry-run]
```

### Configuration

```bash
# Client config
miniqmt-cli config client init          # Create template ~/.miniqmt_cli/client.toml
miniqmt-cli config client show          # Print resolved config
miniqmt-cli config client set-server-url http://127.0.0.1:8765

# Server config (run on Windows)
miniqmt-cli config server init          # Create template ~/.miniqmt_cli/server.toml
miniqmt-cli config server show          # Print config (account IDs masked)
```

### Deployment (Mac to Windows)

The `setup` wizard walks through 9 steps: parameters, client.toml, SSH check, remote Python, Windows service registration, server.toml, code deploy, SSH tunnel, smoke test.

```bash
# Full wizard (idempotent, remembers progress)
miniqmt-cli setup

# Re-run a specific step (1-9)
miniqmt-cli setup --step 9

# Start fresh
miniqmt-cli setup --reset
```

For day-to-day code updates after initial setup:

```bash
# Deploy script (uses env from wizard state)
WIN_HOST=oopslink@10.211.55.3 WIN_REPO=C:/apps/trading-skills bash scripts/deploy.sh
```

Restart the daemon on Windows:

```bash
ssh oopslink@10.211.55.3 "schtasks /run /tn MiniqmtDaemon"
```

## SSH Tunnel

The tunnel is the lifeline between Mac CLI and Windows daemon.

```bash
# Manual tunnel (foreground)
ssh -N -L 8765:127.0.0.1:8765 oopslink@10.211.55.3

# Persistent tunnel via autossh + launchd (setup wizard can generate the plist)
launchctl load ~/Library/LaunchAgents/com.miniqmt.tunnel.plist
```

## Config File Reference

### client.toml (`~/.miniqmt_cli/client.toml`)

```toml
[client]
mode = "auto"                         # auto | local | remote
server_url = "http://127.0.0.1:8765"  # required when mode = remote

[client.output]
format = "table"
```

Env overrides: `MINIQMT_CLI_MODE`, `MINIQMT_CLI_SERVER_URL`, `MINIQMT_CLI_FORMAT`

### server.toml (`~/.miniqmt_cli/server.toml`, on Windows)

```toml
[server]
host = "127.0.0.1"
port = 8765
qmt_path = "C:/国金QMT交易端/userdata_mini"

[accounts.sim]
account_id = "55001234"
account_type = "STOCK"

[accounts.live]
account_id = "88881234"
account_type = "STOCK"
requires_confirm_live = true

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"
```

Env overrides: `MINIQMT_CLI_SERVER_HOST`, `MINIQMT_CLI_SERVER_PORT`, `MINIQMT_CLI_SERVER_QMT_PATH`

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "cannot reach daemon" | Tunnel down or daemon stopped | Check `ssh -N -L ...` is running; `schtasks /run /tn MiniqmtDaemon` |
| `daemon_up_xtquant_missing` | `qmt_path` wrong or miniQMT not installed | Edit server.toml `qmt_path`; ensure miniQMT client directory exists |
| `daemon_up_no_trader` | No account has been accessed yet | Normal on fresh start; first account/order command triggers login |
| Exit code -1073741510 | Daemon was killed (Ctrl+C / task stopped) | Restart: `schtasks /run /tn MiniqmtDaemon` |
| `GuardExit` on order | Safety guard blocked the order | Check: `--dry-run` was set, confirmation was declined, or `--confirm-live` missing/wrong |
| `BrokerReject` | Broker refused the order | Check order params, market hours, account balance |
| GBK encoding errors in SSH | Windows CMD default codepage | Use `chcp 65001` prefix or pipe through Python |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic error |
| 2 | Broker rejected the order (`BrokerReject`) |
| 3 | Safety guard refused to proceed (`GuardExit`) |
