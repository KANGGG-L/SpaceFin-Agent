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
import store
import valuation

# 城市映射统一放 config.CITY_MAP（增量消费链共用），此处保留别名兼容既有引用。
CITY_MAP = config.CITY_MAP

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


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])


def main():
    ap = argparse.ArgumentParser()
    # 默认业务日期按 Asia/Shanghai（config.business_date），不用本地时区：见 config.BUSINESS_TZ
    ap.add_argument("--date", default=config.business_date())
    ap.add_argument("--out-dir", default="output/risk")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印，不写库")
    ap.add_argument("--write-db", action="store_true", help="写入 MySQL ADS 表（默认只落 CSV）")
    args = ap.parse_args()

    env = config.load_env()
    biz = pymysql.connect(**config.business_params(env), charset="utf8mb4")
    crawl = pymysql.connect(**config.crawl_params(env), charset="utf8mb4")
    t0 = time.time()

    loans = store.load_loans(biz)
    collaterals = store.load_collaterals(biz)
    customers = store.load_customers(biz)
    dwd_unit = valuation.load_dwd_unit_prices(crawl)
    avm_model = valuation.load_avm_model()
    print(
        f"[risk] loans={len(loans)} collaterals={len(collaterals)} dwd_districts={len(dwd_unit)} "
        f"avm_model={'loaded' if avm_model else 'absent'}"
    )

    rows = store.compute_rows(loans, collaterals, customers, dwd_unit, avm_model)

    agg = risk_engine.build_aggregate(rows)
    alerts = [r for r in rows if r["alert"]]
    dwd_hits = sum(1 for r in rows if r.get("dwd_hit"))
    avm_hits = sum(1 for r in rows if r.get("avm_hit"))

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
        "valuation_src": {
            "avm_hits": avm_hits,
            "dwd_hits": dwd_hits,
            "fallback_true_market": len(rows) - avm_hits - dwd_hits,
        },
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
        # 写库全部走 store：与 tools/cdc/consumer.py 的增量路径共用同一套 UPSERT/预警语义，
        # 保证「全量重算」与「增量重算」结果可互相验证。
        wconn = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
        store.ensure_ads_tables(wconn)
        # 全量口径：业务库已不存在的贷款要从 DWS 清掉（增量 DELETE 事件漏消费时的兜底对账）。
        cur = wconn.cursor()
        cur.execute("SELECT loan_id FROM dws_risk_class")
        stale = {r[0] for r in cur.fetchall()} - {r["loan_id"] for r in rows}
        cur.close()
        store.delete_loans(wconn, sorted(stale), args.date)
        store.upsert_dws(wconn, rows)
        store.replace_alerts(wconn, rows, args.date)
        store.refresh_ads_risk_class(wconn, args.date)
        wconn.close()
        print(
            f"[risk] ADS 表已写: dws_risk_class={len(rows)}(清理陈旧 {len(stale)}) "
            f"ads_ltv_alerts={len(alerts)} ads_risk_class={len(agg['by_class'])}"
        )

    biz.close()
    crawl.close()


if __name__ == "__main__":
    main()
