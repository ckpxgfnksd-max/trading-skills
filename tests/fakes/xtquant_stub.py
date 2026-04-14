"""Fake xtquant package: xtdata, xttrader, xttype, xtconstant.

Install into sys.modules by `install()` in tests. Use `reset()` to clear
interaction counts between tests.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


class FakeXtData:
    def __init__(self):
        self.subscribed: Dict[int, dict] = {}
        self.unsubscribed_seqs: List[int] = []
        self._next_seq = 1
        self.sectors = ["沪深A股", "上证A股"]
        self.stocks = {
            "沪深A股": ["000001.SZ", "600000.SH"],
            "上证A股": ["600000.SH"],
        }
        self.instrument_details = {
            "000001.SZ": {"InstrumentName": "平安银行", "ExchangeID": "SZSE"},
            "600000.SH": {"InstrumentName": "浦发银行", "ExchangeID": "SSE"},
        }
        self.ticks = {
            "000001.SZ": {"lastPrice": 12.34, "volume": 10000},
            "600000.SH": {"lastPrice": 7.89, "volume": 5000},
        }
        self.market_data: dict = {}

    def get_sector_list(self):
        return list(self.sectors)

    def get_stock_list_in_sector(self, sector):
        return list(self.stocks.get(sector, []))

    def get_instrument_detail(self, code):
        return dict(self.instrument_details.get(code, {}))

    def get_full_tick(self, codes):
        return {c: dict(self.ticks.get(c, {})) for c in codes}

    def get_market_data_ex(self, field_list, stock_list, period, start_time, end_time):
        return dict(self.market_data)

    def subscribe_quote(self, stock_code, period, callback):
        seq = self._next_seq
        self._next_seq += 1
        self.subscribed[seq] = {
            "code": stock_code, "period": period, "callback": callback,
        }
        return seq

    def unsubscribe_quote(self, seq):
        self.unsubscribed_seqs.append(seq)
        self.subscribed.pop(seq, None)

    def push(self, seq, events):
        info = self.subscribed.get(seq)
        if info:
            info["callback"](events)


@dataclass
class FakeStockAccount:
    account_id: str
    account_type: str


class FakeTrader:
    def __init__(self, userdata_path, session_id):
        self.userdata_path = userdata_path
        self.session_id = session_id
        self.started = False
        self.connected = False
        self.subscribed_accounts: List[FakeStockAccount] = []
        self.orders_placed: List[dict] = []
        self.cancels: List[int] = []
        self._next_seq = 100
        self.should_fail_order = False

    def start(self):
        self.started = True

    def connect(self):
        self.connected = True
        return 0

    def subscribe(self, acc):
        self.subscribed_accounts.append(acc)
        return 0

    def order_stock(self, acc, code, direction, volume, price_type, price):
        if self.should_fail_order:
            raise RuntimeError("fake order failure")
        seq = self._next_seq
        self._next_seq += 1
        self.orders_placed.append({
            "seq": seq, "code": code, "direction": direction,
            "volume": volume, "price": price, "price_type": price_type,
            "account": acc.account_id,
        })
        return seq

    def cancel_order_stock(self, acc, order_id):
        self.cancels.append(int(order_id))
        return 0

    def query_stock_asset(self, acc):
        return {"cash": 100000.0, "total_asset": 200000.0, "account_id": acc.account_id}

    def query_stock_positions(self, acc):
        return [
            {"code": "000001.SZ", "volume": 100, "avg_price": 12.0, "account": acc.account_id}
        ]

    def query_stock_orders(self, acc):
        return list(self.orders_placed)

    def query_stock_trades(self, acc):
        return []


class FakeXtTrader:
    XtQuantTrader = FakeTrader
    _trader_factory_calls = 0

    def __init__(self):
        # instance-level counter resets via reset()
        self.trader_factory_calls = 0
        self.traders: List[FakeTrader] = []

    def make_trader(self, userdata, session_id):
        self.trader_factory_calls += 1
        t = FakeTrader(userdata, session_id)
        self.traders.append(t)
        return t


class FakeXtType:
    StockAccount = FakeStockAccount


class FakeXtConstant:
    STOCK_BUY = 23
    STOCK_SELL = 24
    FIX_PRICE = 11
    LATEST_PRICE = 5


# Module-level singletons used via sys.modules
_XTDATA = FakeXtData()
_XTTRADER_NS = types.SimpleNamespace(
    XtQuantTrader=None,  # replaced by install()
    _fake_state=None,
)
_XTTYPE = FakeXtType()
_XTCONSTANT = FakeXtConstant()


class FakeState:
    def __init__(self):
        self.xtdata = _XTDATA
        self.trader_factory_calls = 0
        self.traders: List[FakeTrader] = []

    def reset(self):
        global _XTDATA
        _XTDATA = FakeXtData()
        # Also update the module reference for xtquant.xtdata
        xtdata_mod = sys.modules.get("xtquant.xtdata")
        if xtdata_mod is not None:
            for name in dir(_XTDATA):
                if not name.startswith("_"):
                    setattr(xtdata_mod, name, getattr(_XTDATA, name))
        self.xtdata = _XTDATA
        self.trader_factory_calls = 0
        self.traders = []


_STATE = FakeState()


def install() -> FakeState:
    """Inject fake xtquant into sys.modules. Resets state on every call."""
    global _XTDATA, _STATE
    _XTDATA = FakeXtData()
    _STATE = FakeState()

    pkg = types.ModuleType("xtquant")
    pkg.__path__ = []  # mark as package

    xtdata_mod = types.ModuleType("xtquant.xtdata")
    for name in dir(_XTDATA):
        if not name.startswith("_"):
            setattr(xtdata_mod, name, getattr(_XTDATA, name))

    def _factory(userdata, session_id):
        _STATE.trader_factory_calls += 1
        trader = FakeTrader(userdata, session_id)
        _STATE.traders.append(trader)
        return trader

    xttrader_mod = types.ModuleType("xtquant.xttrader")
    xttrader_mod.XtQuantTrader = _factory

    xttype_mod = types.ModuleType("xtquant.xttype")
    xttype_mod.StockAccount = FakeStockAccount

    xtconst_mod = types.ModuleType("xtquant.xtconstant")
    for k in ("STOCK_BUY", "STOCK_SELL", "FIX_PRICE", "LATEST_PRICE"):
        setattr(xtconst_mod, k, getattr(FakeXtConstant, k))

    sys.modules["xtquant"] = pkg
    sys.modules["xtquant.xtdata"] = xtdata_mod
    sys.modules["xtquant.xttrader"] = xttrader_mod
    sys.modules["xtquant.xttype"] = xttype_mod
    sys.modules["xtquant.xtconstant"] = xtconst_mod
    _STATE.xtdata = _XTDATA
    # Bypass xtquant_loader's sys.path injection — mark as already loaded.
    from miniqmt_cli.server import xtquant_loader
    xtquant_loader._loaded = True
    return _STATE


def uninstall():
    for k in ("xtquant", "xtquant.xtdata", "xtquant.xttrader",
              "xtquant.xttype", "xtquant.xtconstant"):
        sys.modules.pop(k, None)
    from miniqmt_cli.server import xtquant_loader
    xtquant_loader.reset_for_tests()


def state() -> FakeState:
    return _STATE
