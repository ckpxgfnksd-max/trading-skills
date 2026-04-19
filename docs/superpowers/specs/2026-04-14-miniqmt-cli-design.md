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
┌─ Windows host ─────────────────────┐          ┌─ macOS host ─────────┐
│                                    │          │                      │
│   miniQMT client (GUI)             │          │   miniqmt-cli        │
│          ▲                         │          │   (remote mode)      │
│          │ xtquant (sys.path)      │          │         │            │
│   miniqmt-cli serve (FastAPI)      │          │         ▼            │
│          ▲                         │ ◀── HTTP ── transport.py        │
│          │ HTTP localhost          │  (user-  │                      │
│   miniqmt-cli (local mode)         │  managed │                      │
│                                    │   net)   │                      │
└────────────────────────────────────┘          └──────────────────────┘

Request direction: macOS CLI → Windows daemon. Network reachability
(SSH local-forward, VPN, LAN) is the operator's responsibility — see §2.
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

- A pool of logged-in `xttrader` sessions, keyed by account name (`sim`, `live`, ...). Login is lazy — on first use of an account — and cached for the daemon lifetime. Each account has a dedicated `asyncio.Lock` held for the duration of login, so two concurrent requests for the same uninitialized account result in exactly one `create_trader` + `login` pair, not two.
- A subscription registry scoped to individual HTTP connections. Subscriptions are allocated inside a streaming response's async generator and released in its `finally` block, so the lifetime of each subscription is bounded by the lifetime of its HTTP connection.
- An append-only audit log for every `order` request.
- A short-TTL idempotency cache (`client_req_id → order result`) covering the last N minutes of order requests, used to deduplicate CLI retries.

The CLI client is stateless. No local caching of market data (unlike `tushare-cli`'s optional cache), because xtquant data is typically real-time.

### 3.4 Streaming

Real-time tick and kline streams use **Server-Sent Events** (`text/event-stream`) served via FastAPI `StreamingResponse`. One JSON event per SSE `data:` line. CLI reads lines, parses each, and prints a formatted row until Ctrl+C closes the HTTP connection.

Cleanup is implemented inside the async generator itself, not via a framework callback (FastAPI/Starlette have no `on_disconnect` hook for streaming responses). The canonical pattern is:

```python
async def stream_tick(request, codes):
    seq_ids = xtdata_adapter.subscribe_quote(codes, push=queue.put_nowait)
    try:
        while not await request.is_disconnected():
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
    finally:
        xtdata_adapter.unsubscribe_quote(seq_ids)
```

This ties subscription lifetime to connection lifetime by construction — there are no dangling subscriptions even if the generator is cancelled.

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
- `--config <path>` — override **client** config file path (client commands only)
- `--server-config <path>` — override **server** config file path (`serve` command only)

```
miniqmt-cli
├── config
│   ├── client init | show | set-server-url <url>
│   └── server init | show                            # masks account_id
├── version                                           # local + remote version info
├── health                                            # daemon reachability + xtquant state
├── serve [--host] [--port] [--dry-run] [--server-config <path>]
├── instrument
│   ├── list (--sector <name> | --limit <n>)          # one of the two is required
│   └── info --code <ts_code>
├── sector list
├── kline --code <ts_code> --period {1d,1m,5m} --start <date> --end <date>
├── ticks --code <ts_code> --start <datetime> --end <datetime>   # historical tick sequence
├── tick  --code <ts_code>...                                    # latest tick snapshot
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
    ├── buy    --account <name> --code <ts_code> --volume <int> --price <float> [--type {limit,market}] [--dry-run] [--yes] [--confirm-live <last4>]
    ├── sell   (same options as buy)
    └── cancel --account <name> --order-id <id> [--yes]
```

**Disambiguation:**

- `tick` returns the latest tick snapshot (point-in-time). `ticks` returns a historical tick sequence (time range). `kline` returns OHLCV bars and therefore rejects `--period tick` — raw ticks are only available via `ticks` or `stream tick`.
- `instrument list` without `--sector` or `--limit` errors out instead of dumping several thousand rows by default.

`serve --dry-run` starts the HTTP server but stubs out xtquant adapters, so the CLI client side can be tested end-to-end without a real QMT client.

### 5.1 version and health behavior

`version` always succeeds and prints the local CLI version. If the daemon is configured and reachable, it also appends the daemon's response (`{tag, version, xtquant_build}`); otherwise it prints `remote: unreachable` and exits 0.

`health` returns one of four states:

| State                         | Meaning                                                                       | Exit |
|-------------------------------|-------------------------------------------------------------------------------|------|
| `daemon_down`                 | Cannot reach daemon at configured URL                                         | 1    |
| `daemon_up_xtquant_missing`   | Daemon reachable but failed to load xtquant from `qmt_path`                   | 1    |
| `daemon_up_no_trader`         | Daemon and xtquant loaded, but no trader session logged in yet                | 0    |
| `ready`                       | Daemon up, xtquant loaded, at least one trader session logged in and alive   | 0    |

`daemon_up_no_trader` is a healthy state — it only means no account has been touched yet since daemon start.

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
    → daemon: routes_stream.tick → async generator
       ├─ subscribe_quote(codes, callback=queue.put_nowait)
       ├─ loop: yield "data: {...}\n\n" from queue
       └─ finally: unsubscribe_quote(seq_ids)
CLI  → iterate lines → json.loads → format single row → flush
Ctrl+C
CLI  → closes HTTP connection → generator cancelled → finally runs
```

**Backpressure policy:** the per-connection queue is bounded. The daemon applies different overflow policies by stream type:

- **`stream tick`** — *drop oldest*. Tick streams are high-volume and losing an individual tick is acceptable. Dropped count is included in the next event's metadata.
- **`stream kline`** — *coalesce by bar*. Each event is keyed by `(code, bar_start_ts)`; newer events for the same bar overwrite older ones in the queue. A partially-updated bar is always eventually consistent with the final closed bar.

Both policies emit a WARN log line when they kick in so the operator can tell when the CLI consumer is too slow.

### 6.3 Order placement

Example: `miniqmt-cli order buy --account sim --code 000001.SZ --volume 100 --price 12.34`

Three layers of defense, **all enforced independently at both CLI and daemon**. The CLI is treated as an untrusted client — every guard that exists in the CLI must also exist in the daemon, because an attacker who can reach the daemon's port can bypass the CLI entirely (`curl -X POST /trade/order`).

**CLI layer**

1. `--account` is required, no default.
2. CLI fetches account metadata from the daemon (`GET /trade/account/meta?name=<name>`). If the daemon reports `requires_confirm_live = true`, `--confirm-live <last4>` must also be present with a 4-digit string; otherwise the CLI exits without sending the order. The CLI does **not** verify the last-4 value itself — it just forwards it, and the daemon is the authority.
3. CLI calls `GET /trade/preview?...` to fetch last price, estimated cost, and account buying power.
4. CLI generates a `client_req_id` (UUIDv4) for this attempt and remembers it; on retry after a network error the CLI **reuses** the same id.
5. CLI prints a confirmation table:
   ```
   Account:   sim (1230001)
   Code:      000001.SZ  平安银行
   Side:      BUY
   Volume:    100
   Price:     12.34  (limit)
   Est.Cost:  1234.00 + fee ~0.61
   ───────────────────────────────
   Type "yes" to confirm:
   ```
6. On `--dry-run`, the confirmation table is printed and the flow ends (no POST sent).
7. `--yes` skips the interactive prompt but all other checks still run.
8. CLI sends `POST /trade/order` with body `{account, code, side, volume, price, type, client_req_id, confirm_live_last4}` where `confirm_live_last4` is either `null` (no `--confirm-live`) or the 4-digit string passed on the CLI.

**Daemon layer**

1. **Whitelist**: reject if `account` is not in `[accounts.*]`. HTTP 400, no side effects.
2. **Live gate** (independent of the CLI check): look up the account's `requires_confirm_live`. If true:
   - `confirm_live_last4` must be present in the request body.
   - It must equal the last 4 characters of the account's `account_id` (string compare after stripping leading zeros is **not** performed — exact last-4-char match).
   - Otherwise return HTTP 400 with `detail = "live account requires confirm_live_last4 matching last 4 digits of account_id"`. No audit row, no trader call.
3. **Idempotency**: look up `client_req_id` in the TTL cache. If present, return the cached response immediately without re-calling `order_stock`. TTL default: 5 minutes.
4. `audit.append(phase="pre", ...)` — write to `orders.jsonl` **before** calling xttrader (`fsync` on the line).
5. `xttrader_adapter.order_stock(...)`.
6. `audit.append(phase="post", result=..., order_id=..., seq=...)`.
7. Store result in the idempotency cache keyed by `client_req_id`.
8. Return the order id and broker status.

**Audit layer**

`~/.miniqmt_cli/orders.jsonl`, append-only, one JSON object per line. Timestamps are UTC ISO 8601 with trailing `Z`, produced by `datetime.now(timezone.utc).isoformat()` — never `datetime.now()` (which depends on the machine's local tz):

```json
{"ts":"2026-04-14T02:23:41.284Z","phase":"pre","client_req_id":"5f2a...",
 "account":"sim","account_id":"1230001","code":"000001.SZ",
 "side":"buy","volume":100,"price":12.34,"type":"limit","confirm_live_last4":null}
{"ts":"2026-04-14T02:23:41.319Z","phase":"post","client_req_id":"5f2a...",
 "order_id":"12345","seq":7,"status":"ok"}
```

The `pre` record is `fsync`-ed before the xttrader call, so even a daemon crash mid-call leaves a trail. When `audit.log_path` exceeds 100 MB the daemon logs a WARN on every append until the file is rotated manually (automatic rotation is post-v1 per §10).

### 6.4 Cancel

Same flow as order placement. The confirmation table shows `order_id`, `code`, and unfilled volume, fetched from `query_stock_orders` in the preview step. Audit log covers both phases.

### 6.5 Error mapping

| Source                                                             | CLI behavior                                                     | Exit |
|--------------------------------------------------------------------|------------------------------------------------------------------|------|
| Network / connection failure                                       | `ClickException("cannot reach daemon at <url>")`                 | 1    |
| Daemon 5xx                                                         | Print generic "daemon error", log body at debug level            | 1    |
| Daemon 4xx with `detail` (generic)                                 | Print `detail` in red                                            | 1    |
| xttrader login failed                                              | Suggest checking `qmt_path` / `session_id` / `account_id`        | 1    |
| Safety guard (whitelist, live-gate, `--dry-run`, confirm declined) | Print which guard triggered, no audit row                        | 3    |
| Broker rejected order (seq ≤ 0)                                    | Print broker return code + description in red                    | 2    |

Exit code `3` is reserved for "stopped by a safety guard" so shell scripts can distinguish `rm /tmp/flag-file && order buy ...` from real failures, and can retry on `1` (transient) without risking duplicate trades on `2` (broker rejected — already counted against rate limits).

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
host = "127.0.0.1"                    # see "Binding" note below — DO NOT change casually
port = 8765

# qmt_path MUST point at the miniQMT client install directory.
# The daemon loads xtquant from <qmt_path>/bin.x64/Lib/site-packages by
# injecting that directory into sys.path before the first xtquant import.
# This is how xtquant ships — there is no pip package.
qmt_path = "C:/国金QMT交易端/userdata_mini"

# session_id is xttrader's per-process unique session number. Any int will
# do as long as two daemon processes on the same machine do not collide.
# The default implementation uses os.getpid() if this field is omitted.
session_id = 123456

[accounts.sim]
account_id = "1230001"
account_type = "STOCK"                # STOCK | CREDIT | FUTURE

[accounts.live]
account_id = "1230002"
account_type = "STOCK"
requires_confirm_live = true

[audit]
log_path = "~/.miniqmt_cli/orders.jsonl"
```

Read only by `commands/server.py` (the `serve` command) and code under `server/`. Never read by client code paths. Resolution order mirrors the client config with `MINIQMT_CLI_SERVER_*` env vars, plus the `--server-config <path>` flag on `serve`.

**Binding / exposure**

The default `host = "127.0.0.1"` is intentional. The daemon serves **plain HTTP with no authentication** and exposes the full trading surface — any client that can reach the port can place orders. Changing `host` to `0.0.0.0` or a LAN-reachable interface removes the last safety net (§2 lists daemon auth as post-v1).

**Recommended recipe for macOS → Windows (v1):**

```
# On the Mac, forward local port 8765 to the Windows daemon over SSH.
ssh -N -L 8765:127.0.0.1:8765 user@windows-host

# Keep server.toml on Windows at host = 127.0.0.1.
# Keep client.toml on Mac at server_url = "http://127.0.0.1:8765".
```

With this setup the daemon is never exposed outside the Windows host, and the Mac CLI transparently reaches it through the SSH tunnel. If the operator chooses instead to expose the daemon directly, the spec assumes they have their own network-level controls (firewall, mTLS proxy, etc.) — the daemon itself offers none.

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

- **Audit integrity**: `pre` record is on disk (and `fsync`-ed) before the xttrader call; `post` record (with `status=error`) is written even when xttrader raises.
- **Whitelist enforcement (CLI)**: an account name not in `[accounts.*]` causes the CLI to exit 3 with no HTTP request.
- **Whitelist enforcement (daemon, bypass-CLI)**: a direct `POST /trade/order` with an unknown account returns 400, with zero `order_stock` invocations on the fake and zero audit rows.
- **Live-account gate (CLI, no flag)**: `miniqmt-cli order buy --account live ...` without `--confirm-live` exits 3 before any HTTP request.
- **Live-account gate (daemon, no field)**: a direct `POST /trade/order` for a `requires_confirm_live = true` account without `confirm_live_last4` returns 400.
- **Live-account gate (daemon, wrong last4)**: `confirm_live_last4 = "9999"` against an account_id ending in `1234` returns 400, no `order_stock` invocations, no audit rows.
- **Live-account gate (daemon, correct last4)**: `confirm_live_last4 = "1234"` against the same account proceeds through the full order flow.
- **Dry-run**: `--dry-run` prints the confirmation table, makes no `POST /trade/order` call, writes no audit row, exits 3.
- **`--yes`**: skips the interactive prompt, still runs all other guards, still writes audit rows, still enforces live gate.
- **Idempotency**: sending the same `client_req_id` twice returns the same `order_id` both times; the fake `order_stock` is called **exactly once**.
- **Idempotency TTL**: after the cache TTL, reusing a `client_req_id` does call `order_stock` again (documents the boundary condition).
- **SSE cleanup (normal close)**: after the CLI disconnects, the daemon's fake `xtdata.unsubscribe_quote` is called exactly once per active `seq`. The `finally` branch of the async generator is covered.
- **SSE cleanup (generator cancelled)**: same assertion when the generator is cancelled from outside, not just when the client closes.
- **Stream backpressure — tick**: when the consumer is slow, the tick queue drops the oldest events and surfaces a drop count in the next event metadata.
- **Stream backpressure — kline**: when the consumer is slow, successive updates for the same `(code, bar_start_ts)` coalesce in the queue; no duplicate intermediate bars are delivered.
- **Config separation**: loading `client.toml` does not read `server.toml`, and vice versa. Missing `server.toml` does not break client commands.
- **Lazy xtquant import**: importing `miniqmt_cli.main` on a machine without xtquant installed does not raise. Only `miniqmt-cli serve` triggers the import.
- **sys.path injection**: `server/xtdata_adapter.py` injects `<qmt_path>/bin.x64/Lib/site-packages` into `sys.path` before importing xtquant; test asserts the path is present after the first adapter call (using a fake qmt_path layout).
- **Per-account login lock**: two concurrent requests for the same uninitialized account trigger exactly one `create_trader` + `login` pair on the fake.
- **Exit code map**: assert each documented source in §6.5 produces its documented exit code.

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

[project.scripts]
miniqmt-cli = "miniqmt_cli.main:cli"

[tool.setuptools.packages.find]
where = ["src"]
```

**There is no `xtquant` pip dependency.** xtquant is not distributed on PyPI; it ships as a directory inside the miniQMT client install at `<qmt_path>/bin.x64/Lib/site-packages/xtquant/`. The daemon loads it via runtime `sys.path` injection:

```python
# server/xtquant_loader.py (called once by session.py on startup)
import sys, os
from miniqmt_cli.server_config import load_server_config

def load_xtquant():
    cfg = load_server_config()
    site_packages = os.path.join(cfg.qmt_path, "bin.x64", "Lib", "site-packages")
    if not os.path.isdir(site_packages):
        raise RuntimeError(
            f"xtquant not found under {site_packages!r}. "
            f"Check [server].qmt_path in server.toml — it must point at the "
            f"miniQMT client install directory."
        )
    if site_packages not in sys.path:
        sys.path.insert(0, site_packages)
    import xtquant.xtdata       # noqa: F401
    import xtquant.xttrader     # noqa: F401
```

`xtdata_adapter.py` and `xttrader_adapter.py` call `load_xtquant()` once at their first use (guarded by a module-level flag), then `from xtquant import xtdata, xttrader` as normal. This keeps the spec's "lazy import" promise (§4 Key boundaries) intact: on macOS the loader is never called, so `xtquant` is never touched.

Install matrix:

- **Windows**: `pip install -e tools/miniqmt_cli` + a working miniQMT client install + `[server].qmt_path` pointing at it.
- **macOS (client-only)**: `pip install -e tools/miniqmt_cli`. No `qmt_path` needed. Attempting `miniqmt-cli serve` fails fast with the error message from `load_xtquant()` above.

## 10. Open questions

None blocking v1. Items intentionally deferred to post-v1:

- Daemon-level auth token (currently relies on network-layer trust, see §7.2 binding note)
- Condition/algo orders
- Multi-account aggregation views
- Reconnect/retry policy on xttrader session drops (v1 simply re-logs-in on next request)
- Automatic audit log rotation (v1 keeps a single append-only file; v1 does emit a WARN when the file exceeds 100 MB, per §6.3)
<!-- confirm-live last4 now in v1 (§6.3) -->

## 11. Scope decisions (agreed in brainstorming)

These are the decisions locked in during brainstorming. Changes to any of them require revisiting the design.

- [x] Scope = data + account + trading (C), with Mac remote usage supported
- [x] Architecture = HTTP daemon (B), `server_url` configurable, network setup out-of-scope
- [x] Trading safety = whitelist + interactive confirm + dry-run + audit log (C), enforced **on both CLI and daemon** independently
- [x] Streaming = snapshot + SSE-based `stream tick` / `stream kline` (B), cleanup via async generator `finally`
- [x] v1 command surface as listed in §5
- [x] Client and server configs separated into two files
- [x] xtquant loaded via `sys.path` injection from `qmt_path`, not via pip
- [x] Order requests carry a `client_req_id` for idempotency
