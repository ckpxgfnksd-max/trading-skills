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
