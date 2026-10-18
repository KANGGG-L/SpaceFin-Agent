#!/usr/bin/env python
"""采集后数据质量闸门（G2）：统计当日新增 / 刷新爬取行数，过低则告警 / 拦停。

口径（两表 crawl_housing_sale / crawl_housing_rent 合计）：
- new_rows：first_seen_date = --date，计为「当日新增」（新出现的房源）；
- refreshed： last_seen_date = --date **且 last_seen_page IS NOT NULL**，计为「当日刷新」
  （当天真的在列表页上被重新扫到的存量房源）。页码条件是必须的：`last_seen_date` 单独
  不是观测，只是 ETL 重跑日的函数（2026-08-30 实测零页面爬取仍报 refreshed=25,241 放行）。
（出数页率属采集管线指标、不落 DWD 表，故以行数作为「今日是否采到数据」的代理。）

行为：
- 默认（严格模式关闭）：低于软下限只发 Slack 告警，exit 0 放行；
  低于硬下限也只告警放行（不阻断，避免单日空数据误杀整条链路）。
- 严格模式（--strict 或 SPACEFIN_CRAWL_GATE_STRICT=1）：低于硬下限 → exit 2，
  由 Airflow on_failure_callback 通知并把链路拦在 ETL 之前。
- **new_rows == 0 且 refreshed == 0 永远拦停（exit 2，不论 strict）**：爬虫当天一行
  都没扫到，意味着爬虫根本没产出数据（未实际运行 / 代理或源站故障 / CRAWL_RUN_ID
  幂等跳过），绝不是「少但有一些」，绝不静默放行，强制在 ETL 前标红失败
  （修复「假成功」缺陷：此前 0 行被当硬下限只告警而绿了）。
- **new_rows == 0 但 refreshed > 0 → 告警放行（exit 0，level='refreshed'）**：
  判定为「全量页已爬穿、当日无新房源、仅刷新存量」，属正常稳态而非故障——
  断点续爬把 21 城 sale 各 100 页（=PAGES_SALE 上限）爬完后，每日只回扫前若干页，
  新房源为 0 是必然结果；而带页码的刷新行说明今天确实在列表页上见到了这些房源，
  这才是「爬虫活着」的证据。此时 ETL / 风险 / 快照仍需按日推进，否则快照点会断档。
- **观测列 `last_seen_page` 缺失 → 永远拦停（exit 2）**：`refreshed` 口径不可用（未迁移的存量库、
  或 gate 跑在 `etl_finalize` 之前）。闸门保持只读，只点名根因，不去 ALTER 补列。
- 0 行城市名单（check_zero_row_cities）只作告警，不影响拦停判定。
- **无完备观测的 (城,型) 必须点名**（incomplete_census_tasks）：查 `crawl_census`，把当日
  没有任何 `complete=1` 行的 (city, typ) 逐一点进告警文案。它们是「今天没可信地看过」的
  部分，S3 不得对其产出任何 absent 判定——沉默的缺席会直接被下游读成「已下架」。
  账本缺失（表不存在 / 当日无行）比「有行但 complete=0」更严重：那等于全城无证据，
  同样点名，绝不退化成「没账本 = 都完备」。

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

# crawl_census 的 typ 口径（采集任务类型，非 DWD 表名）：fangyuan 落 crawl_housing_rent。
CRAWL_TYPE_ORDER = ("sale", "fangyuan")


def decide(
    new_rows: int, hard: int, soft: int, strict: bool, refreshed: int = 0
) -> tuple[int, str]:
    """纯决策函数（便于单测）：返回 (exit_code, level)。

    level ∈ {'ok','soft','hard','refreshed','zero'}；strict 且 hard 未达 → exit 2，否则 0。
    - new_rows<=0 且 refreshed<=0 → (2,'zero')，不论 strict：爬虫当天一行都没扫到，
      属故障而非「单日偏低」，必须拦停（修复 0 行被当硬下限静默放行的假成功）。
    - new_rows<=0 但 refreshed>0 → (0,'refreshed')：全量页已爬穿、无新房源只刷新
      存量，属正常稳态，告警放行，避免快照点断档。
    """
    if new_rows <= 0:
        return (0, "refreshed") if refreshed > 0 else (2, "zero")
    if new_rows <= hard:
        return (2 if strict else 0, "hard")
    if new_rows <= soft:
        return (0, "soft")
    return (0, "ok")


def missing_observe_columns(conn) -> list[str]:
    """返回缺 `last_seen_page` 观测列的 DWD 表；空列表 = schema 就绪。

    列由 `etl.py::_ensure_columns` 在每次运行时幂等补齐（`CREATE TABLE IF NOT EXISTS`
    在活库上永远不会长出新的列）。DAG 里 gate 排在 `etl_finalize` 之后，所以正常链路
    总是先有列；但手工单跑 gate 可能撞上未迁移的存量库——闸门保持只读、不去 ALTER，
    而是把「列缺失」当成一个独立的拦停原因报出来。
    """
    cur = conn.cursor()
    missing = []
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s AND column_name='last_seen_page'",
            (tbl,),
        )
        if cur.fetchone()[0] == 0:
            missing.append(tbl)
    cur.close()
    return missing


def count_new_rows(conn, date: str) -> int:
    cur = conn.cursor()
    total = 0
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE first_seen_date=%s", (date,))
        total += cur.fetchone()[0]
    cur.close()
    return total


def count_refreshed_rows(conn, date: str) -> int:
    """当日**有页码证据**的刷新行数：last_seen_date = date AND last_seen_page IS NOT NULL。

    用于区分「爬虫死了」(new=0 且 refreshed=0) 与「无新房源但存量已刷新」。

    `last_seen_page IS NOT NULL` 是这个口径的全部意义：S1 之前该列不存在，而
    `last_seen_date` 只是 ETL 重跑日的函数——2026-08-30 实测零页面爬取仍报
    refreshed=25,241 并放行。只有带页码的行才是「今天真的在列表页上见到了它」。
    """
    cur = conn.cursor()
    total = 0
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        cur.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE last_seen_date=%s AND last_seen_page IS NOT NULL",
            (date,),
        )
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


def incomplete_census_tasks(conn, date: str) -> tuple[list[str], bool]:
    """当日**无任何 complete=1 行**的 (city, typ)；第二个返回值 = 账本是否可用。

    `crawl_census` 是缺席（absent）判定的唯一证据源：某 (城,型) 当日没有 complete=1 的行，
    就等于「今天没可信地看过它」，下游不得据此判 absent。

    账本不可用（表不存在 / 当日无行）时返回 `([], False)`：这不是「全部完备」，恰恰
    相反——没有账本就没有任何一城有证据，故调用方必须把「账本缺失」本身点名出来。
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='crawl_census'",
        )
        if cur.fetchone()[0] == 0:
            return [], False
        cur.execute(
            "SELECT city, typ, MAX(complete) FROM crawl_census WHERE ds=%s GROUP BY city, typ",
            (date,),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
    if not rows:
        return [], False
    order = {
        f"{c}:{t}": i for i, (c, t) in enumerate((c, t) for c in CITIES for t in CRAWL_TYPE_ORDER)
    }
    bad = [f"{city}:{typ}" for city, typ, mx in rows if not mx]
    return sorted(bad, key=lambda x: order.get(x, len(order))), True


def census_note(incomplete: list[str], ready: bool) -> str:
    """census 点名文案；无异常时返回空串（追加到 detail 后不产生噪音）。"""
    if not ready:
        return (
            "；crawl_census 当日无账本行（表缺失或上游 crawl_census 任务未跑）："
            "全部 21 城当日无完备观测证据，不得产生任何 absent 判定"
        )
    if not incomplete:
        return ""
    return (
        f"；{len(incomplete)} 个 (城,型) 当日无完备观测"
        f"（crawl_census 无 complete=1 行）: {', '.join(incomplete)}"
        f"；这些城市当日不得产生任何 absent 判定"
    )


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
    # 缺省即未知：账本没读到之前，「有哪些 (城,型) 无完备观测」的答案是「全都未知」。
    incomplete: list[str] = []
    census_ready = False
    conn = pymysql.connect(**config.crawl_params(env), charset="utf8mb4")
    try:
        missing = missing_observe_columns(conn)
        if missing:
            # 观测列缺失 → 三个口径都没法算（refreshed 会直接抛 1054），跳过统计
            new_rows = refreshed = 0
            zero_cities = []
        else:
            new_rows = count_new_rows(conn, args.date)
            refreshed = count_refreshed_rows(conn, args.date)
            zero_cities = check_zero_row_cities(conn, args.date)
            incomplete, census_ready = incomplete_census_tasks(conn, args.date)
    finally:
        conn.close()
    note = census_note(incomplete, census_ready)

    webhook = os.getenv("SPACEFIN_ALERT_SLACK_WEBHOOK", "")
    mentions = os.getenv("SPACEFIN_ALERT_MENTIONS", "")
    owner = "开发"

    if missing:
        # fail closed：把「列缺失」报成拦停原因，而不是让 1054 栈把真因埋掉。
        detail = (
            f"DWD 表 {', '.join(missing)} 缺 `last_seen_page` 列，闸门的刷新口径"
            f"（last_seen_date={{ds}} 且 last_seen_page 非空）无法计算。"
            f"该列由 `etl.py::_ensure_columns` 在每次 ETL 运行时幂等补齐——"
            f"要么 `etl_finalize` 还没跑（gate 必须排在它之后），要么是存量库尚未迁移。"
            f"处理：python3 tools/orchestrator/etl.py --date {args.date} 补列后重跑闸门。"
        )
        msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
        post_slack(webhook, msg)
        print(f"[gate] 观测列缺失（{', '.join(missing)}）：拦停，refreshed 口径不可用", flush=True)
        return 2

    print(
        f"[gate] date={args.date} new_rows={new_rows} refreshed={refreshed} "
        f"hard={args.hard_floor} soft={args.soft_floor} strict={args.strict}",
        flush=True,
    )
    if zero_cities:
        print(
            f"[gate] warning: 0-row cities ({len(zero_cities)}): {', '.join(zero_cities)}",
            flush=True,
        )
    if note:
        print(f"[gate] warning: crawl_census {note.lstrip('；')}", flush=True)

    code, level = decide(new_rows, args.hard_floor, args.soft_floor, args.strict, refreshed)

    if level == "ok":
        # 点名优先于「达标」：无完备观测的城市即使新增行数好看，当日也不得判 absent，
        # 这一条必须有人看见，不能因为总量达标就静默。
        if zero_cities or note:
            detail = f"总新增行数 {new_rows} 达标"
            if zero_cities:
                detail += f"，但有 {len(zero_cities)} 个城市新增为 0: {', '.join(zero_cities)}"
            detail += note
            msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
            post_slack(webhook, msg)
        print("[gate] OK", flush=True)
        return 0

    if level == "zero":
        # 爬虫当天一行都没扫到（新增 0 且刷新 0）：绝不静默放行（修复假成功）。
        detail = (
            f"当日新增爬取行数 = 0（first_seen_date={args.date}）且当日刷新行数 = 0"
            f"（last_seen_date={args.date} 且 last_seen_page 非空），爬虫当天未产出任何数据，"
            f"链路不可信，必须拦停在 ETL 前。常见根因：master 因 CRAWL_RUN_ID 幂等已 done "
            f"而跳过未实际派发、代理/源站故障、或数据已存在但未按本日打标。"
            f"若刚切到按天 raw 目录（S1），也可能是当日批次里全部行都没有页码证据——"
            f"旧行 last_seen_page 为 NULL 不再算刷新，用 crawl_reconcile.py --date {args.date} 对账。"
        )
        if zero_cities:
            detail += f" 另有 {len(zero_cities)} 个城市新增为 0: {', '.join(zero_cities)}"
        detail += note
        msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
        post_slack(webhook, msg)
        print("[gate] 0 新增且 0 刷新：拦停（爬虫未产出数据，非 strict 也失败）", flush=True)
        return 2

    if level == "refreshed":
        # 无新房源但存量被刷新：全量页已爬穿的正常稳态，告警放行，避免快照点断档。
        detail = (
            f"当日新增爬取行数 = 0（first_seen_date={args.date}），但当日刷新行数 = {refreshed}"
            f"（last_seen_date={args.date} 且 last_seen_page 非空，即今天在列表页上真的"
            f"见到了这些存量房源）：判定为「无新房源、仅刷新存量」"
            f"（全量页已爬穿 + 每日回扫前几页），爬虫存活，告警放行。"
        )
        if zero_cities:
            detail += f"；{len(zero_cities)} 个城市新增为 0（仅告警）: {', '.join(zero_cities)}"
        detail += note
        msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
        post_slack(webhook, msg)
        print(
            f"[gate] 0 新增但已刷新 {refreshed} 行：告警放行（无新房源、仅刷新存量）",
            flush=True,
        )
        return 0

    if level == "hard":
        detail = (
            f"当日新增爬取行数 {new_rows} ≤ 硬下限 {args.hard_floor}，数据可能缺失，下游结果不可信"
        )
    else:
        detail = f"当日新增爬取行数 {new_rows} 偏低（软下限 {args.soft_floor}），结果可能失真"

    if zero_cities:
        detail += f"；且有 {len(zero_cities)} 个城市新增为 0: {', '.join(zero_cities)}"
    detail += note

    msg = build_alert_message("crawl_quality_gate", args.date, owner, detail, mentions)
    post_slack(webhook, msg)

    if level == "hard" and args.strict:
        print("[gate] 严格模式：低于硬下限，拦停在 ETL 前", flush=True)
        return 2
    print(f"[gate] {level} 下限：告警后放行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
