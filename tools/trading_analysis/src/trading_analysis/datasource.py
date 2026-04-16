"""Fetch tick snapshots from the miniqmt-cli daemon."""
from __future__ import annotations

from typing import List

from miniqmt_cli.client.transport import Transport


def fetch_ticks(
    transport: Transport,
    code: str,
    date: str,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch tick snapshots for a single stock."""
    return transport.get(
        "/data/ticks",
        params={"code": code, "start": f"{date}{start}", "end": f"{date}{end}"},
    )


def fetch_kline(
    transport: Transport,
    code: str,
    period: str,
    start: str,
    end: str,
) -> list[dict]:
    """Fetch kline bars. Returns list of dicts with 'close' field."""
    return transport.get(
        "/data/kline",
        params={"code": code, "period": period, "start": start, "end": end},
    )


def fetch_tick_snapshot(transport: Transport, codes: list[str]) -> dict:
    """Fetch latest tick snapshot for one or more codes.

    Returns dict keyed by code, e.g. {"002028.SZ": {...}, ...}
    """
    return transport.get(
        "/data/tick",
        params=[("code", c) for c in codes],
    )
