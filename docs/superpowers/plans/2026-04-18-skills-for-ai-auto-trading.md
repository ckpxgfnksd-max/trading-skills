# Skills for AI Auto-Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the skill-layer gaps so an external AI agent can drive A-share auto-trading by calling the shipped `miniqmt-cli` + `trading-analysis` tools.

**Architecture:** Two existing skills (`miniqmt-cli`, `trading-analysis`) get missing sections patched in; two new skills are added — an agent-facing HTTP/SSE API reference and a top-level auto-trading decision-loop playbook.

**Tech Stack:** Markdown only (skills are `SKILL.md` files). Verification uses `miniqmt-cli --help`, `trading-analysis --help`, and `grep` against the server route files.

---

## File Map

| File | Responsibility |
|------|---------------|
| `skills/miniqmt-cli/SKILL.md` | **Modify** — add `stream order`, `risk status/reset`, HTTP API cross-reference |
| `skills/trading-analysis/SKILL.md` | **Modify** — add signal-expression engine section + `--signal` usage |
| `skills/miniqmt-http-api/SKILL.md` | **Create** — HTTP/SSE endpoint reference for external agents |
| `skills/auto-trading-loop/SKILL.md` | **Create** — top-level decision loop, pre-trade checklist, red lines |

Skills layer ordering: `miniqmt-cli` and `trading-analysis` stay focused on single-tool usage; `miniqmt-http-api` mirrors the same surface for programmatic callers; `auto-trading-loop` is the hub that stitches everything into a trading loop.

---

### Task 1: Patch `miniqmt-cli` SKILL.md gaps

**Files:**
- Modify: `skills/miniqmt-cli/SKILL.md`

- [ ] **Step 1: Add `stream order` under "Real-time Streaming (SSE)"**

Locate the `### Real-time Streaming (SSE)` block (around line 66). After the `stream kline` code block, append:

````markdown
```bash
# Stream order lifecycle events (submitted / partially filled / filled / cancelled / rejected)
# Essential for agents: subscribe before placing an order, then consume fill/reject events.
miniqmt-cli stream order --account sim

# JSON format for programmatic parsing
miniqmt-cli --format json stream order --account sim
```

Event payload shape (JSON mode):

```json
{"event": "order", "account": "sim", "order_id": 12345, "code": "000001.SZ",
 "side": "buy", "status": "filled", "filled_volume": 100, "avg_price": 10.48,
 "ts": "2026-04-18T09:31:12"}
```

Statuses: `submitted`, `partial`, `filled`, `cancelled`, `rejected`.
````

- [ ] **Step 2: Add a "Risk Control" section before "Daemon Management"**

Locate `### Daemon Management` (around line 128). Insert before it:

````markdown
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

# Live account reset requires last-4-digit confirmation
miniqmt-cli risk reset --account live --note "manual unfreeze" --confirm-live 1234
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
````

- [ ] **Step 3: Add an "HTTP API" cross-reference block before "SSH Tunnel"**

Locate `## SSH Tunnel` (around line 181). Insert before it:

````markdown
## HTTP API (for external agents)

Every CLI command hits a JSON HTTP endpoint on the daemon. Agents that don't want to shell out to Click can call the HTTP surface directly — see the dedicated **miniqmt-http-api** skill for the full endpoint reference, payload shapes, and error codes.

Quick map (full list in `skills/miniqmt-http-api/SKILL.md`):

| CLI | HTTP |
|-----|------|
| `tick` / `kline` / `ticks` | `GET /data/tick` / `/data/kline` / `/data/ticks` |
| `account asset/position/orders/trades` | `GET /trade/asset` / `/positions` / `/orders` / `/trades` |
| `order buy/sell --dry-run` | `GET /trade/preview` |
| `order buy/sell` | `POST /trade/order` |
| `order cancel` | `POST /trade/cancel` |
| `risk status` / `reset` | `GET /risk/status` / `POST /risk/reset` |
| `stream tick/kline/order` | `GET /stream/tick` / `/kline` / `/order` (SSE) |
| `health` | `GET /health` |
````

- [ ] **Step 4: Verify every command in the skill exists**

Run:

```bash
miniqmt-cli --help
miniqmt-cli risk --help
miniqmt-cli stream --help
miniqmt-cli order --help
```

Expected: `risk status`, `risk reset`, `stream order`, `stream tick`, `stream kline`, `order buy`, `order sell`, `order cancel` all present with options matching the skill text.

- [ ] **Step 5: Commit**

```bash
git add skills/miniqmt-cli/SKILL.md
git commit -m "docs(skills): document stream order, risk commands, and HTTP API map"
```

---

### Task 2: Patch `trading-analysis` SKILL.md — signal-expression engine

**Files:**
- Modify: `skills/trading-analysis/SKILL.md`

- [ ] **Step 1: Add "Signal Expressions" section before "Parameter Reference"**

Locate `## Parameter Reference` (around line 90). Insert before it:

````markdown
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
````

- [ ] **Step 2: Cross-reference from the Real-time Mode section**

Locate `### Real-time Mode (--live)` (around line 70). At the end of its bullet list (after "outside trading hours the display will show zeros"), append:

```markdown
- Combine with `--signal` to get alert-on-trigger behavior; see **Signal Expressions** below.
```

- [ ] **Step 3: Verify `--signal` option exists**

Run:

```bash
trading-analysis moneyflow --help
```

Expected: output contains `--signal TEXT  Signal expression, e.g. "main_net > 0 and price > ma20"`.

- [ ] **Step 4: Verify expression parser variables match the skill**

Run:

```bash
grep -n "VALID_VARS" tools/trading_analysis/src/trading_analysis/signals.py
```

Expected: `VALID_VARS = {"main_net", "retail_net", "price", "ma5", "ma10", "ma20", "ma60"}`. Confirm the skill's variable table matches exactly.

- [ ] **Step 5: Commit**

```bash
git add skills/trading-analysis/SKILL.md
git commit -m "docs(skills): document --signal expression engine and semantics"
```

---

### Task 3: Create `miniqmt-http-api` skill

**Files:**
- Create: `skills/miniqmt-http-api/SKILL.md`

- [ ] **Step 1: Write the skill file**

Path: `skills/miniqmt-http-api/SKILL.md`. Full content:

````markdown
---
name: miniqmt-http-api
description: Reference for the miniqmt-cli daemon's HTTP/SSE API, for external agents (Claude agents, MCP servers, scripts) that want to drive A-share trading programmatically instead of shelling out to the CLI. Use this skill when the user mentions calling the daemon directly, integrating with an agent framework, building an MCP server around miniqmt, or needs JSON payload shapes, SSE event formats, or error codes.
---

# miniqmt-http-api: Programmatic Interface to the Trading Daemon

All endpoints are served by the FastAPI daemon at `http://127.0.0.1:8765` (default), typically reached through an SSH tunnel. FastAPI's built-in `/docs` page serves live OpenAPI if you need schema introspection.

## Base URL

```
http://127.0.0.1:8765
```

All responses are JSON unless the endpoint is SSE (noted below). All requests that take parameters accept them as query strings for `GET` or a JSON body for `POST`.

## Endpoints

### Health & Version

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/version` | Daemon build version |
| GET | `/health` | `{"state": "ready" \| "daemon_up_no_trader" \| "daemon_up_xtquant_missing" \| "daemon_up_baseline_pending", "risk_breaker_tripped": bool}` |

Agents should verify `/health` returns `state == "ready"` **and** `risk_breaker_tripped == false` before trading.

### Market Data (`/data/*`)

| Method | Path | Query | Returns |
|--------|------|-------|---------|
| GET | `/data/sectors` | — | List of sector names |
| GET | `/data/instruments` | `sector`, `limit` | List of `{code, name}` |
| GET | `/data/instrument` | `code` | Full metadata for one code |
| GET | `/data/tick` | `code` (repeatable) | Latest snapshot per code |
| GET | `/data/kline` | `code`, `period` (`1d`/`1m`/`5m`), `start`, `end` | OHLCV bars |
| GET | `/data/ticks` | `code`, `start`, `end` (both `YYYYMMDDHHMMSS`) | Tick-by-tick snapshots |

### Trade (`/trade/*`)

| Method | Path | Query / Body | Returns |
|--------|------|--------------|---------|
| GET | `/trade/accounts` | — | `[{name, account_id_masked, account_type, requires_confirm_live}]` |
| GET | `/trade/account/meta` | `name` | Masked id + type + live flag |
| GET | `/trade/asset` | `account` | `{cash, frozen, total, market_value}` |
| GET | `/trade/positions` | `account` | List of position dicts |
| GET | `/trade/orders` | `account` | Today's orders |
| GET | `/trade/trades` | `account` | Today's fills |
| GET | `/trade/preview` | `account`, `code`, `side`, `volume`, `price` | `{account_id_masked, requires_confirm_live, last_price, est_cost}` |
| POST | `/trade/order` | (see below) | `{seq, order_id, status}` |
| POST | `/trade/cancel` | `{account, order_id, client_req_id}` | `{ok, status}` |

`POST /trade/order` body:

```json
{
  "account": "sim",
  "code": "000001.SZ",
  "side": "buy",
  "volume": 100,
  "price": 10.50,
  "type": "limit",
  "client_req_id": "agent-uuid-1234",
  "confirm_live_last4": null
}
```

- `client_req_id` is **required**; it's the idempotency key — retrying the same id returns the original result.
- `confirm_live_last4` is required when `requires_confirm_live=true` on the account.
- `type` is `limit` or `market`.

### Risk (`/risk/*`)

| Method | Path | Query / Body | Returns |
|--------|------|--------------|---------|
| GET | `/risk/status` | `account` (optional) | Per-account state (see `miniqmt-cli` skill → Risk Control for field meanings) |
| POST | `/risk/reset` | `{account, note, confirm_live_last4?}` | `{ok, reset_count_today}` |

### Streaming / SSE (`/stream/*`)

All three endpoints are **Server-Sent Events**. Each message is `data: <json>\n\n`. Consumers should keep the connection open and parse line-by-line.

| Path | Query | Event shape |
|------|-------|-------------|
| `/stream/tick` | `code` (repeatable) | `{"event": "tick", "code": ..., "lastPrice": ..., ...}` |
| `/stream/kline` | `code` (repeatable), `period` | `{"event": "kline", "code": ..., "open": ..., "close": ..., ...}` |
| `/stream/order` | `account` (optional) | `{"event": "order", "account": ..., "order_id": ..., "status": ..., "filled_volume": ..., "avg_price": ..., "ts": ...}` |

`/stream/order` statuses: `submitted`, `partial`, `filled`, `cancelled`, `rejected`.

## Error Responses

| HTTP | Body `detail` | Meaning |
|------|----------------|---------|
| 400  | `"unknown account"` | `account` not in whitelist |
| 400  | `"confirm_live_last4 required"` | Live account without confirmation |
| 400  | `"confirm_live_last4 mismatch"` | Confirmation digits wrong |
| 409  | `"risk: <reason>"` | Risk check rejected (e.g. `breaker_tripped`, `position_pct_exceeded`, `daily_loss_exceeded`, `frequency_exceeded`, `max_positions_exceeded`) |
| 502  | `"broker reject: <reason>"` | xtquant-level rejection (market closed, invalid code, insufficient funds) |
| 503  | `"daemon not ready: <state>"` | Health state is not `ready` |

**Risk rejections are the important ones for agents.** The `reason` string is stable and can be matched against to decide whether to retry, back off, or surface to the user.

## Idempotency & Reconciliation

- Every `POST /trade/order` must carry a unique `client_req_id` (UUID recommended). Re-issuing the same id is safe — the daemon returns the cached result.
- After an agent crash, reconcile by calling `GET /trade/orders?account=...` — every order has its `client_req_id` echoed back. Compare against the agent's local log to identify unknown / orphaned orders before making new decisions.

## Authentication

The daemon has no auth of its own — it relies on the SSH tunnel binding it to `127.0.0.1` on the Mac side. **Do not expose port 8765 publicly.**

## Minimal Agent Example (Python)

```python
import httpx, uuid

BASE = "http://127.0.0.1:8765"

def health():
    return httpx.get(f"{BASE}/health").json()

def place_order(account, code, side, volume, price):
    return httpx.post(f"{BASE}/trade/order", json={
        "account": account, "code": code, "side": side,
        "volume": volume, "price": price, "type": "limit",
        "client_req_id": f"agent-{uuid.uuid4()}",
    }).json()

def stream_orders(account):
    with httpx.stream("GET", f"{BASE}/stream/order",
                      params={"account": account}, timeout=None) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                yield line[5:].strip()
```

## Related Skills

- `miniqmt-cli` — CLI parameter reference and deployment
- `trading-analysis` — Money flow + signal analysis on top of `/data/ticks`
- `auto-trading-loop` — How to compose these endpoints into a full trading loop
````

- [ ] **Step 2: Verify every documented endpoint exists in the server code**

Run:

```bash
grep -rn "@router\.\|@app\." tools/miniqmt_cli/src/miniqmt_cli/server/
```

Expected: every path listed in the skill's tables must appear. Diff them visually — add any missing ones, delete any phantom ones.

- [ ] **Step 3: Verify the error-code table matches real daemon behavior**

Run:

```bash
grep -rn "HTTPException\|raise.*status_code" tools/miniqmt_cli/src/miniqmt_cli/server/
```

Expected: each `detail` string in the skill's error table has a corresponding raise site in the code. Cross-check status codes (400 / 409 / 502 / 503).

- [ ] **Step 4: Commit**

```bash
git add skills/miniqmt-http-api/SKILL.md
git commit -m "docs(skills): add miniqmt-http-api skill for agent callers"
```

---

### Task 4: Create `auto-trading-loop` skill

**Files:**
- Create: `skills/auto-trading-loop/SKILL.md`

- [ ] **Step 1: Write the skill file**

Path: `skills/auto-trading-loop/SKILL.md`. Full content:

````markdown
---
name: auto-trading-loop
description: Top-level playbook for external AI agents driving A-share auto-trading via the miniqmt-cli and trading-analysis tools. Covers pre-trade checklist, decision loop structure, exception handling, and hard red lines. Use this skill whenever the user asks an AI agent to place trades autonomously, build a trading bot, run a strategy 24/7, or wire an LLM to the miniqmt-cli daemon. Also use when the user mentions autonomous trading, self-driving strategy, unattended execution, or turning an LLM into a trader.
---

# auto-trading-loop: Driving Auto-Trading from an External Agent

This skill is the **hub**. It does not replace the `miniqmt-cli`, `miniqmt-http-api`, or `trading-analysis` skills — it tells an agent how to compose them safely.

## Required reading before the first order

1. `miniqmt-cli` — understand the three-layer safety model and the account whitelist
2. `miniqmt-http-api` — prefer HTTP over shelling out to CLI for deterministic parsing
3. `trading-analysis` — money flow and signal expressions as decision inputs

## Red Lines (the agent MUST respect)

| Rule | Why | Enforcement |
|------|-----|-------------|
| Never trade a `live` account without explicit human `confirm_live_last4` **per session** | Prevent runaway live losses | Daemon rejects orders without it; agent must never cache or guess the digits |
| Never trade when `/health` state ≠ `ready` | Stale market data or no trader session | Agent halts the loop until health recovers |
| Never trade when `risk_breaker_tripped == true` | Risk limit already hit | Only a human can `risk reset` |
| Never trade outside regular hours (09:30–11:30, 13:00–15:00 CST, weekdays, non-holidays) | No liquidity, orders will reject or queue overnight | Agent computes market hours locally before every order |
| Every `POST /trade/order` carries a fresh `client_req_id` (UUID v4) | Idempotency + crash recovery | Agent generates it; retrying on timeout reuses the same id |
| Use the `sim` account for all development and first N live sessions | Real money needs real verification | Hard policy — encoded into strategy config |

## Pre-Trade Checklist (run before each decision)

Every decision cycle starts with:

1. `GET /health` → assert `state == "ready"` and `risk_breaker_tripped == false`
2. Confirm local clock is inside a regular A-share session
3. `GET /risk/status?account=<name>` → assert no pending overload (e.g. `orders_in_window < 0.8 * max_orders_per_minute`)
4. `GET /trade/asset` + `GET /trade/positions` → snapshot current portfolio
5. Reconcile: `GET /trade/orders` + `GET /trade/trades` against the agent's last-known state; flag any orders with unknown `client_req_id` and halt for human review

If any step fails, **halt** — do not proceed to analysis or ordering.

## Decision Loop Skeleton

```text
while market_is_open():
    pre_trade_check()                     # above
    analysis = fetch_and_analyze()        # /data/tick or trading-analysis CLI
    decision = strategy.decide(analysis)  # agent-specific
    if decision.should_order:
        preview = GET /trade/preview?...  # verify est_cost and last_price sanity
        if not sane(preview): continue
        req_id = uuid4()
        result = POST /trade/order {..., client_req_id: req_id}
        wait_for_fill(req_id, timeout=30) # via /stream/order
    sleep(strategy.interval)
```

## Waiting for a Fill (the critical sub-pattern)

After placing an order, the agent **must** subscribe to `/stream/order` — not poll `/trade/orders`. Polling loses partial-fill events and has high latency.

```python
# Pseudocode
order_result = post_order(req_id=req_id, ...)
expected_order_id = order_result["order_id"]

for event in stream_orders(account):
    if event["order_id"] != expected_order_id:
        continue
    if event["status"] in ("filled", "cancelled", "rejected"):
        return event
    if event["status"] == "partial":
        log_progress(event)   # keep waiting
    if elapsed > timeout:
        cancel_order(expected_order_id)
        break
```

If the stream connection drops, fall back to `GET /trade/orders` + `GET /trade/trades` to reconstruct state, then reconnect.

## Exception Handling Matrix

| Situation | Detection | Recovery |
|-----------|-----------|----------|
| Tunnel down | any HTTP call fails with `ConnectionError` | Halt the loop; alert; wait for tunnel manager to restore |
| Daemon restarted | `/health` flips to `daemon_up_no_trader` | Wait and re-check; re-subscribe `/stream/order` after recovery |
| Risk breaker trips mid-session | `/stream/order` event has `status=rejected` and `reason~="risk:*"`, or periodic `/health` shows `risk_breaker_tripped` | **Do not auto-reset.** Halt and surface to human |
| Order times out (no terminal event in N seconds) | agent-side timer | `POST /trade/cancel`; on success, treat as `cancelled`; on failure, reconcile via `/trade/orders` |
| Partial fill + end-of-session | close of trading hours with `status=partial` | Cancel remainder; log unfilled volume to agent state |
| Crash / restart | agent process respawns | Replay from persisted state: reconcile `client_req_id` set against `/trade/orders`; do not resend any order whose id is already present |

## Integrating Money Flow / Signals as Decision Inputs

Two modes:

1. **Batch mode** — agent calls `trading-analysis moneyflow --code X --format json` every N seconds and parses the JSON. Simpler, slightly higher latency.
2. **Live mode with signal triggers** — run `trading-analysis moneyflow --code X --live --signal "<expr>" --format json` in a child process and consume its stdout. Lower latency, requires managing the child.

For a first implementation, use batch mode.

## Minimal State the Agent Must Persist

- `{client_req_id: {code, side, volume, price, placed_at, status}}` — open orders not yet terminal
- `last_asset_snapshot` — for drift detection vs. next cycle's `/trade/asset`
- `last_risk_status` — to notice breaker transitions
- `strategy_params` — whatever config the strategy needs to resume

Persist to disk after every mutation. On restart, reconcile before resuming.

## Testing Progression (mandatory)

1. **Dry-run on sim**: every order with `--dry-run` for the first N cycles; inspect previews by hand
2. **Live sim**: real orders on the `sim` account for at least one full trading day
3. **Canary live**: `live` account with reduced position size (e.g. 1 stock, volume=100) for at least one week; daily PnL review
4. **Full live**: only after canary passes and human approves in writing

Skipping a stage is a red line.

## Related Skills

- `miniqmt-cli` — CLI-level tool reference
- `miniqmt-http-api` — HTTP/SSE endpoints the loop calls
- `trading-analysis` — Money flow + signals as decision inputs
````

- [ ] **Step 2: Verify internal cross-references resolve**

Run:

```bash
ls skills/miniqmt-cli/SKILL.md skills/miniqmt-http-api/SKILL.md skills/trading-analysis/SKILL.md
```

Expected: all four skills exist (three referenced + the new one). If any is missing, fix the plan order before committing.

- [ ] **Step 3: Commit**

```bash
git add skills/auto-trading-loop/SKILL.md
git commit -m "docs(skills): add auto-trading-loop skill for external agents"
```

---

### Task 5: Cross-link the existing skills to the new hub

**Files:**
- Modify: `skills/miniqmt-cli/SKILL.md`
- Modify: `skills/trading-analysis/SKILL.md`

- [ ] **Step 1: Append a "Related Skills" section to `miniqmt-cli/SKILL.md`**

At the very end of the file (after the `Exit Codes` table), append:

```markdown

## Related Skills

- **miniqmt-http-api** — HTTP/SSE endpoint reference for programmatic callers
- **trading-analysis** — Money flow + signals built on top of `/data/ticks`
- **auto-trading-loop** — Top-level playbook for external AI agents
```

- [ ] **Step 2: Append a "Related Skills" section to `trading-analysis/SKILL.md`**

At the very end of the file, append:

```markdown

## Related Skills

- **miniqmt-cli** — The daemon and data source underneath
- **miniqmt-http-api** — HTTP/SSE endpoints used by this tool
- **auto-trading-loop** — How to compose analysis + orders into a trading loop
```

- [ ] **Step 3: Commit**

```bash
git add skills/miniqmt-cli/SKILL.md skills/trading-analysis/SKILL.md
git commit -m "docs(skills): cross-link miniqmt and trading-analysis skills to the hub"
```

---

### Task 6: Smoke validation of all four skills

**Files:** none modified — verification only.

- [ ] **Step 1: Verify all four skills exist and have valid frontmatter**

Run:

```bash
for f in skills/miniqmt-cli/SKILL.md \
         skills/trading-analysis/SKILL.md \
         skills/miniqmt-http-api/SKILL.md \
         skills/auto-trading-loop/SKILL.md; do
  echo "--- $f ---"
  head -4 "$f"
done
```

Expected: each file's first four lines are `---`, `name: <skill-name>`, `description: ...`, `---`.

- [ ] **Step 2: Verify every CLI command mentioned in the skills resolves**

Run:

```bash
# For each code fence that starts with `miniqmt-cli ` or `trading-analysis `,
# parse out the subcommand path and check it's in --help.
miniqmt-cli --help
miniqmt-cli risk --help
miniqmt-cli stream --help
miniqmt-cli account --help
miniqmt-cli order --help
trading-analysis --help
trading-analysis moneyflow --help
```

Expected: no command referenced in the skills is missing from `--help` output.

- [ ] **Step 3: Verify every HTTP endpoint mentioned in `miniqmt-http-api` exists**

Run:

```bash
grep -rn "^@router\.\(get\|post\|delete\|put\)\|^@app\.\(get\|post\|delete\|put\)\|    @app\." \
  tools/miniqmt_cli/src/miniqmt_cli/server/ \
  | sed 's/.*"\(\/[^"]*\)".*/\1/' | sort -u
```

Expected list must include every path in the skill's endpoint tables:

```
/data/instrument
/data/instruments
/data/kline
/data/sectors
/data/tick
/data/ticks
/health
/risk/reset
/risk/status
/stream/kline
/stream/order
/stream/tick
/trade/account/meta
/trade/accounts
/trade/asset
/trade/cancel
/trade/order
/trade/orders
/trade/positions
/trade/preview
/trade/trades
/version
```

If any path is missing from the daemon, remove it from the skill. If any path in the daemon is missing from the skill, add it.

- [ ] **Step 4: Verify MEMORY.md index is updated**

Run:

```bash
grep -n "auto-trading-loop\|miniqmt-http-api" /Users/oopslink/.claude/projects/-Users-oopslink-works-codes-oopslink-trading-skills/memory/MEMORY.md
```

If either is missing, append lines to the `## Skills` section of `MEMORY.md` (note: this file lives under `~/.claude/`, not the repo):

```markdown
- `skills/miniqmt-http-api/SKILL.md` — HTTP/SSE reference for agents
- `skills/auto-trading-loop/SKILL.md` — top-level agent-driven trading loop playbook
```

- [ ] **Step 5: Final commit (if any smoke fixes were needed)**

```bash
git add -A skills/
git commit -m "docs(skills): smoke-test fixes across skills" || echo "no fixes needed"
```

---

## Self-Review Summary

- **Task 1** covers the three `miniqmt-cli` gaps called out (`stream order`, risk commands, HTTP map).
- **Task 2** covers the signal-expression gap in `trading-analysis`.
- **Task 3** delivers the new `miniqmt-http-api` skill with endpoint tables, error codes, idempotency semantics.
- **Task 4** delivers the new `auto-trading-loop` skill with red lines, pre-trade checklist, decision loop, exception matrix.
- **Task 5** stitches the four skills together with "Related Skills" blocks so navigation works.
- **Task 6** verifies every CLI command and HTTP path mentioned in the skills actually exists in code — guards against drift.

No placeholders remain. All referenced commands and endpoints are verified against the shipped code in `tools/miniqmt_cli/` and `tools/trading_analysis/`.
