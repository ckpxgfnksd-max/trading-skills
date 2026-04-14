# miniqmt-cli Design

**Status:** Draft (awaiting review)
**Date:** 2026-04-14
**Target version:** v0.1.0

## 1. Goal

Build a command-line tool, `miniqmt-cli`, that exposes the daily workflow of [xtquant / miniQMT](https://dict.thinktrader.net/nativeApi/xtquant.html) — real-time market data, account queries, and live trading — through a consistent CLI. The tool must be usable in two environments:

1. **Directly on Windows**, where `xtquant` is importable and the miniQMT client is running.
2. **From macOS via a remote daemon**, where the operator SSHs or otherwise reaches a Windows box that hosts the daemon. Network reachability is assumed to be handled out-of-band (SSH tunnel, LAN, VPN).

The CLI must make local and remote modes behave identically from the user's point of view, while keeping the long-lived xtquant session and all trading state on the Windows side.

Reference connection info: `{tag: "sp3", version: "1.0"}`.

## 2. Non-goals (v1)

- Level 2 market data, 融资融券, options, 期货算法单, 条件单
- Multi-user daemon (single operator per daemon instance)
- GUI or web dashboard
- Windows service registration / auto-start
- Authentication between CLI and daemon beyond network-layer trust (user's responsibility)

## 3. Architecture

### 3.1 Deployment

```
┌─ Windows host ───────────────────┐         ┌─ macOS host ────────┐
│                                  │         │                     │
│  miniQMT client (GUI)            │         │  miniqmt-cli        │
│        ▲                         │         │  (remote mode)      │
│        │ xtquant.dll             │         │        │            │
│  miniqmt-cli serve   (FastAPI)   │ ◀──HTTP─┤        │            │
│        ▲                         │ (user-  │        ▼            │
│        │ HTTP localhost          │ managed │   transport.py      │
│  miniqmt-cli  (local mode)       │  net)   │                     │
└──────────────────────────────────┘         └─────────────────────┘
```

Every CLI invocation — whether on Windows or on macOS — talks to the daemon over HTTP. The CLI never imports `xtquant`. This keeps:

- CLI and daemon lifecycles independent (CLI crash cannot disturb an active trading session or subscriptions)
- A single code path for both modes (the only difference is the target URL)
- Testability high (the daemon can be swapped for a fake HTTP server in integration tests)

### 3.2 Run modes

Mode is resolved by `client_config.py`:

| `client.mode` | Behavior                                                                                |
|---------------|-----------------------------------------------------------------------------------------|
| `local`       | Use `http://127.0.0.1:8765` unconditionally                                              |
| `remote`      | Use `client.server_url`; error out if missing                                           |
| `auto`        | If `server_url` is set, use it; otherwise fall back to `http://127.0.0.1:8765`          |

### 3.3 Statefulness

The daemon is stateful and long-lived. It holds:

- A pool of logged-in `xttrader` sessions, keyed by account name (`sim`, `live`, ...). Login is lazy — on first use of an account — and cached for the daemon lifetime.
- A subscription registry scoped to individual HTTP connections. When a streaming HTTP connection ends, its subscriptions are unsubscribed via a disconnect hook.
- An append-only audit log for every `order` request.

The CLI client is stateless. No local caching of market data (unlike `tushare-cli`'s optional cache), because xtquant data is typically real-time.

### 3.4 Streaming

Real-time tick and kline streams use **Server-Sent Events** (`text/event-stream`) served via FastAPI `StreamingResponse`. One JSON event per SSE `data:` line. CLI reads lines, parses each, and prints a formatted row until Ctrl+C closes the HTTP connection, at which point the daemon's `on_disconnect` hook fires and unsubscribes.

This ties subscription lifetime to connection lifetime by construction — there are no dangling subscriptions.

## 4. Module layout

```
tools/miniqmt_cli/
  pyproject.toml
  README.md
  src/miniqmt_cli/
    __init__.py
    main.py                   # click root group

    client_config.py          # loads ~/.miniqmt_cli/client.toml
    server_config.py          # loads ~/.miniqmt_cli/server.toml
    output.py                 # table / json / csv formatters (tushare_cli-style)

    client/
      __init__.py
      transport.py            # HTTP client: request() + stream()
      errors.py               # HTTP → ClickException mapping

    server/
      __init__.py
      app.py                  # FastAPI app factory
      routes_data.py          # /data/*
      routes_trade.py         # /trade/*
      routes_stream.py        # /stream/* (SSE)
      xtdata_adapter.py       # wraps xtquant.xtdata (lazy import)
      xttrader_adapter.py     # wraps xtquant.xttrader (lazy import)
      session.py              # trader session pool + subscription registry
      audit.py                # append-only orders.jsonl

    commands/
      instrument.py           # instrument list / info
      sector.py               # sector list
      kline.py                # kline history
      tick.py                 # tick snapshot
      stream.py               # stream tick / stream kline
      account.py              # account list/asset/position/orders/trades
      order.py                # order buy/sell/cancel (with confirmation flow)
      server.py               # serve / health / version
      config.py               # config client/server init/show/...
```

**Key boundaries:**

- `client_config.py` and `server_config.py` are independent — neither imports the other, and neither imports code from `server/` or `client/` inappropriately.
- Anything under `server/` is only imported inside `commands/server.py` (the `serve` / `health` / `version` commands). Mac clients never load `server/` code and never attempt to `import xtquant`.
- `xtquant` is imported lazily inside `server/xtdata_adapter.py` and `server/xttrader_adapter.py` at the point the adapter is first used, never at module import time.

## 5. Command surface (v1)

Global flags (inherited from the click root group):

- `--format {table,json,csv}` — output format (default `table`)
- `--config <path>` — override client config file path

```
miniqmt-cli
├── config
│   ├── client init | show | set-server-url <url>
│   └── server init | show                            # masks account_id
├── version                                           # local + remote version info
├── health                                            # daemon reachability + xtquant state
├── serve [--host] [--port] [--dry-run]               # launch daemon
├── instrument
│   ├── list [--sector <name>]
│   └── info --code <ts_code>
├── sector list
├── kline --code <ts_code> --period {1d,1m,5m,tick} --start <date> --end <date>
├── tick --code <ts_code>...
├── stream
│   ├── tick  --code <ts_code>...
│   └── kline --code <ts_code>... --period {1m,5m}
├── account
│   ├── list
│   ├── asset    --account <name>
│   ├── position --account <name>
│   ├── orders   --account <name>
│   └── trades   --account <name>
└── order
    ├── buy    --account <name> --code <ts_code> --volume <int> --price <float> [--type {limit,market}] [--dry-run] [--yes] [--confirm-live]
    ├── sell   (same options as buy)
    └── cancel --account <name> --order-id <id> [--yes]
```

`serve --dry-run` starts the HTTP server but stubs out xtquant adapters, so the CLI client side can be tested end-to-end without a real QMT client.

## 6. Data flow

### 6.1 Read-only queries

Example: `miniqmt-cli account position --account sim`

```
CLI → client_config.resolve_mode()
    → transport.request("GET", "/trade/positions?account=sim")
    → daemon: routes_trade.positions
    → session.get_trader("sim")  (lazy login + cache)
    → xttrader_adapter.query_stock_positions(trader)
    → list[dict]
    → response body
CLI → output.format_output(df, ctx.obj["fmt"])
```

All read-only endpoints are `GET` with query-string parameters.

### 6.2 Streaming

Example: `miniqmt-cli stream tick --code 000001.SZ --code 600000.SH`

```
CLI → transport.stream("GET", "/stream/tick?codes=000001.SZ&codes=600000.SH")
      (keeps HTTP connection open)
    → daemon: routes_stream.tick
    → subscribe_quote(codes, callback=push_to_asyncio_queue)
    → async generator yields SSE events ("data: {...}\n\n") from the queue
CLI  → iterate lines → json.loads → format single row → flush
Ctrl+C
CLI  → closes HTTP connection
daemon → on_disconnect hook → unsubscribe_quote(seq_ids)
```

Both sides use backpressure-friendly iteration. The daemon's queue is bounded; if the CLI cannot keep up the daemon drops the oldest tick and logs a warning.

### 6.3 Order placement

Example: `miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 12.34`

Three layers of defense:

**CLI layer**

1. `--account` is required, no default.
2. CLI fetches the account metadata from the daemon (`GET /trade/account/meta?name=<name>`); if the daemon reports `requires_confirm_live = true`, `--confirm-live` must also be present, otherwise the CLI exits without sending the order.
3. CLI calls `GET /trade/preview?...` to fetch last price, estimated cost, and account buying power.
4. CLI prints a confirmation table:
   ```
   Account:   sim (55001234)
   Code:      000001.SZ  平安银行
   Side:      BUY
   Volume:    100
   Price:     12.34  (limit)
   Est.Cost:  1234.00 + fee ~0.61
   ───────────────────────────────
   Type "yes" to confirm:
   ```
5. On `--dry-run`, the confirmation table is printed and the flow ends (no POST sent).
6. `--yes` skips the interactive prompt but all other checks still run.

**Daemon layer**

1. Re-validate that `account` is in `[accounts.*]` whitelist (CLI can't be trusted).
2. `audit.append(phase="pre", ...)` — write to `orders.jsonl` **before** calling xttrader.
3. `xttrader_adapter.order_stock(...)`.
4. `audit.append(phase="post", result=..., order_id=..., seq=...)`.
5. Return the order id and broker status.

**Audit layer**

`~/.miniqmt_cli/orders.jsonl`, append-only, one JSON object per line:

```json
{"ts":"2026-04-14T10:23:41+08:00","phase":"pre","client_req_id":"abc...",
 "account":"sim","account_id":"55001234","code":"000001.SZ",
 "side":"buy","volume":100,"price":12.34,"type":"limit"}
{"ts":"2026-04-14T10:23:41+08:00","phase":"post","client_req_id":"abc...",
 "order_id":"12345","seq":7,"status":"ok"}
```

The `pre` record is flushed before the xttrader call, so even a daemon crash mid-call leaves a trail.

### 6.4 Cancel

Same flow as order placement. The confirmation table shows `order_id`, `code`, and unfilled volume, fetched from `query_stock_orders` in the preview step. Audit log covers both phases.

### 6.5 Error mapping

| Source                          | CLI behavior                                                          |
|---------------------------------|-----------------------------------------------------------------------|
| Network / connection failure    | `ClickException("cannot reach daemon at <url>")`, exit 1             |
| Daemon 4xx with `detail`        | Print `detail` in red, exit 1                                        |
| Daemon 5xx                      | Print generic "daemon error", log body at debug level, exit 1        |
| xttrader login failed           | Suggest checking `qmt_path` / `session_id` / `account_id`            |
| Broker rejected order (seq ≤ 0) | Print broker return code + description in red, exit 2               |

## 7. Configuration

### 7.1 Client config — `~/.miniqmt_cli/client.toml`

```toml
[client]
mode = "auto"                         # auto | local | remote
server_url = "http://127.0.0.1:8765"  # required when mode = remote

[client.output]
format = "table"                      # default output format
```

Resolution order (highest precedence first):

1. CLI flag (`--config`, `--format`, ...)
2. Environment variables (`MINIQMT_CLI_SERVER_URL`, `MINIQMT_CLI_MODE`, `MINIQMT_CLI_FORMAT`)
3. `./miniqmt_cli.client.toml` (project-local override)
4. `~/.miniqmt_cli/client.toml`

### 7.2 Server config — `~/.miniqmt_cli/server.toml`

```toml
[server]
host = "127.0.0.1"
port = 8765
qmt_path = "C:/国金QMT交易端/userdata_mini"
session_id = 123456

[accounts.sim]
account_id = "55001234"
account_type = "STOCK"                # STOCK | CREDIT | FUTURE

[accounts.live]
account_id = "88881234"
account_type = "STOCK"
requires_confirm_live = true

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"
```

Read only by `commands/server.py` (the `serve` command) and code under `server/`. Never read by client code paths. Resolution order mirrors the client config with `MINIQMT_CLI_SERVER_*` env vars.

### 7.3 Config commands

- `miniqmt-cli config client init` — write a template `client.toml`
- `miniqmt-cli config client show` — print the effective resolved client config
- `miniqmt-cli config client set-server-url <url>` — convenience writer
- `miniqmt-cli config server init` — write a template `server.toml`
- `miniqmt-cli config server show` — print the effective resolved server config, masking every `account_id`

## 8. Testing strategy

Tests live in `tests/miniqmt_cli/`, mirroring `tests/tushare_cli/`.

| Layer       | Scope                                                              | Tooling                                                     | Platform  |
|-------------|--------------------------------------------------------------------|-------------------------------------------------------------|-----------|
| Unit        | `client_config.py`, `server_config.py`, `output.py`, `audit.py`, `session.py`, click command parsing | pytest                                                      | any       |
| Adapter     | `xtdata_adapter.py`, `xttrader_adapter.py`                         | pytest + **fake xtquant stub** injected into `sys.modules`  | any       |
| Integration | FastAPI app + CLI transport end-to-end                             | pytest + `httpx.AsyncClient` / FastAPI `TestClient`         | any       |

### 8.1 Fake xtquant

`tests/fakes/xtquant_stub.py` provides controllable `xtdata` and `xttrader` modules. A `conftest.py` fixture installs it into `sys.modules["xtquant"]` before the daemon imports its adapters. All adapter and integration tests run on Mac and Linux without any real QMT install.

### 8.2 Required test cases (MUST pass)

- **Audit integrity**: `pre` record is on disk before the xttrader call; `post` record (with `status=error`) is written even when xttrader raises.
- **Whitelist enforcement**: an account name not in `[accounts.*]` is rejected at the daemon before any trader call, and this is visible in tests (no `order_stock` invocation on the fake).
- **Live-account gate**: `--account live` without `--confirm-live` exits non-zero with a clear message and zero side effects (no audit row, no HTTP request).
- **Dry-run**: `--dry-run` prints the confirmation table, makes no `POST /trade/order` call, writes no audit row.
- **`--yes`**: skips the interactive prompt, still runs all other guards, still writes audit rows.
- **SSE cleanup**: after the CLI disconnects, the daemon's fake `xtdata.unsubscribe_quote` is called exactly once per active `seq`.
- **Config separation**: loading `client.toml` does not read `server.toml`, and vice versa. Missing `server.toml` does not break client commands.
- **Lazy xtquant import**: importing `miniqmt_cli.main` on a machine without xtquant installed does not raise. Only `miniqmt-cli serve` triggers the import.

## 9. Packaging

`tools/miniqmt_cli/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "miniqmt-cli"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
    "rich>=13.0",
    "pandas>=2.0",
    "fastapi>=0.110",
    "uvicorn>=0.27",
    "httpx>=0.27",
]

[project.optional-dependencies]
server = ["xtquant"]   # only installable on Windows

[project.scripts]
miniqmt-cli = "miniqmt_cli.main:cli"

[tool.setuptools.packages.find]
where = ["src"]
```

Install matrix:

- **Windows (full)**: `pip install -e "tools/miniqmt_cli[server]"` — installs xtquant.
- **macOS (client-only)**: `pip install -e tools/miniqmt_cli` — xtquant not installed, `serve` is still importable but fails fast with a clear message if invoked.

## 10. Open questions

None blocking v1. Items intentionally deferred to post-v1:

- Daemon-level auth token (currently relies on network-layer trust)
- Condition/algo orders
- Multi-account aggregation views
- Reconnect/retry policy on xttrader session drops (v1 simply re-logs-in on next request)
- Audit log rotation (v1 keeps a single append-only file)

## 11. Approval checklist

- [x] Scope = data + account + trading (C), with Mac remote usage supported
- [x] Architecture = HTTP daemon (B), `server_url` configurable, network setup out-of-scope
- [x] Trading safety = whitelist + interactive confirm + dry-run + audit log (C)
- [x] Streaming = snapshot + SSE-based `stream tick`/`stream kline` (B)
- [x] v1 command surface as listed in §5
- [x] Client and server configs separated into two files
