from unittest.mock import MagicMock

from trading_analysis.datasource import fetch_ticks


def _make_transport(return_value):
    t = MagicMock()
    t.get.return_value = return_value
    return t


class TestFetchTicks:
    def test_returns_list_of_dicts(self):
        data = [{"stime": "093000", "amount": 100}]
        t = _make_transport(data)
        result = fetch_ticks(t, "002028.SZ", "20260416", "093000", "150000")
        assert result == data
        t.get.assert_called_once_with(
            "/data/ticks",
            params={"code": "002028.SZ", "start": "20260416093000", "end": "20260416150000"},
        )

    def test_empty_list_returned(self):
        t = _make_transport([])
        result = fetch_ticks(t, "002028.SZ", "20260416", "093000", "150000")
        assert result == []

    def test_date_time_concatenation(self):
        t = _make_transport([])
        fetch_ticks(t, "000001.SZ", "20260415", "100000", "113000")
        t.get.assert_called_once_with(
            "/data/ticks",
            params={"code": "000001.SZ", "start": "20260415100000", "end": "20260415113000"},
        )
