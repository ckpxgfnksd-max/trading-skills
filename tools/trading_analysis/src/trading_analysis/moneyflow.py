"""Core money flow computation: delta, direction, tier, aggregation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


def compute_deltas(snapshots: list[dict]) -> list[dict]:
    """Diff adjacent snapshots. Skip first and any with negative delta."""
    deltas = []
    for i in range(1, len(snapshots)):
        prev, curr = snapshots[i - 1], snapshots[i]
        d_amount = curr["amount"] - prev["amount"]
        d_volume = curr["volume"] - prev["volume"]
        d_txn = curr["transactionNum"] - prev["transactionNum"]
        if d_amount < 0 or d_volume < 0:
            continue
        deltas.append({
            "stime": curr["stime"],
            "delta_amount": d_amount,
            "delta_volume": d_volume,
            "delta_txn": d_txn,
            "avg_amount": d_amount / max(d_txn, 1),
            "last_price": curr["lastPrice"],
            "ask0": curr["askPrice"][0],
            "bid0": curr["bidPrice"][0],
        })
    return deltas
