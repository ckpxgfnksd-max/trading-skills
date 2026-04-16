from trading_analysis.signals import (
    compute_ma,
    evaluate,
    parse_required_mas,
    tokenize,
)


class TestTokenize:
    def test_simple(self):
        assert tokenize("main_net > 0") == ["main_net", ">", "0"]

    def test_and(self):
        assert tokenize("main_net > 0 and price > ma20") == [
            "main_net", ">", "0", "and", "price", ">", "ma20",
        ]

    def test_operators(self):
        assert tokenize("price >= 10.5") == ["price", ">=", "10.5"]
        assert tokenize("price <= 10") == ["price", "<=", "10"]
        assert tokenize("price == 10") == ["price", "==", "10"]

    def test_or(self):
        assert tokenize("main_net > 0 or retail_net < 0") == [
            "main_net", ">", "0", "or", "retail_net", "<", "0",
        ]


class TestParseRequiredMas:
    def test_single_ma(self):
        assert parse_required_mas("price > ma20") == {20}

    def test_multiple_mas(self):
        assert parse_required_mas("price > ma5 and price > ma60") == {5, 60}

    def test_no_ma(self):
        assert parse_required_mas("main_net > 0") == set()


class TestEvaluate:
    def test_simple_gt_true(self):
        r = evaluate("main_net > 0", {"main_net": 100.0})
        assert r.triggered is True

    def test_simple_gt_false(self):
        r = evaluate("main_net > 0", {"main_net": -50.0})
        assert r.triggered is False

    def test_and_both_true(self):
        r = evaluate("main_net > 0 and price > ma20", {
            "main_net": 100.0, "price": 15.0, "ma20": 10.0,
        })
        assert r.triggered is True

    def test_and_one_false(self):
        r = evaluate("main_net > 0 and price > ma20", {
            "main_net": 100.0, "price": 8.0, "ma20": 10.0,
        })
        assert r.triggered is False

    def test_or_one_true(self):
        r = evaluate("main_net > 0 or retail_net < 0", {
            "main_net": -10.0, "retail_net": -50.0,
        })
        assert r.triggered is True

    def test_or_both_false(self):
        r = evaluate("main_net > 0 or retail_net < 0", {
            "main_net": -10.0, "retail_net": 50.0,
        })
        assert r.triggered is False

    def test_missing_variable_returns_false(self):
        r = evaluate("price > ma20", {"price": 15.0, "ma20": None})
        assert r.triggered is False

    def test_gte(self):
        r = evaluate("price >= 10", {"price": 10.0})
        assert r.triggered is True

    def test_lte(self):
        r = evaluate("price <= 10", {"price": 10.0})
        assert r.triggered is True

    def test_eq(self):
        r = evaluate("price == 10", {"price": 10.0})
        assert r.triggered is True

    def test_float_literal(self):
        r = evaluate("price > 10.5", {"price": 11.0})
        assert r.triggered is True

    def test_empty_expression(self):
        r = evaluate("", {})
        assert r.triggered is False

    def test_complex_and_or(self):
        r = evaluate("main_net > 0 and price > ma20 or retail_net < 0", {
            "main_net": -10.0, "price": 5.0, "ma20": 10.0, "retail_net": -100.0,
        })
        # "and" binds tighter: (main_net > 0 and price > ma20) or (retail_net < 0)
        # = (False and False) or True = True
        assert r.triggered is True


class TestComputeMa:
    def test_exact_period(self):
        closes = [10.0, 11.0, 12.0, 13.0, 14.0]
        assert compute_ma(closes, 5) == 12.0

    def test_more_data_than_period(self):
        closes = [1.0, 2.0, 10.0, 11.0, 12.0, 13.0, 14.0]
        assert compute_ma(closes, 5) == 12.0  # last 5: 10,11,12,13,14

    def test_not_enough_data(self):
        closes = [10.0, 11.0]
        assert compute_ma(closes, 5) is None

    def test_single_value(self):
        assert compute_ma([10.0], 1) == 10.0
