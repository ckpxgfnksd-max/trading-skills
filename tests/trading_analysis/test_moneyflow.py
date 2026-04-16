from trading_analysis.moneyflow import compute_deltas


def _snap(stime, amount, volume, txn, last_price, ask0, bid0):
    """Build a minimal snapshot dict matching xtquant tick format."""
    return {
        "stime": stime,
        "lastPrice": last_price,
        "amount": amount,
        "volume": volume,
        "transactionNum": txn,
        "askPrice": [ask0, 0, 0, 0, 0],
        "bidPrice": [bid0, 0, 0, 0, 0],
    }


class TestComputeDeltas:
    def test_basic_two_snapshots(self):
        snaps = [
            _snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9),
            _snap("093003", 1_500_000, 150, 80, 10.2, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert len(deltas) == 1
        d = deltas[0]
        assert d["delta_amount"] == 500_000
        assert d["delta_volume"] == 50
        assert d["delta_txn"] == 30
        assert d["last_price"] == 10.2
        assert d["ask0"] == 10.1
        assert d["bid0"] == 9.9

    def test_first_snapshot_skipped(self):
        snaps = [_snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9)]
        deltas = compute_deltas(snaps)
        assert len(deltas) == 0

    def test_negative_delta_skipped(self):
        snaps = [
            _snap("093000", 1_000_000, 100, 50, 10.0, 10.1, 9.9),
            _snap("093003", 500_000, 50, 20, 10.0, 10.1, 9.9),
            _snap("093006", 800_000, 80, 40, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert len(deltas) == 1
        assert deltas[0]["delta_amount"] == 300_000

    def test_avg_amount_computed(self):
        snaps = [
            _snap("093000", 0, 0, 0, 10.0, 10.1, 9.9),
            _snap("093003", 600_000, 60, 10, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert deltas[0]["avg_amount"] == 60_000

    def test_zero_txn_delta_uses_one(self):
        snaps = [
            _snap("093000", 0, 0, 0, 10.0, 10.1, 9.9),
            _snap("093003", 100_000, 10, 0, 10.0, 10.1, 9.9),
        ]
        deltas = compute_deltas(snaps)
        assert deltas[0]["avg_amount"] == 100_000


from trading_analysis.moneyflow import classify_direction


class TestClassifyDirection:
    def test_active_buy(self):
        delta = {"last_price": 10.2, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "buy"

    def test_active_sell(self):
        delta = {"last_price": 9.8, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "sell"

    def test_neutral(self):
        delta = {"last_price": 10.0, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "neutral"

    def test_equal_to_ask_is_buy(self):
        delta = {"last_price": 10.1, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "buy"

    def test_equal_to_bid_is_sell(self):
        delta = {"last_price": 9.9, "ask0": 10.1, "bid0": 9.9}
        assert classify_direction(delta) == "sell"


from trading_analysis.moneyflow import (
    classify_tier,
    aggregate_moneyflow,
    DEFAULT_THRESHOLDS,
    MoneyFlowSummary,
)


class TestClassifyTier:
    def test_small(self):
        assert classify_tier(30_000) == "small"

    def test_medium(self):
        assert classify_tier(100_000) == "medium"

    def test_large(self):
        assert classify_tier(500_000) == "large"

    def test_extra_large(self):
        assert classify_tier(1_500_000) == "xlarge"

    def test_boundary_medium(self):
        assert classify_tier(40_000) == "medium"

    def test_boundary_large(self):
        assert classify_tier(200_000) == "large"

    def test_boundary_xlarge(self):
        assert classify_tier(1_000_000) == "xlarge"

    def test_custom_thresholds(self):
        assert classify_tier(50_000, thresholds=(50_000, 200_000, 1_000_000)) == "medium"
        assert classify_tier(49_999, thresholds=(50_000, 200_000, 1_000_000)) == "small"


class TestAggregateMoneyflow:
    def test_single_buy_xlarge(self):
        deltas = [{
            "delta_amount": 2_000_000, "delta_volume": 100, "delta_txn": 1,
            "avg_amount": 2_000_000, "last_price": 10.2,
            "ask0": 10.1, "bid0": 9.9, "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["xlarge"].buy == 2_000_000
        assert result.tiers["xlarge"].sell == 0
        assert result.tiers["xlarge"].net == 2_000_000

    def test_single_sell_small(self):
        deltas = [{
            "delta_amount": 10_000, "delta_volume": 10, "delta_txn": 5,
            "avg_amount": 2_000, "last_price": 9.8,
            "ask0": 10.1, "bid0": 9.9, "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["small"].sell == 10_000
        assert result.tiers["small"].buy == 0

    def test_neutral_split_half(self):
        deltas = [{
            "delta_amount": 100_000, "delta_volume": 50, "delta_txn": 2,
            "avg_amount": 50_000, "last_price": 10.0,
            "ask0": 10.1, "bid0": 9.9, "stime": "093003",
        }]
        result = aggregate_moneyflow(deltas)
        assert result.tiers["medium"].buy == 50_000
        assert result.tiers["medium"].sell == 50_000
        assert result.tiers["medium"].net == 0

    def test_main_force_net(self):
        deltas = [
            {
                "delta_amount": 2_000_000, "delta_volume": 100, "delta_txn": 1,
                "avg_amount": 2_000_000, "last_price": 10.2,
                "ask0": 10.1, "bid0": 9.9, "stime": "093003",
            },
            {
                "delta_amount": 500_000, "delta_volume": 50, "delta_txn": 1,
                "avg_amount": 500_000, "last_price": 9.8,
                "ask0": 10.1, "bid0": 9.9, "stime": "093006",
            },
        ]
        result = aggregate_moneyflow(deltas)
        assert result.main_force_net == 2_000_000 - 500_000

    def test_stats_counts(self):
        deltas = [
            {
                "delta_amount": 100_000, "delta_volume": 10, "delta_txn": 5,
                "avg_amount": 20_000, "last_price": 10.2,
                "ask0": 10.1, "bid0": 9.9, "stime": "093003",
            },
            {
                "delta_amount": 50_000, "delta_volume": 5, "delta_txn": 3,
                "avg_amount": 16_667, "last_price": 9.8,
                "ask0": 10.1, "bid0": 9.9, "stime": "093006",
            },
            {
                "delta_amount": 80_000, "delta_volume": 8, "delta_txn": 4,
                "avg_amount": 20_000, "last_price": 10.0,
                "ask0": 10.1, "bid0": 9.9, "stime": "093009",
            },
        ]
        result = aggregate_moneyflow(deltas)
        assert result.stats["buy_count"] == 1
        assert result.stats["sell_count"] == 1
        assert result.stats["neutral_count"] == 1
        assert result.stats["total_intervals"] == 3
