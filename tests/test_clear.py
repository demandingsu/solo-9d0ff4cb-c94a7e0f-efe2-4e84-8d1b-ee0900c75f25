"""清空工位后的交印/重取/重试语义，以及旧库升级与重启后的一致性。

覆盖题目要求：
- 清空后当天重新出首单，不得沿用清空前的单号；
- 清空前保存的首单链接（JSON / CSV）提示单据不存在，不下载到新单；
- 用清空前已成功交印的幂等键重试：提示该笔交印已清空，不出单、不改共需清单；
- 清空仍删除字库、分册与旧单内容；
- 交印与清空重叠时按生效次序处理；清空失败时原单仍可查询和复用；
- 服务重启后继续区分清空前后的单号与键；已有 SQLite 库可直接升级。
"""
from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import threading

import pytest

from fastapi.testclient import TestClient

from app import database, service
from app.main import app


@pytest.fixture()
def client():
    service.ensure_db()
    service.purge_all()
    with TestClient(app) as c:
        yield c


def _csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


def _upload_lib(client, rows=None):
    rows = rows or [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")]
    buf = io.StringIO()
    buf.write("字库,版次,字模号,字\n")
    for r in rows:
        buf.write(",".join(r) + "\n")
    return client.post(
        "/api/libraries",
        files={"file": ("libraries.csv", _csv(buf.getvalue()), "text/csv")},
    )


def _upload_vol(client, name: str, chars: str):
    body = "字\n" + "\n".join(chars) + "\n"
    return client.post(
        f"/api/volumes/{name}/chars",
        files={"file": (f"{name}.csv", _csv(body), "text/csv")},
    )


def _prepare(client, name: str = "册甲", chars: str = "天地"):
    assert _upload_lib(client).status_code == 200
    assert _upload_vol(client, name, chars).status_code == 200


def _submit(client, name: str = "册甲", key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/volumes/{name}/submit", headers=headers)


def _pool_chars(client) -> set[str]:
    state = client.get("/api/state").json()
    return {x["glyph_char"] for x in state["shared_pool"]}


# ---------- 清空后：旧单不存在，新单不得沿用旧号 ----------


def test_clear_invalidates_old_order_and_new_order_gets_new_number(client):
    _prepare(client)
    old = _submit(client).json()
    old_no = old["order_no"]
    assert client.get(f"/api/orders/{old_no}").status_code == 200

    assert client.post("/api/reset").json()["status"] == "cleared"

    # 清空后：旧单 JSON / CSV 都提示单据不存在（404），而不是返回到新单。
    r = client.get(f"/api/orders/{old_no}")
    assert r.status_code == 404 and "单据不存在" in r.json()["error"]
    r = client.get(f"/api/orders/{old_no}/download")
    assert r.status_code == 404 and "单据不存在" in r.json()["error"]

    # 清空仍删除字库、分册和旧单内容。
    state = client.get("/api/state").json()
    assert state["volumes"] == [] and state["libraries"] == [] and state["shared_pool"] == []
    assert client.get("/api/orders").json() == []

    # 当天重新出首单：单号必须是新号，旧单链接依然 404。
    _prepare(client)
    new = _submit(client).json()
    assert new["order_no"] != old_no
    assert new["order_no"].startswith("MO")
    assert new["generation"] == 1
    assert client.get(f"/api/orders/{old_no}").status_code == 404
    assert client.get(f"/api/orders/{new['order_no']}").status_code == 200

    # 再清空一次，第三代单号与第一、二代都不同。
    client.post("/api/reset")
    _prepare(client)
    third = _submit(client).json()
    assert third["order_no"] not in (old_no, new["order_no"])
    assert third["generation"] == 2


def test_old_order_download_cannot_return_new_order(client):
    """即使新单内容和旧单完全一致，旧单号下载也不能拿到任何 CSV。"""
    _prepare(client, chars="天")
    old_no = _submit(client).json()["order_no"]
    client.post("/api/reset")
    _prepare(client, chars="天")  # 同名册、同用字，内容与旧单一致
    new = _submit(client).json()
    assert new["order_no"] != old_no
    r = client.get(f"/api/orders/{old_no}/download")
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")


# ---------- 旧幂等键重试：提示已清空，不出单、不改共需清单 ----------


def test_retry_old_idempotency_key_after_clear_is_rejected(client):
    _prepare(client)
    old = _submit(client, key="job-001").json()
    old_no = old["order_no"]
    assert old["reused"] is False

    client.post("/api/reset")
    # 重新上传同名册（单、双两种情况都得拒绝）
    _prepare(client)
    state_before = client.get("/api/state").json()

    r = _submit(client, key="job-001")
    assert r.status_code == 409
    assert r.json()["type"] == "ClearedIdempotencyKeyError"
    assert "已清空" in r.json()["error"]

    # 不出单：没有新订单，也没有沿用旧号。
    assert client.get("/api/orders").json() == []
    # 不改共需清单：与重试前完全一致（重新上传后尚未交印，共需为空）。
    assert client.get("/api/state").json()["shared_pool"] == state_before["shared_pool"] == []

    # 旧键在清空后永远被拒绝；同册不带键正常出单不受影响。
    assert _submit(client, key="job-001").status_code == 409
    fresh = _submit(client).json()
    assert fresh["order_no"] != old_no and fresh["reused"] is False
    assert _pool_chars(client) == {"天", "地"}


def test_retry_old_key_without_reuploading_volume_is_also_rejected(client):
    """清空后尚未重新上传该册：旧键重试也提示已清空，而不是泛泛的 404。"""
    _prepare(client)
    _submit(client, key="job-002")
    client.post("/api/reset")
    r = _submit(client, key="job-002")
    assert r.status_code == 409
    assert r.json()["type"] == "ClearedIdempotencyKeyError"


def test_retry_old_key_after_multiple_clears_stays_rejected(client):
    _prepare(client)
    _submit(client, key="job-003")
    for _ in range(3):
        client.post("/api/reset")
        _prepare(client)
    r = _submit(client, key="job-003")
    assert r.status_code == 409
    assert r.json()["type"] == "ClearedIdempotencyKeyError"


# ---------- 清空失败：原单仍可查询和复用 ----------


def test_failed_clear_keeps_orders_queryable_and_reusable(client):
    _prepare(client)
    old = _submit(client, key="job-004").json()
    old_no = old["order_no"]

    r = client.post("/api/reset", headers={"X-Fail-Clear": "1"})
    assert r.status_code == 500
    assert r.json()["type"] == "ClearFailureError"

    # 原单仍可查询、下载。
    assert client.get(f"/api/orders/{old_no}").status_code == 200
    assert client.get(f"/api/orders/{old_no}/download").status_code == 200
    # 原键仍可复用原单。
    again = _submit(client, key="job-004").json()
    assert again["order_no"] == old_no and again["reused"] is True
    # 分册与共需清单未被破坏。
    assert [v["volume"] for v in client.get("/api/state").json()["volumes"]] == ["册甲"]
    assert _pool_chars(client) == {"天", "地"}
    # 未产生清空记录。
    assert client.get("/api/state").json()["clears"] == []


# ---------- 交印与清空重叠：按生效次序 ----------


def _overlap_run(client_factory):
    """submit 与 reset 并发；结果只有两种合法线性化次序。"""
    _prepare(client_factory())
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_submit():
        with TestClient(app) as local:
            barrier.wait()
            results["submit"] = local.post("/api/volumes/册甲/submit")

    def do_clear():
        with TestClient(app) as local:
            barrier.wait()
            results["clear"] = local.post("/api/reset")

    threads = [threading.Thread(target=do_submit), threading.Thread(target=do_clear)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_submit_and_clear_overlap_is_linearizable(client):
    results = _overlap_run(lambda: client)
    sub, clear = results["submit"], results["clear"]
    assert clear.status_code == 200

    with TestClient(app) as local:
        state = local.get("/api/state").json()
        orders = local.get("/api/orders").json()

    if sub.status_code == 409:
        # 清空先生效后旧键被墓碑拦住：工位为空，无单。
        assert orders == [] and state["volumes"] == []
        return
    if sub.status_code == 404:
        # 清空先生效：该册已随清空删除，交印按“册不存在”失败。
        assert orders == [] and state["volumes"] == []
        return
    assert sub.status_code == 200

    with TestClient(app) as local:
        payload = sub.json()
        status = local.get(f"/api/orders/{payload['order_no']}").status_code

    if status == 404:
        # 交印先生效、清空后生效：旧单被删，工位为空。
        assert state["volumes"] == [] and orders == [] and state["shared_pool"] == []
    else:
        # 清空先生效、交印后生效：工位与订单都存在，共需与单号自洽。
        assert state["volumes"] and [o["order_no"] for o in orders] == [payload["order_no"]]
        assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地"}
        assert status == 200


# ---------- 重启：继续区分清空前后的单号与键 ----------


def test_restart_keeps_cleared_boundaries(client):
    _prepare(client)
    old = _submit(client, key="job-005").json()
    old_no = old["order_no"]
    client.post("/api/reset")
    _prepare(client)
    new = _submit(client).json()
    new_no = new["order_no"]
    assert new_no != old_no

    # 模拟服务重启：重新建表/升级（幂等），不改写业务数据。
    service.ensure_db()

    assert client.get(f"/api/orders/{old_no}").status_code == 404  # 旧单仍未复活
    assert client.get(f"/api/orders/{new_no}").status_code == 200  # 新单仍在
    assert _submit(client, key="job-005").status_code == 409       # 旧键仍被拒绝
    # 新世代继续按 G{世代} 递增，不复用旧号。
    nxt = _submit(client).json()  # 同用字无变化 -> 复用最近一单
    assert nxt["order_no"] == new_no and nxt["reused"] is True


# ---------- 旧库可直接升级 ----------


def _legacy_db(path: str) -> None:
    """按旧版 schema 建库并写入一单，模拟升级前的既有 SQLite 文件。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE libraries (library_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, edition TEXT NOT NULL, priority INTEGER NOT NULL,
            current INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(name, edition));
        CREATE TABLE glyphs (glyph_id INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id INTEGER NOT NULL, matrix_no TEXT NOT NULL,
            glyph_char TEXT NOT NULL, UNIQUE(library_id, matrix_no));
        CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE volume_chars (volume_id INTEGER NOT NULL, glyph_char TEXT NOT NULL,
            source_order INTEGER NOT NULL, PRIMARY KEY (volume_id, glyph_char));
        CREATE TABLE active_matrices (volume_id INTEGER NOT NULL, glyph_id INTEGER NOT NULL,
            PRIMARY KEY (volume_id, glyph_id));
        CREATE TABLE submit_seq (seq INTEGER NOT NULL);
        CREATE TABLE orders (order_no TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE,
            volume_id INTEGER NOT NULL, volume_name TEXT NOT NULL, idem_key TEXT,
            fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE UNIQUE INDEX idx_orders_idem ON orders(idem_key)
            WHERE idem_key IS NOT NULL;
        """
    )
    payload = {
        "order_no": "MO20260101-000001",
        "seq": 1,
        "volume": "旧册",
        "idem_key": "legacy-key",
        "reused": False,
        "fingerprint": "fp",
        "effective_at": "2026-01-01T00:00:00+00:00",
        "pool_before": [],
        "pool_after": [
            {"library_name": "宋韵楼", "edition": "初刻", "matrix_no": "SR-1", "glyph_char": "天"}
        ],
        "added": [
            {"library_name": "宋韵楼", "edition": "初刻", "matrix_no": "SR-1", "glyph_char": "天"}
        ],
        "removed": [],
        "volume_before": [],
        "volume_after": [
            {"library_name": "宋韵楼", "edition": "初刻", "matrix_no": "SR-1", "glyph_char": "天"}
        ],
    }
    import json

    conn.execute(
        "INSERT INTO libraries(library_id, name, edition, priority, current) "
        "VALUES (1, '宋韵楼', '初刻', 1, 1)"
    )
    conn.execute(
        "INSERT INTO glyphs(glyph_id, library_id, matrix_no, glyph_char) "
        "VALUES (1, 1, 'SR-1', '天')"
    )
    conn.execute("INSERT INTO volumes(volume_id, name) VALUES (1, '旧册')")
    conn.execute("INSERT INTO volume_chars VALUES (1, '天', 0)")
    conn.execute("INSERT INTO active_matrices VALUES (1, 1)")
    conn.execute("INSERT INTO submit_seq(seq) VALUES (1)")
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
        ("MO20260101-000001", 1, 1, "旧册", "legacy-key", "fp",
         json.dumps(payload, ensure_ascii=False), "2026-01-01 00:00:00"),
    )
    conn.commit()
    conn.close()


def test_existing_sqlite_database_upgrades_in_place(monkeypatch):
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "legacy.db")
    _legacy_db(path)
    monkeypatch.setattr(database, "DB_PATH", path)

    service.ensure_db()  # 就地升级，不重建库

    with TestClient(app) as local:
        # 库内既有单据照常查询，按键照常复用。
        r = local.get("/api/orders/MO20260101-000001")
        assert r.status_code == 200 and r.json()["volume"] == "旧册"
        assert local.get("/api/orders/MO20260101-000001/download").status_code == 200
        again = local.post(
            "/api/volumes/旧册/submit", headers={"Idempotency-Key": "legacy-key"}
        )
        assert again.status_code == 200 and again.json()["reused"] is True
        # 既有数据仍在。
        state = local.get("/api/state").json()
        assert [v["volume"] for v in state["volumes"]] == ["旧册"]
        assert {x["glyph_char"] for x in state["shared_pool"]} == {"天"}

        # 清空后旧单/旧键都按新语义失效，新一代单号与旧号不同。
        assert local.post("/api/reset").status_code == 200
        assert local.get("/api/orders/MO20260101-000001").status_code == 404
        assert (
            local.post("/api/volumes/旧册/submit", headers={"Idempotency-Key": "legacy-key"})
            .status_code == 409
        )
        assert _upload_lib(local).status_code == 200
        assert _upload_vol(local, "旧册", "天").status_code == 200
        new = local.post("/api/volumes/旧册/submit").json()
        assert new["order_no"] != "MO20260101-000001"
