#!/usr/bin/env python
"""P3 · 五级分类迁徙矩阵（US-02 / R-STA-01，主用户：数据分析师）。

为什么这个页面要自己造快照表：迁徙矩阵的本质是「同一批贷款在两个时点的分类对比」，
而 dws_risk_class 是**幂等覆盖写**的当前态明细（主键 loan_id，每次风险重算整表刷新），
历史分类被直接抹掉。ads_risk_class 虽有 stat_date，但只到「类」粒度（5 行），
丢了 loan_id 就无法追踪单笔的迁徙方向。因此必须在贷款粒度上留每日快照，
本模块建 dws_risk_class_snapshot（snap_date + loan_id）补上这一层。

三个入口：
  1. snapshot(date)      —— 把 dws_risk_class 当前态落成 date 的快照（真实数据）。
                            供 tools/pipeline/run_pipeline.py 在 risk 阶段之后调用，
                            或手工 CLI：python tools/frontend/pages/p3_migration.py --snapshot
  2. backfill_demo(...)  —— 【演示用】基于当前 200 行造历史快照，见函数注释里的免责说明。
  3. GET /api/migration  —— 页面接口：迁徙矩阵 + Roll Rate 趋势 + 可选日期列表。

CLI（可重复执行，主键幂等覆盖）：
    python tools/frontend/pages/p3_migration.py --backfill          # 造 6 期演示历史 + 今日真实快照
    python tools/frontend/pages/p3_migration.py --snapshot          # 只落今日真实快照
    python tools/frontend/pages/p3_migration.py --purge-demo        # 清掉所有演示回填数据
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta

# 既要能被 app.py 以 pages.p3_migration 导入，也要能 python 直接跑本文件当 CLI，
# 两种情况下 tools/frontend 与 tools/risk 都必须在 sys.path 上。
# tools/risk 显式注入而不是靠 db.py 的副作用——依赖 import 顺序会被 isort 重排打断。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RISK_DIR = os.path.join(os.path.dirname(_FRONTEND_DIR), "risk")
for _p in (_FRONTEND_DIR, _RISK_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 分类函数必须复用风险引擎的同一份 config.classify，
# 否则回填出来的历史分类与线上口径漂移，矩阵对角线会凭空产生假迁徙。
import config  # noqa: E402
import db  # noqa: E402

SNAPSHOT_TABLE = "dws_risk_class_snapshot"
CLASS_ORDER = db.CLASS_ORDER
# 类序号：数字越大越差，迁徙方向 = 期末序号 - 期初序号。
CLASS_IDX = {c: i for i, c in enumerate(CLASS_ORDER)}

# ---- 演示回填参数（仅 backfill_demo 使用，不影响真实快照）----
# 估值按期递减 1.0%：制造一个「行情下行 → LTV 抬升 → 分类下迁」的可讲故事的走势。
DEMO_VALUATION_DRIFT = 0.010
# 余额按期摊还 0.4%：越早的时点余额越高，与真实还款曲线方向一致。
DEMO_AMORT_PER_PERIOD = 0.004
# 单笔随机游走步长（标准差）。取 0.022 是为了让 6 期累计波动（≈5.4%）与累计趋势（≈6.2%）
# 量级相当：小于这个值整张矩阵只剩下三角，大于则趋势被噪声淹没，两种极端都不像真实组合。
DEMO_WALK_SIGMA = 0.022
DEMO_SEED = 20260805

_table_ready = False


# ---------------- 建表 / 写入 ----------------


def ensure_snapshot_table():
    """幂等建快照表（root+房产库，app 用户无 DDL 权限，与 db.ensure_alert_confirm_table 同策略）。

    is_demo 列是本表的关键设计：演示回填与真实快照混在同一张表里，
    必须在数据层面留下「这行是造出来的」标记，让接口和页面都能如实告知用户，
    而不是把回填数据包装成真实历史。
    """
    global _table_ready
    if _table_ready:
        return
    conn = db.ddl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} ("
            "snap_date DATE NOT NULL,"
            "loan_id INT NOT NULL,"
            "customer_id INT,"
            "collateral_id INT,"
            "balance DECIMAL(14,2),"
            "market_valuation DECIMAL(14,2),"
            "ltv DECIMAL(8,4),"
            "risk_class VARCHAR(8),"
            "is_demo TINYINT NOT NULL DEFAULT 0 COMMENT '1=演示回填，非真实历史',"
            "etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "PRIMARY KEY (snap_date, loan_id),"
            "KEY idx_date_class (snap_date, risk_class)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COMMENT='五级分类每日贷款级快照，供迁徙矩阵/Roll Rate 使用'"
        )
        conn.commit()
        cur.close()
        _table_ready = True
    finally:
        conn.close()


def _upsert_rows(rows):
    """按 (snap_date, loan_id) 幂等写入；重跑同一天覆盖而不是堆重复行。"""
    if not rows:
        return 0
    ensure_snapshot_table()
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        cur.executemany(
            f"INSERT INTO {SNAPSHOT_TABLE} "
            "(snap_date, loan_id, customer_id, collateral_id, balance, "
            "market_valuation, ltv, risk_class, is_demo) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE customer_id=VALUES(customer_id),"
            "collateral_id=VALUES(collateral_id),balance=VALUES(balance),"
            "market_valuation=VALUES(market_valuation),ltv=VALUES(ltv),"
            "risk_class=VALUES(risk_class),is_demo=VALUES(is_demo),"
            "etl_ts=CURRENT_TIMESTAMP",
            rows,
        )
        conn.commit()
        cur.close()
        return len(rows)
    finally:
        conn.close()


def _current_detail():
    """读 dws_risk_class 当前态明细（回填与真实快照的共同数据源）。"""
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT loan_id, customer_id, collateral_id, balance, "
            "market_valuation, ltv, risk_class FROM dws_risk_class"
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def snapshot(date=None):
    """把 dws_risk_class 当前态落成 date 的**真实**快照（is_demo=0）。

    为什么放在风险重算之后调用：dws_risk_class 是覆盖写，只有重算完成的那一刻
    才是该业务日的终态；提前落快照会把上一日的残留当成今日分类。
    """
    date = date or config.business_date()
    rows = [(date, r[0], r[1], r[2], r[3], r[4], r[5], r[6], 0) for r in _current_detail()]
    n = _upsert_rows(rows)
    return {"snap_date": date, "rows": n, "is_demo": False}


def backfill_demo(periods=6, step_days=7, end_date=None, seed=DEMO_SEED):
    """【演示数据 · 非真实历史】基于当前 200 行反推若干历史时点的分类快照。

    ⚠️ 这些行 is_demo=1，接口与页面都会显式标注「演示回填」。
    之所以要造：项目此前从未落过贷款级历史快照，DB 里只有一个时点，
    迁徙矩阵没有期初就无从展示。这里用「估值按期回溯 + 单笔随机游走」倒推过去的 LTV，
    再用风险引擎同一个 config.classify 重新定级——分类逻辑是真的，行情输入是造的。
    真实历史从 snapshot() 每日累积产生，两者可共存于同一张表，靠 is_demo 区分。

    幂等：同样的 seed + end_date 每次产出完全相同的行，主键覆盖写，可反复执行。
    """
    end_date = end_date or config.business_date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    detail = _current_detail()

    rows = []
    for loan_id, cust, coll, balance, valuation, ltv, risk_class in detail:
        # 每笔贷款一条独立但确定的随机游走：同一 loan 在相邻时点的扰动是累积的，
        # 若每期独立取随机数，贷款会在分类边界来回横跳，矩阵上会出现大量假迁徙。
        rnd = random.Random(f"{seed}:{loan_id}")
        cum = 0.0
        for k in range(1, periods + 1):
            cum += rnd.gauss(0, DEMO_WALK_SIGMA)
            snap_dt = end_dt - timedelta(days=step_days * k)
            if valuation is None or float(valuation) <= 0 or balance is None:
                # 估值缺失的贷款无法反推 LTV，原样保留当前分类，避免造出假的迁徙。
                rows.append(
                    (
                        snap_dt.isoformat(),
                        loan_id,
                        cust,
                        coll,
                        balance,
                        valuation,
                        ltv,
                        risk_class,
                        1,
                    )
                )
                continue
            # 越往前推：估值越高（行情下行前）、余额越高（还款更少）→ LTV 更低。
            val_k = float(valuation) * ((1 + DEMO_VALUATION_DRIFT) ** k) * (1 + cum)
            bal_k = float(balance) * (1 + DEMO_AMORT_PER_PERIOD * k)
            if val_k <= 0:
                val_k = float(valuation)
            ltv_k = bal_k / val_k
            rows.append(
                (
                    snap_dt.isoformat(),
                    loan_id,
                    cust,
                    coll,
                    round(bal_k, 2),
                    round(val_k, 2),
                    round(ltv_k, 4),
                    config.classify(ltv_k),
                    1,
                )
            )

    n = _upsert_rows(rows)
    # 最新一期用真实当前态收口：页面上「期末」永远是真数据，只有期初可能是演示回填。
    cur_snap = snapshot(end_date)
    return {
        "demo_rows": n,
        "demo_dates": periods,
        "latest": cur_snap,
    }


def purge_demo():
    """清掉全部演示回填行（保留真实快照），用于切换到真实历史积累模式。"""
    ensure_snapshot_table()
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {SNAPSHOT_TABLE} WHERE is_demo=1")
        conn.commit()
        n = cur.rowcount
        cur.close()
        return {"deleted": n}
    finally:
        conn.close()


# ---------------- 查询 ----------------


def _snapshot_dates(cur):
    """可选快照日期（含每期笔数与是否演示），倒序——页面默认取最近两期。"""
    cur.execute(
        f"SELECT snap_date, COUNT(*), MAX(is_demo) FROM {SNAPSHOT_TABLE} "
        "GROUP BY snap_date ORDER BY snap_date DESC"
    )
    return [
        {"snap_date": str(r[0]), "loan_count": int(r[1]), "is_demo": bool(r[2])}
        for r in cur.fetchall()
    ]


def _matrix(cur, date_from, date_to):
    """5×5 迁徙矩阵：行=期初分类，列=期末分类，按贷款号内联两个时点。

    只统计两期都存在的贷款（内联）；期间新增/退出单独计数，
    否则新增贷款会被算进某一行的分母，把迁徙率稀释掉。
    """
    cur.execute(
        f"SELECT a.risk_class, b.risk_class, COUNT(*), COALESCE(SUM(b.balance),0) "
        f"FROM {SNAPSHOT_TABLE} a JOIN {SNAPSHOT_TABLE} b "
        "ON a.loan_id=b.loan_id AND b.snap_date=%s "
        "WHERE a.snap_date=%s GROUP BY a.risk_class, b.risk_class",
        (date_to, date_from),
    )
    cells = {}
    for from_cls, to_cls, cnt, bal in cur.fetchall():
        cells[(from_cls, to_cls)] = (int(cnt), float(bal))

    matrix = []
    for from_cls in CLASS_ORDER:
        row = []
        for to_cls in CLASS_ORDER:
            cnt, bal = cells.get((from_cls, to_cls), (0, 0.0))
            row.append({"count": cnt, "balance": round(bal, 2)})
        matrix.append(row)

    # 行/列合计 + 行内占比（迁徙率的分母是期初该类的总笔数）。
    row_totals = []
    for i, from_cls in enumerate(CLASS_ORDER):
        total = sum(c["count"] for c in matrix[i])
        row_totals.append({"risk_class": from_cls, "count": total})
        for c in matrix[i]:
            c["pct"] = round(c["count"] / total, 4) if total else 0.0
    col_totals = [
        {
            "risk_class": to_cls,
            "count": sum(matrix[i][j]["count"] for i in range(len(CLASS_ORDER))),
        }
        for j, to_cls in enumerate(CLASS_ORDER)
    ]

    stay = up = down = 0
    down_balance = 0.0
    for i in range(len(CLASS_ORDER)):
        for j in range(len(CLASS_ORDER)):
            c = matrix[i][j]
            if not c["count"]:
                continue
            if i == j:
                stay += c["count"]
            elif j > i:  # 序号变大 = 分类变差 = 下迁
                down += c["count"]
                down_balance += c["balance"]
            else:
                up += c["count"]

    # 期间新增/退出：只出现在一侧的贷款号。
    cur.execute(
        f"SELECT SUM(b.loan_id IS NOT NULL AND a.loan_id IS NULL) FROM "
        f"(SELECT loan_id FROM {SNAPSHOT_TABLE} WHERE snap_date=%s) b "
        f"LEFT JOIN (SELECT loan_id FROM {SNAPSHOT_TABLE} WHERE snap_date=%s) a "
        "ON a.loan_id=b.loan_id",
        (date_to, date_from),
    )
    new_loans = int(cur.fetchone()[0] or 0)
    cur.execute(
        f"SELECT SUM(b.loan_id IS NULL) FROM "
        f"(SELECT loan_id FROM {SNAPSHOT_TABLE} WHERE snap_date=%s) a "
        f"LEFT JOIN (SELECT loan_id FROM {SNAPSHOT_TABLE} WHERE snap_date=%s) b "
        "ON a.loan_id=b.loan_id",
        (date_from, date_to),
    )
    exited_loans = int(cur.fetchone()[0] or 0)

    matched = stay + up + down
    return {
        "matrix": matrix,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "summary": {
            "matched": matched,
            "stay": stay,
            "up": up,
            "down": down,
            "down_pct": round(down / matched, 4) if matched else 0.0,
            "up_pct": round(up / matched, 4) if matched else 0.0,
            "down_balance": round(down_balance, 2),
            "new_loans": new_loans,
            "exited_loans": exited_loans,
        },
    }


def _roll_rate(cur, dates):
    """各类别下迁率（Roll Rate）随时间的变化。

    Roll Rate 口径：某期初分类 C 的贷款中，期末落到更差分类的笔数 / 期初 C 的总笔数。
    为什么用全部快照期而不是只用页面选中的期间：选中区间常常只有两期，
    画出来是一个孤点；趋势图的价值恰恰在于跨多期看恶化是否在加速。
    """
    if len(dates) < 2:
        return {"periods": [], "series": [], "overall": []}

    cur.execute(
        f"SELECT snap_date, loan_id, risk_class FROM {SNAPSHOT_TABLE} "
        f"WHERE snap_date IN ({','.join(['%s'] * len(dates))})",
        dates,
    )
    by_date = {d: {} for d in dates}
    for snap_date, loan_id, risk_class in cur.fetchall():
        by_date[str(snap_date)][loan_id] = risk_class

    periods, overall = [], []
    series = {c: [] for c in CLASS_ORDER}
    for prev, curr in zip(dates, dates[1:], strict=False):
        label = f"{prev[5:]}→{curr[5:]}"
        periods.append({"from": prev, "to": curr, "label": label})
        base = dict.fromkeys(CLASS_ORDER, 0)
        down = dict.fromkeys(CLASS_ORDER, 0)
        tot_base = tot_down = 0
        for loan_id, from_cls in by_date[prev].items():
            to_cls = by_date[curr].get(loan_id)
            if to_cls is None or from_cls not in CLASS_IDX or to_cls not in CLASS_IDX:
                continue
            base[from_cls] += 1
            tot_base += 1
            if CLASS_IDX[to_cls] > CLASS_IDX[from_cls]:
                down[from_cls] += 1
                tot_down += 1
        for cls in CLASS_ORDER:
            series[cls].append(
                {
                    "label": label,
                    "base": base[cls],
                    "down": down[cls],
                    # 「损失」是最差一级，没有更差可去，rate 恒为 0，前端会灰显。
                    "rate": round(down[cls] / base[cls], 4) if base[cls] else 0.0,
                }
            )
        overall.append(
            {
                "label": label,
                "base": tot_base,
                "down": tot_down,
                "rate": round(tot_down / tot_base, 4) if tot_base else 0.0,
            }
        )

    return {
        "periods": periods,
        "series": [{"risk_class": c, "points": series[c]} for c in CLASS_ORDER],
        "overall": overall,
    }


def migration(date_from=None, date_to=None):
    """迁徙矩阵页主查询：日期候选 + 矩阵 + Roll Rate 趋势。

    快照表不存在或不足两期时不报错，返回 message 交由页面提示（首次部署即为此状态）。
    """
    ensure_snapshot_table()
    conn = db.crawl_conn()
    try:
        cur = conn.cursor()
        dates = _snapshot_dates(cur)
        if len(dates) < 2:
            return {
                "dates": dates,
                "classes": CLASS_ORDER,
                "date_from": None,
                "date_to": None,
                "matrix": None,
                "roll_rate": {"periods": [], "series": [], "overall": []},
                "message": (
                    "快照不足两期，无法计算迁徙。请先执行 "
                    "python tools/frontend/pages/p3_migration.py --backfill"
                ),
            }

        available = [d["snap_date"] for d in dates]
        # 默认看最近一期迁徙（期初=次新，期末=最新）；非法日期直接回落到默认值，
        # 不抛 400——日期选择器的值来自本接口自己下发的列表，出错多半是快照被清过。
        date_to = date_to if date_to in available else available[0]
        date_from = date_from if date_from in available else available[1]
        if date_from > date_to:
            date_from, date_to = date_to, date_from

        result = _matrix(cur, date_from, date_to)
        asc_dates = sorted(available)
        roll = _roll_rate(cur, asc_dates)
        demo_dates = [d["snap_date"] for d in dates if d["is_demo"]]

        cur.close()
        return {
            "dates": dates,
            "classes": CLASS_ORDER,
            "date_from": date_from,
            "date_to": date_to,
            **result,
            "roll_rate": roll,
            "demo_dates": demo_dates,
            "message": None,
        }
    finally:
        conn.close()


# ---------------- 路由 ----------------


def handle_migration(ctx):
    """GET /api/migration?date_from=&date_to="""
    q = ctx.query
    return migration(
        date_from=(q.get("date_from", [None])[0] or None),
        date_to=(q.get("date_to", [None])[0] or None),
    )


PAGE = {
    "id": "migration",
    "label": "五级分类迁徙矩阵",
    # DA 是主用户；贷后（postloan）不涉及分类迁徙分析，按 PRD §7.3 不开放。
    "roles": {"admin", "risk", "da"},
    "order": 30,
    "js": "p3_migration.js",
    "routes": {
        ("GET", "/api/migration"): handle_migration,
    },
}


# ---------------- CLI ----------------


def main():
    ap = argparse.ArgumentParser(description="P3 迁徙矩阵 · 快照写入 / 演示回填")
    ap.add_argument("--snapshot", action="store_true", help="落一份当日真实快照")
    ap.add_argument("--backfill", action="store_true", help="生成演示历史快照（可重复执行）")
    ap.add_argument("--purge-demo", action="store_true", help="删除全部演示回填行")
    ap.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD，默认当前业务日")
    ap.add_argument("--periods", type=int, default=6, help="回填期数（默认 6）")
    ap.add_argument("--step-days", type=int, default=7, help="回填步长天数（默认 7）")
    args = ap.parse_args()

    if not (args.snapshot or args.backfill or args.purge_demo):
        ap.error("至少指定 --snapshot / --backfill / --purge-demo 之一")

    ensure_snapshot_table()
    if args.purge_demo:
        print(f"[p3] 清理演示数据 {purge_demo()}")
    if args.backfill:
        print(
            f"[p3] 演示回填（非真实历史，is_demo=1）"
            f"{backfill_demo(periods=args.periods, step_days=args.step_days, end_date=args.date)}"
        )
    elif args.snapshot:
        print(f"[p3] 真实快照 {snapshot(args.date)}")


if __name__ == "__main__":
    main()
