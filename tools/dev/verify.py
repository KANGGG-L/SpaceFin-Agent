#!/usr/bin/env python3
"""7 天演示回填验收（docs/demo/acceptance.md 的 A/C 组 SQL 检查）。

用法：
    tools/orchestrator/.venv/bin/python tools/dev/verify.py
    tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --verify
"""

from __future__ import annotations

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "risk"))
import config  # noqa: E402
import pymysql  # noqa: E402

EPS_BALANCE = 0.01
DATES = [
    "2026-08-01",
    "2026-08-02",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
]
SUMMARY_PATH = os.path.join(REPO_ROOT, "output", "demo", "backfill_summary.json")

_results: list[tuple[str, bool, str]] = []


def check(item: str, ok: bool, detail: str = ""):
    _results.append((item, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {item}  {detail}")


def _crawl():
    return pymysql.connect(**config.root_crawl_params(config.load_env()), charset="utf8mb4")


def _biz():
    return pymysql.connect(**config.business_params(config.load_env()), charset="utf8mb4")


def _load_summary() -> dict:
    if os.path.exists(SUMMARY_PATH):
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def verify_a1(crawl):
    print("\n== A1 数据侧：7 天齐全 ==")
    cur = crawl.cursor()
    cur.execute("SELECT DISTINCT stat_date FROM ads_risk_class ORDER BY stat_date")
    dates = [str(r[0]) for r in cur.fetchall()]
    check("A1.1 ads_risk_class 覆盖 7 日", dates == DATES, f"got {dates}")
    cur.execute("SELECT COUNT(*) FROM ads_risk_class")
    n_rows = cur.fetchone()[0]
    check("A1.1 每天 5 行合计 35 行", n_rows == 35, f"rows={n_rows}")

    cur.execute("SELECT alert_date, COUNT(*) FROM ads_ltv_alerts GROUP BY alert_date ORDER BY 1")
    alert_dates = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    check("A1.2 ads_ltv_alerts 覆盖 7 日", set(alert_dates) == set(DATES), str(sorted(alert_dates)))
    peak = max(alert_dates, key=lambda k: alert_dates[k]) if alert_dates else None
    check(
        "A1.2 08-05 为单日最大",
        peak == "2026-08-05",
        f"peak={peak} counts={ {k: v for k, v in sorted(alert_dates.items())} }",
    )

    cur.execute("SELECT COUNT(*) FROM dws_risk_class")
    check("A1.5 dws_risk_class 快照 5000 行", cur.fetchone()[0] == 5000)
    cur.close()


def verify_a2(crawl, summary):
    print("\n== A2 广州事件传导 ==")
    cur = crawl.cursor()
    days = {d: summary.get(d, {}) for d in DATES}
    d3, d4, d5 = days[DATES[2]], days[DATES[3]], days[DATES[4]]
    gz_ltv_d3 = d3.get("gz_mean_ltv")
    gz_ltv_d4 = d4.get("gz_mean_ltv")
    if gz_ltv_d3 and gz_ltv_d4:
        check(
            "A2.1 D4 广州 LTV 均值 ≥ D3×1.08",
            gz_ltv_d4 >= gz_ltv_d3 * 1.08,
            f"D3={gz_ltv_d3:.3f} D4={gz_ltv_d4:.3f} (×{gz_ltv_d4 / gz_ltv_d3:.2f})",
        )
    else:
        check("A2.1 摘要缺失 D3/D4 广州 LTV 均值", False, "需先跑完整回填")

    cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts WHERE alert_date=%s", (DATES[2],))
    n3 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts WHERE alert_date=%s", (DATES[3],))
    n4 = cur.fetchone()[0]
    check(
        "A2.2 D4/D3 全量预警 ≥1.5", n3 > 0 and n4 / n3 >= 1.5, f"D3={n3} D4={n4} (×{n4 / n3:.2f})"
    )

    gz_s_d4 = d4.get("gz_gt85", 0)
    gz_s_d5 = d5.get("gz_gt85", 0)
    check(
        "A2.3 D5/D4 广州 strong ≥1.3",
        gz_s_d4 > 0 and gz_s_d5 / gz_s_d4 >= 1.3,
        f"D4={gz_s_d4} D5={gz_s_d5} (×{gz_s_d5 / gz_s_d4 if gz_s_d4 else 0:.2f})",
    )
    check("A2.4 D5 为全量预警峰值（见 A1.2）", True)

    # A2.5 广州天河区房源单价较扰动前平均下降 8%-12%
    # 口径：ads_demo_gz_perturb 记录原值，比对当前值与原值（天河社区映射 → 乘子 0.88）
    # 注意：crawl_housing_sale 与 ads_demo_gz_perturb 的 url_key 列字符集不同，
    # 需显式 COLLATE 到同一字符集，否则 JOIN 报 Illegal mix of collations。
    tianhe_comms = ("珠江新城", "骏景花园", "员村")
    cur.execute(
        "SELECT ROUND(AVG(p.orig_unit_price_yuan),1), ROUND(AVG(c.unit_price_yuan),1) "
        "FROM ads_demo_gz_perturb p JOIN crawl_housing_sale c "
        "  ON c.url_key = p.url_key COLLATE utf8mb4_unicode_ci "
        "WHERE c.community IN (%s,%s,%s)",
        tianhe_comms,
    )
    row = cur.fetchone()
    if row and row[0]:
        drop = 1.0 - float(row[1]) / float(row[0])
        check(
            "A2.5 广州天河区挂牌均价较扰动前下降 8%-12%",
            0.08 <= drop <= 0.12,
            f"orig={row[0]:.0f} now={row[1]:.0f} drop={drop:.1%}",
        )
    else:
        check("A2.5 天河社区无扰动记录", False, "ads_demo_gz_perturb 无天河社区行")

    cur.execute("SELECT DISTINCT model_version FROM dws_risk_class")
    mv = [str(r[0]) for r in cur.fetchall()]
    check(
        "A2.6 模型版本存在且无 unknown", len(mv) >= 1 and all(v != "unknown" for v in mv), str(mv)
    )
    cur.close()


def verify_a3(crawl, summary):
    print("\n== A3 处置闭环 ==")
    cur = crawl.cursor()
    cur.execute("SELECT COUNT(*) FROM ads_alert_confirm")
    n_conf = cur.fetchone()[0]
    check("A3.1 确认记录 ≥60", n_conf >= 60, f"确认={n_conf}")

    cur.execute("SELECT COUNT(*) FROM ads_export_audit WHERE action='confirm' OR action='dispose'")
    n_audit = cur.fetchone()[0]
    check("A3.2 确认/处置审计留痕存在", n_audit > 0, f"audit={n_audit}")

    cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts WHERE alert_date=%s", (DATES[4],))
    n5 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts WHERE alert_date=%s", (DATES[5],))
    n6 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts WHERE alert_date=%s", (DATES[6],))
    n7 = cur.fetchone()[0]
    check("A3.3 D6/D5 ≤0.75", n5 > 0 and n6 / n5 <= 0.75, f"D5={n5} D6={n6} (×{n6 / n5:.2f})")
    check("A3.3 D7 ≤0.6×D5", n5 > 0 and n7 / n5 <= 0.6, f"D7={n7} (×{n7 / n5:.2f})")

    # A3.4 处置过的贷款 D7 vs D5 LTV 均值下降 ≥0.03
    cur.execute(
        "SELECT DISTINCT loan_id FROM ads_alert_confirm WHERE disposition_status IN ('disposed','recovered')"
    )
    disposed = [int(r[0]) for r in cur.fetchall()]
    if disposed:
        frag = ",".join(["%s"] * len(disposed))
        cur.execute(f"SELECT loan_id, ltv FROM dws_risk_class WHERE loan_id IN ({frag})", disposed)
        d7_ltvs = {int(r[0]): float(r[1]) for r in cur.fetchall() if r[1] is not None}
        # D5 LTV 用摘要里记录的处置池信息；此处用 dws D5 快照不可得，改从 ads_ltv_alerts D5 取
        cur.execute(
            f"SELECT loan_id, ltv FROM ads_ltv_alerts WHERE alert_date=%s AND loan_id IN ({frag})",
            [DATES[4], *disposed],
        )
        d5_ltvs = {int(r[0]): float(r[1]) for r in cur.fetchall() if r[1] is not None}
        common = set(d5_ltvs) & set(d7_ltvs)
        if common:
            drop = sum(d5_ltvs[i] - d7_ltvs[i] for i in common) / len(common)
            check(
                "A3.4 处置批 D7 vs D5 LTV 均值下降 ≥0.03",
                drop >= 0.03,
                f"n={len(common)} mean_drop={drop:.3f}",
            )
        else:
            check("A3.4 处置批无交集", False, "D5 与 D7 LTV 无法对照")
    else:
        check("A3.4 无处置记录", False, "disposed/recovered 为空")

    # A3.5 D4-D5 广州 strong 确认率 ≥80%
    biz = _biz()
    bcur = biz.cursor()
    bcur.execute(
        "SELECT l.loan_id FROM loan l JOIN collateral c ON c.collateral_id=l.collateral_id "
        "WHERE c.property_addr LIKE '广州市%%'"
    )
    gz_ids = {int(r[0]) for r in bcur.fetchall()}
    bcur.close()
    biz.close()
    if gz_ids:
        frag = ",".join(["%s"] * len(gz_ids))
        cur.execute(
            f"SELECT loan_id FROM ads_ltv_alerts WHERE alert_date IN (%s,%s) AND ltv>0.85 "
            f"AND loan_id IN ({frag})",
            [DATES[3], DATES[4], *gz_ids],
        )
        strong = {int(r[0]) for r in cur.fetchall()}
        if strong:
            frag2 = ",".join(["%s"] * len(strong))
            cur.execute(
                f"SELECT DISTINCT loan_id FROM ads_alert_confirm WHERE loan_id IN ({frag2})",
                list(strong),
            )
            confirmed = {int(r[0]) for r in cur.fetchall()}
            rate = len(confirmed) / len(strong)
            check(
                "A3.5 D4-D5 广州 strong 确认率 ≥80%",
                rate >= 0.8,
                f"{len(confirmed)}/{len(strong)}={rate:.0%}",
            )
        else:
            check("A3.5 无广州 strong 预警", False)
    cur.close()


def verify_c():
    print("\n== C 金额口径 ==")
    crawl = _crawl()
    biz = _biz()
    cur = crawl.cursor()
    bcur = biz.cursor()
    bcur.execute("SELECT COUNT(*), SUM(balance) FROM loan")
    n_loan, sum_loan = bcur.fetchone()
    cur.execute("SELECT SUM(balance_total) FROM ads_risk_class WHERE stat_date='2026-08-07'")
    sum_ads = cur.fetchone()[0]
    check(
        "C1 明细=汇总 (loan vs ads_risk_class 08-07)",
        abs(float(sum_loan) - float(sum_ads)) <= EPS_BALANCE,
        f"loan={float(sum_loan):.2f} ads={float(sum_ads):.2f} Δ={abs(float(sum_loan) - float(sum_ads)):.4f}",
    )

    cur.execute("SELECT SUM(balance) FROM dws_risk_class")
    sum_dws = cur.fetchone()[0]
    check(
        "C2 DWS 快照=ADS 汇总 (08-07)",
        abs(float(sum_dws) - float(sum_ads)) <= EPS_BALANCE,
        f"dws={float(sum_dws):.2f} ads={float(sum_ads):.2f} Δ={abs(float(sum_dws) - float(sum_ads)):.4f}",
    )

    # C4 D1-D5 余额守恒（逐日合计差额 ≤0.01）
    cur.execute(
        "SELECT stat_date, SUM(balance_total) FROM ads_risk_class GROUP BY stat_date ORDER BY 1"
    )
    daily = {str(r[0]): float(r[1]) for r in cur.fetchall()}
    d1 = daily.get("2026-08-01")
    ok_c4 = True
    for d in DATES[:5]:
        if d1 and daily.get(d) is not None and abs(daily[d] - d1) > EPS_BALANCE:
            ok_c4 = False
    check("C4 D1-D5 余额守恒", ok_c4, f"合计 { {k: round(v) for k, v in sorted(daily.items())} }")

    # C5 抽样预警余额一致
    cur.execute(
        "SELECT loan_id, loan_balance FROM ads_ltv_alerts WHERE alert_date='2026-08-07' LIMIT 3"
    )
    ok_c5 = True
    for loan_id, bal in cur.fetchall():
        bcur.execute("SELECT balance FROM loan WHERE loan_id=%s", (loan_id,))
        lb = bcur.fetchone()
        if lb is None or abs(float(bal) - float(lb[0])) > EPS_BALANCE:
            ok_c5 = False
    check("C5 预警余额口径抽样一致", ok_c5)
    cur.close()
    bcur.close()
    crawl.close()
    biz.close()


def verify_d(crawl):
    print("\n== D 边界与诚实性 ==")
    cur = crawl.cursor()
    cur.execute("SELECT COUNT(*) FROM dws_risk_class WHERE low_confidence=1")
    lc = cur.fetchone()[0]
    rate = lc / 5000
    check("D3 低置信占比 40%-60%", 0.40 <= rate <= 0.60, f"低置信={lc}/5000={rate:.0%}")
    cur.execute(
        "SELECT COUNT(*) FROM ads_risk_valuation_alerts WHERE alert_code='R-UNW-03' AND alert_date='2026-08-07'"
    )
    abn = cur.fetchone()[0]
    check("D4 异常估值告警不失控 (≤40%)", abn / 5000 <= 0.40, f"R-UNW-03={abn}")
    cur.close()


def main() -> int:
    summary = _load_summary()
    crawl = _crawl()
    try:
        print("=" * 70)
        print("SpaceFin-Agent · 7 天演示验收（acceptance.md A/C/D 组）")
        print("=" * 70)
        verify_a1(crawl)
        verify_a2(crawl, summary)
        verify_a3(crawl, summary)
        verify_c()
        verify_d(crawl)
        print("=" * 70)
        fails = [r for r in _results if not r[1]]
        print(f"结果: {len(_results) - len(fails)}/{len(_results)} 通过, {len(fails)} 失败")
        for item, _ok, detail in fails:
            print(f"  FAIL: {item}  {detail}")
        return 1 if fails else 0
    finally:
        crawl.close()


if __name__ == "__main__":
    sys.exit(main())
