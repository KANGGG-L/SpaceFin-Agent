#!/usr/bin/env python
"""1104 G11 资产质量报送 CLI（S4 L5 合规，AC-05 / AC-08）。

数据源：spacefin_crawler.ads_risk_class（风险引擎写好的五级汇总）。

本 CLI 做三件事：
1. 把 ads_risk_class 按 1104 G11 模板重排（正常/关注/次级/可疑/损失 + 合计行），
   落 ads_1104_g11 表并导出 CSV/JSON；
2. 三出口口径一致性校验（TC-08）：1104 模板 vs dws_risk_class 明细 SQL 聚合 vs
   ads_risk_class 内部累计，余额/笔数/占比任一项对不上就**阻断报送**并写 ads_report_alert
   告警（TC-05）——合规报送宁可拒报，也不能把口径不一致的数字送出去；
3. 幂等 upsert：同一 (stat_date, risk_class) 重跑覆盖，不产生脏数据。

为什么校验要拉 dws_risk_class 明细聚合而不是只信 ads_risk_class：ads_risk_class 是
风险引擎的产物，若引擎或增量消费链出了 bug，汇总表和明细表会静默漂移；明细聚合是
唯一能从原始明细独立复算的出口，用它当裁判才拦得住「汇总错了但没人发现」的场景。

用法：
    tools/orchestrator/.venv/bin/python tools/reporting/main.py --date 2026-08-05
    tools/orchestrator/.venv/bin/python tools/reporting/main.py --dry-run   # 只算不写库
"""

import argparse
import csv
import json
import os
import sys
import time

import pymysql

# 复用 tools/risk/config 的连接与业务日口径（商业日/时区必须全仓一致，见 config.BUSINESS_TZ）。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk"))

import config  # noqa: E402

CLASS_ORDER = config.CLASS_ORDER  # 五级固定顺序：正常/关注/次级/可疑/损失

# 口径容差：余额按分（DECIMAL 四舍五入到 0.01），占比按万分之一。
EPS_BALANCE = 0.01
EPS_PCT = 0.0001

G11_COLS = ["risk_class", "loan_count", "balance", "balance_pct"]
REPORT_TYPE = "1104_g11"


def connect(env):
    """root + 房产库连接（ADS DDL 需要 root）。"""
    return pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")


def ensure_tables(conn):
    """幂等建报表表与告警表（与 tools/risk/store.py 的 ensure_ads_tables 同款模式）。"""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_1104_g11 (
            stat_date DATE NOT NULL,
            risk_class VARCHAR(8) NOT NULL,
            loan_count INT,
            balance_total DECIMAL(16,2),
            balance_pct DECIMAL(8,4),
            is_total TINYINT,
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stat_date, risk_class)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_report_alert (
            id INT AUTO_INCREMENT PRIMARY KEY,
            report_date DATE,
            report_type VARCHAR(16),
            alert_level VARCHAR(8),
            check_name VARCHAR(64),
            detail VARCHAR(255),
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_date (report_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def load_internal(conn, date):
    """出口① 内部累计：ads_risk_class（风险引擎已写，含五级与占比）。

    缺级（如损失=0）补默认零行，保证三出口对比时有完整五级。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT risk_class, loan_count, balance_total, balance_pct "
        "FROM ads_risk_class WHERE stat_date=%s",
        (date,),
    )
    got = {r[0]: (int(r[1]), float(r[2]), float(r[3])) for r in cur.fetchall()}
    cur.close()
    out = {}
    for cls in CLASS_ORDER:
        cnt, bal, pct = got.get(cls, (0, 0.0, 0.0))
        out[cls] = {"count": cnt, "balance": bal, "balance_pct": pct}
    return out


def load_dws_agg(conn):
    """出口② dws_risk_class 明细 SQL 聚合（独立复算，不受 ads_risk_class 影响）。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) "
        "FROM dws_risk_class GROUP BY risk_class"
    )
    got = {r[0]: (int(r[1]), float(r[2])) for r in cur.fetchall()}
    cur.close()
    out = {}
    for cls in CLASS_ORDER:
        cnt, bal = got.get(cls, (0, 0.0))
        out[cls] = {"count": cnt, "balance": bal}
    return out


def build_g11(internal):
    """按 G11 模板排五级并追加合计行（余额占比用分数，展示时 ×100）。"""
    rows = []
    total_balance = round(sum(v["balance"] for v in internal.values()), 2)
    total_count = sum(v["count"] for v in internal.values())
    for cls in CLASS_ORDER:
        v = internal[cls]
        rows.append(
            {
                "risk_class": cls,
                "loan_count": v["count"],
                "balance": round(v["balance"], 2),
                "balance_pct": v["balance_pct"],
                "is_total": 0,
            }
        )
    rows.append(
        {
            "risk_class": "合计",
            "loan_count": total_count,
            "balance": total_balance,
            "balance_pct": round(total_balance / total_balance, 4) if total_balance else 0.0,
            "is_total": 1,
        }
    )
    return rows


def validate_consistency(g11_rows, internal, dws):
    """三出口比对：1104 模板（源自 internal）vs dws 明细聚合。

    任一出口的笔数不等、或余额/占比差超容差，即记一条 mismatch。
    1104 模板与 internal 同源（模板就是 internal 排的序），比对它俩是自检；
    真正的外部裁判是 dws 明细聚合，汇总表与明细表一旦漂移就会被这里拦下。
    """
    mismatches = []
    for cls in CLASS_ORDER:
        tpl = next(r for r in g11_rows if r["risk_class"] == cls)
        if tpl["loan_count"] != internal[cls]["count"]:
            mismatches.append(f"1104_vs_internal:{cls}:loan_count")
        if abs(tpl["balance"] - internal[cls]["balance"]) > EPS_BALANCE:
            mismatches.append(f"1104_vs_internal:{cls}:balance")
        if abs(tpl["balance_pct"] - internal[cls]["balance_pct"]) > EPS_PCT:
            mismatches.append(f"1104_vs_internal:{cls}:balance_pct")
        if tpl["loan_count"] != dws[cls]["count"]:
            mismatches.append(f"1104_vs_dws:{cls}:loan_count")
        if abs(tpl["balance"] - dws[cls]["balance"]) > EPS_BALANCE:
            mismatches.append(f"1104_vs_dws:{cls}:balance")
        if dws[cls]["count"] != internal[cls]["count"]:
            mismatches.append(f"dws_vs_internal:{cls}:loan_count")
        if abs(dws[cls]["balance"] - internal[cls]["balance"]) > EPS_BALANCE:
            mismatches.append(f"dws_vs_internal:{cls}:balance")
    return mismatches


def write_alerts(conn, date, mismatches):
    """口径不一致 → 写合规告警（阻断证据留痕，供合规角色核查）。"""
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO ads_report_alert "
        "(report_date, report_type, alert_level, check_name, detail) "
        "VALUES (%s,%s,%s,%s,%s)",
        [
            (
                date,
                REPORT_TYPE,
                "block",
                m,
                f"口径不一致: {m}",
            )
            for m in mismatches
        ],
    )
    conn.commit()
    cur.close()


def upsert_g11(conn, date, rows):
    """幂等 upsert：同一 (stat_date, risk_class) 覆盖，跨日各留各的。"""
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO ads_1104_g11 "
        "(stat_date, risk_class, loan_count, balance_total, balance_pct, is_total) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        " loan_count=VALUES(loan_count), balance_total=VALUES(balance_total), "
        " balance_pct=VALUES(balance_pct), is_total=VALUES(is_total), "
        " etl_ts=CURRENT_TIMESTAMP",
        [
            (date, r["risk_class"], r["loan_count"], r["balance"], r["balance_pct"], r["is_total"])
            for r in rows
        ],
    )
    conn.commit()
    cur.close()


def write_outputs(out_dir, date, g11_rows, status, mismatches, report):
    """落 CSV（G11 模板，占比按百分数展示）+ JSON（含分数占比与校验结论，供审计）。"""
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"ads_1104_g11_{date}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(G11_COLS)
        for r in g11_rows:
            w.writerow(
                [r["risk_class"], r["loan_count"], r["balance"], round(r["balance_pct"] * 100, 2)]
            )
    json_path = os.path.join(out_dir, f"g11_report_{date}.json")
    report["status"] = status
    report["mismatches"] = mismatches
    report["rows"] = [
        {**r, "balance_pct_percent": round(r["balance_pct"] * 100, 2)} for r in g11_rows
    ]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return csv_path, json_path


def main():
    ap = argparse.ArgumentParser(description="1104 G11 资产质量报送")
    ap.add_argument(
        "--date",
        default=config.business_date(),
        help="业务日 YYYY-MM-DD（默认今天，Asia/Shanghai）",
    )
    ap.add_argument("--out-dir", default="output/reporting", help="CSV/JSON 输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只计算与校验，不写库")
    ap.add_argument(
        "--simulate-mismatch",
        action="store_true",
        help="演练：人为让 dws 聚合余额偏移 1 元，验证阻断+告警路径（不污染数据）",
    )
    args = ap.parse_args()

    env = config.load_env()
    t0 = time.time()
    conn = connect(env)
    if not args.dry_run:
        ensure_tables(conn)

    internal = load_internal(conn, args.date)
    dws = load_dws_agg(conn)

    # 演练钩子：只在内存里改 dws 聚合，用于验证 AC-05 阻断链路，不写任何库。
    if args.simulate_mismatch:
        first = CLASS_ORDER[0]
        dws[first] = {"count": dws[first]["count"], "balance": dws[first]["balance"] + 1.0}

    g11_rows = build_g11(internal)
    mismatches = validate_consistency(g11_rows, internal, dws)
    blocked = bool(mismatches)

    report = {
        "date": args.date,
        "report_type": REPORT_TYPE,
        "total_loans": sum(v["count"] for v in internal.values()),
        "total_balance": round(sum(v["balance"] for v in internal.values()), 2),
        "internal": internal,
        "dws_agg": dws,
        "consistent": not blocked,
        "blocked": blocked,
    }

    if blocked:
        if not args.dry_run:
            write_alerts(conn, args.date, mismatches)
        status = "blocked"
        print(
            f"[reporting] 口径不一致，报送已阻断: {len(mismatches)} 项 "
            f"{mismatches}（告警已写 ads_report_alert）"
        )
        csv_path, json_path = write_outputs(
            args.out_dir, args.date, g11_rows, status, mismatches, report
        )
        print(json.dumps(report, ensure_ascii=False, indent=1))
        conn.close()
        print(f"[reporting] blocked {time.time() - t0:.1f}s | csv={csv_path} json={json_path}")
        return 1

    if not args.dry_run:
        upsert_g11(conn, args.date, g11_rows)
    status = "submitted"
    csv_path, json_path = write_outputs(
        args.out_dir, args.date, g11_rows, status, mismatches, report
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    conn.close()
    print(
        f"[reporting] G11 报送通过 三出口一致 | 合计 {report['total_loans']} 笔 / "
        f"余额 {report['total_balance']} | csv={csv_path} json={json_path} "
        f"{time.time() - t0:.1f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
