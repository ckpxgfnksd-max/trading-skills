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


def classify_direction(delta: dict) -> str:
    """Classify a delta interval as buy, sell, or neutral."""
    price = delta["last_price"]
    if price >= delta["ask0"]:
        return "buy"
    if price <= delta["bid0"]:
        return "sell"
    return "neutral"


DEFAULT_THRESHOLDS = (40_000, 200_000, 1_000_000)

TIER_NAMES = ("small", "medium", "large", "xlarge")


def classify_tier(
    avg_amount: float,
    thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS,
) -> str:
    if avg_amount >= thresholds[2]:
        return "xlarge"
    if avg_amount >= thresholds[1]:
        return "large"
    if avg_amount >= thresholds[0]:
        return "medium"
    return "small"


@dataclass
class TierBucket:
    buy: float = 0.0
    sell: float = 0.0

    @property
    def net(self) -> float:
        return self.buy - self.sell


@dataclass
class MoneyFlowSummary:
    tiers: dict[str, TierBucket] = field(
        default_factory=lambda: {name: TierBucket() for name in TIER_NAMES}
    )
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "total_intervals": 0,
            "buy_count": 0,
            "sell_count": 0,
            "neutral_count": 0,
        }
    )

    @property
    def main_force_net(self) -> float:
        return self.tiers["xlarge"].net + self.tiers["large"].net

    @property
    def retail_net(self) -> float:
        return self.tiers["medium"].net + self.tiers["small"].net


def aggregate_moneyflow(
    deltas: list[dict],
    thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS,
) -> MoneyFlowSummary:
    summary = MoneyFlowSummary()
    for d in deltas:
        direction = classify_direction(d)
        tier = classify_tier(d["avg_amount"], thresholds)
        bucket = summary.tiers[tier]
        amount = d["delta_amount"]

        if direction == "buy":
            bucket.buy += amount
            summary.stats["buy_count"] += 1
        elif direction == "sell":
            bucket.sell += amount
            summary.stats["sell_count"] += 1
        else:
            bucket.buy += amount / 2
            bucket.sell += amount / 2
            summary.stats["neutral_count"] += 1

        summary.stats["total_intervals"] += 1
    return summary
