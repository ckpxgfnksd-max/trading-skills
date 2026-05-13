"""Adapter wrapping xtquant.xttrader. Keeps a per-account XtQuantTrader instance."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from miniqmt_cli.server.xtquant_loader import load_xtquant
from miniqmt_cli.server_config import AccountConfig

log = logging.getLogger(__name__)


def _xttrader_module():
    import xtquant.xttrader as xttrader  # type: ignore
    return xttrader


def _xttype_module():
    import xtquant.xttype as xttype  # type: ignore
    return xttype


class TraderCallback:
    """Bridge xtquant's callback thread to the daemon's event dispatcher.

    Each method extracts relevant fields from xtquant objects and forwards
    a normalized dict to the dispatcher callable. Both `dispatcher` and
    `on_disconnect` are optional and independent; if `dispatcher` is None,
    order/trade/async-response events are dropped (explicit "I only care
    about disconnect" mode) rather than silently no-op'd via a sentinel.
    """

    def __init__(
        self,
        dispatcher: Optional[Callable[[dict], None]],
        account_name: str,
        on_disconnect: Optional[Callable[[str], None]] = None,
    ):
        self._dispatch = dispatcher
        self._account = account_name
        self._on_disconnect = on_disconnect

    def on_disconnected(self):
        log.warning("xttrader disconnected for account %s", self._account)
        if self._on_disconnect is not None:
            try:
                self._on_disconnect(self._account)
            except Exception:
                log.exception("on_disconnect handler failed for %s", self._account)

    def on_order_stock_async_response(self, response):
        if self._dispatch is None:
            return
        try:
            self._dispatch({
                "type": "order_response",
                "account": self._account,
                "seq": int(getattr(response, "seq", 0)),
                "code": getattr(response, "stock_code", ""),
            })
        except Exception:
            log.exception("on_order_stock_async_response dispatch failed")

    def on_order_event(self, order):
        if self._dispatch is None:
            return
        try:
            self._dispatch({
                "type": "order_status",
                "account": self._account,
                "order_id": int(getattr(order, "order_id", 0)),
                "status": _map_order_status(getattr(order, "order_status", -1)),
                "code": getattr(order, "stock_code", ""),
                "side": _map_direction(getattr(order, "order_type", -1)),
                "volume": int(getattr(order, "order_volume", 0)),
                "filled_volume": int(getattr(order, "traded_volume", 0)),
                "avg_price": float(getattr(order, "traded_price", 0.0)),
                "frozen": float(getattr(order, "frozen", 0.0)),
            })
        except Exception:
            log.exception("on_order_event dispatch failed")

    def on_trade_event(self, trade):
        if self._dispatch is None:
            return
        try:
            self._dispatch({
                "type": "trade",
                "account": self._account,
                "order_id": int(getattr(trade, "order_id", 0)),
                "trade_id": int(getattr(trade, "traded_id", 0)),
                "code": getattr(trade, "stock_code", ""),
                "side": _map_direction(getattr(trade, "order_type", -1)),
                "price": float(getattr(trade, "traded_price", 0.0)),
                "volume": int(getattr(trade, "traded_volume", 0)),
                "amount": float(getattr(trade, "traded_amount", 0.0)),
            })
        except Exception:
            log.exception("on_trade_event dispatch failed")

    def on_account_status(self, status):
        pass  # not needed for Phase 1


def _map_order_status(raw_status: int) -> str:
    """Map xtquant order_status integer to a human-readable string."""
    mapping = {
        48: "unknown",
        49: "submitted",
        50: "confirmed",
        51: "partially_filled",
        52: "cancelled",
        53: "rejected",
        54: "expired",
        55: "pending_cancel",
        56: "filled",
    }
    return mapping.get(raw_status, f"unknown_{raw_status}")


def _map_direction(raw_type: int) -> str:
    """Map xtquant order_type to buy/sell."""
    # xtconstant.STOCK_BUY = 23, STOCK_SELL = 24
    if raw_type == 23:
        return "buy"
    if raw_type == 24:
        return "sell"
    return f"unknown_{raw_type}"


def create_trader(
    session_id: int,
    qmt_userdata_path: str,
    dispatcher: Optional[Callable[[dict], None]] = None,
    account_name: str = "",
    on_disconnect: Optional[Callable[[str], None]] = None,
):
    xttrader = _xttrader_module()
    trader = xttrader.XtQuantTrader(qmt_userdata_path, session_id)
    if dispatcher is not None or on_disconnect is not None:
        callback = TraderCallback(
            dispatcher, account_name, on_disconnect=on_disconnect,
        )
        trader.register_callback(callback)
    trader.start()
    connect_rc = trader.connect()
    if connect_rc != 0:
        raise RuntimeError(
            f"XtQuantTrader.connect failed with rc={connect_rc}; "
            f"check that miniQMT client is running"
        )
    return trader


def subscribe_account(trader, account: AccountConfig):
    xttype = _xttype_module()
    acc = xttype.StockAccount(account.account_id, account.account_type)
    rc = trader.subscribe(acc)
    if rc != 0:
        raise RuntimeError(
            f"trader.subscribe failed for {account.name} rc={rc}; "
            f"check account_id/account_type and that the account is "
            f"authorized on this QMT client"
        )
    return acc


def query_stock_asset(trader, acc) -> Dict[str, Any]:
    raw = trader.query_stock_asset(acc)
    return _to_dict(raw)


def query_stock_positions(trader, acc) -> List[Dict[str, Any]]:
    raw = trader.query_stock_positions(acc) or []
    return [_to_dict(p) for p in raw]


def query_stock_orders(trader, acc) -> List[Dict[str, Any]]:
    raw = trader.query_stock_orders(acc) or []
    return [_to_dict(o) for o in raw]


def query_stock_trades(trader, acc) -> List[Dict[str, Any]]:
    raw = trader.query_stock_trades(acc) or []
    return [_to_dict(t) for t in raw]


def order_stock(
    trader,
    acc,
    code: str,
    side: str,
    volume: int,
    price: float,
    order_type: str = "limit",
) -> Dict[str, Any]:
    xttrader_c = _xttrader_c_module()
    order_type_const, price_type_const = _side_to_consts(side, order_type, xttrader_c)
    seq = trader.order_stock(
        acc, code, order_type_const, int(volume), price_type_const, float(price)
    )
    return {"seq": int(seq)}


def cancel_order_stock(trader, acc, order_id: int) -> Dict[str, Any]:
    seq = trader.cancel_order_stock(acc, int(order_id))
    return {"seq": int(seq)}


def _xttrader_c_module():
    import xtquant.xtconstant as c  # type: ignore
    return c


def _side_to_consts(side: str, order_type: str, c) -> tuple:
    side = side.lower()
    if side == "buy":
        direction = c.STOCK_BUY
    elif side == "sell":
        direction = c.STOCK_SELL
    else:
        raise ValueError(f"unsupported side: {side!r}")
    if order_type == "limit":
        ptype = c.FIX_PRICE
    elif order_type == "market":
        ptype = c.LATEST_PRICE
    else:
        raise ValueError(f"unsupported order_type: {order_type!r}")
    return direction, ptype


def _to_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    out = {}
    for k in dir(obj):
        if k.startswith("_"):
            continue
        v = getattr(obj, k, None)
        if callable(v):
            continue
        out[k] = v
    return out
