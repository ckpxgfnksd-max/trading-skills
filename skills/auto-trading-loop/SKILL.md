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
| Never trade when `/health` state == `risk_breaker_tripped` | Risk limit already hit | Only a human can `risk reset` |
| Never trade outside regular hours (09:30–11:30, 13:00–15:00 CST, weekdays, non-holidays) | No liquidity, orders will reject or queue overnight | Agent computes market hours locally before every order |
| Every `POST /trade/order` carries a fresh `client_req_id` (UUID v4) | Idempotency + crash recovery | Agent generates it; retrying on timeout reuses the same id |
| Use the `sim` account for all development and first N live sessions | Real money needs real verification | Hard policy — encoded into strategy config |

## Pre-Trade Checklist (run before each decision)

Every decision cycle starts with:

1. `GET /health` → assert `state == "ready"`
2. Confirm local clock is inside a regular A-share session
3. `GET /risk/status?account=<name>` → assert `breaker_tripped == false` and no pending overload (e.g. `orders_in_window < 0.8 * max_orders_per_minute`)
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

Agents must handle the actual wire format from `miniqmt-http-api`:
- First line is an envelope `{"event": "subscribed", ...}` — skip it.
- Subsequent messages are `{"type": "order_status", ...}`, `{"type": "order_response", ...}`, or `{"type": "trade", ...}`. Match on `type`.
- Terminal `order_status` values: `filled`, `cancelled`, `rejected`, `expired`.
- In-flight values: `submitted`, `confirmed`, `partially_filled`, `pending_cancel`.
- Defensive: any `status` starting with `unknown` means fall back to polling `/trade/orders`.

```python
# Pseudocode
order_result = post_order(req_id=req_id, ...)
expected_order_id = order_result["order_id"]

for event in stream_orders(account):
    if event.get("type") != "order_status":
        continue
    if event["order_id"] != expected_order_id:
        continue
    if event["status"] in ("filled", "cancelled", "rejected", "expired"):
        return event
    if event["status"] == "partially_filled":
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
| Risk breaker trips mid-session | `/health` returns state `risk_breaker_tripped`, or `POST /trade/order` returns HTTP 400 with `{"error": "risk_reject", "code": "breaker_tripped", ...}` | **Do not auto-reset.** Halt and surface to human |
| Order times out (no terminal event in N seconds) | agent-side timer | `POST /trade/cancel`; on success, treat as `cancelled`; on failure, reconcile via `/trade/orders` |
| Partial fill + end-of-session | close of trading hours with `status=partially_filled` | Cancel remainder; log unfilled volume to agent state |
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
