#!/usr/bin/env python
"""S5 前端驾驶舱 · 数据访问层。

只读 MySQL（PyMySQL），按页组织查询；全部查询面向 200 行级明细，走简单聚合即可。
为什么不用 ORM / 独立配置：全仓唯一 Python 是 tools/orchestrator/.venv（conda spark 3.10），
其中已带 PyMySQL；连接参数直接复用 tools/risk/config 的 load_env / crawl_params，
保证「业务日、时区、库名」口径与风险引擎、报送链路完全一致（见 config.py 的说明）。

连接策略：每请求开短连接（autocommit），200 行级数据毫秒级返回；DDL（建确认表）走 root。
"""

import importlib.util
import os
import sys

import pymysql

# 复用 tools/risk/config：连接参数与业务日口径必须全仓一致。
_RISK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk")
sys.path.insert(0, _RISK_DIR)

# 显式按路径加载 risk/config，避免被同名顶层模块（如 tools/spatial/config）抢注
# sys.modules['config'] 导致 CLASS_ORDER 取错——测试混合跑 frontend/spatial 时会踩到。
_config_path = os.path.join(_RISK_DIR, "config.py")
_spec = importlib.util.spec_from_file_location("risk_config", _config_path)
config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config)  # noqa: E402

# 五级固定顺序：与 config.CLASS_ORDER 保持一致，前端排序依赖此顺序。
CLASS_ORDER = config.CLASS_ORDER
# 口径容差：余额按分（DECIMAL 四舍五入到 0.01）。
EPS_BALANCE = 0.01

# 预警来源标记：离线 T+1（ads_ltv_alerts）与实时（ads_stream_ltv_alerts）合并展示。
ALERT_SOURCES = {"offline", "stream"}


def _conn(params):
    """开一个短连接；调用方负责 close。"""
    return pymysql.connect(**params, charset="utf8mb4", autocommit=True)


def crawl_conn():
    """房产库只读连接（app 用户，与风险引擎读 ADS 同权限）。"""
    return _conn(config.crawl_params(config.load_env()))


def biz_conn():
    """业务库只读连接（root，读 collateral 地址/坐标）。"""
    return _conn(config.business_params(config.load_env()))


def ddl_conn():
    """root + 房产库：仅用于前端自用表（ads_alert_confirm）的建表 DDL。"""
    return _conn(config.root_crawl_params(config.load_env()))


# ---------------- 驾驶舱 ----------------


def dashboard():
    """驾驶舱聚合：KPI、五级分布、LTV 直方图、城市分布、高危区、预警概览。"""
    crawl = crawl_conn()
    biz = biz_conn()
    try:
        cur = crawl.cursor()

        # KPI：明细表实时口径（驾驶舱「当前快照」）。
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(balance),0), "
            "SUM(alert), SUM(low_confidence), SUM(is_high_risk_zone) "
            "FROM dws_risk_class"
        )
        row = cur.fetchone()
        kpi = {
            "loan_count": int(row[0]),
            "total_balance": float(row[1]),
            "alert_loans": int(row[2] or 0),
            "low_confidence": int(row[3] or 0),
            "high_risk_zone_loans": int(row[4] or 0),
        }

        # 五级分布：取 T+1 汇总表（ads_risk_class），驾驶舱口径 = 报送口径。
        cur.execute(
            "SELECT risk_class, loan_count, balance_total, balance_pct "
            "FROM ads_risk_class WHERE stat_date="
            "(SELECT MAX(stat_date) FROM ads_risk_class)"
        )
        got = {r[0]: r for r in cur.fetchall()}
        five_class = []
        for cls in CLASS_ORDER:
            r = got.get(cls)
            five_class.append(
                {
                    "risk_class": cls,
                    "loan_count": int(r[1]) if r else 0,
                    "balance_total": float(r[2]) if r else 0.0,
                    "balance_pct": float(r[3]) if r else 0.0,
                }
            )

        # LTV 分布直方图：明细实时口径，桶宽按红线 0.85 加密（0.05）。
        cur.execute("SELECT COALESCE(ltv, -1) FROM dws_risk_class")
        ltv_buckets = {
            b: 0
            for b in [
                "<0.50",
                "0.50-0.60",
                "0.60-0.70",
                "0.70-0.80",
                "0.80-0.85",
                "0.85-0.90",
                "0.90-1.00",
                ">1.00",
                "缺失",
            ]
        }
        for (ltv,) in cur.fetchall():
            if ltv is None or ltv < 0:
                ltv_buckets["缺失"] += 1
            elif ltv < 0.5:
                ltv_buckets["<0.50"] += 1
            elif ltv < 0.6:
                ltv_buckets["0.50-0.60"] += 1
            elif ltv < 0.7:
                ltv_buckets["0.60-0.70"] += 1
            elif ltv < 0.8:
                ltv_buckets["0.70-0.80"] += 1
            elif ltv < 0.85:
                ltv_buckets["0.80-0.85"] += 1
            elif ltv < 0.9:
                ltv_buckets["0.85-0.90"] += 1
            elif ltv <= 1.0:
                ltv_buckets["0.90-1.00"] += 1
            else:
                ltv_buckets[">1.00"] += 1
        ltv_hist = [{"bucket": k, "count": v} for k, v in ltv_buckets.items()]

        # 预警概览：离线/实时预警笔数与按风险类拆分。
        cur.execute(
            "SELECT src, risk_class, cnt FROM ("
            "SELECT 'offline' src, risk_class, COUNT(*) cnt FROM ads_ltv_alerts GROUP BY risk_class "
            "UNION ALL "
            "SELECT 'stream', risk_class, COUNT(*) cnt FROM ads_stream_ltv_alerts GROUP BY risk_class"
            ") t ORDER BY src"
        )
        alert_breakdown = {
            "offline": {r[1]: int(r[2]) for r in cur.fetchall() if r[0] == "offline"}
        }
        cur.execute(
            "SELECT 'stream', risk_class, COUNT(*) FROM ads_stream_ltv_alerts GROUP BY risk_class"
        )
        alert_breakdown["stream"] = {r[1]: int(r[2]) for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts")
        alert_breakdown["offline_total"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM ads_stream_ltv_alerts")
        alert_breakdown["stream_total"] = int(cur.fetchone()[0])

        # 城市分布：明细 join 业务库抵押物地址（dws_spatial_feature 的 district 全为 NULL，
        # 地址是最可靠的城市来源），按 CITY_MAP 解析「XX市」前缀。
        cur.execute(
            "SELECT r.collateral_id, r.balance, r.is_high_risk_zone "
            "FROM dws_risk_class r WHERE r.collateral_id IS NOT NULL"
        )
        dws_rows = cur.fetchall()
        bcur = biz.cursor()
        if dws_rows:
            cids = [r[0] for r in dws_rows]
            placeholders = ",".join(["%s"] * len(cids))
            bcur.execute(
                "SELECT collateral_id, property_addr FROM collateral "
                f"WHERE collateral_id IN ({placeholders})",
                cids,
            )
        else:
            bcur.execute("SELECT collateral_id, property_addr FROM collateral WHERE 1=0")
        addrs = {r[0]: r[1] for r in bcur.fetchall()}
        city_agg = {}
        city_risk = {}
        for cid, bal, high_risk in dws_rows:
            city = _city_from_addr(addrs.get(cid))
            agg = city_agg.setdefault(city, {"loan_count": 0, "balance_total": 0.0})
            agg["loan_count"] += 1
            agg["balance_total"] += float(bal or 0)
            if high_risk:
                city_risk[city] = city_risk.get(city, 0) + 1
        city_dist = [
            {
                "city": c,
                "loan_count": v["loan_count"],
                "balance_total": round(v["balance_total"], 2),
                "high_risk_loans": city_risk.get(c, 0),
            }
            for c, v in sorted(city_agg.items(), key=lambda kv: kv[1]["loan_count"], reverse=True)
        ]

        cur.close()
        return {
            "kpi": kpi,
            "five_class": five_class,
            "ltv_hist": ltv_hist,
            "city_dist": city_dist,
            "alert_breakdown": alert_breakdown,
        }
    finally:
        crawl.close()
        biz.close()


def _city_from_addr(addr):
    """从「广州市黄埔区…」取城市名；取不到标「未标注」。城市清单与 config.CITY_MAP 一致。"""
    if not addr:
        return "未标注"
    for city in config.CITY_MAP:
        if str(addr).startswith(city):
            return city
    return "未标注"


# ---------------- LTV 预警列表 ----------------


def alerts(
    risk_class=None,
    ltv_min=None,
    ltv_max=None,
    date_from=None,
    date_to=None,
    source=None,
    page=1,
    page_size=20,
):
    """离线 + 实时预警合并分页查询；返回总条数 + 本页明细（附抵押物地址）。"""
    where, params = [], []
    if risk_class:
        where.append("risk_class=%s")
        params.append(risk_class)
    if ltv_min is not None:
        where.append("ltv>=%s")
        params.append(ltv_min)
    if ltv_max is not None:
        where.append("ltv<=%s")
        params.append(ltv_max)
    if date_from:
        where.append("alert_date>=%s")
        params.append(date_from)
    if date_to:
        where.append("alert_date<=%s")
        params.append(date_to)
    if source and source in ALERT_SOURCES:
        where.append("src=%s")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    base = (
        "SELECT src, id, loan_id, customer_id, collateral_id, "
        "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
        "alert_date, alert_level "
        "FROM ("
        "SELECT 'offline' src, id, loan_id, customer_id, collateral_id, "
        "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
        "alert_date, alert_level "
        "FROM ads_ltv_alerts "
        "UNION ALL "
        "SELECT 'stream', event_id, loan_id, customer_id, collateral_id, "
        "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
        "alert_date, NULL "
        "FROM ads_stream_ltv_alerts"
        f") t {where_sql}"
    )

    crawl = crawl_conn()
    biz = biz_conn()
    try:
        cur = crawl.cursor()
        cur.execute(f"SELECT COUNT(*) FROM ({base}) t", params)
        total = int(cur.fetchone()[0])
        offset = max(0, (page - 1) * page_size)
        cur.execute(
            f"SELECT * FROM ({base}) t ORDER BY alert_date DESC, id DESC LIMIT %s OFFSET %s",
            [*params, page_size, offset],
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

        # 批量取抵押物地址（200 行级 IN 查询，避免 N+1）。
        cids = {r["collateral_id"] for r in rows if r.get("collateral_id") is not None}
        addr_map = {}
        if cids:
            bcur = biz.cursor()
            placeholders = ",".join(["%s"] * len(cids))
            bcur.execute(
                f"SELECT collateral_id, property_addr FROM collateral "
                f"WHERE collateral_id IN ({placeholders})",
                list(cids),
            )
            addr_map = {r[0]: r[1] for r in bcur.fetchall()}
            bcur.close()

        # 确认状态：按 (loan_id, alert_date, src) 关联前端自建确认表。
        # 表由 app 启动时用 root 建；若权限不足未建成，降级为无确认状态而不报错。
        conf_map = {}
        if rows:
            try:
                cur.execute(
                    "SELECT loan_id, alert_date, src, confirmed_by, confirmed_ts "
                    "FROM ads_alert_confirm"
                )
                for r in cur.fetchall():
                    conf_map[(r[0], str(r[1]), r[2])] = {
                        "confirmed_by": r[3],
                        "confirmed_ts": str(r[4]),
                    }
            except pymysql.err.ProgrammingError:
                conf_map = {}

        out = []
        for r in rows:
            key = (r["loan_id"], str(r["alert_date"]), r["src"])
            r["property_addr"] = addr_map.get(r.get("collateral_id")) or "未标注"
            r["confirmed"] = conf_map.get(key)
            out.append(r)
        cur.close()
        return {"total": total, "page": page, "page_size": page_size, "rows": out}
    finally:
        crawl.close()
        biz.close()


# ---------------- 预警确认（风控/管理员可写） ----------------


def ensure_alert_confirm_table():
    """幂等建前端自用确认表（root+房产库）。为什么单独建表：不改动预警链路既有表，
    确认动作只在这张前端表留痕，报表/推送链路不受影响。"""
    conn = ddl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ads_alert_confirm ("
            "id INT AUTO_INCREMENT PRIMARY KEY,"
            "loan_id INT NOT NULL,"
            "alert_date DATE NOT NULL,"
            "src VARCHAR(16) NOT NULL,"
            "confirmed_by VARCHAR(32) NOT NULL,"
            "confirmed_ts DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "UNIQUE KEY uq_loan_date_src (loan_id, alert_date, src)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def confirm_alert(loan_id, alert_date, source, user):
    """写入确认记录；重复确认幂等（UNIQUE 键 + ON DUPLICATE 覆盖）。"""
    ensure_alert_confirm_table()
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ads_alert_confirm (loan_id, alert_date, src, confirmed_by) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE confirmed_by=VALUES(confirmed_by), "
            "confirmed_ts=CURRENT_TIMESTAMP",
            (loan_id, alert_date, source, user),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ---------------- 操作审计（TC-06：导出/确认留痕） ----------------


def ensure_export_audit_table():
    """幂等建操作审计表（root+房产库）。独立于预警/确认链路，只做 who/when/what 留痕；
    不改动任何既有表，导出/确认失败也不影响业务主流程。"""
    conn = ddl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ads_export_audit ("
            "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
            "action VARCHAR(32) NOT NULL,"
            "username VARCHAR(32) NOT NULL,"
            "role VARCHAR(16) NOT NULL,"
            "detail TEXT,"
            "result VARCHAR(16) NOT NULL,"
            "ip VARCHAR(64),"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "KEY idx_action_ts (action, created_at)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def write_audit(action, username, role, detail, result, ip):
    """写一条操作审计记录（TC-06：who/role/when(created_at)/what(detail)/result/ip）。
    建表用 root，写用 app 用户（与确认表同权限）；表缺失时静默降级，不阻断导出/确认动作。"""
    ensure_export_audit_table()
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ads_export_audit (action, username, role, detail, result, ip) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (action, username, role, detail, result, ip),
        )
        conn.commit()
        cur.close()
    except pymysql.err.ProgrammingError:
        pass
    finally:
        conn.close()


# ---------------- 1104 报送 ----------------


def report(date=None):
    """G11 报送页：报表行 + 口径校验状态 + 阻断告警历史。

    校验逻辑与 tools/reporting/main.py 同源：以 dws_risk_class 明细聚合为裁判，
    比对 ads_1104_g11 各五级（含合计）的笔数与余额；任一超容差即标记不一致。
    为什么在页面实时复算：ads_report_alert 只在报送 CLI 被调用时落库，
    页面直接读明细能反映「当前 DB 是否仍一致」，与 CLI 阻断结论互相印证。
    """
    crawl = crawl_conn()
    try:
        cur = crawl.cursor()
        if not date:
            cur.execute("SELECT MAX(stat_date) FROM ads_1104_g11")
            row = cur.fetchone()
            date = str(row[0]) if row and row[0] else None
        if not date:
            return {"date": None, "rows": [], "consistent": None, "mismatches": [], "alerts": []}

        cur.execute(
            "SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, is_total, etl_ts "
            "FROM ads_1104_g11 WHERE stat_date=%s ORDER BY is_total ASC, "
            "FIELD(risk_class,%s,%s,%s,%s,%s)",
            (date, *CLASS_ORDER),
        )
        rows = []
        for r in cur.fetchall():
            rows.append(
                {
                    "stat_date": str(r[0]),
                    "risk_class": r[1],
                    "loan_count": int(r[2]),
                    "balance_total": float(r[3]),
                    "balance_pct": float(r[4]),
                    "is_total": bool(r[5]),
                    "etl_ts": str(r[6]),
                }
            )

        # dws 明细聚合（裁判出口）。
        cur.execute(
            "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) "
            "FROM dws_risk_class GROUP BY risk_class"
        )
        dws = {r[0]: (int(r[1]), float(r[2])) for r in cur.fetchall()}

        mismatches = []
        for r in rows:
            if r["is_total"]:
                continue
            cnt, bal = dws.get(r["risk_class"], (0, 0.0))
            if r["loan_count"] != cnt:
                mismatches.append(f"g11_vs_dws:{r['risk_class']}:loan_count")
            if abs(r["balance_total"] - bal) > EPS_BALANCE:
                mismatches.append(f"g11_vs_dws:{r['risk_class']}:balance")

        # 阻断告警历史。
        cur.execute(
            "SELECT id, report_date, alert_level, check_name, detail, etl_ts "
            "FROM ads_report_alert WHERE report_type='1104_g11' AND report_date=%s "
            "ORDER BY id DESC",
            (date,),
        )
        alerts = [
            {
                "id": r[0],
                "report_date": str(r[1]),
                "alert_level": r[2],
                "check_name": r[3],
                "detail": r[4],
                "etl_ts": str(r[5]),
            }
            for r in cur.fetchall()
        ]

        cur.close()
        return {
            "date": date,
            "rows": rows,
            "consistent": not mismatches,
            "mismatches": mismatches,
            "alerts": alerts,
        }
    finally:
        crawl.close()


def report_dates():
    """报送页日期下拉候选。"""
    crawl = crawl_conn()
    try:
        cur = crawl.cursor()
        cur.execute("SELECT DISTINCT stat_date FROM ads_1104_g11 ORDER BY stat_date DESC")
        dates = [str(r[0]) for r in cur.fetchall()]
        cur.close()
        return dates
    finally:
        crawl.close()
