"""Fetch tick snapshots from the miniqmt-cli daemon."""
from __future__ import annotations

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
