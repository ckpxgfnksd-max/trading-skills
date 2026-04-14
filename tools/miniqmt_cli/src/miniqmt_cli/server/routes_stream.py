"""SSE streaming endpoints. Subscriptions live in the async generator."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Dict, List, Tuple

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from miniqmt_cli.server import xtdata_adapter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

TICK_QUEUE_MAX = 256
KLINE_QUEUE_MAX = 256


def _session(request: Request):
    return request.app.state.session


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _tick_generator(
    request: Request, codes: List[str]
) -> AsyncIterator[str]:
    await _session(request).ensure_xtquant()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=TICK_QUEUE_MAX)
    dropped = 0

    def push(events):
        nonlocal dropped
        for ev in events or []:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
                    dropped += 1
                except asyncio.QueueFull:
                    dropped += 1

    seqs: List[int] = []
    try:
        for code in codes:
            seq = xtdata_adapter.subscribe_quote(code, "tick", push)
            seqs.append(seq)
        yield _sse({"event": "subscribed", "codes": codes, "seqs": seqs})
        while True:
            if await request.is_disconnected():
                break
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            payload = {"tick": ev}
            if dropped:
                payload["dropped"] = dropped
                dropped = 0
            yield _sse(payload)
    finally:
        for seq in seqs:
            try:
                xtdata_adapter.unsubscribe_quote(seq)
            except Exception as e:  # noqa: BLE001
                log.warning("unsubscribe failed for seq=%s: %s", seq, e)


async def _kline_generator(
    request: Request, codes: List[str], period: str
) -> AsyncIterator[str]:
    await _session(request).ensure_xtquant()
    loop = asyncio.get_running_loop()
    bars: Dict[Tuple[str, int], dict] = {}
    bars_lock = asyncio.Lock()
    notify = asyncio.Event()

    def push(events):
        async def _ingest(events_inner):
            async with bars_lock:
                for ev in events_inner or []:
                    code = ev.get("code") or ev.get("ts_code") or ""
                    ts = int(ev.get("time") or ev.get("timestamp") or 0)
                    bars[(code, ts)] = ev
                notify.set()
        asyncio.run_coroutine_threadsafe(_ingest(events), loop)

    seqs: List[int] = []
    try:
        for code in codes:
            seq = xtdata_adapter.subscribe_quote(code, period, push)
            seqs.append(seq)
        yield _sse({"event": "subscribed", "codes": codes, "seqs": seqs, "period": period})
        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.wait_for(notify.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            async with bars_lock:
                drained = list(bars.values())
                bars.clear()
                notify.clear()
            for ev in drained:
                yield _sse({"bar": ev})
    finally:
        for seq in seqs:
            try:
                xtdata_adapter.unsubscribe_quote(seq)
            except Exception as e:  # noqa: BLE001
                log.warning("unsubscribe failed for seq=%s: %s", seq, e)


@router.get("/tick")
async def stream_tick(request: Request, codes: List[str] = Query(..., alias="code")):
    return StreamingResponse(
        _tick_generator(request, codes), media_type="text/event-stream"
    )


@router.get("/kline")
async def stream_kline(
    request: Request,
    codes: List[str] = Query(..., alias="code"),
    period: str = Query("1m"),
):
    return StreamingResponse(
        _kline_generator(request, codes, period), media_type="text/event-stream"
    )
