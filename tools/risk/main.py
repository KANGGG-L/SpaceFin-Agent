#!/usr/bin/env python
"""SpaceFin 风险引擎 CLI（Sprint A）：估值 → LTV → 五级分类 → 贷后保全预警。

用法:
    python tools/risk/main.py --date 2026-08-05 --out-dir output/risk
    python tools/risk/main.py --date 2026-08-05 --dry-run      # 只算不写库

输出:
    - MySQL spacefin_crawler：dws_risk_class(打宽明细) / ads_ltv_alerts(预警) / ads_risk_class(五级汇总)
    - 文件：{out_dir}/dws_risk_class.csv / ads_ltv_alerts.csv / risk_report.json

对账说明：DWD 行情估值命中才用真实行情；未命中（当前合成种子地址无城市）回退业务库
true_market_price，命中率与 LTV 分布写入 risk_report.json，便于对齐数据后复核。
"""

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import pymysql
import risk_engine
import valuation

# 广东 21 城：中文城市名/常用简称 → DWD 城市码
CITY_MAP = {
    "广州": "gz",
    "深圳": "sz",
    "佛山": "fs",
    "东莞": "dg",
    "珠海": "zh",
    "中山": "zs",
    "惠州": "hui",
    "江门": "jm",
    "肇庆": "zq",
    "清远": "qy",
    "韶关": "sg",
    "汕头": "st",
    "汕尾": "sw",
    "揭阳": "jy",
    "潮州": "cz",
    "梅州": "mz",
    "河源": "hy",
    "阳江": "yj",
    "茂名": "mm",
    "湛江": "zj",
    "云浮": "yf",
}

DWS_COLS = [
    "loan_id",
    "customer_id",
    "collateral_id",
    "balance",
    "interest_rate",
    "market_valuation",
    "ltv",
    "risk_class",
    "low_confidence",
    "is_high_risk_zone",
    "alert",
]
ALERT_COLS = [
    "loan_id",
    "customer_id",
    "collateral_id",
    "loan_balance",
    "market_valuation",
    "ltv",
    "risk_class",
    "is_high_risk_zone",
    "alert_date",
]


def load_loans(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT loan_id, customer_id, collateral_id, loan_amount, balance, interest_rate, risk_class, origination_date FROM loan"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return rows


def load_collaterals(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT collateral_id, property_addr, lat, lng, area, age, true_market_price, poi_density, commute_min, is_high_risk_zone, spatial_feat_missing_pct FROM collateral"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return {r["collateral_id"]: r for r in rows}


def load_customers(conn):
    cur = conn.cursor()
    cur.execute("SELECT customer_id, credit_score, income_monthly, debt_ratio FROM customer")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return {r["customer_id"]: r for r in rows}


def ensure_ads_tables(conn):
    """幂等建 ADS 表（app 账号或 root 均可，etl 同款模式）。"""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dws_risk_class (
            loan_id INT PRIMARY KEY,
            customer_id INT, collateral_id INT,
            balance DECIMAL(14,2), interest_rate DECIMAL(5,2),
            market_valuation DECIMAL(14,2), ltv DECIMAL(8,4),
            risk_class VARCHAR(8), low_confidence TINYINT,
            is_high_risk_zone TINYINT, alert TINYINT,
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_ltv_alerts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            loan_id INT, customer_id INT, collateral_id INT,
            loan_balance DECIMAL(14,2), market_valuation DECIMAL(14,2),
            ltv DECIMAL(8,4), risk_class VARCHAR(8),
            is_high_risk_zone TINYINT, alert_date DATE,
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_loan (loan_id), KEY idx_date (alert_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ads_risk_class (
            stat_date DATE, risk_class VARCHAR(8),
            loan_count INT, balance_total DECIMAL(16,2), balance_pct DECIMAL(8,4),
            etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stat_date, risk_class)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--out-dir", default="output/risk")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不写库")
    ap.add_argument("--write-db", action="store_true", help="写入 MySQL ADS 表（默认只落 CSV）")
    args = ap.parse_args()

    env = config.load_env()
    biz = pymysql.connect(**config.business_params(env), charset="utf8mb4")
    crawl = pymysql.connect(**config.crawl_params(env), charset="utf8mb4")
    t0 = time.time()

    loans = load_loans(biz)
    collaterals = load_collaterals(biz)
    customers = load_customers(biz)
    dwd_unit = valuation.load_dwd_unit_prices(crawl)
    print(f"[risk] loans={len(loans)} collaterals={len(collaterals)} dwd_districts={len(dwd_unit)}")

    rows = []
    for ln in loans:
        col = collaterals.get(ln["collateral_id"])
        cust = customers.get(ln["customer_id"])
        rows.append(risk_engine.enrich_loan(ln, col, cust, dwd_unit, CITY_MAP))

    agg = risk_engine.build_aggregate(rows)
    alerts = [r for r in rows if r["alert"]]
    dwd_hits = sum(1 for r in rows if r.get("dwd_hit"))

    os.makedirs(args.out_dir, exist_ok=True)
    dws_path = os.path.join(args.out_dir, "dws_risk_class.csv")
    alert_path = os.path.join(args.out_dir, "ads_ltv_alerts.csv")
    write_csv(dws_path, DWS_COLS, rows)
    write_csv(
        alert_path,
        ALERT_COLS,
        [dict(r, loan_balance=r["balance"], alert_date=args.date) for r in alerts],
    )

    report = {
        "date": args.date,
        "total_loans": len(rows),
        "valuation_src": {"dwd_hits": dwd_hits, "fallback_true_market": len(rows) - dwd_hits},
        "ltv_distribution": {
            "red_line": config.LTV_RED_LINE,
            "over_red_line": sum(
                1 for r in rows if r["ltv"] is not None and r["ltv"] > config.LTV_RED_LINE
            ),
            "low_confidence": sum(1 for r in rows if r["low_confidence"]),
        },
        "by_class": agg["by_class"],
        "total_balance": agg["total_balance"],
        "alerts_count": len(alerts),
    }
    report_path = os.path.join(args.out_dir, "risk_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(
        f"[risk] 完成 {time.time() - t0:.1f}s | dws={dws_path} alerts={alert_path} report={report_path}"
    )

    if args.write_db and not args.dry_run:
        # ADS 建表/写库用 root（DDL 需要 root）；读 DWD 走 app 账号。见 config.root_crawl_params
        wconn = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
        ensure_ads_tables(wconn)
        cur = wconn.cursor()
        cur.execute("DELETE FROM dws_risk_class")
        cur.execute("DELETE FROM ads_ltv_alerts")
        cur.execute("DELETE FROM ads_risk_class WHERE stat_date=%s", (args.date,))
        cur.executemany(
            "INSERT INTO dws_risk_class (loan_id, customer_id, collateral_id, balance, interest_rate, market_valuation, ltv, risk_class, low_confidence, is_high_risk_zone, alert) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    r["loan_id"],
                    r["customer_id"],
                    r["collateral_id"],
                    r["balance"],
                    r["interest_rate"],
                    r["market_valuation"],
                    r["ltv"],
                    r["risk_class"],
                    int(r["low_confidence"]),
                    r["is_high_risk_zone"],
                    int(r["alert"]),
                )
                for r in rows
            ],
        )
        cur.executemany(
            "INSERT INTO ads_ltv_alerts (loan_id, customer_id, collateral_id, loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, alert_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    r["loan_id"],
                    r["customer_id"],
                    r["collateral_id"],
                    r["balance"],
                    r["market_valuation"],
                    r["ltv"],
                    r["risk_class"],
                    r["is_high_risk_zone"],
                    args.date,
                )
                for r in alerts
            ],
        )
        cur.executemany(
            "INSERT INTO ads_risk_class (stat_date, risk_class, loan_count, balance_total, balance_pct) VALUES (%s,%s,%s,%s,%s)",
            [
                (args.date, cls, v["count"], v["balance"], v["balance_pct"])
                for cls, v in agg["by_class"].items()
            ],
        )
        wconn.commit()
        cur.close()
        wconn.close()
        print(
            f"[risk] ADS 表已写: dws_risk_class={len(rows)} ads_ltv_alerts={len(alerts)} ads_risk_class={len(agg['by_class'])}"
        )

    biz.close()
    crawl.close()


if __name__ == "__main__":
    main()
