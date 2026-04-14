"""Adapter wrapping xtquant.xttrader. Keeps a per-account XtQuantTrader instance."""
from __future__ import annotations

from typing import Any, Dict, List

from miniqmt_cli.server.xtquant_loader import load_xtquant
from miniqmt_cli.server_config import AccountConfig


def _xttrader_module():
    import xtquant.xttrader as xttrader  # type: ignore
    return xttrader


def _xttype_module():
    import xtquant.xttype as xttype  # type: ignore
    return xttype


def create_trader(session_id: int, qmt_userdata_path: str):
    xttrader = _xttrader_module()
    trader = xttrader.XtQuantTrader(qmt_userdata_path, session_id)
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
