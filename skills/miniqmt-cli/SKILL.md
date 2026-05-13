---
name: miniqmt-cli
description: Operate miniQMT/xtquant via the miniqmt-cli tool -- market data queries, real-time streaming, account management, order placement with safety guards, daemon health checks, SSH tunnel management, and deployment to Windows. Use this skill whenever the user mentions miniqmt, miniQMT, xtquant, QMT trading, placing stock orders via CLI, checking positions or assets on a QMT account, streaming ticks or klines from QMT, deploying the trading daemon, or troubleshooting the miniqmt-cli daemon. Also use when the user asks about A-share programmatic trading via a CLI daemon architecture.
---

# miniqmt-cli: Drive miniQMT / xtquant from the Command Line

## Access Policy — read first

**All programmatic access to the trading daemon MUST go through `miniqmt-cli`.** Direct HTTP calls to `http://127.0.0.1:8765/...` are no longer a supported integration surface — the URL space, payload shapes, and error semantics are internal implementation details and may change without notice.

Reasons:

- **One source of truth.** The CLI's command set is the only documented API. The HTTP routes underneath exist solely to let the CLI talk to the daemon over an SSH tunnel; their paths are unstable.
- **Drift kills callers.** When a parallel HTTP reference existed, a polling agent followed a stale `/positions` / `/orders` / `/trades` (unprefixed) path and silently 404'd for hours. The CLI shields callers from this class of bug.
- **Safety pipeline.** The CLI carries the masking, idempotency client_req_id generation, and `--confirm-live` handshake conventions. Bypassing it weakens those guarantees even though the daemon enforces most of them.

Callers from non-Python runtimes should wrap the CLI as a subprocess (`subprocess.run(["miniqmt-cli", "account", "asset", "--account", "main", "--format", "json"], capture_output=True)`) rather than constructing HTTP requests by hand. SSE consumers should use `miniqmt-cli stream tick|kline|order ...` and parse its stdout, not subscribe to `/stream/*` directly.

If you're an agent on a different host than the daemon, configure remote mode in `~/.miniqmt_cli/client.toml`:

```toml
[client]
mode = "remote"
server_url = "http://127.0.0.1:8765"   # the daemon, via SSH tunnel
```

Then bring up the tunnel (see [SSH Tunnel](#ssh-tunnel) below). The CLI then works transparently — no proxy config needed.

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
| `ready` | Daemon + xtquant loaded + trader logged in + today's risk baseline captured for every configured account | Good to go |
| `daemon_up_no_trader` | Daemon has not yet opened an `XtQuantTrader` session for any account this process lifetime. **Does NOT mean miniQMT is logged out** — traders are created lazily on the first account/order request. | Normal on fresh daemon; run any `account` command to trigger the session and re-check |
| `daemon_up_baseline_pending` | Trader is logged in for at least one account, but today's risk baseline could not be captured for one or more configured accounts. Returns `{"accounts_pending": [...]}`. Orders on a pending account will be rejected with `BASELINE_PENDING` (fail-closed). | Normally self-heals: baseline is captured on trader login, so hitting any `account asset/position/orders` against the pending account re-tries. If it persists, check daemon logs for `baseline capture after login failed` — usually a transient broker blip; re-run the `account asset` probe. |
| `daemon_up_xtquant_missing` | xtquant module could not be loaded | Check `qmt_path` in server.toml |
| `risk_breaker_tripped` | At least one account's risk breaker is tripped (e.g. daily loss limit exceeded). Opening orders are blocked; cancels / closing sells still work. Returns `{"tripped_accounts": [...]}`. | Review `miniqmt-cli risk status --account <name>`; reset with `miniqmt-cli risk reset --account <name> --note "<reason>"` after confirming the cause |
| Connection refused | Daemon not running or tunnel down | Check tunnel, then restart daemon |

**Important — do not confuse `daemon_up_no_trader` with "miniQMT not logged in".** The daemon only tracks its own in-memory trader session pool (`len(self._traders)`). It never queries miniQMT's GUI login state. To probe the actual login/broker connection, run an account command (e.g. `miniqmt-cli account asset --account <name>`) — a real login failure surfaces there as `trader.connect failed rc=...` or `trader.subscribe failed rc=...`, not in `health`.

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

```bash
# Stream order lifecycle events (submitted / partially filled / filled / cancelled / rejected)
# Essential for agents: subscribe before placing an order, then consume fill/reject events.
miniqmt-cli stream order --account sim

# JSON format for programmatic parsing
miniqmt-cli --format json stream order --account sim
```

Event payload shape (JSON mode). The **first** line after subscription is an envelope, then each order state change emits an `order_status` dict:

```json
{"event": "subscribed", "filter_account": "sim"}
{"type": "order_status", "account": "sim", "order_id": 12345, "code": "000001.SZ",
 "side": "buy", "status": "filled", "volume": 100, "filled_volume": 100,
 "avg_price": 10.48, "frozen": 0.0}
```

Also emitted on the same stream: `{"type": "order_response", ...}` (async submit ack) and `{"type": "trade", ..., "trade_id": ..., "price": ..., "amount": ...}` (per-fill detail).

Possible `status` values (from xtquant `order_status`): `submitted`, `confirmed`, `partially_filled`, `filled`, `cancelled`, `rejected`, `expired`, `pending_cancel`, `unknown`. Unknown codes are returned as `unknown_<n>` — agents should have a default branch.

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

> **Terminology — "live account" (实盘账户)**: Any account with `requires_confirm_live = true` in `server.toml`. This is a **property**, not an account name. Set to `false` for sim/paper accounts. In the examples below, `sim` is a paper account and `real` is a live account.
>
> Where `--confirm-live <last-4-digits-of-account_id>` is required on live accounts (current behavior):
>
> | Command | `--confirm-live` required on live? | Enforced by |
> |---------|---|---|
> | `order buy` / `order sell` (incl. `--dry-run`) | Yes | CLI (`order.py`) + daemon (`/trade/order`) |
> | `risk reset` | Yes | CLI (`risk.py`) + daemon (`/risk/reset`) |
> | `order cancel` | **No** — the CLI command has no `--confirm-live` flag and `/trade/cancel` does not re-check the live gate. Cancels are still gated by the daemon account whitelist. | — |

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

# Live (real-money) account requires last-4-digit verification
miniqmt-cli order buy --account real --code 000001.SZ --volume 100 --price 10.50 --confirm-live 1234

# Cancel an order
miniqmt-cli order cancel --account sim --order-id 12345 --yes
```

Order types: `--type limit` (default) or `--type market`.

#### Complete Trading Workflow (agent-facing)

Follow these steps in order. Each step has a purpose — skipping is how agents end up with "order placed, no idea what happened".

**1. Pre-flight health — probe real connectivity, not just `/health`.**

`health` returning `daemon_up_no_trader` is normal on a fresh daemon and is NOT a login failure (see the Prerequisites section). To confirm the account is actually usable, hit a trader-touching endpoint:

```bash
miniqmt-cli --format json account asset --account sim
```

Success (`cash` / `total_asset` numeric) proves: daemon up → xtquant loaded → trader connected → account subscribed. After this, `health` will be `ready`.

If this fails with `trader.connect failed rc=...` or `trader.subscribe failed rc=...`, stop — investigate before sending any order.

**2. Check risk state.**

```bash
miniqmt-cli --format json risk status --account sim
```

If `breaker_tripped: true`, opening orders will be rejected at the daemon layer regardless of CLI confirmations. Closing trades and cancels still work. Resolve with `risk reset` (requires a `--note`) before opening.

**3. Preview first (dry run).**

```bash
miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 10.50 --dry-run
```

Dry-run hits `/trade/preview` (no order sent), shows masked account id, last price, and estimated cost. Exit code 3 (`GuardExit`: "dry-run: order not sent") is the success path. Agents should read the preview numbers to catch typos (volume unit is shares not lots; price is yuan per share).

**4. Place the order, wait for a terminal status in one call.**

```bash
miniqmt-cli --format json order buy --account sim --code 000001.SZ \
    --volume 100 --price 10.50 --yes --wait 30
```

`--wait N` blocks up to N seconds after the POST returns, subscribing to `/stream/order` and waiting for a terminal status (`filled` / `cancelled` / `rejected`) on the returned `seq`. **The subscription happens after submit**, so fast fills can theoretically race — in practice miniQMT fills take enough time that `--wait` catches them, but early lifecycle events (`submitted`, `confirmed`) may be missed. For zero-loss event capture, use the separate subscribe/place pattern below.

- The JSON response carries `{"seq": <order_id>, "status": "ok"|"rejected", "client_req_id": ...}`. **Persist `seq`** — you need it to cancel or to correlate stream events.
- `client_req_id` is a UUID the CLI auto-generates for idempotency. Re-submitting with the same id within the TTL window returns the original response with `"idempotent_hit": true` instead of placing a duplicate.
- **Note on `--format json` output**: the order command still prints human-readable preview lines (`Account:`, `Code:`, `Side:`, etc.) to stdout before the final JSON line. Parsers must read the last line, e.g. `... | tail -1 | jq -r '.seq'`.

If you want to run the submit and the event loop separately (e.g. long-running agent) to avoid missing early events:

```bash
# Terminal A — subscribe BEFORE placing the order
miniqmt-cli --format json stream order --account sim > events.jsonl &

# Terminal B — place the order, capture seq from the JSON response (last line)
SEQ=$(miniqmt-cli --format json order buy --account sim --code 000001.SZ \
      --volume 100 --price 10.50 --yes | tail -1 | jq -r '.seq')

# Watch events.jsonl for order_status where order_id == $SEQ
```

Subscribing **before** placing is the only way to guarantee you see the full lifecycle (`submitted` → `confirmed` → `filled`). If the stream subscriber is registered after the daemon has already dispatched an event, that event is lost for this subscriber (the daemon fans out only to currently-registered subscribers; see `session.py` `dispatch_order_event`).

**5. Verify the outcome.**

```bash
miniqmt-cli account orders --account sim   # all of today's orders
miniqmt-cli account trades --account sim   # all of today's fills
miniqmt-cli account position --account sim # current positions
```

The broker is the source of truth, not the stream. If a stream event is missed (connection blip, subscriber queue overflow — drops are logged server-side), these queries reconcile.

#### Cancelling

Know which scenario you're in before cancelling:

| Order state | Can cancel? | What happens |
|-------------|-------------|--------------|
| `submitted` / `confirmed` (unfilled) | Yes | Full cancel; `status` → `cancelled` |
| `partially_filled` | Yes | Cancels the remaining unfilled portion only; already-filled shares stay |
| `filled` / `cancelled` / `rejected` | No | Broker will reject the cancel |

Cancel takes the same form for sim and live accounts — the CLI has no `--confirm-live` flag on cancel (see the Terminology table above for the full matrix). Whitelist enforcement still happens at the daemon layer.

```bash
miniqmt-cli order cancel --account sim --order-id 12345 --yes
miniqmt-cli order cancel --account real --order-id 12345 --yes

# JSON output for scripting
miniqmt-cli --format json order cancel --account sim --order-id 12345 --yes
```

Cancel flow:

1. Get `order-id` from the place response (`seq` field) or from `account orders`.
2. POST `/trade/cancel` returns `{"status": "ok", "seq": <cancel_seq>}` synchronously — this is the **submit ack**, not confirmation the order is cancelled.
3. Watch `/stream/order` for `order_status` where `order_id == <original order_id>` and `status == "cancelled"`. The actual cancel can take a moment, and the broker may reject if the order filled first.
4. Reconcile with `account orders` if you need certainty.

Cancels are also idempotent via `client_req_id` (CLI auto-generates). Re-running the same cancel command without changes creates a new `client_req_id`, so it **will** hit the broker again — use the HTTP API with a stable `client_req_id` if you need true idempotency across retries.

#### Error Handling

| Exit | Python class | Meaning | Agent action |
|------|------|---------|-------|
| 0 | — | Success | Proceed |
| 2 | `BrokerReject` | Broker refused the order (out of hours, insufficient balance, price band, halted stock, etc.) | Read message, fix input, don't blindly retry |
| 3 | `GuardExit` | Safety guard refused: `--dry-run`, user declined "yes", missing/wrong `--confirm-live` | Expected for `--dry-run`; otherwise fix flags |
| 1 | generic | Network error, timeout, unknown failure | Check tunnel / daemon health, then retry |

Risk rejections come back as HTTP 400 with body `{"error": "risk_reject", "code": "<limit_name>", "message": "..."}` — in CLI they surface as exit 1 with the message. Agents using HTTP directly should branch on `code` (e.g. `max_daily_loss`, `max_position_pct`, `max_orders_per_minute`).

### Risk Control

The daemon enforces risk limits independently (v0.2.0+). When a limit trips, the breaker enters **block-open-allow-close** mode: new opening orders are rejected, but closing / cancel operations still work.

```bash
# Show risk status for one account (baseline, PnL, breaker state, pending orders)
miniqmt-cli risk status --account sim

# Show all accounts
miniqmt-cli risk status

# JSON for agents
miniqmt-cli --format json risk status --account sim

# Reset the breaker (operator action — requires a justification note)
miniqmt-cli risk reset --account sim --note "false positive: baseline re-captured"

# Live (real-money) account reset requires last-4-digit confirmation
miniqmt-cli risk reset --account real --note "manual unfreeze" --confirm-live 1234
```

`risk status` fields of interest:

| Field | Meaning |
|-------|---------|
| `baseline_total_asset` | Opening snapshot asset at session start |
| `baseline_imprecise` | `true` if baseline was captured after first trade (less reliable) |
| `current_total_asset` | Latest cached asset |
| `daily_pnl` | `current - baseline` |
| `breaker_tripped` | Boolean — if true, `breaker_reason` explains which limit |
| `pending_orders` | Map of `code -> {buy_volume, buy_amount, sell_volume, sell_amount}` |
| `orders_in_window` | Count of orders in the rolling 60s frequency window |

`server.toml` `[risk]` defaults:

```toml
[risk]
enabled = true
max_daily_loss = 50000          # yuan
max_position_pct = 30           # % of total asset per single stock
max_orders_per_minute = 10
max_positions = 10
```

Per-account overrides live in `[accounts.<name>.risk]`.

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
WIN_HOST=<user>@<windows-host> WIN_REPO=C:/apps/trading-skills bash scripts/deploy.sh
```

Restart the daemon on Windows:

```bash
ssh <user>@<windows-host> "schtasks /run /tn MiniqmtDaemon"
```

## SSH Tunnel

The tunnel is the lifeline between Mac CLI and Windows daemon.

```bash
# Manual tunnel (foreground)
ssh -N -L 8765:127.0.0.1:8765 <user>@<windows-host>

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
account_id = "1230001"
account_type = "STOCK"

[accounts.real]                       # real-money account; name is arbitrary
account_id = "1230002"
account_type = "STOCK"
requires_confirm_live = true          # marks this as a "live account" — extra confirmation required

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"
```

Env overrides: `MINIQMT_CLI_SERVER_HOST`, `MINIQMT_CLI_SERVER_PORT`, `MINIQMT_CLI_SERVER_QMT_PATH`

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "cannot reach daemon" | Tunnel down or daemon stopped | Check `ssh -N -L ...` is running; `schtasks /run /tn MiniqmtDaemon` |
| `daemon_up_xtquant_missing` | `qmt_path` wrong or miniQMT not installed | Edit server.toml `qmt_path`; ensure miniQMT client directory exists |
| `daemon_up_no_trader` | Daemon's trader pool is empty — no account API has been called yet this process lifetime. Does NOT mean miniQMT is logged out. | Not an error. Run an `account` command to trigger lazy session creation; real login failures surface there |
| `daemon_up_baseline_pending` | Trader is up but today's risk baseline wasn't captured (login-time capture failed, e.g. broker blip). Orders on the pending account will be rejected `BASELINE_PENDING`. | Run `miniqmt-cli account asset --account <name>` again — login re-captures baseline. Check daemon logs for `baseline capture after login failed` if it persists. |
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

## Related Skills

- **trading-analysis** — Money flow + signals built on top of `miniqmt-cli ticks`
