import json

from miniqmt_cli.server.audit import AuditLog


def test_audit_appends_jsonl(tmp_path):
    log = AuditLog(tmp_path / "orders.jsonl")
    log.append(phase="pre", client_req_id="abc", account="sim")
    log.append(phase="post", client_req_id="abc", status="ok")
    lines = (tmp_path / "orders.jsonl").read_text().splitlines()
    assert len(lines) == 2
    r1 = json.loads(lines[0])
    assert r1["phase"] == "pre"
    assert r1["ts"].endswith("Z")
    r2 = json.loads(lines[1])
    assert r2["status"] == "ok"


def test_audit_timestamp_is_utc(tmp_path):
    log = AuditLog(tmp_path / "o.jsonl")
    log.append(phase="pre")
    line = (tmp_path / "o.jsonl").read_text().strip()
    ts = json.loads(line)["ts"]
    assert ts.endswith("Z")
    assert "+" not in ts  # UTC, not local tz
