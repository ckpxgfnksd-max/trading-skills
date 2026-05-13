"""Read-only market data endpoints."""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from miniqmt_cli.server import xtdata_adapter
from miniqmt_cli.server._xt_call import (
    XtCallSaturated, XtCallTimeout, xt_call,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])

# Timeout budget for xtquant calls; the shared xt_call helper enforces them
# on a bounded dedicated thread pool so a stuck backend cannot exhaust the
# generic asyncio thread pool or freeze the event loop.
XT_TIMEOUT_LIGHT = 10.0      # snapshot-style queries
XT_TIMEOUT_HEAVY = 60.0      # history download / large pulls


async def _xt(fn: Callable[..., Any], *args, timeout: float, label: str) -> Any:
    """Local wrapper: surface XtCall* exceptions as HTTP 503 for read paths.

    Read endpoints have no state to reconcile, so 503 is the right code:
    'backend unavailable, safe to retry later'.
    """
    try:
        return await xt_call(fn, *args, timeout=timeout, label=label)
    except XtCallTimeout as e:
        raise HTTPException(
            status_code=503,
            detail=f"xtquant {e.label} timed out after {e.timeout_seconds}s",
        ) from e
    except XtCallSaturated as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
        ) from e


def _session(request: Request):
    return request.app.state.session


@router.get("/version")
def version(request: Request):
    return {"tag": "sp3", "version": "1.0", "xtquant_state": _session(request).xtquant_state()}


@router.get("/sectors")
async def sectors(request: Request):
    await _session(request).ensure_xtquant()
    data = await _xt(xtdata_adapter.get_sector_list, timeout=XT_TIMEOUT_LIGHT, label="get_sector_list")
    return {"sectors": data}


@router.get("/instruments")
async def instruments(
    request: Request,
    sector: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    await _session(request).ensure_xtquant()
    target = sector if sector else "沪深A股"
    codes = await _xt(
        xtdata_adapter.get_stock_list_in_sector, target,
        timeout=XT_TIMEOUT_LIGHT, label="get_stock_list_in_sector",
    )
    if limit:
        codes = codes[:limit]
    return {"codes": codes}


@router.get("/instrument")
async def instrument(request: Request, code: str = Query(...)):
    await _session(request).ensure_xtquant()
    detail = await _xt(
        xtdata_adapter.get_instrument_detail, code,
        timeout=XT_TIMEOUT_LIGHT, label="get_instrument_detail",
    )
    if not detail:
        raise HTTPException(status_code=404, detail=f"instrument not found: {code}")
    return detail


@router.get("/tick")
async def tick(request: Request, codes: List[str] = Query(..., alias="code")):
    await _session(request).ensure_xtquant()
    return await _xt(
        xtdata_adapter.get_full_tick, codes,
        timeout=XT_TIMEOUT_LIGHT, label="get_full_tick",
    )


@router.get("/kline")
async def kline(
    request: Request,
    code: str = Query(...),
    period: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
):
    if period == "tick":
        raise HTTPException(
            status_code=400,
            detail="period=tick is not supported by kline; use /data/ticks",
        )
    await _session(request).ensure_xtquant()
    try:
        data = await _xt(
            xtdata_adapter.get_market_data_ex,
            [code], period, start, end,
            timeout=XT_TIMEOUT_HEAVY, label="get_market_data_ex(kline)",
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("kline xtquant call failed")
        raise HTTPException(
            status_code=500, detail=f"xtquant get_market_data_ex failed: {e}"
        )
    return _kline_to_records(data, code)


@router.get("/ticks")
async def ticks(
    request: Request,
    code: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
):
    await _session(request).ensure_xtquant()
    try:
        data = await _xt(
            xtdata_adapter.get_market_data_ex,
            [code], "tick", start, end,
            timeout=XT_TIMEOUT_HEAVY, label="get_market_data_ex(ticks)",
        )
    except HTTPException:
        raise
    except Exception as e:
        log.exception("ticks xtquant call failed")
        raise HTTPException(
            status_code=500, detail=f"xtquant get_market_data_ex failed: {e}"
        )
    return _kline_to_records(data, code)


def _kline_to_records(data, code: str):
    """Convert xtquant's per-code DataFrame dict into a list[dict] for CLI."""
    if not data:
        return []
    bucket = data.get(code) if isinstance(data, dict) else None
    if bucket is None:
        return []
    try:
        import pandas as pd
        if isinstance(bucket, pd.DataFrame):
            df = bucket.reset_index()
            return df.to_dict(orient="records")
    except Exception:
        pass
    if isinstance(bucket, list):
        return bucket
    if isinstance(bucket, dict):
        return [bucket]
    return []
