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

# REPO 根目录加入 sys.path：前端按需从 tools/spatial/store 等上层模块做 schema 自愈
# （如 ensure_spatial_schema 直接复用空间链路的 ensure_tables）。
# db.py 位于 <REPO>/tools/frontend/，向上三层到仓库根。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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

        # 预警概览：离线（T+1）预警笔数与按风险类拆分。
        # 实时预警（ads_stream_ltv_alerts，由已废弃的 Kafka/Flink 链产出）已从概览移除。
        cur.execute("SELECT risk_class, COUNT(*) cnt FROM ads_ltv_alerts GROUP BY risk_class")
        alert_breakdown = {"offline": {r[0]: int(r[1]) for r in cur.fetchall()}}
        cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts")
        alert_breakdown["offline_total"] = int(cur.fetchone()[0])

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

        # 数据底座信任卡：爬取规模 / 新鲜度 / 解析成功率。
        # 全部来自真实运行态，无 mock——爬取规模与 P1 登记表、README 的数字互相印证。
        cur.execute("SELECT COUNT(*) FROM crawl_housing_sale")
        sale_rows = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM crawl_housing_rent")
        rent_rows = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM community_coords")
        coords_rows = int(cur.fetchone()[0])
        # 只数 21 城白名单内的 district，排除 beijing/nb 等外省桶（代理 IP 带偏的房源），
        # 否则"城市数"会被算成 23 而非真实的广东 21 城。CITY_MAP 来自 risk/config（单一事实源）。
        _gd_codes = tuple(config.CITY_MAP.values())
        cur.execute(
            f"SELECT COUNT(DISTINCT district) FROM crawl_housing_sale "
            f"WHERE district IN ({','.join('%s' for _ in _gd_codes)})",
            _gd_codes,
        )
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

        trust_cards = {
            "crawl_scale": crawl_scale,
            "freshness": {"layers": freshness_layers, "overall": overall},
            "parse_success": parse_success,
        }

        # 预警闭环漏斗：预警 → 确认 → 处置 → 恢复。
        # 预警量 = 离线（T+1）；确认量 = confirmed_by 非空的行（处置契约：
        # 有 confirmed_by/confirmed_ts 才算确认；处置/恢复行同样带确认人，
        # 故确认≥处置，漏斗单调）。
        # 处置量 = 累计已处置（disposed + recovered），恢复量 = recovered。
        # B2.2 要求 预警≥确认≥处置≥解除 单调：「处置中」只是瞬时存量，解除是
        # 处置的下游动作，必须计入处置累计，否则处置(瞬时 2) < 解除(100) 倒挂。
        # （字段未就绪时 _confirm_has_cols 降级为 0，漏斗照常渲染。）
        cur.execute("SELECT COUNT(*) FROM ads_ltv_alerts")
        off_total = int(cur.fetchone()[0])
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
            "alert": off_total,
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
    keyword=None,
    zone=None,
    page=1,
    page_size=20,
):
    """离线 + 实时预警合并分页查询；返回总条数 + 本页明细（附抵押物地址）。

    city 按抵押物地址前缀过滤（如「广州」匹配 property_addr LIKE '广州%'），
    需查业务库 collateral 表得到 collateral_id 集合后在 SQL 里 IN 过滤——
    分页在 SQL 侧完成，避免「先全量查出再内存筛城市导致页码错位」。
    keyword 按贷款号/客户号模糊搜索（参数化 LIKE，防注入）；为空时行为不变。
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
    if keyword:
        # q 搜索：贷款号 / 客户号模糊匹配（LIKE 值走参数化，不拼 SQL，防注入）。
        where.append("(loan_id LIKE %s OR customer_id LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if zone:
        # 方向 1：按高危区 zone_id 过滤。以子查询解析落在该区的抵押物集合
        # （dws_spatial_feature 在 crawl 库，与 alerts 同连接），不传 zone 时
        # 该分支跳过，行为与现状完全一致（向后兼容）。
        where.append(
            "collateral_id IN (SELECT entity_id FROM dws_spatial_feature "
            "WHERE zone_id=%s AND entity_type='collateral')"
        )
        params.append(zone)

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
        # low_confidence（D-06）：只在分页 SELECT 的**外层** LEFT JOIN dws_risk_class。
        # 为什么不能放进 base（`) t {where_sql}` 之前）：dws_risk_class 与预警视图同名列
        # 多达 5 个（loan_id/collateral_id/ltv/risk_class/is_high_risk_zone），WHERE 里
        # 的裸列名会立刻 1052 Column ambiguous；且 COUNT 查询共用 base，一旦 JOIN 进去
        # 还得改 COUNT。loan_id 是 dws_risk_class 主键，1:1 不放大行数，total 不受影响。
        # ORDER BY 保持裸列名：alert_date / id 都只存在于 t（dws_risk_class 无这两列），无歧义。
        cur.execute(
            f"SELECT t.*, COALESCE(d.low_confidence,0) low_confidence FROM ({base}) t "
            "LEFT JOIN dws_risk_class d ON d.loan_id=t.loan_id "
            "ORDER BY alert_date DESC, id DESC LIMIT %s OFFSET %s",
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

        # 方向 2：批量关联高危区 zone_id（参照 addr_map 的 IN 查询，禁止逐行查）。
        # dws_spatial_feature 主键含 build_date，同一抵押物可能有多行；且同一
        # 抵押物可能同时落在 price 与 ltv 两类高危区（两行）。按确定性规则固定
        # 取唯一 zone_id：build_date 最新优先，同级时 ltv 型优先——方向 1/2 须一致。
        zone_map = {}
        if cids:
            placeholders = ",".join(["%s"] * len(cids))
            cur.execute(
                f"SELECT entity_id, zone_id, build_date FROM dws_spatial_feature "
                f"WHERE entity_type='collateral' "
                f"AND entity_id IN ({placeholders})",
                list(cids),
            )
            best = {}
            for eid, zid, bdate in cur.fetchall():
                prefer = 0 if str(zid).startswith("ltv-") else 1  # ltv 优先
                existing = best.get(eid)
                if existing is None or (bdate, -prefer) > (existing[1], -existing[2]):
                    best[eid] = (zid, bdate, prefer)
            zone_map = {eid: v[0] for eid, v in best.items()}

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
            r["zone_id"] = zone_map.get(r.get("collateral_id")) or None
            # 用 get 兜底：假连接（测试）与旧列头可能没有 low_confidence 列。
            r["low_confidence"] = bool(r.get("low_confidence"))
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


def derive_suggestion(risk_factors):
    """按风险因子现算「处置建议」（规则派生，不建表、不落库）。

    为什么规则派生而不新建建议表：建议的输入（LTV、估值偏差、高危区、低置信）全部已在
    dws_risk_class 落库，再存一份文案就会与阈值调参漂移——阈值改了旧建议还挂在表里。
    这里每次读详情现算，天然跟随 config 阈值。

    优先级（自上而下，命中即返回，只给一个主建议避免风控看到互相矛盾的动作）：
      1. low_confidence      → 空间特征严重缺失，估值不可信，先人工复核（并抑制自动推送）
      2. abnormal_valuation 或 |偏差| > VALUATION_DEVIATION_THRESHOLD → 转人工复估
      3. is_high_risk_zone   → 区域下行风险，加严审批 / 提高抵押率要求
      4. ltv > LTV_RED_LINE  → 已越强预警线，压降敞口（追保/提前收回）
      5. ltv > LTV_WARN_LINE → 越警示线，加密监控
      6. 其余                → 常规监控
    返回 {level, headline, actions[], basis[]}；risk_factors 为 None（实时流新贷款查不到
    风险宽表）时返回 None，前端照常渲染其余区块。

    level 取值与前端色条对应：bad（红）/ warn（黄）/ info（蓝）/ ok（绿）。
    """
    if not risk_factors:
        return None
    rf = risk_factors
    ltv = rf.get("ltv")
    dev = rf.get("valuation_deviation_pct")
    dev_line = config.VALUATION_DEVIATION_THRESHOLD
    red = config.LTV_RED_LINE
    warn = config.LTV_WARN_LINE
    miss = config.LOW_CONF_MISSING_PCT

    def pct(v):
        return f"{v * 100:.1f}%" if v is not None else "—"

    if rf.get("low_confidence"):
        return {
            "level": "warn",
            "headline": "转人工复核（估值置信度不足）",
            "actions": [
                "暂不依据本次自动估值做处置决策，先转人工/线下复核抵押物",
                "补齐抵押物空间特征（坐标、小区行情样本）后重跑估值",
                "复核期间该笔预警不进入自动推送队列，避免误催收",
            ],
            "basis": [
                f"空间特征缺失率超过 {miss:.0f}% 阈值，模型标记为低置信",
                f"当前 LTV {pct(ltv)}（低置信下该值仅供参考）",
            ],
        }

    if rf.get("abnormal_valuation") or (dev is not None and abs(dev) > dev_line):
        return {
            "level": "bad",
            "headline": "转人工复估（估值偏差异常）",
            "actions": [
                "冻结本笔自动估值结果，转评估部门人工复估",
                "核对抵押物面积/楼层/挂牌样本，确认是否同名小区串号",
                "复估结论回填后重算 LTV 与五级分类",
            ],
            "basis": [
                f"AVM 估值与参考基准偏差 {pct(abs(dev)) if dev is not None else '—'}，"
                f"超过阈值 {pct(dev_line)}",
                f"当前 LTV {pct(ltv)}",
            ],
        }

    if rf.get("is_high_risk_zone"):
        return {
            "level": "warn",
            "headline": "加严审批（抵押物位于高危区）",
            "actions": [
                "该笔新增授信/展期需上提一级审批",
                "提高抵押率要求或追加担保物",
                "纳入区域下行风险名单，按月复估抵押物",
            ],
            "basis": [
                "抵押物落在空间高危区（区域价格/LTV 双高）",
                f"当前 LTV {pct(ltv)}",
            ],
        }

    if ltv is not None and ltv > red:
        return {
            "level": "bad",
            "headline": "压降敞口（已越强预警线）",
            "actions": [
                "启动追加保证金或追加抵押物流程",
                "评估提前收回部分本金，把 LTV 压回警示线以下",
                "同步贷后资产保全，纳入重点跟踪名单",
            ],
            "basis": [
                f"LTV {pct(ltv)} 已越强预警线 {pct(red)}",
                f"五级分类：{rf.get('risk_class') or '—'}",
            ],
        }

    if ltv is not None and ltv > warn:
        return {
            "level": "info",
            "headline": "加密监控（已越警示线）",
            "actions": [
                "监控频次提高至每周复估抵押物估值",
                "提示客户经理跟进还款意愿与经营现金流",
                "如继续上行越过强预警线，转压降敞口流程",
            ],
            "basis": [
                f"LTV {pct(ltv)} 越警示线 {pct(warn)}，但未越强预警线 {pct(red)}",
                f"五级分类：{rf.get('risk_class') or '—'}",
            ],
        }

    return {
        "level": "ok",
        "headline": "常规监控",
        "actions": [
            "按 T+1 批次常规跟踪，无需额外处置动作",
            "下一次估值更新后自动重算 LTV 与五级分类",
        ],
        "basis": [
            f"LTV {pct(ltv)} 未越警示线 {pct(warn)}，无估值异常与高危区标记",
        ],
    }


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
            # 处置建议：按风险因子现算（阈值全走 config），无风险宽表记录时为 None。
            "suggestion": derive_suggestion(risk_factors),
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
        # R2-7 路线 B：推演历史要落库整份 impact 快照（rows 全量 + cover.rows 全量），
        # 单条 JSON 可达 MB 级，TEXT(64KB) 装不下 → 升级 detail 为 MEDIUMTEXT(16MB)。
        # 幂等：先查 INFORMATION_SCHEMA 当前 COLUMN_TYPE，已是 MEDIUMTEXT 就跳过，
        # 否则执行 ALTER；捕获「无需变更」类异常（如 Redundant / 1060）忽略，保证旧库
        # 可平滑升级、重复调用幂等。
        cur = conn.cursor()
        # SELECT 失败（权限/库名）必须冒泡，不能和 ALTER 的冗余异常一起被吞掉——
        # 否则连 INFORMATION_SCHEMA 都查不了时，会静默跳过迁移，直到写大 JSON 才暴露失败。
        cur.execute(
            "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ads_export_audit' "
            "AND COLUMN_NAME = 'detail'"
        )
        row = cur.fetchone()
        if not row or str(row[0]).upper() != "MEDIUMTEXT":
            try:
                cur.execute("ALTER TABLE ads_export_audit MODIFY detail MEDIUMTEXT")
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - 个别 MySQL 把「已是 MEDIUMTEXT」报冗余异常
                print(f"[db] detail 列扩容 MEDIUMTEXT 跳过（{exc}）", file=sys.stderr)
        cur.close()
    finally:
        conn.close()


def ensure_spatial_schema():
    """启动时幂等确保空间库表结构（建表 + 补列 + 补索引），防止旧库 schema 漂移导致
    /api/spatial 查询 500（真实踩过的坑：ads_spatial_zone 缺 ltv_sample_count 列）。

    直接复用 tools/spatial/store.ensure_tables：CREATE TABLE IF NOT EXISTS + 补新增列
    （_ensure_zone_columns，1060 Duplicate column name 忽略）+ 补索引
    （_ensure_feature_indexes，1061 Duplicate key name 忽略），全部幂等。
    与空间链路写库同源——store.py 后续新增列会自动覆盖，前端无需再改。用 root 连接
    （ddl_conn）执行 DDL。
    """
    from tools.spatial import store  # 延迟导入：避免前端模块加载期硬依赖 tools/spatial

    conn = ddl_conn()
    try:
        store.ensure_tables(conn)
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

# 报送单类型：本页只做 G11 资产质量表，落库时与 ads_report_alert 的 report_type 同口径。
REPORT_TYPE = "1104_g11"

# 报送状态五态（前端徽标与按钮禁用规则共用这一份定义）：
#   pending    尚无可报送快照（该业务日 ads_1104_g11 无行）
#   consistent 口径一致，可报送
#   submitted  已报送（落库，刷新/重启后保留）
#   blocked    口径不一致，已阻断（含「已报送后快照又漂移」）
#   checking   正在实时复算（前端瞬时态，**永不落库**）
REPORT_STATUSES = {"pending", "consistent", "submitted", "blocked", "checking"}


# ads_1104_g11 重建前置检查的必要列（与 tools/reporting/main.py 建表 DDL 对齐）。
_G11_REQUIRED_COLS = {
    "stat_date",
    "risk_class",
    "loan_count",
    "balance_total",
    "balance_pct",
    "is_total",
}


def _latest_g11_date(cur):
    """取 ads_1104_g11 最新的 stat_date（date 缺省时的默认值）。"""
    cur.execute("SELECT MAX(stat_date) FROM ads_1104_g11")
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _load_g11_rows(cur, date):
    """读 ads_1104_g11 该日快照行（合计行置尾，五级按 CLASS_ORDER 排序）。"""
    cur.execute(
        "SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, is_total, etl_ts "
        "FROM ads_1104_g11 WHERE stat_date=%s ORDER BY is_total ASC, "
        "FIELD(risk_class,%s,%s,%s,%s,%s)",
        (date, *CLASS_ORDER),
    )
    return [
        {
            "stat_date": str(r[0]),
            "risk_class": r[1],
            "loan_count": int(r[2]),
            "balance_total": float(r[3]),
            "balance_pct": float(r[4]),
            "is_total": bool(r[5]),
            "etl_ts": str(r[6]),
        }
        for r in cur.fetchall()
    ]


def _dws_aggregate(cur):
    """dws_risk_class 明细聚合（裁判出口）：risk_class -> (笔数, 余额)。"""
    cur.execute(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) "
        "FROM dws_risk_class GROUP BY risk_class"
    )
    return {r[0]: (int(r[1]), float(r[2])) for r in cur.fetchall()}


def _g11_consistency(rows, dws):
    """以 dws 明细聚合为裁判，比对 g11 快照行的笔数/余额，返回 mismatch 明细列表。

    与 tools/reporting/main.py 的 1104_vs_dws 校验同源：余额差超 EPS_BALANCE 即不一致。
    每项携带双方取值与差值（snapshot=1104 快照行，dws=明细聚合，diff=snapshot-dws），
    前端据此渲染「两边各是多少、差多少」的对照矩阵，用户不用再猜差异在哪。
    调用方用 m["key"] 即可派生旧版的字符串 mismatch 列表（兼容既有测试与告警 detail）。
    """
    details = []
    for r in rows:
        if r["is_total"]:
            continue
        cnt, bal = dws.get(r["risk_class"], (0, 0.0))
        if r["loan_count"] != cnt:
            details.append(
                {
                    "key": f"g11_vs_dws:{r['risk_class']}:loan_count",
                    "risk_class": r["risk_class"],
                    "field": "loan_count",
                    "snapshot": r["loan_count"],
                    "dws": cnt,
                    "diff": r["loan_count"] - cnt,
                    "tolerance": 0,
                }
            )
        if abs(r["balance_total"] - bal) > EPS_BALANCE:
            details.append(
                {
                    "key": f"g11_vs_dws:{r['risk_class']}:balance",
                    "risk_class": r["risk_class"],
                    "field": "balance",
                    "snapshot": r["balance_total"],
                    "dws": bal,
                    "diff": round(r["balance_total"] - bal, 2),
                    "tolerance": EPS_BALANCE,
                }
            )
    return details


def ensure_report_submission_table():
    """幂等建报送状态表（root+房产库）。

    为什么单独建表：报送「已报送」是前端页面的动作留痕，不属于 ads_1104_g11 快照本身
    （快照可以被重建，报送事实不能被重算）。用 (report_date, report_type) 做主键，
    同一业务日重复报送幂等覆盖，submitted_at 保留首报时刻（见 set_report_submitted）。
    """
    conn = ddl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ads_report_submission ("
            "report_date DATE NOT NULL,"
            "report_type VARCHAR(16) NOT NULL,"
            "status VARCHAR(16) NOT NULL,"
            "submitted_by VARCHAR(32) NULL,"
            "submitted_at DATETIME NULL,"
            "updated_at DATETIME NULL,"
            "PRIMARY KEY (report_date, report_type)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _load_submission(cur, date):
    """读该业务日的报送记录；无记录或表还没建 → None（降级，不让页面 500）。"""
    try:
        cur.execute(
            "SELECT status, submitted_by, submitted_at, updated_at "
            "FROM ads_report_submission WHERE report_date=%s AND report_type=%s",
            (date, REPORT_TYPE),
        )
    except pymysql.err.ProgrammingError:
        return None
    r = cur.fetchone()
    if not r:
        return None
    return {
        "status": r[0],
        "submitted_by": r[1],
        "submitted_at": str(r[2]) if r[2] is not None else None,
        "updated_at": str(r[3]) if r[3] is not None else None,
    }


def _block_alert_sent(cur, date):
    """该业务日已落多少条 block 级阻断告警（ads_report_alert 实时派生，不新建列）。

    「告警已发」= 报送 CLI 已经把阻断结论推出去了，此时页面上再点报送没有意义。
    表缺失 → 0（降级）。
    """
    try:
        cur.execute(
            "SELECT COUNT(*) FROM ads_report_alert "
            "WHERE report_type=%s AND report_date=%s AND alert_level='block'",
            (REPORT_TYPE, date),
        )
    except pymysql.err.ProgrammingError:
        return 0
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else 0


def _derive_report_status(rows, mismatches, submission):
    """五态派生：blocked > submitted > consistent > pending（checking 只在前端出现）。

    无快照（rows 空）优先判 pending——没有快照谈不上一致/不一致。
    已报送后快照又漂移 → blocked（而不是 submitted），提示需要重报；此时 submission
    仍随返回体下发，前端据此显示「曾于 X 报送，快照已变更」。
    """
    if not rows:
        return "pending"
    if mismatches:
        return "blocked"
    if submission and submission.get("status") == "submitted":
        return "submitted"
    return "consistent"


def set_report_submitted(date, user):
    """把该业务日标记为已报送（PK upsert，幂等）。

    submitted_at 用 COALESCE 保留**首报时刻**：重复点报送不刷新首报时间（幂等语义），
    updated_at 每次都刷新，用于区分「首报」与「最近一次确认」。
    """
    ensure_report_submission_table()
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ads_report_submission "
            "(report_date, report_type, status, submitted_by, submitted_at, updated_at) "
            "VALUES (%s,%s,'submitted',%s,NOW(),NOW()) "
            "ON DUPLICATE KEY UPDATE status='submitted', "
            "submitted_by=VALUES(submitted_by), "
            "submitted_at=COALESCE(submitted_at, VALUES(submitted_at)), "
            "updated_at=NOW()",
            (date, REPORT_TYPE, user),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def reset_report_submission(date):
    """重建快照后回退报送态：删除该业务日的报送记录（无记录 = pending）。

    为什么必须回退：快照被重建后内容已经变了，之前那次「已报送」对应的是旧快照，
    继续显示「已报送」会让报送人以为新快照也报过。表缺失时静默降级。
    """
    conn = crawl_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM ads_report_submission WHERE report_date=%s AND report_type=%s",
                (date, REPORT_TYPE),
            )
            conn.commit()
        except pymysql.err.ProgrammingError:
            pass
        cur.close()
    finally:
        conn.close()


def report_submit(date=None, user="system"):
    """「报送」动作：服务端自复算口径，一致才落 submitted。

    绝不相信前端传来的 consistent——页面上的状态可能是几分钟前的快照，期间 dws 明细
    可能已被重跑。这里重新走一遍 _load_g11_rows + _dws_aggregate + _g11_consistency，
    不一致直接返回 ok=False（**不写任何状态**），由 handler 转 409。
    """
    crawl = crawl_conn()
    try:
        cur = crawl.cursor()
        if not date:
            date = _latest_g11_date(cur)
        rows = _load_g11_rows(cur, date) if date else []
        if not rows:
            cur.close()
            return {
                "ok": False,
                "reason": "该业务日尚无可报送快照",
                "date": date,
                "status": "pending",
                "consistent": None,
                "mismatches": [],
                "submission": None,
                "alert_sent": False,
                "alert_sent_count": 0,
            }
        mismatch_details = _g11_consistency(rows, _dws_aggregate(cur))
        mismatches = [m["key"] for m in mismatch_details]
        alert_count = _block_alert_sent(cur, date)
        if mismatches:
            submission = _load_submission(cur, date)
            cur.close()
            return {
                "ok": False,
                "reason": "口径不一致，报送已阻断",
                "date": date,
                "status": "blocked",
                "consistent": False,
                "mismatches": mismatches,
                "mismatch_details": mismatch_details,
                "submission": submission,
                "alert_sent": alert_count > 0,
                "alert_sent_count": alert_count,
            }
        cur.close()
    finally:
        crawl.close()

    set_report_submitted(date, user)

    crawl = crawl_conn()
    try:
        cur = crawl.cursor()
        submission = _load_submission(cur, date)
        cur.close()
    finally:
        crawl.close()
    return {
        "ok": True,
        "date": date,
        "status": "submitted",
        "consistent": True,
        "mismatches": [],
        "submission": submission,
        "alert_sent": alert_count > 0,
        "alert_sent_count": alert_count,
    }


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
            date = _latest_g11_date(cur)
        if not date:
            return {
                "date": None,
                "rows": [],
                "consistent": None,
                "mismatches": [],
                "mismatch_details": [],
                "alerts": [],
                "status": "pending",
                "submission": None,
                "alert_sent": False,
                "alert_sent_count": 0,
            }

        rows = _load_g11_rows(cur, date)
        mismatch_details = _g11_consistency(rows, _dws_aggregate(cur))
        mismatches = [m["key"] for m in mismatch_details]
        submission = _load_submission(cur, date)
        alert_count = _block_alert_sent(cur, date)

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
            # consistent 保留：旧前端与既有测试依赖该字段，status 是叠加而非替换。
            "consistent": not mismatches,
            "mismatches": mismatches,
            "mismatch_details": mismatch_details,
            "alerts": alerts,
            "status": _derive_report_status(rows, mismatches, submission),
            "submission": submission,
            "alert_sent": alert_count > 0,
            "alert_sent_count": alert_count,
        }
    finally:
        crawl.close()


def report_recheck(date=None):
    """1104 口径实时复算（只读，不写表、不落审计）。

    复用 report() 同一套校验逻辑（_load_g11_rows + _dws_aggregate + _g11_consistency），
    只返回校验结论与 checked_at。checked_at 用 SQL 侧 NOW()：宿主 UTC / MySQL +08 的
    时区差由 MySQL 自己消化，绝不在 Python 里做 naive datetime 减法。
    """
    crawl = crawl_conn()
    try:
        cur = crawl.cursor()
        if not date:
            date = _latest_g11_date(cur)
        if not date:
            return {
                "date": None,
                "consistent": None,
                "mismatches": [],
                "mismatch_details": [],
                "checked_at": None,
                "status": "pending",
                "submission": None,
                "alert_sent": False,
                "alert_sent_count": 0,
            }
        cur.execute("SELECT NOW()")
        checked_at = str(cur.fetchone()[0])
        rows = _load_g11_rows(cur, date)
        mismatch_details = _g11_consistency(rows, _dws_aggregate(cur))
        mismatches = [m["key"] for m in mismatch_details]
        submission = _load_submission(cur, date)
        alert_count = _block_alert_sent(cur, date)
        cur.close()
        return {
            "date": date,
            "consistent": not mismatches,
            "mismatches": mismatches,
            "mismatch_details": mismatch_details,
            "checked_at": checked_at,
            "status": _derive_report_status(rows, mismatches, submission),
            "submission": submission,
            "alert_sent": alert_count > 0,
            "alert_sent_count": alert_count,
        }
    finally:
        crawl.close()


def report_rebuild(date=None):
    """按 dws_risk_class 现算重建 ads_1104_g11 快照（可操作闭环的兜底）。

    快照与明细口径漂了时，直接以明细聚合为基准覆盖重算该 stat_date：先删该日
    已有行再插入（单事务，任一步失败整体回滚），保持 CLASS_ORDER 排序，
    is_total 合计行一并生成，etl_ts 用 SQL NOW()。重建后与明细同源，校验应恒一致，
    仍跑一遍返回供前端展示。
    """
    conn = crawl_conn()
    try:
        # 关闭自动提交：DELETE + INSERT 必须落在同一事务，失败整体回滚。
        conn.autocommit(False)
        cur = conn.cursor()
        try:
            if not date:
                date = _latest_g11_date(cur)
            if not date:
                conn.rollback()
                return {
                    "date": None,
                    "rebuilt_rows": 0,
                    "consistent": None,
                    "mismatches": [],
                    "mismatch_details": [],
                    "etl_ts": None,
                }

            # 写入前置：表/列结构缺失 → 明确报错而非 500（前端能展示，不裸崩）。
            _ensure_g11_schema(cur)
            dws = _dws_aggregate(cur)
            bad = [cls for cls in dws if cls not in CLASS_ORDER]
            if bad:
                raise ValueError(
                    "dws_risk_class 出现非五级 risk_class，阻断重建: "
                    + ", ".join(str(x) for x in bad)
                )
            rows = _build_g11_rows(date, dws)

            cur.execute("DELETE FROM ads_1104_g11 WHERE stat_date=%s", (date,))
            cur.executemany(
                "INSERT INTO ads_1104_g11 "
                "(stat_date, risk_class, loan_count, balance_total, balance_pct, is_total, etl_ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,NOW())",
                [
                    (
                        date,
                        r["risk_class"],
                        r["loan_count"],
                        r["balance_total"],
                        r["balance_pct"],
                        r["is_total"],
                    )
                    for r in rows
                ],
            )
            cur.execute("SELECT NOW()")
            etl_ts = str(cur.fetchone()[0])
            mismatch_details = _g11_consistency(rows, dws)
            mismatches = [m["key"] for m in mismatch_details]
            conn.commit()
            return {
                "date": date,
                "rebuilt_rows": len(rows),
                "consistent": not mismatches,
                "mismatches": mismatches,
                "mismatch_details": mismatch_details,
                "etl_ts": etl_ts,
            }
        finally:
            cur.close()
    finally:
        conn.close()


def _ensure_g11_schema(cur):
    """rebuild 前置结构检查：ads_1104_g11 必要列缺失 → ValueError（明确错误而非 500）。"""
    try:
        cur.execute("SHOW COLUMNS FROM ads_1104_g11")
        cols = {r[0] for r in cur.fetchall()}
    except pymysql.err.ProgrammingError as err:
        raise ValueError(
            "ads_1104_g11 表不存在，无法重建（请先运行 tools/reporting/main.py 建表）"
        ) from err
    missing = _G11_REQUIRED_COLS - cols
    if missing:
        raise ValueError(f"ads_1104_g11 缺少必要列，无法重建: {sorted(missing)}")


def _build_g11_rows(date, dws):
    """dws 明细聚合 → G11 模板行：五级按 CLASS_ORDER + 合计行（is_total=1）。"""
    total_count = sum(cnt for cnt, _ in dws.values())
    total_balance = round(sum(bal for _, bal in dws.values()), 2)
    rows = []
    for cls in CLASS_ORDER:
        cnt, bal = dws.get(cls, (0, 0.0))
        rows.append(
            {
                "risk_class": cls,
                "loan_count": cnt,
                "balance_total": round(bal, 2),
                "balance_pct": round(bal / total_balance, 4) if total_balance else 0.0,
                "is_total": 0,
            }
        )
    rows.append(
        {
            "risk_class": "合计",
            "loan_count": total_count,
            "balance_total": total_balance,
            "balance_pct": round(total_balance / total_balance, 4) if total_balance else 0.0,
            "is_total": 1,
        }
    )
    return rows


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
