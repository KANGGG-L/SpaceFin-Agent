#!/usr/bin/env python
"""采集后数据质量闸门（G2）：统计当日新增爬取行数，过低则告警 / 拦停。

口径：crawl_housing_sale / crawl_housing_rent 的 first_seen_date = --date 计为「当日新增」。
（出数页率属采集管线指标、不落 DWD 表，故以新增行数作为「今日是否采到数据」的代理。）

行为：
- 默认（严格模式关闭）：低于软下限只发 Slack 告警，exit 0 放行；
  低于硬下限也只告警放行（不阻断，避免单日空数据误杀整条链路）。
- 严格模式（--strict 或 SPACEFIN_CRAWL_GATE_STRICT=1）：低于硬下限 → exit 2，
  由 Airflow on_failure_callback 通知并把链路拦在 ETL 之前。

依赖：config（tools/risk）、pymysql、slack_notify（同目录）。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk"))

import config  # noqa: E402
import pymysql  # noqa: E402
from slack_notify import build_alert_message, post_slack  # noqa: E402

CITIES = [
    "gz",
    "sz",
    "zh",
    "st",
    "fs",
    "sg",
    "zj",
    "zq",
    "jm",
    "mm",
    "hui",
    "mz",
    "sw",
    "hy",
    "yj",
    "qy",
    "dg",
    "zs",
    "cz",
    "jy",
    "yf",
]


def decide(new_rows: int, hard: int, soft: int, strict: bool) -> tuple[int, str]:
    """纯决策函数（便于单测）：返回 (exit_code, level)。

    level ∈ {'ok','soft','hard'}；strict 且 hard 未达 → exit 2，否则 0。
    """
    if new_rows <= hard:
        return (2 if strict else 0, "hard")
    if new_rows <= soft:
        return (0, "soft")
    return (0, "ok")


def count_new_rows(conn, date: str) -> int:
    cur = conn.cursor()
    total = 0
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE first_seen_date=%s", (date,))
        total += cur.fetchone()[0]
    cur.close()
    return total


def check_zero_row_cities(conn, date: str) -> list[str]:
    """检查 21 个城市在当日是否有 0 新增行数的城市，返回 0 行城市列表。"""
    cur = conn.cursor()
    city_counts = {c: 0 for c in CITIES}
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        cur.execute(
            f"SELECT district, COUNT(*) FROM {tbl} WHERE first_seen_date=%s GROUP BY district",
            (date,),
        )
        for row in cur.fetchall():
            district, cnt = row[0], row[1]
            if district in city_counts:
                city_counts[district] += cnt
    cur.close()
    return [c for c in CITIES if city_counts[c] == 0]


def main() -> int:
    ap = argparse.ArgumentParser(description="采集后数据质量闸门（G2）")
    ap.add_argument("--date", required=True, help="业务日 YYYY-MM-DD")
    ap.add_argument(
        "--hard-floor", type=int, default=int(os.getenv("SPACEFIN_MIN_CRAWL_ROWS", "50"))
    )
    ap.add_argument(
        "--soft-floor", type=int, default=int(os.getenv("SPACEFIN_MIN_CRAWL_ROWS_WARN", "200"))
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        default=os.getenv("SPACEFIN_CRAWL_GATE_STRICT", "").lower() in ("1", "true", "yes"),
        help="低于硬下限时拦停（exit 2）；默认关闭（仅告警放行）",
    )
    args = ap.parse_args()

    env = config.load_env()
    conn = pymysql.connect(**config.crawl_params(env), charset="utf8mb4")
    try:
        new_rows = count_new_rows(conn, args.date)
        zero_cities = check_zero_row_cities(conn, args.date)
    finally:
        conn.close()

    print(
        f"[gate] date={args.date} new_rows={new_rows} "
        f"hard={args.hard_floor} soft={args.soft_floor} strict={args.strict}",
        flush=True,
    )
    if zero_cities:
        print(
            f"[gate] warning: 0-row cities ({len(zero_cities)}): {', '.join(zero_cities)}",
            flush=True,
        )

    code, level = decide(new_rows, args.hard_floor, args.soft_floor, args.strict)
    webhook = os.getenv("SPACEFIN_ALERT_SLACK_WEBHOOK", "")
    mentions = os.getenv("SPACEFIN_ALERT_MENTIONS", "")
    owner = "开发"

    if level == "ok":
        if zero_cities:
            detail = f"总新增行数 {new_rows} 达标，但有 {len(zero_cities)} 个城市新增为 0: {', '.join(zero_cities)}"
            msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
            post_slack(webhook, msg)
        print("[gate] OK", flush=True)
        return 0

    if level == "hard":
        detail = (
            f"当日新增爬取行数 {new_rows} ≤ 硬下限 {args.hard_floor}，数据可能缺失，下游结果不可信"
        )
    else:
        detail = f"当日新增爬取行数 {new_rows} 偏低（软下限 {args.soft_floor}），结果可能失真"

    if zero_cities:
        detail += f"；且有 {len(zero_cities)} 个城市新增为 0: {', '.join(zero_cities)}"

    msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
    post_slack(webhook, msg)

    if level == "hard" and args.strict:
        print("[gate] 严格模式：低于硬下限，拦停在 ETL 前", flush=True)
        return 2
    print(f"[gate] {level} 下限：告警后放行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
