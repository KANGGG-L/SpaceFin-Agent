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

# 信任卡「数据新鲜度」分层阈值（秒）：与 pages/p1_datasource 的 DWD/DWS/ADS 同口径——
# T+1 批跑 26h 未更新 = warn（黄）、48h = bad（红）。驾驶舱信任卡简化为一卡，
# 但判定尺子必须与 P1 数据底座页一致，避免两个页面口径打架。
FRESHNESS_WARN_SECONDS = 26 * 3600
FRESHNESS_BAD_SECONDS = 48 * 3600

# 信任卡「新鲜度」展示的分层（表名, 时间列, 中文说明），取每层最关键的一张主表：
# DWD=房源挂牌 / DWS=风险宽表 / ADS=离线预警。实时预警表单独口径，不进这张卡。
FRESHNESS_LAYERS = [
    ("DWD", "crawl_housing_sale", "etl_ts", "房源挂牌"),
    ("DWS", "dws_risk_class", "etl_ts", "风险宽表"),
    ("ADS", "ads_ltv_alerts", "etl_ts", "离线预警"),
]


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

        # 数据底座信任卡：爬取规模 / 新鲜度 / 解析成功率 / 模型版本。
        # 全部来自真实运行态，无 mock——爬取规模与 P1 登记表、README 的数字互相印证。
        cur.execute("SELECT COUNT(*) FROM crawl_housing_sale")
        sale_rows = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM crawl_housing_rent")
        rent_rows = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM community_coords")
        coords_rows = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(DISTINCT district) FROM crawl_housing_sale")
        crawl_scale = {
            "sale": sale_rows,
            "rent": rent_rows,
            "coords": coords_rows,
            "cities": int(cur.fetchone()[0]),
        }

        # 新鲜度：各层 MAX(时间列) 距今，状态 = ok(绿)/warn(黄)/bad(红)/empty(灰)。
        # 年龄一律在 SQL 侧用 TIMESTAMPDIFF(..., NOW()) 算（宿主 UTC / MySQL +08 的时区坑，
        # 与 P1 同一原则：绝不把 naive datetime 拿到 Python 里减）。
        freshness_layers = []
        for layer, table, ts_col, note in FRESHNESS_LAYERS:
            cur.execute(
                f"SELECT COUNT(*), MAX({ts_col}), "
                f"TIMESTAMPDIFF(SECOND, MAX({ts_col}), NOW()) FROM {table}"
            )
            r = cur.fetchone()
            rows, last, age = int(r[0]), r[1], r[2]
            if rows == 0 or age is None:
                status = "empty"
            elif age >= FRESHNESS_BAD_SECONDS:
                status = "bad"
            elif age >= FRESHNESS_WARN_SECONDS:
                status = "warn"
            else:
                status = "ok"
            freshness_layers.append(
                {
                    "layer": layer,
                    "note": note,
                    "table": table,
                    "rows": rows,
                    "last_update": str(last) if last else None,
                    "age_seconds": int(age) if age is not None else None,
                    "status": status,
                }
            )
        # 整体状态取最差一层（含 bad→bad，含 warn→warn，否则 ok/empty）。
        overall = "ok"
        if any(layer["status"] == "bad" for layer in freshness_layers):
            overall = "bad"
        elif any(layer["status"] == "warn" for layer in freshness_layers):
            overall = "warn"
        elif all(layer["status"] == "empty" for layer in freshness_layers):
            overall = "empty"

        # 解析成功率：合并 sale+rent 两张挂牌表的 geocode_status（hit/miss/pending）。
        # hit=解析成功 / miss=失败 / pending=待解析（配额未回填）。成功率只算 hit/(hit+miss)，
        # pending 单独展示「待解析量」，否则 44k 房源里 3 万条 pending 会把成功率砸到 30%，
        # 而实际是「还没轮到解析」，不是解析质量差。
        geocode = {}
        for t in ("crawl_housing_sale", "crawl_housing_rent"):
            cur.execute(f"SELECT geocode_status, COUNT(*) FROM {t} GROUP BY geocode_status")
            for status, cnt in cur.fetchall():
                geocode[status] = geocode.get(status, 0) + int(cnt)
        hit, miss, pending = (
            geocode.get("hit", 0),
            geocode.get("miss", 0),
            geocode.get("pending", 0),
        )
        # 双口径（信任卡「解析成功率」）：rate = 已解析(hit+miss)中的命中率；
        # pending_pct = 待解析占全部挂牌量(hit+miss+pending)的比例——两个口径必须
        # 并排放，否则 44k 房源里 3 万条 pending 会被 95.32% 掩盖（那是「还没轮到
        # 解析」，不是解析质量差）。total = 挂牌总量，供前端计算占比展示。
        parse_success = {
            "success": hit,
            "failed": miss,
            "pending": pending,
            "total": hit + miss + pending,
            "rate": round(hit / (hit + miss) * 100, 2) if (hit + miss) else None,
            "pending_pct": round(pending / (hit + miss + pending) * 100, 2)
            if (hit + miss + pending)
            else None,
        }

        # 模型版本：dws_risk_class 最新产出的估值模型（按 etl_ts 取最新一组）。
        cur.execute(
            "SELECT model_version, MAX(etl_ts), COUNT(*) FROM dws_risk_class "
            "GROUP BY model_version ORDER BY MAX(etl_ts) DESC LIMIT 1"
        )
        mv = cur.fetchone()
        model_version = {
            "version": str(mv[0]) if mv and mv[0] else None,
            "etl_ts": str(mv[1]) if mv and mv[1] else None,
            "loan_count": int(mv[2]) if mv else 0,
        }

        trust_cards = {
            "crawl_scale": crawl_scale,
            "freshness": {"layers": freshness_layers, "overall": overall},
            "parse_success": parse_success,
            "model_version": model_version,
        }

        # 预警闭环漏斗：预警 → 确认 → 处置 → 恢复。
        # 预警量 = 离线 + 实时；确认量 = confirmed_by 非空的行（处置契约：
        # 有 confirmed_by/confirmed_ts 才算确认；处置/恢复行同样带确认人，
        # 故确认≥处置，漏斗单调）。
        # 处置量 = 累计已处置（disposed + recovered），恢复量 = recovered。
        # B2.2 要求 预警≥确认≥处置≥解除 单调：「处置中」只是瞬时存量，解除是
        # 处置的下游动作，必须计入处置累计，否则处置(瞬时 2) < 解除(100) 倒挂。
        # （字段未就绪时 _confirm_has_cols 降级为 0，漏斗照常渲染。）
        cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts")
        off_total = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM ads_stream_ltv_alerts")
        str_total = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM ads_alert_confirm WHERE confirmed_by IS NOT NULL")
        confirm_total = int(cur.fetchone()[0])
        disposed_total = recovered_total = 0
        if "disposition_status" in _confirm_has_cols(cur):
            cur.execute(
                "SELECT disposition_status, COUNT(*) FROM ads_alert_confirm "
                "WHERE disposition_status IN ('disposed','recovered') GROUP BY disposition_status"
            )
            disp_map = dict(cur.fetchall())
            recovered_total = int(disp_map.get("recovered", 0))
            disposed_total = int(disp_map.get("disposed", 0)) + recovered_total
        alert_funnel = {
            "alert": off_total + str_total,
            "confirmed": confirm_total,
            "disposed": disposed_total,
            "recovered": recovered_total,
        }

        cur.close()
        return {
            "kpi": kpi,
            "five_class": five_class,
            "ltv_hist": ltv_hist,
            "city_dist": city_dist,
            "alert_breakdown": alert_breakdown,
            "trust_cards": trust_cards,
            "alert_funnel": alert_funnel,
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
    city=None,
    page=1,
    page_size=20,
):
    """离线 + 实时预警合并分页查询；返回总条数 + 本页明细（附抵押物地址）。

    city 按抵押物地址前缀过滤（如「广州」匹配 property_addr LIKE '广州%'），
    需查业务库 collateral 表得到 collateral_id 集合后在 SQL 里 IN 过滤——
    分页在 SQL 侧完成，避免「先全量查出再内存筛城市导致页码错位」。
    """
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

    crawl = crawl_conn()
    biz = biz_conn()
    try:
        cur = crawl.cursor()
        if city:
            # 城市 = 抵押物地址前缀（「广州」匹配「广州市…」）；未标注 → 地址为空。
            bcur = biz.cursor()
            if city == "未标注":
                bcur.execute(
                    "SELECT collateral_id FROM collateral "
                    "WHERE property_addr IS NULL OR property_addr=''"
                )
            else:
                bcur.execute(
                    "SELECT collateral_id FROM collateral WHERE property_addr LIKE %s",
                    (f"{city}%",),
                )
            cids = [r[0] for r in bcur.fetchall()]
            bcur.close()
            if cids:
                placeholders = ",".join(["%s"] * len(cids))
                where.append(f"collateral_id IN ({placeholders})")
                params.extend(cids)
            else:
                where.append("1=0")
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

        # 确认/处置状态：按 (loan_id, alert_date, src) 关联前端自建确认表。
        # 表由 app 启动时用 root 建；若权限不足未建成，降级为无状态而不报错。
        # 处置字段未就绪（处置字段尚未补列）时只读 confirmed，disposition 由 confirmed 推导。
        conf_map = {}
        if rows:
            cols = _confirm_has_cols(cur)
            if "disposition_status" in cols:
                conf_sel = (
                    "loan_id, alert_date, src, confirmed_by, confirmed_ts, "
                    "disposition_status, disposition_ts, disposition_by"
                )
            else:
                conf_sel = "loan_id, alert_date, src, confirmed_by, confirmed_ts"
            try:
                cur.execute(f"SELECT {conf_sel} FROM ads_alert_confirm")
                for r in cur.fetchall():
                    conf_map[(r[0], str(r[1]), r[2])] = _confirm_row_from(r, cols)
            except pymysql.err.ProgrammingError:
                conf_map = {}

        out = []
        for r in rows:
            key = (r["loan_id"], str(r["alert_date"]), r["src"])
            r["property_addr"] = addr_map.get(r.get("collateral_id")) or "未标注"
            conf = conf_map.get(key)
            r["confirmed"] = (
                {"confirmed_by": conf["confirmed_by"], "confirmed_ts": conf["confirmed_ts"]}
                if conf
                else None
            )
            r["disposition"] = (
                conf["disposition"] if conf else {"status": "pending", "by": None, "ts": None}
            )
            out.append(r)
        cur.close()
        return {"total": total, "page": page, "page_size": page_size, "rows": out}
    finally:
        crawl.close()
        biz.close()


# ---------------- 预警确认与处置（风控/管理员可写） ----------------

# 处置状态枚举（预警闭环漏斗四阶段）：pending=未确认 / confirmed=已确认待处置 /
# disposed=处置中 / recovered=已恢复。前端漏斗卡与列表处置按钮共用这一定义。
DISPOSITION_STATUSES = {"pending", "confirmed", "disposed", "recovered"}

# 处置字段 DDL（数据链路侧会同步补列，这里保证前端自建表也带同名字段，
# 两边都幂等，谁先建都行）。disposition_status 默认 pending，NULL 视为 pending。
_DISPOSITION_COL_DDL = {
    "disposition_status": "disposition_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
    "disposition_ts": "disposition_ts DATETIME NULL",
    "disposition_by": "disposition_by VARCHAR(32) NULL",
}


def _confirm_has_cols(cur):
    """返回 ads_alert_confirm 现有列名集合。

    兼容「处置字段尚未补全」的阶段：列表/漏斗查询据此决定查哪些列，
    缺列时处置统计降级为 0、列表 disposition 由 confirmed 推导，页面不报错。
    """
    try:
        cur.execute("SHOW COLUMNS FROM ads_alert_confirm")
        return {r[0] for r in cur.fetchall()}
    except pymysql.err.ProgrammingError:
        return set()


def ensure_alert_confirm_table():
    """幂等建前端自用确认表（root+房产库）。为什么单独建表：不改动预警链路既有表，
    确认/处置动作只在这张前端表留痕，报表/推送链路不受影响。

    建表 DDL 直接带处置字段；若表已存在但缺列（数据链路侧尚未同步、或旧库），
    用 ALTER 逐列补齐——保证列表/漏斗查询永远能读到 disposition_status。
    """
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
            "disposition_status VARCHAR(16) NOT NULL DEFAULT 'pending',"
            "disposition_ts DATETIME NULL,"
            "disposition_by VARCHAR(32) NULL,"
            "UNIQUE KEY uq_loan_date_src (loan_id, alert_date, src)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        existing = _confirm_has_cols(cur)
        for col, ddl in _DISPOSITION_COL_DDL.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE ads_alert_confirm ADD COLUMN {ddl}")
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _confirm_row_from(r, cols):
    """把 ads_alert_confirm 一行翻译成前端需要的结构（含处置状态）。

    兼容缺列阶段：没有 disposition_status 列时，由 confirmed_by 推导——
    有确认记录 → confirmed，否则 pending；disposed/recovered 只能等字段建好才有值。
    """
    row = {
        "confirmed_by": r[3],
        "confirmed_ts": str(r[4]) if r[4] is not None else None,
    }
    if "disposition_status" in cols:
        raw = r[5]
        row["disposition"] = {
            "status": raw if raw in DISPOSITION_STATUSES else "pending",
            "by": r[7],
            "ts": str(r[6]) if r[6] is not None else None,
        }
    else:
        row["disposition"] = {
            "status": "confirmed",
            "by": r[3],
            "ts": str(r[4]) if r[4] is not None else None,
        }
    return row


def confirm_alert(loan_id, alert_date, source, user):
    """写入确认记录；重复确认幂等（UNIQUE 键 + ON DUPLICATE 覆盖）。

    确认是处置闭环的第二步：落库时把 disposition_status 置为 confirmed，
    这样漏斗卡「确认量」与列表处置状态天然一致。
    """
    ensure_alert_confirm_table()
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ads_alert_confirm (loan_id, alert_date, src, confirmed_by, disposition_status) "
            "VALUES (%s, %s, %s, %s, 'confirmed') "
            "ON DUPLICATE KEY UPDATE confirmed_by=VALUES(confirmed_by), "
            "confirmed_ts=CURRENT_TIMESTAMP, disposition_status='confirmed'",
            (loan_id, alert_date, source, user),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def dispose_alert(loan_id, alert_date, source, status, user):
    """更新处置状态（disposed=处置中 / recovered=已恢复）。

    处置是确认的后续动作，隐含「已确认」：若该条还没有确认记录，
    直接落一条完整记录（confirmed_by=操作人）。重复处置幂等。
    status 只接受 disposed / recovered——pending/confirmed 由确认动作管理。
    """
    if status not in ("disposed", "recovered"):
        raise ValueError(f"dispose 只支持 disposed / recovered，收到 {status!r}")
    ensure_alert_confirm_table()
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ads_alert_confirm "
            "(loan_id, alert_date, src, confirmed_by, disposition_status, disposition_by, disposition_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE disposition_status=VALUES(disposition_status), "
            "disposition_by=VALUES(disposition_by), disposition_ts=VALUES(disposition_ts), "
            "confirmed_by=COALESCE(confirmed_by, VALUES(confirmed_by)), "
            "confirmed_ts=COALESCE(confirmed_ts, CURRENT_TIMESTAMP)",
            (loan_id, alert_date, source, user, status, user),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def alert_detail(loan_id, alert_date, source):
    """单笔预警行内详情：预警基础 + 风险因子（dws_risk_class）+ 抵押物地址 + 确认/处置记录。

    供预警列表「行内展开」使用。预警基础从离线/实时合并视图按 (loan_id, alert_date, src)
    精确取一行；风险因子按 loan_id 关联 dws_risk_class（实时流里的新贷款可能查不到，
    此时 risk_factors=None，前端照常展示其余字段）。
    """
    crawl = crawl_conn()
    biz = biz_conn()
    try:
        cur = crawl.cursor()
        cur.execute(
            "SELECT src, loan_id, customer_id, collateral_id, "
            "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
            "alert_date, alert_level FROM ("
            "SELECT 'offline' src, loan_id, customer_id, collateral_id, "
            "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
            "alert_date, alert_level FROM ads_ltv_alerts "
            "UNION ALL "
            "SELECT 'stream', loan_id, customer_id, collateral_id, "
            "loan_balance, market_valuation, ltv, risk_class, is_high_risk_zone, "
            "alert_date, NULL FROM ads_stream_ltv_alerts"
            ") t WHERE loan_id=%s AND alert_date=%s AND src=%s",
            (loan_id, alert_date, source),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        alert = dict(zip(cols, row, strict=True))
        alert["ltv"] = float(alert["ltv"]) if alert["ltv"] is not None else None
        alert["loan_balance"] = float(alert["loan_balance"] or 0)
        alert["market_valuation"] = float(alert["market_valuation"] or 0)

        # 风险因子：dws_risk_class 按 loan_id 取（估值偏差/异常估值/低置信等）。
        cur.execute(
            "SELECT market_valuation, ltv, risk_class, low_confidence, "
            "is_high_risk_zone, valuation_deviation_pct, abnormal_valuation, "
            "alert_level, model_version FROM dws_risk_class WHERE loan_id=%s",
            (loan_id,),
        )
        r = cur.fetchone()
        risk_factors = None
        if r:
            risk_factors = {
                "ltv": float(r[1]) if r[1] is not None else None,
                "risk_class": r[2],
                "low_confidence": bool(r[3]),
                "is_high_risk_zone": bool(r[4]),
                "valuation_deviation_pct": float(r[5]) if r[5] is not None else None,
                "abnormal_valuation": bool(r[6]),
                "model_version": r[8],
            }

        # 抵押物地址。
        addr = "未标注"
        if alert.get("collateral_id") is not None:
            bcur = biz.cursor()
            bcur.execute(
                "SELECT property_addr FROM collateral WHERE collateral_id=%s",
                (alert["collateral_id"],),
            )
            ar = bcur.fetchone()
            bcur.close()
            if ar and ar[0]:
                addr = str(ar[0])

        # 确认与处置记录（缺处置列时由 confirmed 推导，与 alerts() 同口径）。
        confirm = None
        ccols = _confirm_has_cols(cur)
        if "disposition_status" in ccols:
            csel = (
                "loan_id, alert_date, src, confirmed_by, confirmed_ts, "
                "disposition_status, disposition_ts, disposition_by"
            )
        else:
            csel = "loan_id, alert_date, src, confirmed_by, confirmed_ts"
        try:
            cur.execute(
                f"SELECT {csel} FROM ads_alert_confirm "
                "WHERE loan_id=%s AND alert_date=%s AND src=%s",
                (loan_id, alert_date, source),
            )
            cr = cur.fetchone()
            if cr:
                confirm = _confirm_row_from(cr, ccols)
        except pymysql.err.ProgrammingError:
            confirm = None

        cur.close()
        return {
            "alert": alert,
            "property_addr": addr,
            "risk_factors": risk_factors,
            "confirm": confirm,
        }
    finally:
        crawl.close()
        biz.close()


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
