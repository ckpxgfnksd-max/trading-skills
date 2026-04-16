"""Tests for Phase 1: order lifecycle (callback, subscriber fan-out, stream)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from miniqmt_cli.server.xttrader_adapter import TraderCallback, _map_order_status, _map_direction


class TestTraderCallback:
    def test_on_order_event_dispatches(self):
        events = []
        cb = TraderCallback(events.append, "sim")
        order = MagicMock()
        order.order_id = 123
        order.order_status = 56  # filled
        order.stock_code = "002028.SZ"
        order.order_type = 23  # buy
        order.order_volume = 100
        order.traded_volume = 100
        order.traded_price = 210.5
        order.frozen = 0.0

        cb.on_order_event(order)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "order_status"
        assert e["account"] == "sim"
        assert e["order_id"] == 123
        assert e["status"] == "filled"
        assert e["code"] == "002028.SZ"
        assert e["side"] == "buy"
        assert e["filled_volume"] == 100
        assert e["avg_price"] == 210.5

    def test_on_trade_event_dispatches(self):
        events = []
        cb = TraderCallback(events.append, "live")
        trade = MagicMock()
        trade.order_id = 456
        trade.traded_id = 789
        trade.stock_code = "000001.SZ"
        trade.order_type = 24  # sell
        trade.traded_price = 15.0
        trade.traded_volume = 50
        trade.traded_amount = 75000.0

        cb.on_trade_event(trade)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "trade"
        assert e["account"] == "live"
        assert e["order_id"] == 456
        assert e["trade_id"] == 789
        assert e["side"] == "sell"
        assert e["price"] == 15.0
        assert e["volume"] == 50

    def test_on_order_stock_async_response_dispatches(self):
        events = []
        cb = TraderCallback(events.append, "sim")
        resp = MagicMock()
        resp.seq = 999
        resp.stock_code = "002028.SZ"

        cb.on_order_stock_async_response(resp)
        assert len(events) == 1
        assert events[0]["type"] == "order_response"
        assert events[0]["seq"] == 999

    def test_callback_exception_does_not_propagate(self):
        def bad_dispatch(event):
            raise RuntimeError("boom")

        cb = TraderCallback(bad_dispatch, "sim")
        order = MagicMock()
        order.order_id = 1
        order.order_status = 49
        order.stock_code = "000001.SZ"
        order.order_type = 23
        order.order_volume = 100
        order.traded_volume = 0
        order.traded_price = 0.0
        order.frozen = 0.0

        # Should not raise
        cb.on_order_event(order)


class TestMapOrderStatus:
    def test_known_statuses(self):
        assert _map_order_status(49) == "submitted"
        assert _map_order_status(51) == "partially_filled"
        assert _map_order_status(56) == "filled"
        assert _map_order_status(52) == "cancelled"
        assert _map_order_status(53) == "rejected"

    def test_unknown_status(self):
        assert _map_order_status(99) == "unknown_99"


class TestMapDirection:
    def test_buy(self):
        assert _map_direction(23) == "buy"

    def test_sell(self):
        assert _map_direction(24) == "sell"

    def test_unknown(self):
        assert _map_direction(0) == "unknown_0"


class TestSessionSubscriber:
    """Test SessionManager subscriber fan-out without real xtquant."""

    @pytest.fixture
    def session(self):
        from miniqmt_cli.server_config import ServerConfig
        from miniqmt_cli.server.session import SessionManager
        return SessionManager(ServerConfig(), dry_run=True)

    @pytest.mark.asyncio
    async def test_subscribe_and_dispatch(self, session):
        q = await session.subscribe_orders()
        event = {"type": "order_status", "order_id": 1}
        session.dispatch_order_event(event)
        result = q.get_nowait()
        assert result == event

    @pytest.mark.asyncio
    async def test_fan_out_to_multiple(self, session):
        q1 = await session.subscribe_orders()
        q2 = await session.subscribe_orders()
        event = {"type": "trade", "order_id": 2}
        session.dispatch_order_event(event)
        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    @pytest.mark.asyncio
    async def test_unsubscribe(self, session):
        q = await session.subscribe_orders()
        await session.unsubscribe_orders(q)
        session.dispatch_order_event({"type": "test"})
        assert q.empty()

    @pytest.mark.asyncio
    async def test_queue_full_does_not_block(self, session):
        q = await session.subscribe_orders()
        # Fill the queue
        for i in range(256):
            session.dispatch_order_event({"i": i})
        # 257th should not raise
        session.dispatch_order_event({"i": 256})
        assert q.qsize() == 256

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_queue(self, session):
        q = asyncio.Queue()
        # Should not raise
        await session.unsubscribe_orders(q)
