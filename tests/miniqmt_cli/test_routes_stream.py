"""Unit-test the SSE generators directly.

TestClient + sync iter_lines deadlocks on SSE generators that wait for
events that never arrive. We exercise the generators directly under
asyncio so we can drive lifecycle (subscribe → cancel → cleanup assertion)
deterministically.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from miniqmt_cli.server import xtquant_loader
from miniqmt_cli.server.app import create_app
from miniqmt_cli.server.routes_stream import _kline_generator, _tick_generator


class FakeRequest:
    """Stands in for starlette.Request in generator tests."""
    def __init__(self, app):
        self.app = app
        self._disconnected = False

    async def is_disconnected(self):
        return self._disconnected

    def disconnect(self):
        self._disconnected = True


def _parse_sse(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: "):].strip())


@pytest.mark.asyncio
async def test_tick_generator_cleans_up_on_cancel(server_cfg, fake_xtquant):
    app = create_app(server_cfg, dry_run=False)
    req = FakeRequest(app)
    gen = _tick_generator(req, ["000001.SZ", "600000.SH"])
    # First yield is the "subscribed" event.
    first = await gen.__anext__()
    payload = _parse_sse(first)
    assert payload["event"] == "subscribed"
    seqs = payload["seqs"]
    assert len(seqs) == 2
    # Close the generator → triggers finally → unsubscribes every seq.
    await gen.aclose()
    assert sorted(fake_xtquant.xtdata.unsubscribed_seqs) == sorted(seqs)


@pytest.mark.asyncio
async def test_tick_generator_cleans_up_on_disconnect(server_cfg, fake_xtquant):
    app = create_app(server_cfg, dry_run=False)
    req = FakeRequest(app)
    gen = _tick_generator(req, ["000001.SZ"])
    await gen.__anext__()  # subscribed event
    req.disconnect()
    # Next __anext__ should observe disconnect, hit the break, run finally.
    with pytest.raises(StopAsyncIteration):
        # May need one or two polls; advance up to 3 times.
        for _ in range(3):
            await gen.__anext__()
    assert len(fake_xtquant.xtdata.unsubscribed_seqs) == 1


@pytest.mark.asyncio
async def test_tick_generator_passes_events_through(server_cfg, fake_xtquant):
    app = create_app(server_cfg, dry_run=False)
    req = FakeRequest(app)
    gen = _tick_generator(req, ["000001.SZ"])
    # consume subscribed event
    await gen.__anext__()
    # push a fake tick into the subscription
    seq = list(fake_xtquant.xtdata.subscribed.keys())[0]
    fake_xtquant.xtdata.push(seq, [{"code": "000001.SZ", "lastPrice": 99.9}])
    # next yield is the tick
    chunk = await gen.__anext__()
    payload = _parse_sse(chunk)
    assert payload["tick"]["lastPrice"] == 99.9
    await gen.aclose()


@pytest.mark.asyncio
async def test_kline_generator_cleans_up_on_cancel(server_cfg, fake_xtquant):
    app = create_app(server_cfg, dry_run=False)
    req = FakeRequest(app)
    gen = _kline_generator(req, ["000001.SZ"], "1m")
    first = await gen.__anext__()
    payload = _parse_sse(first)
    assert payload["event"] == "subscribed"
    seqs = payload["seqs"]
    await gen.aclose()
    assert sorted(fake_xtquant.xtdata.unsubscribed_seqs) == sorted(seqs)


@pytest.mark.asyncio
async def test_kline_generator_coalesces_bars(server_cfg, fake_xtquant):
    """Multiple updates for the same (code, bar_ts) collapse to the latest."""
    app = create_app(server_cfg, dry_run=False)
    req = FakeRequest(app)
    gen = _kline_generator(req, ["000001.SZ"], "1m")
    await gen.__anext__()  # subscribed
    seq = list(fake_xtquant.xtdata.subscribed.keys())[0]
    # Push three events for the same bar_ts — only the last should be yielded.
    bar_ts = 1000000
    fake_xtquant.xtdata.push(seq, [{"code": "000001.SZ", "time": bar_ts, "close": 10.0}])
    fake_xtquant.xtdata.push(seq, [{"code": "000001.SZ", "time": bar_ts, "close": 10.5}])
    fake_xtquant.xtdata.push(seq, [{"code": "000001.SZ", "time": bar_ts, "close": 11.0}])
    # Let the coroutine schedule run to drain events into bars.
    await asyncio.sleep(0.05)
    chunk = await gen.__anext__()
    payload = _parse_sse(chunk)
    assert payload["bar"]["close"] == 11.0
    await gen.aclose()
