#!/usr/bin/env python
"""SpaceFin CDC 下游消费链：ODS 变更 → DWD/DWS/ADS 增量同步（S1 收尾）。

链路定位（与 tools/cdc/main.py 的分工）：
    binlog ──main.py──▶ ODS(data_lake/cdc/ods + ods_cdc_log) ──consumer.py──▶ DWS/ADS
main.py 只负责「贴源、不解释」；本模块负责「解释变更、驱动下游」。两者解耦的原因是
binlog 读取必须单点常驻（server_id 唯一、位点连续），而下游消费要能重放、能补跑、能
按批调度——耦在一起就无法在不重读 binlog 的前提下重算下游。

消费语义：
- 水位：spacefin_crawler.ods_cdc_consumer_offset 记录已消费到的 ods_cdc_log.id。
  以自增 id 而非时间戳做水位，避免同秒多事件被跳过。
- 影响面推导（这是增量的关键，不做全量重算）：
    loan       变更 → 该 loan_id
    collateral 变更 → 挂在该抵押物上的所有 loan_id（估值变了，LTV 全变）
    customer   变更 → 该客户名下所有 loan_id
- DELETE 事件：loan 被删 → 清掉 DWS 明细与当日预警；collateral/customer 被删 → 其名下
  贷款按「无抵押物」保守重算（risk_engine 已处理 collateral=None）。
- ads_risk_class 汇总用 SQL 从 dws_risk_class 现状聚合刷新，占比分母始终是全量。

用法：
    python tools/cdc/consumer.py --once                 # 消费一批后退出（调度/验收用）
    python tools/cdc/consumer.py --loop --interval 20   # 常驻轮询（systemd 用）
    python tools/cdc/consumer.py --once --from-offset 0 # 从头重放（对账用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pymysql

_RISK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk")
sys.path.insert(0, _RISK_DIR)

import config  # noqa: E402  (tools/risk 模块，须在 sys.path 注入之后导入)
import store  # noqa: E402
import valuation  # noqa: E402

OFFSET_TABLE = "ods_cdc_consumer_offset"
CONSUMER_NAME = "risk_downstream"
LOG_TABLE = "ods_cdc_log"


def _ensure_offset_table(conn) -> None:
    """幂等建水位表（DDL 需 root）。"""
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
    """按 id 升序取未消费事件。升序 + 单向水位保证不重不漏（至少一次语义，重算幂等）。"""
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


def _payload(ev: dict) -> dict:
    """取事件主体：DELETE 用 before，其它用 after。"""
    raw = ev["before_json"] if ev["event_type"] == "DELETE" else ev["after_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def plan_impact(events: list[dict], biz_conn) -> dict:
    """把原始变更事件翻译成「要重算哪些 loan_id / 要删哪些 loan_id」。

    删除优先级高于重算：同一批里若某笔 loan 先被 UPDATE 后被 DELETE，最终态是不存在，
    不能再把它 UPSERT 回 DWS。
    """
    recalc: set[int] = set()
    deleted: set[int] = set()
    touched_collateral: set[int] = set()
    touched_customer: set[int] = set()

    for ev in events:
        body = _payload(ev)
        table, etype = ev["table_name"], ev["event_type"]
        if table == "loan":
            lid = body.get("loan_id")
            if lid is None:
                continue
            if etype == "DELETE":
                deleted.add(int(lid))
            else:
                recalc.add(int(lid))
        elif table == "collateral":
            cid = body.get("collateral_id")
            if cid is not None:
                touched_collateral.add(int(cid))
        elif table == "customer":
            cid = body.get("customer_id")
            if cid is not None:
                touched_customer.add(int(cid))

    # 主档变更 → 反查受影响贷款。放在循环外一次性查，避免逐事件打 DB。
    if touched_collateral:
        recalc.update(store.loans_by_collateral(biz_conn, sorted(touched_collateral)))
    if touched_customer:
        recalc.update(store.loans_by_customer(biz_conn, sorted(touched_customer)))

    recalc -= deleted
    return {
        "recalc": sorted(recalc),
        "deleted": sorted(deleted),
        "collateral": sorted(touched_collateral),
        "customer": sorted(touched_customer),
    }


def recalc_loans(biz_conn, crawl_conn, root_conn, loan_ids: list[int], date: str) -> dict:
    """对指定贷款做增量重算并落库；返回统计。

    只读这几笔的 loan/collateral/customer，DWD 单价词典仍需全量加载（它是行情侧口径，
    与贷款笔数无关；44k 行聚合在秒级，没必要为增量拆开）。
    """
    if not loan_ids:
        return {"recalc": 0, "alerts": 0, "dwd_hits": 0}
    loans = store.load_loans(biz_conn, loan_ids)
    if not loans:
        # 事件里有 loan_id 但业务库已查不到：说明后续还有一条 DELETE 没消费到，跳过即可。
        return {"recalc": 0, "alerts": 0, "dwd_hits": 0, "missing": len(loan_ids)}
    collaterals = store.load_collaterals(biz_conn, [ln["collateral_id"] for ln in loans])
    customers = store.load_customers(biz_conn, [ln["customer_id"] for ln in loans])
    dwd_unit = valuation.load_dwd_unit_prices(crawl_conn)
    # 空间特征每次增量都重载：S3 表每日重建，不能缓存陈旧快照（load 在秒级，可接受）。
    spatial = store.load_spatial(crawl_conn)

    rows = store.compute_rows(loans, collaterals, customers, dwd_unit, spatial=spatial)
    store.upsert_dws(root_conn, rows)
    n_alerts = store.replace_alerts(root_conn, rows, date)
    return {
        "recalc": len(rows),
        "alerts": n_alerts,
        "dwd_hits": sum(1 for r in rows if r.get("dwd_hit")),
    }


def consume_once(env: dict, date: str, batch: int, from_offset: int | None = None) -> dict:
    """消费一批变更；返回本批摘要（无事件时 events=0，不触碰下游）。"""
    biz = pymysql.connect(**config.business_params(env), charset="utf8mb4")
    crawl = pymysql.connect(**config.crawl_params(env), charset="utf8mb4")
    root = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
    try:
        _ensure_offset_table(root)
        store.ensure_ads_tables(root)
        offset = read_offset(root) if from_offset is None else from_offset
        events = fetch_events(root, offset, batch)
        if not events:
            return {"events": 0, "offset": offset}

        plan = plan_impact(events, biz)
        stat = recalc_loans(biz, crawl, root, plan["recalc"], date)
        n_deleted = store.delete_loans(root, plan["deleted"], date)
        # 明细变了，汇总必须跟着刷；放在最后一步，保证 ADS 与 DWS 同一时刻一致。
        agg = store.refresh_ads_risk_class(root, date)

        last_id = events[-1]["id"]
        write_offset(root, last_id)
        return {
            "events": len(events),
            "offset_from": offset,
            "offset": last_id,
            "recalc_loans": len(plan["recalc"]),
            "deleted_loans": n_deleted,
            "alerts_written": stat.get("alerts", 0),
            "dwd_hits": stat.get("dwd_hits", 0),
            "total_balance": agg["total_balance"],
        }
    finally:
        biz.close()
        crawl.close()
        root.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="CDC ODS 变更 → DWS/ADS 增量消费")
    ap.add_argument("--once", action="store_true", help="消费一批后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询（systemd 用）")
    ap.add_argument("--interval", type=float, default=20.0, help="--loop 的轮询间隔秒")
    ap.add_argument("--batch", type=int, default=500, help="单批最多消费的事件数")
    ap.add_argument("--date", default=None, help="预警/汇总的业务日期，默认今天")
    ap.add_argument(
        "--from-offset",
        type=int,
        default=None,
        help="忽略水位表，从指定 id 之后重放（对账用；重算幂等）",
    )
    args = ap.parse_args()

    env = config.load_env()
    # 业务日期按 Asia/Shanghai（config.business_date），不用本地时区：见 config.BUSINESS_TZ
    date = args.date or config.business_date()

    if not args.loop:
        res = consume_once(env, date, args.batch, args.from_offset)
        print(f"[cdc-consumer] {json.dumps(res, ensure_ascii=False)}", flush=True)
        return

    print(f"[cdc-consumer] loop start interval={args.interval}s batch={args.batch}", flush=True)
    from_offset = args.from_offset
    while True:
        try:
            # 业务日期每轮重取：常驻进程跨零点后，预警要落到新的一天。
            res = consume_once(env, args.date or config.business_date(), args.batch, from_offset)
            from_offset = None  # --from-offset 只在首轮生效，之后走水位表
            if res.get("events"):
                print(f"[cdc-consumer] {json.dumps(res, ensure_ascii=False)}", flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因单批异常退出，等下一轮重试
            print(f"[cdc-consumer] batch error: {exc}", flush=True)
        time.sleep(args.interval)
    print("[cdc-consumer] stopped", flush=True)


if __name__ == "__main__":
    main()
