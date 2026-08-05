"""tools/stream/producer.py 的最小测试集：消息序列化、批量转发、水位推进。

零 DB / 零 Kafka：pymysql 连接与 KafkaProducer 全部由 monkeypatch 替换为录制型假实现。
"""

import json

import streammods

producer = streammods.producer


def _loan_event(id_=2208, etype="UPDATE", **overrides):
    body = {
        "loan_id": 30001,
        "customer_id": 10001,
        "collateral_id": 20001,
        "balance": 1500000.00,
        "interest_rate": 5.55,
        "risk_class": "正常",
    }
    body.update(overrides)
    return {
        "id": id_,
        "table_name": "loan",
        "event_type": etype,
        "cdc_ts": "2026-08-05 00:00:00",
        "before_json": None,
        "after_json": json.dumps(body, ensure_ascii=False),
    }


# ---------------------------------------------------------------- build_message
def test_build_message_loan_flattens_top_level_fields():
    msg = producer.build_message(_loan_event(), "2026-08-05")
    assert msg["event_id"] == 2208
    assert msg["table_name"] == "loan"
    assert msg["event_type"] == "UPDATE"
    assert msg["cdc_ts"] == "2026-08-05 00:00:00"
    assert msg["biz_date"] == "2026-08-05"
    # 常用字段平铺到顶层，Flink SQL 的 json format 可直接引用
    assert msg["loan_id"] == 30001
    assert msg["customer_id"] == 10001
    assert msg["collateral_id"] == 20001
    assert msg["balance"] == 1500000.0  # Decimal → float
    assert msg["interest_rate"] == 5.55
    assert msg["risk_class"] == "正常"
    assert msg["payload"]["loan_id"] == 30001  # 全量保留在 payload


def test_build_message_delete_uses_before_payload():
    ev = _loan_event(etype="DELETE", loan_id=9)
    ev["before_json"] = ev["after_json"]
    ev["after_json"] = None
    msg = producer.build_message(ev, "2026-08-05")
    assert msg["payload"] == json.loads(ev["before_json"])
    assert msg["loan_id"] == 9


def test_build_message_non_loan_no_flattening():
    ev = {
        "id": 2,
        "table_name": "collateral",
        "event_type": "UPDATE",
        "cdc_ts": "x",
        "before_json": None,
        "after_json": json.dumps({"collateral_id": 501}),
    }
    msg = producer.build_message(ev, "2026-08-05")
    assert "loan_id" not in msg
    assert msg["payload"] == {"collateral_id": 501}


def test_build_message_null_balance_and_bad_json():
    ev = _loan_event(loan_id=5, balance=None, interest_rate=None, risk_class=None)
    msg = producer.build_message(ev, "2026-08-05")
    assert msg["balance"] is None
    assert msg["interest_rate"] is None
    assert msg["risk_class"] is None

    bad = {
        "id": 4,
        "table_name": "loan",
        "event_type": "UPDATE",
        "cdc_ts": "x",
        "before_json": None,
        "after_json": "{bad",
    }
    msg2 = producer.build_message(bad, "2026-08-05")
    assert msg2["payload"] == {}
    assert msg2["loan_id"] is None


# ---------------------------------------------------------------- publish_batch
class RecordingProducer:
    def __init__(self):
        self.sent = []  # [(topic, key, value)]
        self.flushes = 0

    def send(self, topic, key=None, value=None):
        self.sent.append((topic, key, value))
        return self

    def flush(self, timeout=None):
        self.flushes += 1


def test_publish_batch_sends_flushes_and_returns_count():
    prod = RecordingProducer()
    n = producer.publish_batch(
        prod, [_loan_event(id_=10, loan_id=42)], "spacefin.cdc.log", "2026-08-05"
    )
    assert n == 1
    assert prod.flushes == 1
    topic, key, value = prod.sent[0]
    assert topic == "spacefin.cdc.log"
    assert key == b"loan:42"  # 分区键：table:loan_id
    msg = json.loads(value.decode("utf-8"))
    assert msg["event_id"] == 10
    assert msg["balance"] == 1500000.0


def test_publish_batch_key_falls_back_to_event_id(monkeypatch):
    prod = RecordingProducer()
    ev = _loan_event(id_=10, loan_id=None)
    producer.publish_batch(prod, [ev], "t", "2026-08-05")
    assert prod.sent[0][1] == b"loan:10"


# ---------------------------------------------------------------- 水位 / 拉取
def test_read_offset_default_zero(conn):
    conn.when("SELECT last_id FROM ods_cdc_consumer_offset", [])
    assert producer.read_offset(conn) == 0


def test_write_offset_upsert(conn):
    producer.write_offset(conn, 456)
    sql, params = conn.find_sql("INSERT INTO ods_cdc_consumer_offset")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params == (producer.CONSUMER_NAME, 456)
    assert conn.commits == 1


def test_fetch_events_asc_order(conn):
    conn.when(
        "FROM ods_cdc_log WHERE id >",
        [],
        columns=["id", "table_name", "event_type", "before_json", "after_json", "cdc_ts"],
    )
    producer.fetch_events(conn, after_id=5, limit=100)
    sql, params = conn.find_sql("FROM ods_cdc_log WHERE id >")
    assert "ORDER BY id ASC" in sql
    assert "LIMIT %s" in sql
    assert params == (5, 100)


def test_ensure_offset_table_idempotent_ddl(conn):
    producer._ensure_offset_table(conn)
    sql, _ = conn.find_sql("CREATE TABLE IF NOT EXISTS ods_cdc_consumer_offset")
    assert sql is not None
    assert conn.commits == 1


# ---------------------------------------------------------------- forward_once
def test_forward_once_empty_batch_no_offset_move(monkeypatch, conn):
    monkeypatch.setattr(producer.pymysql, "connect", lambda **k: conn)
    conn.when("SELECT last_id FROM ods_cdc_consumer_offset", [(77,)])
    conn.when(
        "FROM ods_cdc_log WHERE id >",
        [],
        columns=["id", "table_name", "event_type", "before_json", "after_json", "cdc_ts"],
    )  # 无积压

    class NoopProducer:
        def __init__(self, **k):
            pass

        def close(self, timeout=None):
            pass

    monkeypatch.setattr(producer, "KafkaProducer", NoopProducer)
    res = producer.forward_once({}, "spacefin.cdc.log", 500)
    assert res == {"events": 0, "offset": 77}
    assert conn.find_sql("INSERT INTO ods_cdc_consumer_offset") is None  # 未推进水位


def test_forward_once_publishes_and_advances_offset(monkeypatch, conn):
    monkeypatch.setattr(producer.pymysql, "connect", lambda **k: conn)
    monkeypatch.setattr(producer.config, "business_date", lambda: "2026-08-05")
    conn.when("SELECT last_id FROM ods_cdc_consumer_offset", [(0,)])
    conn.when(
        "FROM ods_cdc_log WHERE id >",
        [(7, "loan", "UPDATE", None, '{"loan_id": 1, "balance": 100}', "2026-08-05 00:00:00")],
        columns=["id", "table_name", "event_type", "before_json", "after_json", "cdc_ts"],
    )

    sent = []

    class RecordingKafka:
        def __init__(self, **k):
            pass

        def send(self, topic, key=None, value=None):
            sent.append((topic, key, value))
            return self

        def flush(self, timeout=None):
            pass

        def close(self, timeout=None):
            pass

    monkeypatch.setattr(producer, "KafkaProducer", RecordingKafka)
    res = producer.forward_once({}, "spacefin.cdc.log", 500)
    assert res["events"] == 1
    assert res["offset_from"] == 0
    assert res["offset"] == 7
    assert res["topic"] == "spacefin.cdc.log"
    assert res["biz_date"] == "2026-08-05"
    assert len(sent) == 1
    sql, params = conn.find_sql("INSERT INTO ods_cdc_consumer_offset")
    assert params == ("kafka_stream_producer", 7)
