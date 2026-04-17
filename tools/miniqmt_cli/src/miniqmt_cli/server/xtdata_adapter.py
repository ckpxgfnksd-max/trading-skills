"""Adapter wrapping xtquant.xtdata. All functions delegate to the real module
at runtime; tests inject a fake via sys.modules before first call."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from miniqmt_cli.server.xtquant_loader import load_xtquant


def _xtdata():
    import xtquant.xtdata as xtdata  # type: ignore
    return xtdata


def ensure_loaded(qmt_path: str) -> None:
    load_xtquant(qmt_path)


def get_sector_list() -> List[str]:
    xtdata = _xtdata()
    return list(xtdata.get_sector_list() or [])


def get_stock_list_in_sector(sector: str) -> List[str]:
    xtdata = _xtdata()
    return list(xtdata.get_stock_list_in_sector(sector) or [])


def get_instrument_detail(code: str) -> Dict[str, Any]:
    xtdata = _xtdata()
    result = xtdata.get_instrument_detail(code)
    return dict(result or {})


def get_full_tick(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    xtdata = _xtdata()
    return dict(xtdata.get_full_tick(codes) or {})


def get_market_data_ex(
    codes: List[str],
    period: str,
    start_time: str,
    end_time: str,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    xtdata = _xtdata()
    for code in codes:
        # download_history_data is async; download_history_data2 blocks until complete.
        # Fall back to the async version if the newer API is unavailable.
        if hasattr(xtdata, "download_history_data2"):
            xtdata.download_history_data2(code, period, start_time, end_time)
        else:
            import threading
            done = threading.Event()
            xtdata.download_history_data(
                code, period, start_time, end_time,
                callback=lambda _: done.set(),
            )
            done.wait(timeout=60)
    return xtdata.get_market_data_ex(
        field_list=fields or [],
        stock_list=codes,
        period=period,
        start_time=start_time,
        end_time=end_time,
    )


def subscribe_quote(
    code: str,
    period: str,
    callback: Callable[[List[dict]], None],
) -> int:
    xtdata = _xtdata()
    return int(xtdata.subscribe_quote(
        stock_code=code, period=period, callback=callback
    ))


def unsubscribe_quote(seq: int) -> None:
    xtdata = _xtdata()
    xtdata.unsubscribe_quote(seq)
