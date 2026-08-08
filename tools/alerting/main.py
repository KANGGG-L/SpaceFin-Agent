#!/usr/bin/env python
"""LTV 预警推送 CLI（I-05，贷后保全，TC-03）。

数据源：spacefin_crawler.ads_ltv_alerts（风险引擎写好的当日预警）。

本 CLI 做的事：
1. --date 取当日预警清单（ads_ltv_alerts WHERE alert_date = --date）；
2. T+1 去重推送：同一 (loan_id, alert_date) 已推送成功的不重推（TC-03 语义：
   预警 T 日生成、最迟 T+1 送达贷后系统；本 CLI 是推送执行器，任何一天重复执行
   都不会把同一条预警推两遍）；
3. 失败重试状态机：单条推送失败记 attempt_count，未到 max_retries 的下一轮自动重试，
   已耗尽重试的留 failed 待人工；
4. driver 可替换：见 drivers.py——当前推站内告警表 + 文件，真实贷后系统实现
   PostloanHttpDriver 后即可替换。

状态机（单线程串行，每行每轮最多处理一次）：
    pending ──▶ success（记 dispatch_ts）
        └失败──▶ failed, attempt_count+1
                 ├─ attempt_count < max_retries ──▶ 下一轮自动重试
                 └─ attempt_count >= max_retries ──▶ 留 failed（终态，待人工）

推送出口（driver，可组合）：
- site_inbox：写站内告警表 ads_alert_inbox（默认）
- file：追加 JSONL 到 output/alerting（默认，本地联调）
- postloan_http：真实贷后系统 HTTP 推送（I-05 闭环出口）。配置
  SPACEFIN_POSTLOAN_WEBHOOK_URL 后由 resolve_drivers 自动追加启用，无需改命令；
  未配置时仅落库 + 文件，预警仍在台账（ads_alert_dispatch）留有送达记录。

T+1 调度：Airflow DAG guangdong_daily_crawl 在风险引擎重算之后调用本 CLI
（--date {{ ds }}），使当日预警最迟 T+1 送达（含真实贷后系统，若已配置 webhook）。

用法：
    tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05
    tools/orchestrator/.venv/bin/python tools/alerting/main.py --date 2026-08-05 --dry-run
    tools/orchestrator/.venv/bin/python tools/alerting/main.py --force-fail  # 演练重试状态机
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime

import pymysql

# 复用 tools/risk/config 的连接与业务日口径（业务时区必须全仓一致，见 config.BUSINESS_TZ）。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk"))

import config  # noqa: E402
from drivers import make_driver  # noqa: E402 (同包模块)

STATE_SUCCESS = "success"
STATE_FAILED = "failed"

DEFAULT_MAX_RETRIES = 3
DEFAULT_OUT_DIR = "output/alerting"
# 默认推送驱动（真实贷后系统 webhook 由 resolve_drivers 按 env 自动追加）。
DEFAULT_DRIVERS = "site_inbox,file"

DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS ads_alert_dispatch (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loan_id INT,
    alert_date DATE,
    dispatch_date DATE,
    status VARCHAR(16),
    attempt_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    last_error VARCHAR(255),
    dispatch_ts DATETIME,
    etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_loan_date (loan_id, alert_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

DISPATCH_UPSERT_SQL = (
    "INSERT INTO ads_alert_dispatch "
    "(loan_id, alert_date, dispatch_date, status, attempt_count, max_retries, "
    " last_error, dispatch_ts) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    " dispatch_date=VALUES(dispatch_date), status=VALUES(status), "
    " attempt_count=VALUES(attempt_count), max_retries=VALUES(max_retries), "
    " last_error=VALUES(last_error), dispatch_ts=VALUES(dispatch_ts), "
    " etl_ts=CURRENT_TIMESTAMP"
)


def connect(env):
    """root + 房产库连接（ADS DDL 需要 root，读写同库省一条连接）。"""
    return pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")


def _ensure_columns(conn, table: str, columns: list[tuple[str, str]]) -> None:
    """幂等补列：MySQL 8 的 ALTER TABLE 没有 ADD COLUMN IF NOT EXISTS，需先查 information_schema。

    存量表只补新列不动老列，且新列全部允许 NULL——避免给已有行强填充默认值导致全表锁，
    也让「老数据无该列」这一事实保持诚实（与 tools/risk/store.py 同模式）。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table,),
    )
    existing = {r[0] for r in cur.fetchall()}
    for name, ddl in columns:
        if name not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    cur.close()


def ensure_tables(conn):
    """幂等建推送台账表与站内告警表。

    CREATE TABLE IF NOT EXISTS 只保证建新表，不补存量表缺列；旧环境重跑时
    ads_alert_inbox 可能缺 alert_level，这里在 CREATE 之后幂等补列。
    """
    cur = conn.cursor()
    cur.execute(DISPATCH_DDL)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_alert_inbox (
            id INT AUTO_INCREMENT PRIMARY KEY,
            loan_id INT,
            customer_id INT,
            collateral_id INT,
            loan_balance DECIMAL(14,2),
            market_valuation DECIMAL(14,2),
            ltv DECIMAL(8,4),
            risk_class VARCHAR(8),
            is_high_risk_zone TINYINT,
            alert_level VARCHAR(8),
            alert_date DATE,
            received_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_loan_date (loan_id, alert_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    # 存量表补列：ads_alert_inbox 缺 alert_level 的旧环境（如 S6 前建的告警表）幂等补齐。
    _ensure_columns(conn, "ads_alert_inbox", [("alert_level", "VARCHAR(8)")])
    conn.commit()
    cur.close()


def load_alerts(conn, date):
    """取 --date 当日预警清单（ads_ltv_alerts 按 alert_date 过滤）。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, loan_id, customer_id, collateral_id, loan_balance, "
        " market_valuation, ltv, risk_class, is_high_risk_zone, alert_level, alert_date "
        "FROM ads_ltv_alerts WHERE alert_date=%s ORDER BY id",
        (date,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return rows


def load_dispatch(conn, alerts):
    """读本批候选在台账里的既有状态（成功记录用于去重，失败记录用于重试）。"""
    if not alerts:
        return {}
    loan_ids = [a["loan_id"] for a in alerts]
    frag = "(" + ",".join(["%s"] * len(loan_ids)) + ")"
    cur = conn.cursor()
    cur.execute(
        f"SELECT loan_id, alert_date, status, attempt_count, max_retries "
        f"FROM ads_alert_dispatch WHERE loan_id IN {frag}",
        loan_ids,
    )
    out = {}
    for loan_id, alert_date, status, attempts, retries in cur.fetchall():
        out[(int(loan_id), str(alert_date))] = {
            "status": status,
            "attempt_count": int(attempts or 0),
            "max_retries": int(retries or DEFAULT_MAX_RETRIES),
        }
    cur.close()
    return out


def alert_level_label(alert: dict) -> str:
    """alert_level 两档中文文案：warn→警示级、strong→强预警级、缺失/未知→「—」。

    ads_ltv_alerts.alert_level 允许为 NULL（历史行或引擎未写等级），推送侧
    一律按「—」标注，不臆造等级。
    """
    level = (alert.get("alert_level") or "").strip().lower()
    if level == "warn":
        return "警示级"
    if level == "strong":
        return "强预警级"
    return "—"


def run_dispatch(conn, drivers, date, max_retries, force_fail):
    """执行一轮推送；返回摘要（候选/推送/去重/重试/失败/终态失败）。"""
    alerts = load_alerts(conn, date)
    existing = load_dispatch(conn, alerts)
    summary = Counter()
    retried = 0
    for alert in alerts:
        key = (int(alert["loan_id"]), str(alert["alert_date"]))
        prev = existing.get(key)
        if prev is not None and prev["status"] == STATE_SUCCESS:
            summary["dedup"] += 1
            continue
        if (
            prev is not None
            and prev["status"] == STATE_FAILED
            and prev["attempt_count"] >= prev["max_retries"]
        ):
            summary["final_failed"] += 1
            continue

        attempt = (prev["attempt_count"] + 1) if prev else 1
        is_retry = prev is not None and prev["status"] == STATE_FAILED
        if is_retry:
            retried += 1

        now = _now()
        last_error = ""
        try:
            for d in drivers:
                if force_fail:
                    raise RuntimeError("演练注入：模拟推送失败（--force-fail）")
                d.send(alert)
        except Exception as exc:  # noqa: BLE001 - 失败进状态机，由 attempt_count 决定去向
            last_error = str(exc)[:250]
            new_status = STATE_FAILED
            summary["failed"] += 1
            print(
                f"[alerting] loan_id={alert['loan_id']} level={alert_level_label(alert)} "
                f"推送失败: {last_error} "
                f"attempt={attempt} max_retries={max_retries}",
                flush=True,
            )
        else:
            new_status = STATE_SUCCESS
            summary["pushed"] += 1

        upsert_dispatch(conn, alert, date, new_status, attempt, max_retries, last_error, now)
        summary["attempted"] += 1

    summary["candidates"] = len(alerts)
    summary["retried"] = retried
    return dict(summary)


def upsert_dispatch(conn, alert, date, status, attempt, max_retries, last_error, now):
    cur = conn.cursor()
    cur.execute(
        DISPATCH_UPSERT_SQL,
        (
            alert["loan_id"],
            alert["alert_date"],
            date,
            status,
            attempt,
            max_retries,
            last_error,
            now,
        ),
    )
    conn.commit()
    cur.close()


def _now():
    """业务时区墙钟时间（去掉 tzinfo，与 MySQL 容器 +08:00 的 DATETIME 对齐）。"""
    return datetime.now(config.BUSINESS_TZ).replace(tzinfo=None)


def write_outputs(conn, out_dir, date, summary):
    """落推送摘要（JSON）+ 台账 CSV；返回路径。"""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"alert_summary_{date}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"date": date, **summary}, f, ensure_ascii=False, indent=1)
    csv_path = os.path.join(out_dir, f"alert_dispatch_{date}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["loan_id", "alert_date", "dispatch_date", "status", "attempt_count"])
        cur = conn.cursor()
        cur.execute(
            "SELECT loan_id, alert_date, dispatch_date, status, attempt_count "
            "FROM ads_alert_dispatch WHERE alert_date=%s ORDER BY loan_id",
            (date,),
        )
        for r in cur.fetchall():
            w.writerow(r)
        cur.close()
    return {"csv": csv_path, "json": json_path}


def resolve_drivers(
    drivers_arg: str, *, conn, out_dir: str, date: str, env: dict | None = None
) -> list:
    """按 --drivers 构造驱动列表。

    当 SPACEFIN_POSTLOAN_WEBHOOK_URL 已配置时，自动追加 postloan_http 真实贷后
    系统推送驱动——I-05 闭环在配置后即生效，无需改命令。env 缺省读仓库 .env。
    """
    env = env if env is not None else config.load_env()
    names = [n.strip() for n in drivers_arg.split(",") if n.strip()]
    endpoint = env.get("SPACEFIN_POSTLOAN_WEBHOOK_URL")
    if endpoint and "postloan_http" not in names:
        names.append("postloan_http")
        print(
            "[alerting] 检测到 SPACEFIN_POSTLOAN_WEBHOOK_URL，自动启用 postloan_http "
            "真实贷后系统推送（I-05 闭环已接通）",
            flush=True,
        )
    return [make_driver(n, conn=conn, out_dir=out_dir, alert_date=date, env=env) for n in names]


def main():
    ap = argparse.ArgumentParser(description="LTV 预警推送（I-05）：清单生成 + T+1 去重 + 失败重试")
    ap.add_argument(
        "--date", default=config.business_date(), help="业务日 YYYY-MM-DD（推送该日预警清单）"
    )
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="摘要/台账/推送日志输出目录")
    ap.add_argument(
        "--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="单条预警最大尝试次数"
    )
    ap.add_argument(
        "--drivers",
        default=DEFAULT_DRIVERS,
        help="推送驱动（逗号分隔）: site_inbox/file/postloan_http；"
        "配置 SPACEFIN_POSTLOAN_WEBHOOK_URL 后 postloan_http 自动启用",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印清单，不写库不推送")
    ap.add_argument("--force-fail", action="store_true", help="演练：注入推送失败，验证重试状态机")
    args = ap.parse_args()

    env = config.load_env()
    t0 = time.time()
    conn = connect(env)
    try:
        if not args.dry_run:
            ensure_tables(conn)
        alerts = load_alerts(conn, args.date)
        if args.dry_run:
            print(
                f"[alerting] dry-run date={args.date} candidates={len(alerts)} "
                f"drivers={args.drivers} max_retries={args.max_retries}"
            )
            for a in alerts:
                print(
                    f"  loan_id={a['loan_id']} ltv={a['ltv']} level={alert_level_label(a)} "
                    f"risk_class={a['risk_class']} alert_date={a['alert_date']}"
                )
            return 0

        drivers = resolve_drivers(
            args.drivers, conn=conn, out_dir=args.out_dir, date=args.date, env=env
        )
        summary = run_dispatch(conn, drivers, args.date, args.max_retries, args.force_fail)
        paths = write_outputs(conn, args.out_dir, args.date, summary)
        print(
            f"[alerting] date={args.date} drivers={args.drivers} "
            f"{json.dumps(summary, ensure_ascii=False)}"
        )
        print(f"[alerting] 完成 {time.time() - t0:.1f}s | 摘要={paths['json']} 台账={paths['csv']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
