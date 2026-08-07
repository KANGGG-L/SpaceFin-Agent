"""tools/cdc/consumer.py 的最小测试集：事件主体提取、影响面推导、水位与批次拉取。

零 DB：store 的反查函数（loans_by_collateral / loans_by_customer）用 monkeypatch 替换，
pymysql 连接换成录制型假连接。
"""

import cdcmods
import pytest

cdc_consumer = cdcmods.cdc_consumer


# ---------------------------------------------------------------- _payload
def test_payload_delete_uses_before():
    ev = {"event_type": "DELETE", "before_json": '{"loan_id": 3}', "after_json": '{"loan_id": 9}'}
    assert cdc_consumer._payload(ev) == {"loan_id": 3}


@pytest.mark.parametrize("etype", ["INSERT", "UPDATE"])
def test_payload_non_delete_uses_after(etype):
    ev = {"event_type": etype, "before_json": '{"loan_id": 3}', "after_json": '{"loan_id": 7}'}
    assert cdc_consumer._payload(ev) == {"loan_id": 7}


def test_payload_empty_on_missing_or_bad_json():
    assert cdc_consumer._payload({"event_type": "UPDATE", "after_json": None}) == {}
    assert cdc_consumer._payload({"event_type": "UPDATE", "after_json": "{bad json"}) == {}


# ---------------------------------------------------------------- plan_impact
def test_plan_impact_recalc_and_delete_priority(monkeypatch):
    """同一批里 loan 先 UPDATE 后 DELETE → 最终态不存在，只能出现在 deleted。"""
    monkeypatch.setattr(cdc_consumer.store, "loans_by_collateral", lambda c, ids: [])
    monkeypatch.setattr(cdc_consumer.store, "loans_by_customer", lambda c, ids: [])
    events = [
        {"table_name": "loan", "event_type": "UPDATE", "after_json": '{"loan_id": 11}'},
        {"table_name": "loan", "event_type": "UPDATE", "after_json": '{"loan_id": 22}'},
        {"table_name": "loan", "event_type": "DELETE", "before_json": '{"loan_id": 22}'},
    ]
    plan = cdc_consumer.plan_impact(events, biz_conn=None)
    assert plan["recalc"] == [11]
    assert plan["deleted"] == [22]


def test_plan_impact_collateral_customer_reverse_lookup(monkeypatch):
    """抵押物/客户主档变更 → 反查其名下所有贷款，并入重算集合。"""
    monkeypatch.setattr(cdc_consumer.store, "loans_by_collateral", lambda c, ids: [33, 44])
    monkeypatch.setattr(cdc_consumer.store, "loans_by_customer", lambda c, ids: [55])
    events = [
        {
            "table_name": "collateral",
            "event_type": "UPDATE",
            "after_json": '{"collateral_id": 501}',
        },
        {"table_name": "customer", "event_type": "UPDATE", "after_json": '{"customer_id": 101}'},
    ]
    plan = cdc_consumer.plan_impact(events, biz_conn="fake-biz")
    assert plan["recalc"] == [33, 44, 55]
    assert plan["collateral"] == [501]
    assert plan["customer"] == [101]


def test_plan_impact_skips_events_without_key(monkeypatch):
    monkeypatch.setattr(cdc_consumer.store, "loans_by_collateral", lambda c, ids: [])
    monkeypatch.setattr(cdc_consumer.store, "loans_by_customer", lambda c, ids: [])
    events = [
        {"table_name": "loan", "event_type": "UPDATE", "after_json": "{}"},
        {"table_name": "collateral", "event_type": "DELETE", "before_json": "{}"},
        {"table_name": "customer", "event_type": "UPDATE", "after_json": '{"customer_id": null}'},
    ]
    plan = cdc_consumer.plan_impact(events, biz_conn=None)
    assert plan["recalc"] == []
    assert plan["deleted"] == []
    assert plan["collateral"] == []
    assert plan["customer"] == []


def test_plan_impact_sorted_dedup(monkeypatch):
    monkeypatch.setattr(cdc_consumer.store, "loans_by_collateral", lambda c, ids: [5, 3])
    monkeypatch.setattr(cdc_consumer.store, "loans_by_customer", lambda c, ids: [])
    events = [
        {
            "table_name": "collateral",
            "event_type": "UPDATE",
            "after_json": '{"collateral_id": 501}',
        },
        {
            "table_name": "collateral",
            "event_type": "UPDATE",
            "after_json": '{"collateral_id": 502}',
        },
    ]
    plan = cdc_consumer.plan_impact(events, biz_conn=None)
    assert plan["collateral"] == [501, 502]
    assert plan["recalc"] == [3, 5]  # 反查结果并入后去重升序


# ---------------------------------------------------------------- 水位（offset）
def test_read_offset_default_zero(conn):
    conn.when("SELECT last_id FROM ods_cdc_consumer_offset", [])
    assert cdc_consumer.read_offset(conn) == 0


def test_read_offset_value(conn):
    conn.when("SELECT last_id FROM ods_cdc_consumer_offset", [(123,)])
    assert cdc_consumer.read_offset(conn) == 123


def test_write_offset_upsert(conn):
    cdc_consumer.write_offset(conn, 456)
    sql, params = conn.find_sql("INSERT INTO ods_cdc_consumer_offset")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params == (cdc_consumer.CONSUMER_NAME, 456)
    assert conn.commits == 1


# ---------------------------------------------------------------- fetch_events
def test_fetch_events_returns_dicts(conn):
    conn.when(
        "FROM ods_cdc_log WHERE id >",
        [(7, "loan", "UPDATE", None, '{"loan_id": 1}', "2026-08-05 00:00:00")],
        columns=["id", "table_name", "event_type", "before_json", "after_json", "cdc_ts"],
    )
    rows = cdc_consumer.fetch_events(conn, after_id=5, limit=100)
    assert rows == [
        {
            "id": 7,
            "table_name": "loan",
            "event_type": "UPDATE",
            "before_json": None,
            "after_json": '{"loan_id": 1}',
            "cdc_ts": "2026-08-05 00:00:00",
        }
    ]


def test_fetch_events_sql_asc_order_and_params(conn):
    conn.when(
        "FROM ods_cdc_log WHERE id >",
        [],
        columns=["id", "table_name", "event_type", "before_json", "after_json", "cdc_ts"],
    )
    cdc_consumer.fetch_events(conn, after_id=5, limit=100)
    sql, params = conn.find_sql("FROM ods_cdc_log WHERE id >")
    assert "ORDER BY id ASC" in sql
    assert "LIMIT %s" in sql
    assert params == (5, 100)


# ---------------------------------------------------------------- 幂等建表
def test_ensure_offset_table_idempotent_ddl(conn):
    cdc_consumer._ensure_offset_table(conn)
    sql, _ = conn.find_sql("CREATE TABLE IF NOT EXISTS ods_cdc_consumer_offset")
    assert sql is not None
    assert conn.commits == 1
