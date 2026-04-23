"""Read-only market data endpoints."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from miniqmt_cli.server import xtdata_adapter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/data", tags=["data"])


def _session(request: Request):
    return request.app.state.session


@router.get("/version")
def version(request: Request):
    return {"tag": "sp3", "version": "1.0", "xtquant_state": _session(request).xtquant_state()}


@router.get("/sectors")
async def sectors(request: Request):
    await _session(request).ensure_xtquant()
    return {"sectors": xtdata_adapter.get_sector_list()}


@router.get("/instruments")
async def instruments(
    request: Request,
    sector: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
):
    await _session(request).ensure_xtquant()
    if sector:
        codes = xtdata_adapter.get_stock_list_in_sector(sector)
    else:
        codes = xtdata_adapter.get_stock_list_in_sector("沪深A股")
    if limit:
        codes = codes[:limit]
    return {"codes": codes}


@router.get("/instrument")
async def instrument(request: Request, code: str = Query(...)):
    await _session(request).ensure_xtquant()
    detail = xtdata_adapter.get_instrument_detail(code)
    if not detail:
        raise HTTPException(status_code=404, detail=f"instrument not found: {code}")
    return detail


@router.get("/tick")
async def tick(request: Request, codes: List[str] = Query(..., alias="code")):
    await _session(request).ensure_xtquant()
    return xtdata_adapter.get_full_tick(codes)


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
        data = xtdata_adapter.get_market_data_ex(
            codes=[code], period=period, start_time=start, end_time=end
        )
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
        data = xtdata_adapter.get_market_data_ex(
            codes=[code], period="tick", start_time=start, end_time=end
        )
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
