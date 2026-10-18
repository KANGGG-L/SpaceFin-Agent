#!/usr/bin/env python
"""ODS 初始快照补数（bootstrap backfill，S1 验收用）。

背景：ODS(ods_cdc_log) 是 CDC 变更日志，只记录源库 loan/collateral/customer 的
变更事件，不存当前快照。本仓库 demo 数据由批处理（tools/risk/main.py）直接灌入 DWS，
从未经过 binlog，所以 ods_cdc_log 一直是空的——这是「源库静止、无变更流入」的
正确态，而非故障。

本脚本做「初始全量快照」：把当前 spacefin 业务库三张主表的现状，合成成 INSERT 事件
写入 ods_cdc_log（before_json=NULL, after_json=当前行）。随后
`tools/cdc/consumer.py --once` 会把这些事件重放到 DWS/ADS（幂等 UPSERT/REPLACE），
既让 ODS 非空，也端到端验证下游消费链路。重放结果与现有批处理数据应一致，可互证。

快照后把 ods_cdc_position 设到当前主库 binlog 位点，使未来若真正起 main.py 捕获，
从「此刻」续读、不会重放历史 binlog。

用法：
    python tools/cdc/bootstrap_ods.py            # ods_cdc_log 非空则跳过（幂等保护）
    python tools/cdc/bootstrap_ods.py --force   # 先清空 ods_cdc_log 再重补
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

import pymysql
from pymysql.cursors import DictCursor

_RISK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk")
sys.path.insert(0, _RISK_DIR)

import config  # noqa: E402

LOG_TABLE = "ods_cdc_log"
POSITION_TABLE = "ods_cdc_position"
OFFSET_TABLE = "ods_cdc_consumer_offset"
CONSUMER_NAME = "risk_downstream"
TABLES = ["loan", "collateral", "customer"]


def _sanitize(o):
    """把库行值递归转成可 JSON 序列化的原生类型（与 tools/cdc/main.py 同口径）。"""
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "ignore")
    if isinstance(o, dict):
        return {str(k): _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(x) for x in o]
    return o


def _row_count(conn, table):
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    finally:
        cur.close()


def bootstrap(env: dict, force: bool) -> dict:
    biz = pymysql.connect(**config.business_params(env), charset="utf8mb4")
    root = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
    try:
        # 幂等保护：非空且不强制则跳过，避免重复灌入导致事件翻倍。
        cur = root.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {LOG_TABLE}")
        existing = int(cur.fetchone()[0])
        if existing and not force:
            return {
                "skipped": True,
                "existing_events": existing,
                "hint": "ods_cdc_log 已非空，加 --force 可清空后重补",
            }

        if force and existing:
            cur.execute(f"DELETE FROM {LOG_TABLE}")
            cur.execute(f"DELETE FROM {OFFSET_TABLE} WHERE consumer=%s", (CONSUMER_NAME,))
            root.commit()

        # 逐表把当前全量现状写成 INSERT 事件（before_json=NULL）。
        # executemany 批量写，44k 行级也在秒级；after_json 用默认 str 兜底序列化。
        total = 0
        per_table = {}
        for table in TABLES:
            bcur = biz.cursor(DictCursor)
            try:
                bcur.execute(f"SELECT * FROM {table}")
                rows = bcur.fetchall()
            finally:
                bcur.close()
            if not rows:
                per_table[table] = 0
                continue
            payload = [
                (
                    table,
                    "INSERT",
                    None,
                    json.dumps(_sanitize(dict(r)), ensure_ascii=False, default=str),
                )
                for r in rows
            ]
            cur.executemany(
                f"INSERT INTO {LOG_TABLE} (table_name, event_type, before_json, after_json) "
                "VALUES (%s,%s,%s,%s)",
                payload,
            )
            root.commit()
            per_table[table] = len(rows)
            total += len(rows)

        # 位点设到当前主库头：未来真正起 main.py 时从「此刻」续读，不重放历史 binlog。
        cur.execute("SHOW MASTER STATUS")
        m = cur.fetchone()
        if m:
            cur.execute(
                f"INSERT INTO {POSITION_TABLE} (repl_key, log_file, log_pos) VALUES ('binlog', %s, %s) "
                f"ON DUPLICATE KEY UPDATE log_file=VALUES(log_file), log_pos=VALUES(log_pos)",
                (m[0], int(m[1])),
            )
            root.commit()

        return {"skipped": False, "total_events": total, "per_table": per_table}
    finally:
        biz.close()
        root.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="ODS 初始快照补数（写 ods_cdc_log）")
    ap.add_argument("--force", action="store_true", help="先清空 ods_cdc_log 再重补")
    args = ap.parse_args()
    env = config.load_env()
    res = bootstrap(env, args.force)
    print(f"[bootstrap-ods] {json.dumps(res, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    main()
