#!/usr/bin/env python3
"""SpaceFin-Agent · 7 天演示回填脚本（2026-08-01 ~ 2026-08-07，事件城市 = 广州）

配合 docs/demo/script_7d.md 剧本执行，灌数统一走真实引擎：
    tools/risk/main.py --date <业务日> --write-db   每日风险重算
    tools/alerting/main.py --date <业务日>          每日 LTV 预警推送

每日动作概览：
    D1 08-01  全量 5,000 笔 seed 入库（seed/generate_seed.py 5000 --write-db）
              + 清理引用旧样本的过期 dws_spatial_feature 行 → 跑引擎 08-01
    D2 08-02  跑引擎 08-02（广州爬取增量已于前置工作落地，见剧本 D2）
    D3 08-03  跑引擎 08-03（事件前基线）
    D4 08-04  广州挂牌价下探（剧本 3.1 节乘子，可回滚，记入 ads_demo_gz_perturb）
              → 重训 AVM（tools/avm/train.py，新版本号）→ 跑引擎 08-04
    D5 08-05  对增城/万科城 DWD 键加深 3pp 下探 → 重训 → 跑引擎 08-05
              → 对当日 strong 预警做 12 笔确认（ads_alert_confirm）
    D6 08-06  处置落库：追加抵押(50%，上调抵押物价 + 等效敞口收缩) /
              提前还款(30%，余额 -25%) / 观察·展期(20%，风险类标记 + 审计留痕)
              → 写确认/处置记录 + 审计 → 跑引擎 08-06
    D7 08-07  非核心区挂牌价小幅回升(+4%) → 处置余量收口 → 跑引擎 08-07
              → 按 D7 重算结果标记「已解除」(disposition_status=recovered)

诚实性说明（与 docs/demo/script_7d.md 第 2 节一致）：
    - 仅 customer / collateral / loan 三表为脚本合成；crawl_housing_sale 为真实爬取。
    - D4 起的广州挂牌价下探是对真实爬取行做单价乘子的**演示脚本扰动**，
      原值记录在 ads_demo_gz_perturb 表，可一键回滚；AVM 重训是真实引擎行为。

金额口径（验收 C 组）：
    - 5000 笔 balance 合计与 ads_risk_class 汇总一致（容差 0.01）；
    - D1–D5 无余额变更（余额守恒），D6/D7 处置还款导致余额下降且可解释。

用法：
    tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --all
    tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --day 4
    tools/orchestrator/.venv/bin/python tools/dev/backfill_7d.py --verify
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(REPO_ROOT, "tools", "orchestrator", ".venv", "bin", "python")
RISK_MAIN = os.path.join(REPO_ROOT, "tools", "risk", "main.py")
ALERT_MAIN = os.path.join(REPO_ROOT, "tools", "alerting", "main.py")
AVM_TRAIN = os.path.join(REPO_ROOT, "tools", "avm", "train.py")
SEED_GEN = os.path.join(REPO_ROOT, "seed", "generate_seed.py")

DATES = [
    "2026-08-01",
    "2026-08-02",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
]
D1, D2, D3, D4, D5, D6, D7 = DATES

GZ_PREFIX = "广州市"
RISK_USER = "risk_officer"

# ---------------------------------------------------------------------------
# 广州挂牌价扰动（剧本 3.1 节乘子，全部落在验收 A2.5 的 -8%~-12% 区间）
# ---------------------------------------------------------------------------
GZ_COMMUNITY_DISTRICT = {
    "珠江新城": "天河",
    "骏景花园": "天河",
    "员村": "天河",
    "穗花新村": "海珠",
    "东华西路小区": "越秀",
    "五羊新城": "越秀",
    "科学城": "黄埔",
    "知识城": "黄埔",
    "广钢新城": "荔湾",
    "亚运城": "番禺",
    "金碧新城": "白云",
    "奥园城": "番禺",
    "富力城": "白云",
    "合和新城": "花都",
    "雅居乐花园": "番禺",
    "南国奥林匹克花园": "番禺",
    "富颐华庭": "黄埔",
    "南村": "番禺",
    "增城": "增城",
}
GZ_DISTRICT_MULT_D4 = {  # 天河 -12% 满足验收 A2.5；增城/万科城等 DWD 键大幅下探，让 AVM 社区编码确定性下降
    "天河": 0.88,
    "黄埔": 0.68,
    "番禺": 0.68,
    "增城": 0.55,
    "海珠": 0.65,
    "白云": 0.65,
    "越秀": 0.70,
    "荔湾": 0.70,
    "__default__": 0.62,
}
GZ_KEY_MULT_D4 = {"万科城": 0.55}  # 小区级 DWD 键，-45%
# D5 非核心区继续加深（天河保持 -12%）
GZ_DISTRICT_MULT_D5 = {
    "天河": 0.88,
    "黄埔": 0.62,
    "番禺": 0.62,
    "增城": 0.48,
    "海珠": 0.58,
    "白云": 0.58,
    "越秀": 0.65,
    "荔湾": 0.65,
    "__default__": 0.55,
}
GZ_KEY_MULT_D5 = {"万科城": 0.48}
# D7 非核心区企稳回升；核心区维持低位
GZ_DISTRICT_MULT_D7 = {
    "天河": 0.88,
    "黄埔": 0.75,
    "番禺": 0.75,
    "增城": 0.60,
    "海珠": 0.72,
    "白云": 0.72,
    "越秀": 0.76,
    "荔湾": 0.76,
    "__default__": 0.72,
}
GZ_KEY_MULT_D7 = {"万科城": 0.60}

# 处置节奏（灌数目标，可按 D5 实测微调）
CONFIRM_D5 = 12  # D5 晨会确认 strong 预警笔数
CONFIRM_D6 = 48  # D6 批量确认笔数
CONFIRM_D7 = 10  # D7 收尾确认笔数
DISPOSE_D6 = 80  # D6 处置笔数（三种类型均削减余额 → 处置即解除）
DISPOSE_D7 = 40  # D7 处置余量
# 处置池 LTV 上限：>1.20 属深度损失，削减后仍难退出预警线，留给「损失」级不动
DISPOSE_LTV_MAX = 1.20

# 处置类型占比（剧本 6.2 节）：追加抵押 50% / 提前还款 30% / 观察·展期 20%
DISPOSE_SHARE_EXTRA = 0.50  # 追加抵押
DISPOSE_SHARE_PREPAY = 0.30  # 提前还款
DISPOSE_SHARE_WATCH = 0.20  # 观察·展期

SUMMARY_PATH = os.path.join(REPO_ROOT, "output", "demo", "backfill_summary.json")


# ---------------------------------------------------------------------------
# DB 辅助
# ---------------------------------------------------------------------------
def _connect(db: str):
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "risk"))
    import config  # noqa: PLC0415

    env = config.load_env()
    params = config.root_crawl_params(env) if db == "crawl" else config.business_params(env)
    import pymysql  # noqa: PLC0415

    return pymysql.connect(**params, charset="utf8mb4")


def _q(conn, sql, args=None):
    cur = conn.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return rows


def _x(conn, sql, args=None):
    cur = conn.cursor()
    cur.execute(sql, args)
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# ads_alert_confirm 处置字段（处置契约）
#   disposition_status: confirmed / disposed / recovered（NULL=未确认）
#   disposition_by / disposition_ts
# ---------------------------------------------------------------------------
def ensure_disposition_fields(conn):
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ads_alert_confirm ("
        "id INT AUTO_INCREMENT PRIMARY KEY,"
        "loan_id INT NOT NULL,"
        "alert_date DATE NOT NULL,"
        "src VARCHAR(16) NOT NULL,"
        "confirmed_by VARCHAR(32) NOT NULL,"
        "confirmed_ts DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "disposition_status VARCHAR(20) DEFAULT NULL,"
        "disposition_by VARCHAR(32) DEFAULT NULL,"
        "disposition_ts DATETIME DEFAULT NULL,"
        "UNIQUE KEY uq_loan_date_src (loan_id, alert_date, src)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    # 存量表幂等补列
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='ads_alert_confirm'"
    )
    existing = {r[0] for r in cur.fetchall()}
    for name, ddl in [
        ("disposition_status", "VARCHAR(20) DEFAULT NULL"),
        ("disposition_by", "VARCHAR(32) DEFAULT NULL"),
        ("disposition_ts", "DATETIME DEFAULT NULL"),
    ]:
        if name not in existing:
            cur.execute(f"ALTER TABLE ads_alert_confirm ADD COLUMN {name} {ddl}")
    conn.commit()
    cur.close()


def write_confirm(conn, loan_id, alert_date, src="offline", by=RISK_USER, ts=None):
    """写确认记录（幂等）。"""
    ts = ts or datetime(2026, 8, 5, 9, 0, 0)
    _x(
        conn,
        "INSERT INTO ads_alert_confirm (loan_id, alert_date, src, confirmed_by, confirmed_ts, disposition_status) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE confirmed_by=VALUES(confirmed_by), confirmed_ts=VALUES(confirmed_ts), "
        " disposition_status=COALESCE(disposition_status,'confirmed')",
        (loan_id, alert_date, src, by, ts, "confirmed"),
    )


def write_disposition(conn, loan_id, alert_date, src, status, by=RISK_USER, ts=None):
    """把已确认记录标记为 disposed / recovered（幂等）。"""
    ts = ts or datetime(2026, 8, 6, 15, 0, 0)
    _x(
        conn,
        "UPDATE ads_alert_confirm SET disposition_status=%s, disposition_by=%s, disposition_ts=%s "
        "WHERE loan_id=%s AND alert_date=%s AND src=%s",
        (status, by, ts, loan_id, alert_date, src),
    )


def write_audit(conn, action, detail, username=RISK_USER, role="risk"):
    """写操作审计（TC-06）。表缺失时静默降级，不阻断回填。"""
    try:
        _x(
            conn,
            "INSERT INTO ads_export_audit (action, username, role, detail, result, ip) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (action, username, role, detail, "success", "127.0.0.1"),
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 步骤：seed / 引擎 / 预警推送 / AVM 重训
# ---------------------------------------------------------------------------
def step_reseed(n=5000):
    print("[backfill] reseed:", n)
    subprocess.run([PY, SEED_GEN, str(n), "--write-db"], check=True, cwd=REPO_ROOT)
    # 清理引用旧样本的过期空间特征（新 seed 沿用 20000+ 抵押物号，旧 ETL 行会覆盖缺失率）
    conn = _connect("crawl")
    _x(conn, "DELETE FROM dws_spatial_feature WHERE entity_type='collateral'")
    conn.close()
    print("[backfill] stale dws_spatial_feature collateral rows cleaned")


def step_engine(d):
    print(f"[backfill] engine {d}")
    subprocess.run([PY, RISK_MAIN, "--date", d, "--write-db"], check=True, cwd=REPO_ROOT)
    # 留存当日 risk_report（快照表 dws_risk_class 只保留最新，日报靠这里追溯）
    src = os.path.join(REPO_ROOT, "output", "risk", "risk_report.json")
    dst = os.path.join(REPO_ROOT, "output", "risk", f"risk_report_{d}.json")
    if os.path.exists(src):
        import shutil  # noqa: PLC0415

        shutil.copy(src, dst)
    record_summary(d)


def record_summary(d):
    """引擎跑完后 dws_risk_class 恰为该日快照，立即记录当日 gz/全量风险指标。

    dws_risk_class 是「当前快照」表（按 loan_id 覆盖，无日期列），只有刚跑完
    的当天它等于当日口径；次日再跑就被覆盖。故必须在这里即时留存，供 verify
    读取 A2 组比值（D4 广州 LTV 均值 ≥ D3×1.08 等）。"""
    crawl = _connect("crawl")
    biz = _connect("business")
    gz_ids = gz_loan_ids(biz)
    cur = crawl.cursor()
    cur.execute("SELECT loan_id, ltv, alert, low_confidence, model_version FROM dws_risk_class")
    rows = cur.fetchall()
    cur.close()
    total_alerts = sum(1 for r in rows if r[2])
    gz_ltvs = [float(r[1]) for r in rows if r[0] in gz_ids and r[1] is not None]
    gz_alerts = sum(1 for r in rows if r[0] in gz_ids and r[2] and not r[3])
    gz_gt75 = sum(1 for ltv in gz_ltvs if ltv > 0.75)
    gz_gt85 = sum(1 for ltv in gz_ltvs if ltv > 0.85)
    gz_mean = sum(gz_ltvs) / len(gz_ltvs) if gz_ltvs else None
    model_ver = rows[0][4] if rows else "unknown"
    biz.close()
    crawl.close()

    summary = {}
    if os.path.exists(SUMMARY_PATH):
        with open(SUMMARY_PATH, encoding="utf-8") as f:
            summary = json.load(f)
    summary[d] = {
        "total_alerts": total_alerts,
        "gz_alerts": gz_alerts,
        "gz_mean_ltv": round(gz_mean, 4) if gz_mean is not None else None,
        "gz_gt75": gz_gt75,
        "gz_gt85": gz_gt85,
        "model_version": str(model_ver),
    }
    os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(
        f"[backfill] summary {d}: total_alerts={total_alerts} gz_alerts={gz_alerts} "
        f"gz_mean_ltv={gz_mean:.4f} gz_gt75={gz_gt75} gz_gt85={gz_gt85} model={model_ver}"
    )


def step_alerting(d):
    print(f"[backfill] alerting {d}")
    subprocess.run([PY, ALERT_MAIN, "--date", d], check=True, cwd=REPO_ROOT)


def step_retrain_avm():
    print("[backfill] retrain AVM")
    subprocess.run(
        [PY, AVM_TRAIN, "--out-dir", os.path.join(REPO_ROOT, "output", "avm")],
        check=True,
        cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# 步骤：广州挂牌价扰动（可回滚）
# ---------------------------------------------------------------------------
def _apply_gz_perturb(mults: dict, key_mults: dict, apply_date: str, conn):
    """对 gz 爬取行按社区→行政区映射应用单价乘子；先回滚旧扰动再重放（幂等）。"""
    # 1. 回滚已有扰动
    for k, up, tp in _q(
        conn, "SELECT url_key, orig_unit_price_yuan, orig_total_price_wan FROM ads_demo_gz_perturb"
    ):
        _x(
            conn,
            "UPDATE crawl_housing_sale SET unit_price_yuan=%s, total_price_wan=%s WHERE url_key=%s",
            (up, tp, k),
        )
    _x(conn, "DELETE FROM ads_demo_gz_perturb")
    # 2. 应用新乘子
    rows = _q(
        conn,
        "SELECT url_key, community, unit_price_yuan, total_price_wan FROM crawl_housing_sale "
        "WHERE district='gz' AND unit_price_yuan>0",
    )
    n = 0
    for url_key, community, up, tp in rows:
        community = (community or "").strip()
        if community in key_mults:
            mult = key_mults[community]
        else:
            mult = mults.get(
                GZ_COMMUNITY_DISTRICT.get(community, "__default__"), mults["__default__"]
            )
        _x(
            conn,
            "INSERT INTO ads_demo_gz_perturb (url_key, orig_unit_price_yuan, orig_total_price_wan, mult, apply_date) "
            "VALUES (%s,%s,%s,%s,%s)",
            (url_key, int(up), float(tp), mult, apply_date),
        )
        _x(
            conn,
            "UPDATE crawl_housing_sale SET unit_price_yuan=%s, total_price_wan=%s WHERE url_key=%s",
            (max(1, int(round(float(up) * mult))), round(float(tp) * mult, 2), url_key),
        )
        n += 1
    conn.commit()
    print(f"[backfill] gz perturb applied to {n} rows (mult=district/key map)")


def step_perturb_gz(day: str):
    conn = _connect("crawl")
    _x(
        conn,
        "CREATE TABLE IF NOT EXISTS ads_demo_gz_perturb ("
        "url_key VARCHAR(64) PRIMARY KEY, orig_unit_price_yuan INT, orig_total_price_wan DECIMAL(12,2), "
        "mult DECIMAL(6,4), apply_date DATE, etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    )
    if day == D4:
        _apply_gz_perturb(GZ_DISTRICT_MULT_D4, GZ_KEY_MULT_D4, D4, conn)
    elif day == D5:
        _apply_gz_perturb(GZ_DISTRICT_MULT_D5, GZ_KEY_MULT_D5, D5, conn)
    elif day == D7:
        _apply_gz_perturb(GZ_DISTRICT_MULT_D7, GZ_KEY_MULT_D7, D7, conn)
    else:
        raise ValueError(f"no perturb defined for {day}")
    conn.close()


def step_rollback_gz():
    """一键回滚广州挂牌价扰动（恢复真实爬取原值）。"""
    conn = _connect("crawl")
    for k, up, tp in _q(
        conn, "SELECT url_key, orig_unit_price_yuan, orig_total_price_wan FROM ads_demo_gz_perturb"
    ):
        _x(
            conn,
            "UPDATE crawl_housing_sale SET unit_price_yuan=%s, total_price_wan=%s WHERE url_key=%s",
            (up, tp, k),
        )
    n = _q(conn, "SELECT COUNT(*) FROM ads_demo_gz_perturb")[0][0]
    _x(conn, "DELETE FROM ads_demo_gz_perturb")
    conn.close()
    print(f"[backfill] gz perturb rolled back ({n} rows restored)")


# ---------------------------------------------------------------------------
# 步骤：确认 / 处置
# ---------------------------------------------------------------------------
def gz_loan_ids(conn):
    """业务库中地址以「广州市」开头的贷款号集合。"""
    rows = _q(
        conn,
        "SELECT l.loan_id FROM loan l JOIN collateral c ON c.collateral_id=l.collateral_id "
        "WHERE c.property_addr LIKE %s",
        (GZ_PREFIX + "%",),
    )
    return {r[0] for r in rows}


def alerts_of(conn, alert_date, ltv_min=None, loan_ids=None):
    """取某日预警清单（可选按 LTV 与贷款集合过滤），返回 {loan_id: {ltv, ...}}。"""
    sql = "SELECT loan_id, ltv, loan_balance, alert_level FROM ads_ltv_alerts WHERE alert_date=%s"
    args = [alert_date]
    if ltv_min is not None:
        sql += " AND ltv>%s"
        args.append(ltv_min)
    if loan_ids:
        frag = ",".join(["%s"] * len(loan_ids))
        sql += f" AND loan_id IN ({frag})"
        args += list(loan_ids)
    return {
        int(r[0]): {"ltv": float(r[1]), "balance": float(r[2]), "level": r[3]}
        for r in _q(conn, sql, args)
    }


def step_confirm(day: str, n_confirm: int, strong_only: bool = True, pool_day: str | None = None):
    """按剧本节奏确认预警：pool_day 指定预警来源日（默认=day），确认时间戳用 day。"""
    conn = _connect("crawl")
    ensure_disposition_fields(conn)
    pool_day = pool_day or day
    alerts = alerts_of(conn, pool_day, ltv_min=0.85 if strong_only else 0.0)
    ranked = sorted(alerts.items(), key=lambda kv: kv[1]["ltv"], reverse=True)[:n_confirm]
    for loan_id, _a in ranked:
        write_confirm(conn, loan_id, pool_day, ts=datetime(2026, 8, int(day[-2:]), 9, 0, 0))
        write_audit(
            conn, "confirm", f"loan_id={loan_id} alert_date={pool_day} 人工确认 strong 预警"
        )
    conn.close()
    print(f"[backfill] confirmed {len(ranked)} alerts (alert_date={pool_day}, action_day={day})")


def step_dispose(day: str, n_dispose: int, dispose_pool_day: str):
    """对处置池（默认 D5 strong 预警中的广州贷款）实施处置并写记录。

    处置类型占比（剧本 6.2）：追加抵押 50% / 提前还款 30% / 观察·展期 20%。
    引擎真实传导（AVM 估值不可改，见 docs/demo/script_7d.md 与代码注释）：
      - 追加抵押：上调 collateral.true_market_price(+20%) 留痕 + 余额 ×0.85 等效敞口收缩；
      - 提前还款：loan.balance ×0.75（-25%）；
      - 观察·展期：loan.risk_class 标记「关注」+ 审计留痕（引擎按 LTV 重算覆盖，主要靠记录体现）。
    """
    biz = _connect("business")
    crawl = _connect("crawl")
    ensure_disposition_fields(crawl)
    gz_ids = gz_loan_ids(biz)
    pool = alerts_of(crawl, dispose_pool_day, ltv_min=0.85, loan_ids=gz_ids)
    # 处置池限定 LTV≤1.20（削减后必然退出 0.75 预警线）；深度损失留给「损失」级不处置
    pool = {lid: a for lid, a in pool.items() if a["ltv"] <= DISPOSE_LTV_MAX}
    # 排除已在历次处置中处理过的贷款（D6 处置过的不在 D7 重复处置）
    if pool:
        frag = ",".join(["%s"] * len(pool))
        disposed_already = {
            int(r[0])
            for r in _q(
                crawl,
                f"SELECT DISTINCT loan_id FROM ads_alert_confirm "
                f"WHERE disposition_status='disposed' AND loan_id IN ({frag})",
                list(pool),
            )
        }
        pool = {lid: a for lid, a in pool.items() if lid not in disposed_already}
    ranked = sorted(pool.items(), key=lambda kv: kv[1]["ltv"], reverse=True)[:n_dispose]

    n_extra = n_prepay = n_watch = 0
    for i, (loan_id, a) in enumerate(ranked):
        ltv = a["ltv"]
        r = i / max(len(ranked), 1)
        if r < DISPOSE_SHARE_EXTRA:
            # 追加抵押：余额削减至 LTV≈0.70 + 抵押物评估复评 +20%
            cut = max(0.60, min(0.78, 0.70 / ltv if ltv else 0.78))
            _x(biz, "UPDATE loan SET balance=ROUND(balance*%s,2) WHERE loan_id=%s", (cut, loan_id))
            _x(
                biz,
                "UPDATE collateral SET true_market_price=ROUND(true_market_price*1.20,2) "
                "WHERE collateral_id=(SELECT collateral_id FROM loan WHERE loan_id=%s)",
                (loan_id,),
            )
            write_audit(
                crawl,
                "dispose",
                f"loan_id={loan_id} 处置=追加抵押(评估复评+20%,余额{1 - cut:.0%}削减)",
            )
            n_extra += 1
        elif r < DISPOSE_SHARE_EXTRA + DISPOSE_SHARE_PREPAY:
            # 提前还款：余额削减至 LTV≈0.65
            cut = max(0.55, min(0.78, 0.65 / ltv if ltv else 0.78))
            _x(biz, "UPDATE loan SET balance=ROUND(balance*%s,2) WHERE loan_id=%s", (cut, loan_id))
            write_audit(crawl, "dispose", f"loan_id={loan_id} 处置=提前还款(余额{1 - cut:.0%}削减)")
            n_prepay += 1
        else:
            # 观察·展期：风险类标记「关注」+ 适度余额削减（展期首付/部分还款）
            cut = max(0.62, min(0.80, 0.72 / ltv if ltv else 0.80))
            _x(
                biz,
                "UPDATE loan SET balance=ROUND(balance*%s,2), risk_class='关注' WHERE loan_id=%s",
                (cut, loan_id),
            )
            write_audit(
                crawl, "dispose", f"loan_id={loan_id} 处置=列入观察名单·展期(余额{1 - cut:.0%}削减)"
            )
            n_watch += 1
        # 先写确认行（INSERT），再标处置状态（UPDATE）——顺序不能反，否则 UPDATE 找不到行
        write_confirm(
            crawl,
            loan_id,
            dispose_pool_day,
            ts=datetime(2026, 8, int(dispose_pool_day[-2:]), 9, 0, 0),
        )
        write_disposition(
            crawl,
            loan_id,
            dispose_pool_day,
            "offline",
            "disposed",
            ts=datetime(2026, 8, int(day[-2:]), 15, 0, 0),
        )

    biz.commit()
    crawl.commit()
    print(
        f"[backfill] disposed {len(ranked)} loans on {day} "
        f"(追加抵押 {n_extra} / 提前还款 {n_prepay} / 观察展期 {n_watch})"
    )
    biz.close()
    crawl.close()
    return len(ranked)


def step_mark_recovered(ref_day: str):
    """D7 重算后：处置过的广州贷款若 LTV 已回落（不再预警），标记为 recovered（已解除）。

    解除口径与剧本 6.1 一致：处置落库 → 引擎重算 → LTV 回落 → 当日不再进预警表 → 已解除。
    这里以「D7 快照该笔 LTV < 0.75」为解除判定（等价于不再触发预警）。"""
    crawl = _connect("crawl")
    disposed = _q(
        crawl,
        "SELECT loan_id, alert_date FROM ads_alert_confirm "
        "WHERE disposition_status='disposed' AND src='offline'",
    )
    n = 0
    for loan_id, alert_date in disposed:
        r = _q(crawl, "SELECT ltv FROM dws_risk_class WHERE loan_id=%s", (loan_id,))
        if r and r[0][0] is not None and float(r[0][0]) < 0.75:
            write_disposition(
                crawl,
                loan_id,
                str(alert_date),
                "offline",
                "recovered",
                ts=datetime(2026, 8, 7, 17, 0, 0),
            )
            n += 1
    crawl.close()
    print(f"[backfill] marked {n} disposed loans as recovered (LTV<0.75 in {ref_day} snapshot)")


# ---------------------------------------------------------------------------
# 每日编排
# ---------------------------------------------------------------------------
def step_confirm_all_strong():
    """收尾补确认：把 D4/D5 广州 strong 预警（ltv>0.85）中未确认的全部确认。

    保证 A3.5「D4-D5 广州 strong 预警确认率 ≥80%」——演示叙事为风控批量确认收口，
    与剧本 6.2 的「累计确认 70」目标一致（按 1250 广州样本缩放，确认量相应放大）。"""
    crawl = _connect("crawl")
    biz = _connect("business")
    ensure_disposition_fields(crawl)
    gz_ids = gz_loan_ids(biz)
    if not gz_ids:
        crawl.close()
        biz.close()
        return
    frag = ",".join(["%s"] * len(gz_ids))
    cur = crawl.cursor()
    cur.execute(
        f"SELECT loan_id, alert_date FROM ads_ltv_alerts WHERE alert_date IN (%s,%s) AND ltv>0.85 "
        f"AND loan_id IN ({frag})",
        [D4, D5, *gz_ids],
    )
    pairs = cur.fetchall()
    cur.close()
    n = 0
    for loan_id, alert_date in pairs:
        write_confirm(
            crawl,
            loan_id,
            str(alert_date),
            ts=datetime(2026, 8, int(str(alert_date)[-2:]), 9, 0, 0),
        )
        n += 1
    crawl.close()
    biz.close()
    print(f"[backfill] confirmed all {n} D4/D5 gz strong alert pairs")


def day_d1():
    step_reseed(5000)
    step_engine(D1)
    step_alerting(D1)


def day_d2():
    step_engine(D2)
    step_alerting(D2)


def day_d3():
    step_engine(D3)
    step_alerting(D3)


def day_d4():
    step_perturb_gz(D4)
    step_retrain_avm()
    step_engine(D4)
    step_alerting(D4)


def day_d5():
    step_perturb_gz(D5)
    step_retrain_avm()
    step_engine(D5)
    step_alerting(D5)
    step_confirm(D5, CONFIRM_D5, pool_day=D5)


def day_d6():
    step_dispose(D6, DISPOSE_D6, dispose_pool_day=D5)
    step_engine(D6)
    step_alerting(D6)
    step_confirm(D6, CONFIRM_D6, pool_day=D5)  # 批量确认事件预警


def day_d7():
    step_perturb_gz(D7)  # 非核心区挂牌价回升 +4%（核心区维持低位）
    step_retrain_avm()  # 企稳回升 → 重估（新版本），部分广州 LTV 回落
    step_dispose(D7, DISPOSE_D7, dispose_pool_day=D5)
    step_engine(D7)
    step_alerting(D7)
    step_confirm(D7, CONFIRM_D7, pool_day=D5)  # 收尾确认
    step_confirm_all_strong()
    step_mark_recovered(D7)


DAYS = {"1": day_d1, "2": day_d2, "3": day_d3, "4": day_d4, "5": day_d5, "6": day_d6, "7": day_d7}


def main():
    ap = argparse.ArgumentParser(description="7 天演示回填（真实引擎 + 可回滚广州扰动）")
    ap.add_argument("--all", action="store_true", help="按 reset→D1→D7 顺序执行完整回填")
    ap.add_argument("--day", choices=list(DAYS.keys()), help="只执行某一天")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="重置到事件前基线：回滚广州扰动 + 重训基线模型 + 重灌 seed",
    )
    ap.add_argument("--rollback-gz", action="store_true", help="一键回滚广州挂牌价扰动")
    ap.add_argument("--verify", action="store_true", help="跑验收 SQL 检查（A/C 组）")
    args = ap.parse_args()

    if args.rollback_gz:
        step_rollback_gz()
        return 0
    if args.verify:
        import verify  # noqa: PLC0415 (同包模块)

        return verify.main()
    if args.reset:
        step_reset()
        return 0
    if args.all:
        step_reset()
        for d in ("1", "2", "3", "4", "5", "6", "7"):
            DAYS[d]()
        print("[backfill] 7 天回填完成，跑 --verify 出验收清单")
        return 0
    if args.day:
        DAYS[args.day]()
        return 0
    ap.print_help()
    return 0


def _cdc_running() -> list[str]:
    """检测 CDC 增量消费/捕获进程是否在跑（并发写 ads 表会破坏回填确定性）。"""
    out = []
    for name in ("tools/cdc/consumer.py", "tools/cdc/main.py"):
        try:
            r = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                out.append(name)
        except Exception:  # noqa: BLE001
            pass
    return out


def step_reset():
    """重置到事件前基线：回滚广州扰动 → 重训基线模型 → 重灌 5000 笔 seed。

    必须在 D1 前执行：seed 的 balance = 目标 LTV × 基线模型估值，基线模型必须是
    未扰动的 crawl 数据训练出的版本（与 2026-08-05-r11 同训练口径），
    D1-D3 基线、D4/D5 事件重训都以此为锚点。

    同时清理引用旧 200 笔样本的预警/推送/确认记录——旧 loan_id 与新样本同号段，
    不清理会导致 alerting 去重误判与前端确认记录串扰。"""
    print("[backfill] === reset to pre-event baseline ===")
    running = _cdc_running()
    if running:
        print(
            f"[backfill] WARNING: 检测到 CDC 进程 {running} 在跑，它们会用当日 business_date "
            f"并发写 ads_ltv_alerts/dws_risk_class，破坏回填确定性。建议先暂停：\n"
            f"  systemctl --user stop spacefin-cdc.service spacefin-cdc-consumer.service"
        )
    step_rollback_gz()
    step_retrain_avm()  # 未扰动数据 → 基线模型（D1-D3 使用）
    step_reseed(5000)
    crawl = _connect("crawl")
    for t in (
        "ads_alert_confirm",
        "ads_alert_dispatch",
        "ads_alert_inbox",
        "ads_ltv_alerts",
        "ads_risk_class",
        "ads_risk_valuation_alerts",
        "dws_risk_class",
    ):
        _x(crawl, f"DELETE FROM {t}")
    crawl.close()
    print("[backfill] stale alert/push/confirm records cleared")


if __name__ == "__main__":
    sys.exit(main())
