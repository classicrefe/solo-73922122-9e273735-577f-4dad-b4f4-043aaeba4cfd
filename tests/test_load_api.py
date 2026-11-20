"""负荷核算 API 测试：发布原件 -> 核算 -> 查询 -> 重算冲突 -> 失败保留旧结果。"""
from __future__ import annotations

import io
import sqlite3
import threading

import pytest

from app import main as main_mod


FLOW_CSV = (
    b"start,end,volume_m3,max_flow_m3s\n"
    b"00:00,12:00,1800,1\n"
    b"12:00,24:00,1800,1\n"
)
CONC_CSV = (
    b"start,end,conc_low_mg_l,conc_high_mg_l\n"
    b"00:00,24:00,2,4\n"
)


def _publish(client, station, day, order, data):
    return client.post(
        "/releases",
        data={"station_id": station, "sample_date": day, "order_no": order},
        files={"file": ("x.csv", io.BytesIO(data), "text/csv")},
    )


def _publish_pair(client, station="ST-LD", day="2026-11-20",
                  flow_order="FLOW-1", conc_order="CONC-1",
                  flow_data=FLOW_CSV, conc_data=CONC_CSV):
    r1 = _publish(client, station, day, flow_order, flow_data)
    r2 = _publish(client, station, day, conc_order, conc_data)
    assert r1.status_code == 201 and r2.status_code == 201
    return flow_order, conc_order


def _compute(client, station, day, flow_order, conc_order, expected_id=None):
    body = {
        "station_id": station,
        "sample_date": day,
        "flow_order_no": flow_order,
        "conc_order_no": conc_order,
    }
    if expected_id is not None:
        body["expected_id"] = expected_id
    return client.post("/load-results", json=body)


def test_first_compute_and_query_shape(app_client):
    client, _, _ = app_client
    f, c = _publish_pair(client, station="ST-Q1")
    r = _compute(client, "ST-Q1", "2026-11-20", f, c)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["flow_order_no"] == f and body["conc_order_no"] == c
    assert len(body["hours"]) == 24
    assert body["hours"][0]["hour"] == 0
    assert set(body["hours"][0]) == {
        "hour", "window_start", "window_end", "load_low_g", "load_high_g",
    }
    # 全天总水量 3600 m³、浓度 [2,4] -> 全日 [7200, 14400] g。
    assert body["day"] == {"load_low_g": 7200.0, "load_high_g": 14400.0}
    # 两份原件发布单号与摘要都在查询响应中。
    assert len(body["flow_blob_sha256"]) == 64 and len(body["conc_blob_sha256"]) == 64

    got = client.get("/load-results/ST-Q1/2026-11-20").json()
    assert got["id"] == body["id"] and got["hours"] == body["hours"]
    listed = client.get("/load-results").json()
    assert any(x["id"] == body["id"] for x in listed)


def test_recompute_optimistic_concurrency_and_revisions(app_client):
    client, _, _ = app_client
    f, c = _publish_pair(client, station="ST-CC")
    r1 = _compute(client, "ST-CC", "2026-11-20", f, c)
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    # 首次语义已不成立：留空 expected_id 再提 -> 409，现行编号回传。
    r = _compute(client, "ST-CC", "2026-11-20", f, c)
    assert r.status_code == 409 and r.json()["detail"]["current_id"] == id1

    # 携带现行编号重算 -> 新编号，旧版本保留。
    r2 = _compute(client, "ST-CC", "2026-11-20", f, c, expected_id=id1)
    assert r2.status_code == 201 and r2.json()["id"] == id1 + 1

    # 过期编号 -> 409，现行结果不变。
    r = _compute(client, "ST-CC", "2026-11-20", f, c, expected_id=id1)
    assert r.status_code == 409 and r.json()["detail"]["current_id"] == id1 + 1
    assert client.get("/load-results/ST-CC/2026-11-20").json()["id"] == id1 + 1

    revisions = client.get("/load-results/ST-CC/2026-11-20/revisions").json()
    assert [x["id"] for x in revisions] == [id1, id1 + 1]
    assert revisions[0]["is_current"] is False
    assert revisions[1]["is_current"] is True


def test_concurrent_recompute_one_wins_one_conflicts(app_client):
    """两个请求同时携带同一旧编号重算：恰一个成功，另一个 409，不产生两个新版本。"""
    client, app, _ = app_client
    f, c = _publish_pair(client, station="ST-RACE")
    base = _compute(client, "ST-RACE", "2026-11-20", f, c).json()
    old_id = base["id"]

    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        # 直接调用持久层，绕开事件循环以真正并发冲击 BEGIN IMMEDIATE。
        barrier.wait()
        try:
            main_mod.database.save_load_result(
                "ST-RACE", "2026-11-20", f, c,
                base["flow_blob_sha256"], base["conc_blob_sha256"],
                base["hours"], base["day"], old_id,
            )
            results.append("ok")
        except Exception as exc:  # StaleRevisionError
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["StaleRevisionError", "ok"]
    current = client.get("/load-results/ST-RACE/2026-11-20").json()
    assert current["id"] == old_id + 1  # 只插入一个新版本
    revisions = client.get("/load-results/ST-RACE/2026-11-20/revisions").json()
    assert [x["id"] for x in revisions] == [old_id, old_id + 1]


def test_missing_measurement_rejected_and_old_result_kept(app_client):
    client, _, _ = app_client
    f, c = _publish_pair(client, station="ST-GAP")
    r = _compute(client, "ST-GAP", "2026-11-20", f, c)
    assert r.status_code == 201
    kept_id = r.json()["id"]

    # 缺测流量原件：只覆盖半天 -> 422 且指出缺口时段。
    gap_flow = b"start,end,volume_m3,max_flow_m3s\n00:00,12:00,1800,1\n"
    _publish(client, "ST-GAP", "2026-11-20", "FLOW-GAP", gap_flow)
    r = _compute(client, "ST-GAP", "2026-11-20", "FLOW-GAP", c, expected_id=kept_id)
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"]
    assert any("未覆盖全天" in p and "12:00:00~24:00:00" in p for p in problems)

    # 容量矛盾原件：同样 422。
    bad_cap = (
        b"start,end,volume_m3,max_flow_m3s\n"
        b"00:00,01:00,3601,1\n01:00,24:00,0,0\n"
    )
    _publish(client, "ST-GAP", "2026-11-20", "FLOW-CAP", bad_cap)
    r = _compute(client, "ST-GAP", "2026-11-20", "FLOW-CAP", c, expected_id=kept_id)
    assert r.status_code == 422
    assert any("超过上限容量" in p for p in r.json()["detail"]["problems"])

    # 有水缺浓度原件。
    wet = (
        b"start,end,volume_m3,max_flow_m3s\n"
        b"00:00,01:00,100,1\n01:00,24:00,0,0\n"
    )
    half_conc = b"start,end,conc_low_mg_l,conc_high_mg_l\n00:00,00:30,1,2\n"
    _publish(client, "ST-GAP", "2026-11-20", "FLOW-WET", wet)
    _publish(client, "ST-GAP", "2026-11-20", "CONC-HALF", half_conc)
    r = _compute(
        client, "ST-GAP", "2026-11-20", "FLOW-WET", "CONC-HALF", expected_id=kept_id
    )
    assert r.status_code == 422
    assert any("缺浓度" in p for p in r.json()["detail"]["problems"])

    # 三次拒绝后现行结果仍是最初版本，历史无追加。
    current = client.get("/load-results/ST-GAP/2026-11-20").json()
    assert current["id"] == kept_id
    revisions = client.get("/load-results/ST-GAP/2026-11-20/revisions").json()
    assert [x["id"] for x in revisions] == [kept_id]


def test_unpublished_order_and_input_validation(app_client):
    client, _, _ = app_client
    f, c = _publish_pair(client, station="ST-404")
    # 未发布单号 -> 404（先查流量、再查浓度，各自独立）。
    r = _compute(client, "ST-404", "2026-11-20", "MISSING", c)
    assert r.status_code == 404 and "流量" in r.json()["detail"]["message"]
    r = _compute(client, "ST-404", "2026-11-20", f, "MISSING")
    assert r.status_code == 404 and "浓度" in r.json()["detail"]["message"]

    # 入参形态校验。
    r = client.post(
        "/load-results",
        json={"station_id": "ST-404", "sample_date": "2026-02-30",
              "flow_order_no": f, "conc_order_no": c},
    )
    assert r.status_code == 422
    r = client.post(
        "/load-results",
        json={"station_id": "ST-404", "sample_date": "2026-11-20",
              "flow_order_no": f, "conc_order_no": c, "expected_id": 0},
    )
    assert r.status_code == 422

    # 从未核算的站日查询 -> 404。
    assert client.get("/load-results/ST-404/2026-11-19").status_code == 404
    assert client.get("/load-results/ST-404/2026-11-19/revisions").status_code == 404


def test_save_failure_rolls_back_and_keeps_previous_result(app_client, monkeypatch):
    """保存阶段抛错（如卷上数据库瞬时不可写）时事务回滚，旧结果保留。"""
    client, _, _ = app_client
    f, c = _publish_pair(client, station="ST-SAVE")
    first = _compute(client, "ST-SAVE", "2026-11-20", f, c).json()
    old_id = first["id"]

    # 用连接包装器在 INSERT INTO load_results 上注入磁盘故障，
    # 验证 _save_load_result_once 的 except 分支确实 rollback 且不留半截行。
    real_connect = main_mod.database.connect
    inject = {"on": True}

    class BrokenConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, parameters=()):
            if (
                inject["on"]
                and isinstance(sql, str)
                and sql.lstrip().startswith("INSERT INTO load_results")
            ):
                raise sqlite3.OperationalError("disk I/O error (simulated)")
            return self._inner.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def broken_connect():
        ctx = real_connect()
        conn = ctx.__enter__()

        class _Ctx:
            def __enter__(self_inner):
                return BrokenConn(conn)

            def __exit__(self_inner, *exc):
                return ctx.__exit__(*exc)

        return _Ctx()

    monkeypatch.setattr(main_mod.database, "connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        main_mod.database.save_load_result(
            "ST-SAVE", "2026-11-20", f, c,
            first["flow_blob_sha256"], first["conc_blob_sha256"],
            first["hours"], first["day"], old_id,
        )
    inject["on"] = False
    monkeypatch.undo()

    # 现行结果仍是旧版本，历史无追加。
    current = client.get("/load-results/ST-SAVE/2026-11-20").json()
    assert current["id"] == old_id
    revisions = client.get(
        "/load-results/ST-SAVE/2026-11-20/revisions"
    ).json()
    assert [x["id"] for x in revisions] == [old_id]

    # 故障恢复后同编号可正常重算。
    r = _compute(client, "ST-SAVE", "2026-11-20", f, c, expected_id=old_id)
    assert r.status_code == 201 and r.json()["id"] == old_id + 1


def test_hourly_bounds_partial_coverage_segments(app_client):
    """多段流量 + 分段浓度的小时级数值回归。"""
    client, _, _ = app_client
    flow = (
        b"start,end,volume_m3,max_flow_m3s\n"
        b"00:00,01:00,1800,1\n"          # 容量 3600，水量 1800
        b"01:00,12:00,1800,0.5\n"        # 11 小时容量 19800
        b"12:00,24:00,0,0\n"
    )
    conc = (
        b"start,end,conc_low_mg_l,conc_high_mg_l\n"
        b"00:00,01:00,1,3\n"
        b"01:00,12:00,2,5\n"
        b"12:00,24:00,0,0\n"
    )
    f, c = _publish_pair(
        client, station="ST-MULTI", flow_data=flow, conc_data=conc
    )
    body = _compute(client, "ST-MULTI", "2026-11-20", f, c).json()
    h0 = body["hours"][0]
    # 小时 0：w_max=min(1800,3600)=1800，上界 1800×3=5400；
    # w_min=max(0,1800-0)=1800（段外无容量），下界 1800×1=1800。
    assert h0["load_low_g"] == 1800 and h0["load_high_g"] == 5400
    h1 = body["hours"][1]
    # 小时 1 属第二段：w_max=min(1800, 0.5×3600=1800)=1800 -> 上界 1800×5=9000；
    # w_min=max(0, 1800-0.5×36000)=0 -> 下界 0。
    assert h1["load_low_g"] == 0 and h1["load_high_g"] == 9000
    # 干枯段小时全部为 0。
    assert all(
        h["load_low_g"] == 0 and h["load_high_g"] == 0
        for h in body["hours"][12:]
    )
    # 全日：总水量 3600；下界 1800×1 + 1800×2 = 5400，上界 1800×3 + 1800×5 = 14400。
    assert body["day"] == {"load_low_g": 5400.0, "load_high_g": 14400.0}
