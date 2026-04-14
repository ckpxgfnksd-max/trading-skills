from miniqmt_cli.output import format_output, format_row


def test_format_output_empty_json():
    assert format_output([], "json") == "[]"


def test_format_output_json_list():
    out = format_output([{"a": 1, "b": 2}], "json")
    import json as _json
    parsed = _json.loads(out)
    assert parsed == [{"a": 1, "b": 2}]


def test_format_output_table_nonempty():
    out = format_output([{"a": 1}], "table")
    assert "1" in out


def test_format_row_json():
    out = format_row({"code": "000001.SZ", "price": 12.34}, "json")
    assert "000001.SZ" in out


def test_format_row_table_is_kv():
    out = format_row({"code": "X", "price": 1}, "table")
    assert "code=X" in out and "price=1" in out
