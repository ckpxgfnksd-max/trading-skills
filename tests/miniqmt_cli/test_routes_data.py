def test_sectors(client):
    resp = client.get("/data/sectors")
    assert resp.status_code == 200
    assert "沪深A股" in resp.json()["sectors"]


def test_instruments_with_sector(client):
    resp = client.get("/data/instruments", params={"sector": "沪深A股"})
    assert resp.status_code == 200
    codes = resp.json()["codes"]
    assert "000001.SZ" in codes


def test_instrument_detail(client):
    resp = client.get("/data/instrument", params={"code": "000001.SZ"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["InstrumentName"] == "平安银行"


def test_instrument_detail_not_found(client):
    resp = client.get("/data/instrument", params={"code": "999999.XX"})
    assert resp.status_code == 404


def test_tick_snapshot(client):
    resp = client.get("/data/tick", params=[("code", "000001.SZ")])
    assert resp.status_code == 200
    assert resp.json()["000001.SZ"]["lastPrice"] == 12.34


def test_kline_rejects_period_tick(client):
    resp = client.get(
        "/data/kline",
        params={"code": "000001.SZ", "period": "tick", "start": "20260101", "end": "20260401"},
    )
    assert resp.status_code == 400
    assert "tick" in resp.json()["detail"]


def test_kline_passes_list_to_download_history_data2(client, fake_xtquant):
    """download_history_data2 requires stock_list: list — regression guard
    for the TypeError we hit when passing a bare str."""
    resp = client.get(
        "/data/kline",
        params={
            "code": "000001.SZ", "period": "1d",
            "start": "20260101", "end": "20260401",
        },
    )
    assert resp.status_code == 200, resp.text
    calls = fake_xtquant.xtdata.download_calls
    assert len(calls) == 1
    assert calls[0]["stock_list"] == ["000001.SZ"]
    assert calls[0]["period"] == "1d"


def test_ticks_passes_list_to_download_history_data2(client, fake_xtquant):
    resp = client.get(
        "/data/ticks",
        params={
            "code": "000001.SZ",
            "start": "20260401093000", "end": "20260401093500",
        },
    )
    assert resp.status_code == 200, resp.text
    calls = fake_xtquant.xtdata.download_calls
    assert len(calls) == 1
    assert calls[0]["stock_list"] == ["000001.SZ"]
    assert calls[0]["period"] == "tick"


def test_kline_surfaces_xtquant_error_detail(client, fake_xtquant, monkeypatch):
    """A raw xtquant exception must be mapped to HTTPException(500, detail=...)
    so the CLI/agent sees the underlying cause, not a bare Internal Server Error."""
    import sys
    def _boom(*args, **kwargs):
        raise TypeError("synthetic xtquant failure")
    xtdata_mod = sys.modules["xtquant.xtdata"]
    monkeypatch.setattr(xtdata_mod, "download_history_data2", _boom)
    resp = client.get(
        "/data/kline",
        params={
            "code": "000001.SZ", "period": "1d",
            "start": "20260101", "end": "20260401",
        },
    )
    assert resp.status_code == 500
    assert "synthetic xtquant failure" in resp.json()["detail"]
