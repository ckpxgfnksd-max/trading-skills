# Phase 1: Order Lifecycle -- Design Spec

**Date**: 2026-04-17
**Status**: Approved
**Package**: `tools/miniqmt_cli/` (daemon + CLI)

---

## Goal

Close the order feedback loop: after placing an order through miniqmt-cli,
receive real-time status updates (accepted, partially filled, filled, cancelled,
rejected) via SSE push, so strategies can react to execution events.

---

## Architecture

```
xtquant callback thread                    asyncio event loop
┌────────────────────┐                    ┌──────────────────────┐
│ XtQuantTrader      │                    │ SessionManager       │
│   on_order_event   │──dispatch_order──> │   _order_subscribers │
│   on_trade_event   │   event()          │     [queue1]         │
│   on_order_stock_  │  (threadsafe)      │     [queue2]         │
│   async_response   │                    │     [queue3]         │
└────────────────────┘                    └──────┬───────────────┘
                                                 │ fan-out
                                    ┌────────────┼────────────┐
                                    v            v            v
                              SSE client A  SSE client B  --wait consumer
                              (stream order) (stream order) (temporary)
```

Events flow from xtquant's callback thread into an asyncio.Queue per subscriber
via `loop.call_soon_threadsafe(q.put_nowait, event)`. Each SSE connection or
`--wait` call registers its own queue and unregisters on disconnect.

---

## Event Types

Three event types, all forwarded as JSON via SSE:

### order_response (下单回执)

Fired when the broker acknowledges the order submission.

```json
{
  "type": "order_response",
  "account": "sim",
  "seq": 12345,
  "code": "002028.SZ",
  "side": "buy",
  "volume": 100,
  "price": 210.0
}
```

### order_status (订单状态变化)

Fired when order status changes: submitted, partially_filled, filled,
cancelled, rejected.

```json
{
  "type": "order_status",
  "account": "sim",
  "order_id": 12345,
  "status": "filled",
  "code": "002028.SZ",
  "side": "buy",
  "volume": 100,
  "filled_volume": 100,
  "avg_price": 210.5,
  "frozen": 0.0
}
```

Status values (mapped from xtquant constants):
- `submitted` -- order accepted by exchange
- `partially_filled` -- some shares filled
- `filled` -- all shares filled (terminal)
- `cancelled` -- user cancelled (terminal)
- `rejected` -- exchange/broker rejected (terminal)

Terminal states: `filled`, `cancelled`, `rejected`.

### trade (逐笔成交)

Fired for each individual fill.

```json
{
  "type": "trade",
  "account": "sim",
  "order_id": 12345,
  "trade_id": 67890,
  "code": "002028.SZ",
  "side": "buy",
  "price": 210.5,
  "volume": 50,
  "amount": 1052500.0
}
```

---

## Server-Side Changes

### xttrader_adapter.py

New `TraderCallback` class implementing `XtQuantTraderCallback`:

```python
class TraderCallback(XtQuantTraderCallback):
    def __init__(self, dispatcher, account_name):
        self._dispatch = dispatcher
        self._account = account_name

    def on_order_stock_async_response(self, response):
        self._dispatch({
            "type": "order_response",
            "account": self._account,
            # extract fields from response object
        })

    def on_order_event(self, order):
        self._dispatch({
            "type": "order_status",
            "account": self._account,
            # extract fields from order object, map status constants
        })

    def on_trade_event(self, trade):
        self._dispatch({
            "type": "trade",
            "account": self._account,
            # extract fields from trade object
        })
```

`create_trader` updated to accept a `dispatcher` callable and `account_name`,
register the callback via `trader.register_callback(callback)`.

### session.py

New subscriber management on `SessionManager`:

```python
_order_subscribers: list[asyncio.Queue]
_sub_lock: asyncio.Lock

def dispatch_order_event(self, event: dict) -> None:
    """Called from xtquant callback thread. Fan-out to all subscribers."""
    for q in self._order_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop if consumer too slow

async def subscribe_orders(self) -> asyncio.Queue:
    q = asyncio.Queue(maxsize=256)
    async with self._sub_lock:
        self._order_subscribers.append(q)
    return q

async def unsubscribe_orders(self, q: asyncio.Queue) -> None:
    async with self._sub_lock:
        self._order_subscribers.remove(q)
```

`get_trader` passes `self.dispatch_order_event` as the dispatcher to
`create_trader`.

Note: `dispatch_order_event` is called from xtquant's thread. `put_nowait`
on asyncio.Queue is thread-safe. No `call_soon_threadsafe` wrapper needed
because `put_nowait` doesn't interact with the event loop.

### routes_stream.py

New SSE endpoint:

```
GET /stream/order[?account=<name>]
```

- Subscribes to `session.subscribe_orders()`
- Yields SSE events as `data: {json}\n\n`
- Optional `account` query param filters events
- Unsubscribes on client disconnect (finally block)

---

## CLI-Side Changes

### commands/stream.py

New `stream order` command:

```bash
miniqmt-cli stream order [--account <name>]
```

Consumes `/stream/order` SSE, prints one event per line using existing
`format_row` output.

### commands/order.py

New `--wait <seconds>` option on `buy` and `sell`:

```bash
miniqmt-cli order buy --account sim --code 002028.SZ \
  --volume 100 --price 210 --yes --wait 10
```

Flow:
1. POST /trade/order (existing)
2. If `--wait` specified, open SSE stream to `/stream/order?account=<name>`
3. Filter events matching the returned `seq`
4. Wait until a terminal status (filled/cancelled/rejected) or timeout
5. Print final status and exit

Without `--wait`, behavior is unchanged (backward compatible).

### account orders output

No code change needed. The existing `_to_dict` in xttrader_adapter already
serializes all public attributes from xtquant's order objects, which include
`order_status`, `traded_volume`, `traded_price`. Verify field names are present
during implementation; add a field mapping only if names are not user-friendly.

---

## File Change Summary

| File | Change |
|------|--------|
| `server/xttrader_adapter.py` | Add `TraderCallback`, update `create_trader` signature |
| `server/session.py` | Add subscriber management, pass dispatcher to create_trader |
| `server/routes_stream.py` | Add `/stream/order` SSE endpoint |
| `commands/stream.py` | Add `stream order` CLI command |
| `commands/order.py` | Add `--wait` option to buy/sell |

---

## Testing

### Unit tests

- **xttrader_adapter**: Mock `XtQuantTraderCallback`, verify `TraderCallback`
  correctly extracts fields and calls dispatcher for each event type.
- **session.py**: Test subscribe/unsubscribe lifecycle, fan-out to multiple
  queues, queue-full drop behavior, thread-safety of dispatch.

### Integration tests

- **routes_stream /stream/order**: Mock session, verify SSE event format and
  account filtering.
- **order --wait**: Mock transport.stream(), verify wait-for-terminal-status
  and timeout logic.

### Manual verification

- Place an order during market hours via `miniqmt-cli order buy --wait 10`
- Simultaneously run `miniqmt-cli stream order --account sim` in another terminal
- Verify both see the same events in real-time

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| xtquant callback throws | Log warning, skip event, don't crash daemon |
| Subscriber queue full (256) | Drop event silently for that subscriber |
| SSE client disconnects | Unsubscribe queue in finally block |
| --wait timeout | Print "timeout waiting for order status" and exit with code 1 |
| No trader logged in for stream | Return empty SSE stream (no events until trader connects) |
