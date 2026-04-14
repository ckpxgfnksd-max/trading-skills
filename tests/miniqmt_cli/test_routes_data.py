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
