#!/usr/bin/env python
"""SpaceFin CDC → Kafka 实时转发 producer（Workflow B：Kafka + Flink 实时接入）。

把 ODS 变更事件（ods_cdc_log）增量转发到 Kafka topic，供 Flink 实时作业消费。

与 tools/cdc/consumer.py 的关系：后者把变更「解释成 DWS/ADS 增量重算」；本模块只做
「贴源转发」——事件语义的解释全部交给下游（Flink SQL 里的过滤 / join / 预警判定），
两个常驻进程水位互相独立，谁都不用等谁。

水位：复用 tools/cdc 的 ods_cdc_consumer_offset 表，consumer 名取 kafka_stream_producer，
与 risk_downstream 互不干扰。以 ods_cdc_log.id（自增）为水位按 id 升序拉取 + 单向推进
= 至少一次（at-least-once）。重复消费无害：下游预警表以 event_id 为主键，天然幂等。

消息格式（JSON，顶层平铺 + payload 全量）：
    {
      "event_id": 2208, "table_name": "loan", "event_type": "UPDATE",
      "cdc_ts": "2026-08-05 00:00:00", "biz_date": "2026-08-05",   # 业务日，Asia/Shanghai
      "loan_id": 30001, "customer_id": 10001, "collateral_id": 20001,
      "balance": 1500000.00, "interest_rate": 5.55, "risk_class": "正常",
      "payload": { ...after_json 全量... }
    }

用法：
    python tools/stream/producer.py --once                 # 转发当前积压后退出（验收用）
    python tools/stream/producer.py --loop --interval 2    # 常驻轮询（生产用）
    python tools/stream/producer.py --once --from-offset 0 # 忽略水位从头重放（对账用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pymysql
from kafka import KafkaProducer

_RISK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk")
sys.path.insert(0, _RISK_DIR)

import config  # noqa: E402  (tools/risk 模块，须在 sys.path 注入之后导入)

OFFSET_TABLE = "ods_cdc_consumer_offset"
CONSUMER_NAME = "kafka_stream_producer"
LOG_TABLE = "ods_cdc_log"
KAFKA_BROKERS = os.getenv("SPACEFIN_KAFKA_BROKERS", "localhost:9092")
DEFAULT_TOPIC = "spacefin.cdc.log"


def _ensure_offset_table(conn) -> None:
    """幂等建水位表（与 tools/cdc 共用一张表，DDL 需 root）。"""
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {OFFSET_TABLE} (
            consumer VARCHAR(64) PRIMARY KEY,
            last_id BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def read_offset(conn) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT last_id FROM {OFFSET_TABLE} WHERE consumer=%s", (CONSUMER_NAME,))
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row else 0


def write_offset(conn, last_id: int) -> None:
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {OFFSET_TABLE} (consumer, last_id) VALUES (%s,%s) "
        f"ON DUPLICATE KEY UPDATE last_id=VALUES(last_id)",
        (CONSUMER_NAME, last_id),
    )
    conn.commit()
    cur.close()


def fetch_events(conn, after_id: int, limit: int) -> list[dict]:
    """按 id 升序取未转发事件。升序 + 单向水位保证不重不漏（至少一次语义）。"""
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, table_name, event_type, before_json, after_json, cdc_ts "
        f"FROM {LOG_TABLE} WHERE id > %s ORDER BY id ASC LIMIT %s",
        (after_id, limit),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return rows


def build_message(ev: dict, biz_date: str) -> dict:
    """把一条 ODS 事件转成 Kafka 消息。

    DELETE 用 before_json 取主体，其余用 after_json（与 tools/cdc 的取法一致）。
    loan 的常用字段平铺到顶层，方便 Flink SQL 的 json format 直接引用，不用再嵌套解析。
    """
    raw = ev["before_json"] if ev["event_type"] == "DELETE" else ev["after_json"]
    try:
        body = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        body = {}

    msg = {
        "event_id": ev["id"],
        "table_name": ev["table_name"],
        "event_type": ev["event_type"],
        "cdc_ts": str(ev["cdc_ts"]),
        "biz_date": biz_date,
        "payload": body,
    }
    if ev["table_name"] == "loan":
        # Decimal 不是 JSON 原生类型，先转 float；None 保留（表示字段缺失）。
        msg.update(
            {
                "loan_id": body.get("loan_id"),
                "customer_id": body.get("customer_id"),
                "collateral_id": body.get("collateral_id"),
                "balance": float(body["balance"]) if body.get("balance") is not None else None,
                "interest_rate": (
                    float(body["interest_rate"]) if body.get("interest_rate") is not None else None
                ),
                "risk_class": body.get("risk_class"),
            }
        )
    return msg


def publish_batch(producer: KafkaProducer, events: list[dict], topic: str, biz_date: str) -> int:
    """逐条发送并 flush；全部成功后返回本批条数。发送异常由调用方处理（不推进水位）。"""
    for ev in events:
        msg = build_message(ev, biz_date)
        key = f"{ev['table_name']}:{msg.get('loan_id') or ev['id']}".encode()
        producer.send(topic, key=key, value=json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    producer.flush(timeout=30)
    return len(events)


def forward_once(env: dict, topic: str, batch: int, from_offset: int | None = None) -> dict:
    """转发一批事件；返回本批摘要（无事件时 events=0，不动水位）。"""
    conn = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
    producer = KafkaProducer(bootstrap_servers=KAFKA_BROKERS, acks="all")
    try:
        _ensure_offset_table(conn)
        offset = read_offset(conn) if from_offset is None else from_offset
        events = fetch_events(conn, offset, batch)
        if not events:
            return {"events": 0, "offset": offset}

        biz_date = config.business_date()
        n = publish_batch(producer, events, topic, biz_date)
        last_id = events[-1]["id"]
        write_offset(conn, last_id)
        return {
            "events": n,
            "offset_from": offset,
            "offset": last_id,
            "topic": topic,
            "biz_date": biz_date,
        }
    finally:
        producer.close(timeout=5)
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="CDC ODS 变更 → Kafka topic 增量转发")
    ap.add_argument("--once", action="store_true", help="转发一批后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询（生产用）")
    ap.add_argument("--interval", type=float, default=2.0, help="--loop 的轮询间隔秒")
    ap.add_argument("--batch", type=int, default=500, help="单批最多转发的事件数")
    ap.add_argument("--topic", default=DEFAULT_TOPIC, help="目标 Kafka topic")
    ap.add_argument(
        "--from-offset",
        type=int,
        default=None,
        help="忽略水位表，从指定 id 之后重放（对账用；下游按 event_id 幂等）",
    )
    args = ap.parse_args()

    env = config.load_env()

    if not args.loop:
        res = forward_once(env, args.topic, args.batch, args.from_offset)
        print(f"[stream-producer] {json.dumps(res, ensure_ascii=False)}", flush=True)
        return

    print(
        f"[stream-producer] loop start interval={args.interval}s batch={args.batch} "
        f"topic={args.topic} brokers={KAFKA_BROKERS}",
        flush=True,
    )
    from_offset = args.from_offset
    while True:
        try:
            res = forward_once(env, args.topic, args.batch, from_offset)
            from_offset = None  # --from-offset 只在首轮生效，之后走水位表
            if res.get("events"):
                print(f"[stream-producer] {json.dumps(res, ensure_ascii=False)}", flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因单批异常退出，等下一轮重试
            print(f"[stream-producer] batch error: {exc}", flush=True)
        time.sleep(args.interval)
    print("[stream-producer] stopped", flush=True)


if __name__ == "__main__":
    main()
