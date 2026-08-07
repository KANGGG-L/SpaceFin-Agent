#!/usr/bin/env python
"""P5 空间风险画像（设计评审 P0 · 风控策略经理）。

页面回答三个问题：
1. 高危区在哪（网格分布 + 命中规则）；
2. 高危区里的贷款是不是更差（NPL 集中度对比，本页最有业务价值的一块）；
3. 这些结论有多可信（口径交叉核对 + 已知局限如实标注）。

为什么把「口径交叉核对」做成一等公民：
`dws_risk_class.is_high_risk_zone` 来自业务库 collateral 的合成种子字段，
并非 S3 空间画像（tools/spatial）按网格归属推导出来的。本模块用与
tools/spatial 相同的 0.02° 网格把抵押物坐标重新归属一遍，把「引擎口径命中数」
与「空间口径命中数」并排展示——若二者对不上，风控经理必须知道，
否则会把一个非空间证据支撑的标记当成空间结论来用（这是本页最容易误导的地方）。

数据来源（全部在 spacefin_crawler，用 db.crawl_conn()）：
  ads_spatial_zone      —— 区块画像（price/ltv 两类网格）
  dws_spatial_feature   —— 每实体空间特征（listing/community/collateral）
  dws_risk_class        —— 贷款五级分类明细（NPL 口径）
贷款所属城市取业务库 collateral.property_addr 前缀（db.biz_conn()），
与驾驶舱 city_dist 口径一致——dws_spatial_feature.district 对 collateral 全为 NULL，
地址是唯一可靠的城市来源。
"""

import math
import os
import statistics
import sys

# 独立运行（如离线校验脚本）时 frontend 目录可能不在 sys.path，这里兜底。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

import db  # noqa: E402

# 不良 = 五级后三档（次级/可疑/损失）。取 CLASS_ORDER 切片而非硬编码字符串，
# 保证与风险引擎/报送链路的五级口径同源，改一处即可全仓一致。
NPL_CLASSES = tuple(db.CLASS_ORDER[2:])

# 网格边长，必须与 tools/spatial/config.py 的 GRID_DEG 一致（0.02°≈2.2km）。
# 为什么复制常量而不 import tools/spatial：spatial 与 risk 各有一个顶层 config 模块，
# db.py 已把 tools/risk 加进 sys.path 并 import config，再加 spatial 会撞名。
GRID_DEG = 0.02

# 等距圆柱投影的度→公里换算（广东尺度误差 <1%，与 tools/spatial 的距离口径一致）。
KM_PER_DEG_LAT = 110.57
KM_PER_DEG_LNG = 111.32

# 城市码 → 中文名（业务库地址用中文，空间表用城市码，展示层统一成中文）。
CITY_NAME = {code: name for name, code in db.config.CITY_MAP.items()}

# 页面上如实标注的口径与局限，逐条对应 docs/tech/components/spatial-feature.md §7。
# 放在服务端而不是写死在 js 里：口径变化时后端与数据同步改，前端不必跟着发版。
CAVEATS = [
    "POI 密度是「挂牌房源密度」代理（半径 2km 内 DWD 挂牌行数 / 圆面积，单位 个/km²），"
    "反映房源热度而非真实配套；平台无 POI 图层数据源。",
    "通勤时长是「到城市行政中心的球面直线距离 ÷ 30km/h」近似，无路网、不分交通方式，"
    "系统性低估实际通勤（路网系数通常 1.4–1.6×）；城市中心坐标为约值（±2km）。",
    "价格面只覆盖有坐标的挂牌行（12,939 / 44,369 ≈ 29%），无坐标行不参与空间计算，"
    "网格中位单价代表的是「该网格有坐标样本」的中位数。",
    "高危区规则 A（price_low）：网格中位单价 ≤ 城市中位 ×(1-25%) 且样本 ≥20；"
    "规则 B（ltv_high）：网格 LTV 中位 >0.85 且样本 ≥3。阈值可由环境变量覆盖。",
    "LTV 型网格样本极少（2 个、各 3 笔），且坐标为坐标补全前的历史构建结果，"
    "当前无抵押物落入，判别力不足，仅作规则演示。",
    "单机 cKDTree 近似替代 Sedona 分布式空间连接，距离用等距圆柱投影；"
    "数据量到百万级需迁回 R-Tree 半径连接。",
]


# ---------------- 工具函数 ----------------


def _f(v):
    """Decimal/None → float/None（JSON 序列化前统一数值类型）。"""
    return None if v is None else float(v)


def _grid_key(lng, lat):
    """经纬度 → 网格左下角，与 tools/spatial/config.py: grid_key 同算法。"""
    return (
        round(math.floor(lng / GRID_DEG) * GRID_DEG, 2),
        round(math.floor(lat / GRID_DEG) * GRID_DEG, 2),
    )


def _km(lng1, lat1, lng2, lat2):
    """等距圆柱投影下的平面距离（km）。经度按中点纬度做 cos 收缩。"""
    mid = math.radians((lat1 + lat2) / 2.0)
    dx = (lng1 - lng2) * KM_PER_DEG_LNG * math.cos(mid)
    dy = (lat1 - lat2) * KM_PER_DEG_LAT
    return math.hypot(dx, dy)


def _city_code_from_addr(addr):
    """「广州市黄埔区…」→ 'gz'；取不到返回 None（前端显示「未标注」）。

    与 db._city_from_addr 同一份 CITY_MAP，只是返回城市码——本页筛选参数用码，
    与 ads_spatial_zone.city 对齐。
    """
    if not addr:
        return None
    for name, code in db.config.CITY_MAP.items():
        if str(addr).startswith(name):
            return code
    return None


def _rate(num, den):
    """占比；分母为 0 时返回 None，前端显示「-」而不是 0%，避免把「没样本」误读成「没风险」。"""
    return None if not den else round(num / den, 6)


# ---------------- 取数 ----------------


def _load_zones(cur):
    """全部区块画像（132 行级，一次取全，筛选在 Python 侧做，省一次往返）。"""
    cur.execute(
        "SELECT zone_id, zone_type, city, center_lng, center_lat, sample_count, "
        "median_unit_price, median_ltv, price_dev_vs_city, is_high_risk_zone, "
        "high_risk_rule, build_date "
        "FROM ads_spatial_zone"
    )
    zones = []
    for r in cur.fetchall():
        zones.append(
            {
                "zone_id": r[0],
                "zone_type": r[1],
                "city": r[2],
                "city_name": CITY_NAME.get(r[2], "未标注" if r[2] is None else r[2]),
                "center_lng": _f(r[3]),
                "center_lat": _f(r[4]),
                "sample_count": int(r[5] or 0),
                "median_unit_price": _f(r[6]),
                "median_ltv": _f(r[7]),
                "price_dev_vs_city": _f(r[8]),
                "is_high_risk_zone": int(r[9] or 0),
                "high_risk_rule": r[10],
                "build_date": str(r[11]) if r[11] else None,
            }
        )
    return zones


def _load_zone_feature_agg(cur):
    """按 zone_id 聚合实体空间特征：给每个网格补上 POI/通勤/偏差三个图层的值。

    区块表本身只有价格与 LTV，POI 密度与通勤时长在实体粒度（dws_spatial_feature），
    这里按网格取均值，地图切图层时同一批点换个着色维度即可。
    """
    cur.execute(
        "SELECT zone_id, COUNT(*), AVG(poi_density), AVG(commute_min), "
        "AVG(price_deviation), AVG(spatial_feat_missing_pct) "
        "FROM dws_spatial_feature WHERE zone_id IS NOT NULL GROUP BY zone_id"
    )
    return {
        r[0]: {
            "feat_count": int(r[1]),
            "poi_density_avg": _f(r[2]),
            "commute_min_avg": _f(r[3]),
            "price_deviation_avg": _f(r[4]),
            "missing_pct_avg": _f(r[5]),
        }
        for r in cur.fetchall()
    }


def _load_city_feature_stats(cur):
    """城市维度的实体特征统计（图层统计卡片用）。仅统计挂牌行：
    community 是小区去重后的点、collateral 是抵押物，混在一起会重复计权。"""
    cur.execute(
        "SELECT district, COUNT(*), AVG(poi_density), AVG(commute_min), "
        "AVG(price_deviation), AVG(spatial_feat_missing_pct) "
        "FROM dws_spatial_feature WHERE entity_type='listing' AND district IS NOT NULL "
        "GROUP BY district"
    )
    return {
        r[0]: {
            "listing_count": int(r[1]),
            "poi_density_avg": _f(r[2]),
            "commute_min_avg": _f(r[3]),
            "price_deviation_avg": _f(r[4]),
            "missing_pct_avg": _f(r[5]),
        }
        for r in cur.fetchall()
    }


def _load_loans(cur, bcur):
    """贷款明细 + 抵押物空间特征 + 城市（200 行级，全量取回后在内存里按城市切）。

    左连接空间特征表：collateral 的 zone_id 目前全为 NULL（见交叉核对说明），
    但 lat/lng 有值，地图叠加层要用。
    """
    cur.execute(
        "SELECT r.loan_id, r.collateral_id, r.balance, r.ltv, r.risk_class, "
        "r.is_high_risk_zone, r.alert, r.low_confidence, "
        "f.lat, f.lng, f.poi_density, f.commute_min, f.spatial_feat_missing_pct, f.zone_id "
        "FROM dws_risk_class r "
        "LEFT JOIN dws_spatial_feature f "
        "ON f.entity_type='collateral' AND f.entity_id=CAST(r.collateral_id AS CHAR)"
    )
    loans = []
    for r in cur.fetchall():
        loans.append(
            {
                "loan_id": int(r[0]),
                "collateral_id": int(r[1]) if r[1] is not None else None,
                "balance": _f(r[2]) or 0.0,
                "ltv": _f(r[3]),
                "risk_class": r[4],
                "is_high_risk_zone": int(r[5] or 0),
                "alert": int(r[6] or 0),
                "low_confidence": int(r[7] or 0),
                "lat": _f(r[8]),
                "lng": _f(r[9]),
                "poi_density": _f(r[10]),
                "commute_min": _f(r[11]),
                "missing_pct": _f(r[12]),
                "zone_id": r[13],
            }
        )

    # 批量取地址定城市（IN 查询，避免 N+1）。
    cids = [ln["collateral_id"] for ln in loans if ln["collateral_id"] is not None]
    addrs = {}
    if cids:
        placeholders = ",".join(["%s"] * len(cids))
        bcur.execute(
            f"SELECT collateral_id, property_addr FROM collateral "
            f"WHERE collateral_id IN ({placeholders})",
            cids,
        )
        addrs = {r[0]: r[1] for r in bcur.fetchall()}
    for ln in loans:
        addr = addrs.get(ln["collateral_id"])
        ln["property_addr"] = addr or "未标注"
        ln["city"] = _city_code_from_addr(addr)
        ln["city_name"] = CITY_NAME.get(ln["city"], "未标注")
    return loans


# ---------------- 聚合 ----------------


def _npl_group(loans):
    """单组贷款的不良口径汇总：笔数率 + 余额率两个口径都给。

    为什么两个口径都要：笔数率反映「有多少笔烂了」，余额率反映「烂掉多少钱」，
    小样本下两者常背离（本项目 200 笔就明显背离），只给一个会误导。
    """
    cnt = len(loans)
    bal = sum(ln["balance"] for ln in loans)
    npl = [ln for ln in loans if ln["risk_class"] in NPL_CLASSES]
    npl_bal = sum(ln["balance"] for ln in npl)
    ltvs = [ln["ltv"] for ln in loans if ln["ltv"] is not None]
    by_class = {cls: 0 for cls in db.CLASS_ORDER}
    bal_by_class = {cls: 0.0 for cls in db.CLASS_ORDER}
    for ln in loans:
        if ln["risk_class"] in by_class:
            by_class[ln["risk_class"]] += 1
            bal_by_class[ln["risk_class"]] += ln["balance"]
    return {
        "loan_count": cnt,
        "balance": round(bal, 2),
        "npl_count": len(npl),
        "npl_balance": round(npl_bal, 2),
        "npl_rate_count": _rate(len(npl), cnt),
        "npl_rate_balance": _rate(npl_bal, bal),
        "avg_ltv": round(statistics.fmean(ltvs), 6) if ltvs else None,
        "alert_count": sum(ln["alert"] for ln in loans),
        "low_confidence_count": sum(ln["low_confidence"] for ln in loans),
        "by_class": [
            {
                "risk_class": cls,
                "loan_count": by_class[cls],
                "balance": round(bal_by_class[cls], 2),
                "pct": _rate(by_class[cls], cnt),
            }
            for cls in db.CLASS_ORDER
        ],
    }


def _npl_concentration(loans):
    """高危区 vs 非高危区的不良集中度对比 + 倍数（lift）。"""
    high = [ln for ln in loans if ln["is_high_risk_zone"]]
    normal = [ln for ln in loans if not ln["is_high_risk_zone"]]
    g_high = _npl_group(high)
    g_normal = _npl_group(normal)

    def lift(a, b):
        if a is None or not b:
            return None
        return round(a / b, 4)

    return {
        "high": g_high,
        "normal": g_normal,
        "total": _npl_group(loans),
        "lift_count": lift(g_high["npl_rate_count"], g_normal["npl_rate_count"]),
        "lift_balance": lift(g_high["npl_rate_balance"], g_normal["npl_rate_balance"]),
        # 小样本提示交给前端展示：23 笔的不良率一笔迁徙就跳 4pct，不能当结论用。
        "small_sample": g_high["loan_count"] < 30,
    }


def _attribution_check(zones, loans):
    """口径交叉核对：引擎标记 vs 空间网格归属。

    引擎口径 = dws_risk_class.is_high_risk_zone（业务库 collateral 合成字段）；
    空间口径 = 用 tools/spatial 同款 0.02° 网格，把抵押物坐标重新落到 ads_spatial_zone。
    两者对不上说明「高危区」标记当前没有空间证据支撑，必须显式告诉风控经理。
    """
    grid = {}
    for z in zones:
        if z["center_lng"] is None or z["center_lat"] is None:
            continue
        grid[_grid_key(z["center_lng"], z["center_lat"])] = z

    coords = [(z["center_lng"], z["center_lat"], z["is_high_risk_zone"]) for z in zones]
    in_zone = in_high = 0
    dists = []
    for ln in loans:
        if ln["lat"] is None or ln["lng"] is None:
            continue
        hit = grid.get(_grid_key(ln["lng"], ln["lat"]))
        if hit:
            in_zone += 1
            if hit["is_high_risk_zone"]:
                in_high += 1
        if coords:
            dists.append(min(_km(ln["lng"], ln["lat"], c[0], c[1]) for c in coords))

    return {
        "loan_total": len(loans),
        "with_coord": sum(1 for ln in loans if ln["lat"] is not None),
        "flagged_by_engine": sum(1 for ln in loans if ln["is_high_risk_zone"]),
        "in_any_zone_grid": in_zone,
        "in_high_risk_grid": in_high,
        "with_zone_id": sum(1 for ln in loans if ln["zone_id"]),
        "nearest_zone_km_median": round(statistics.median(dists), 2) if dists else None,
        "nearest_zone_km_min": round(min(dists), 2) if dists else None,
        "within_5km": sum(1 for d in dists if d <= 5.0),
        "consistent": in_high == sum(1 for ln in loans if ln["is_high_risk_zone"]),
    }


def _city_options(zones, loans, city_feat):
    """城市下拉：区块城市 ∪ 贷款城市。

    并集而不是只取区块城市：深圳/东莞等城市有贷款但没跑出价格网格（坐标覆盖不足），
    只列区块城市会让风控经理以为这些城市没有敞口。
    """
    opts = {}

    def slot(code):
        return opts.setdefault(
            code,
            {
                "code": code,
                "name": CITY_NAME.get(code, "未标注"),
                "zone_count": 0,
                "high_risk_zones": 0,
                "loan_count": 0,
                "listing_count": (city_feat.get(code) or {}).get("listing_count", 0),
            },
        )

    for z in zones:
        if z["city"] is None:
            continue
        s = slot(z["city"])
        s["zone_count"] += 1
        s["high_risk_zones"] += z["is_high_risk_zone"]
    for ln in loans:
        if ln["city"] is None:
            continue
        slot(ln["city"])["loan_count"] += 1
    return sorted(opts.values(), key=lambda o: (-o["high_risk_zones"], -o["zone_count"], o["code"]))


# ---------------- 路由 handler ----------------


def overview(ctx):
    """GET /api/spatial —— 页面主接口。

    query: city=<城市码|空>、zone_type=price|ltv|<空=全部>
    筛选只影响地图/区块列表/NPL 对比的样本范围；口径交叉核对与城市下拉始终按全量算，
    否则筛完城市后「引擎 vs 空间」的对不上会被局部样本掩盖。
    """
    city = (ctx.query.get("city", [""])[0] or "").strip() or None
    zone_type = (ctx.query.get("zone_type", [""])[0] or "").strip() or None
    if zone_type and zone_type not in ("price", "ltv"):
        raise ValueError("zone_type 只能是 price / ltv")
    if city and city not in CITY_NAME:
        raise ValueError(f"未知城市码：{city}")

    crawl = db.crawl_conn()
    biz = db.biz_conn()
    try:
        cur = crawl.cursor()
        bcur = biz.cursor()
        zones_all = _load_zones(cur)
        zone_feat = _load_zone_feature_agg(cur)
        city_feat = _load_city_feature_stats(cur)
        loans_all = _load_loans(cur, bcur)

        for z in zones_all:
            z.update(
                zone_feat.get(
                    z["zone_id"],
                    {
                        "feat_count": 0,
                        "poi_density_avg": None,
                        "commute_min_avg": None,
                        "price_deviation_avg": None,
                        "missing_pct_avg": None,
                    },
                )
            )

        zones = [
            z
            for z in zones_all
            if (city is None or z["city"] == city)
            and (zone_type is None or z["zone_type"] == zone_type)
        ]
        loans = [ln for ln in loans_all if city is None or ln["city"] == city]

        high_zones = [z for z in zones if z["is_high_risk_zone"]]
        npl = _npl_concentration(loans)

        kpi = {
            "zone_count": len(zones),
            "high_risk_zone_count": len(high_zones),
            "sample_count": sum(z["sample_count"] for z in zones),
            "high_risk_loan_count": npl["high"]["loan_count"],
            "high_risk_npl_rate_balance": npl["high"]["npl_rate_balance"],
            "high_risk_balance": npl["high"]["balance"],
        }

        # 城市图层统计：按当前筛选给出可比的城市列表（未筛选时全给）。
        city_layers = [
            dict(v, code=k, name=CITY_NAME.get(k, k))
            for k, v in city_feat.items()
            if city is None or k == city
        ]
        city_layers.sort(key=lambda c: -(c["listing_count"] or 0))

        cur.close()
        bcur.close()
        return {
            "filters": {"city": city, "zone_type": zone_type},
            "cities": _city_options(zones_all, loans_all, city_feat),
            "build_date": next((z["build_date"] for z in zones_all if z["build_date"]), None),
            "kpi": kpi,
            "zones": zones,
            "high_risk_zones": sorted(high_zones, key=lambda z: z["price_dev_vs_city"] or 0),
            "collaterals": [
                {
                    "loan_id": ln["loan_id"],
                    "collateral_id": ln["collateral_id"],
                    "lat": ln["lat"],
                    "lng": ln["lng"],
                    "risk_class": ln["risk_class"],
                    "ltv": ln["ltv"],
                    "balance": ln["balance"],
                    "is_high_risk_zone": ln["is_high_risk_zone"],
                    "city_name": ln["city_name"],
                    "property_addr": ln["property_addr"],
                }
                for ln in loans
                if ln["lat"] is not None and ln["lng"] is not None
            ],
            "npl": npl,
            "city_layers": city_layers,
            "attribution": _attribution_check(zones_all, loans_all),
            "caveats": CAVEATS,
        }
    finally:
        crawl.close()
        biz.close()


def zone_detail(ctx):
    """GET /api/spatial/zone?zone_id=xxx —— 点击网格后的下钻明细。

    给出该网格的实体特征分布（谁贡献了这个中位价）、价格偏差直方图，
    以及 5km 内的贷款——高危网格若周边根本没有敞口，风控上就不必优先处置。
    """
    zone_id = (ctx.query.get("zone_id", [""])[0] or "").strip()
    if not zone_id:
        raise ValueError("缺少参数 zone_id")

    crawl = db.crawl_conn()
    biz = db.biz_conn()
    try:
        cur = crawl.cursor()
        bcur = biz.cursor()
        cur.execute(
            "SELECT zone_id, zone_type, city, center_lng, center_lat, sample_count, "
            "median_unit_price, median_ltv, price_dev_vs_city, is_high_risk_zone, "
            "high_risk_rule, build_date FROM ads_spatial_zone WHERE zone_id=%s",
            (zone_id,),
        )
        row = cur.fetchone()
        if not row:
            return 404, {"error": f"网格不存在：{zone_id}"}
        zone = {
            "zone_id": row[0],
            "zone_type": row[1],
            "city": row[2],
            "city_name": CITY_NAME.get(row[2], "未标注" if row[2] is None else row[2]),
            "center_lng": _f(row[3]),
            "center_lat": _f(row[4]),
            "sample_count": int(row[5] or 0),
            "median_unit_price": _f(row[6]),
            "median_ltv": _f(row[7]),
            "price_dev_vs_city": _f(row[8]),
            "is_high_risk_zone": int(row[9] or 0),
            "high_risk_rule": row[10],
            "build_date": str(row[11]) if row[11] else None,
        }

        # 网格内实体特征：按 entity_type 拆开，避免小区/挂牌混算。
        cur.execute(
            "SELECT entity_type, COUNT(*), AVG(poi_density), AVG(commute_min), "
            "AVG(price_deviation), AVG(spatial_feat_missing_pct) "
            "FROM dws_spatial_feature WHERE zone_id=%s GROUP BY entity_type",
            (zone_id,),
        )
        entities = [
            {
                "entity_type": r[0],
                "count": int(r[1]),
                "poi_density_avg": _f(r[2]),
                "commute_min_avg": _f(r[3]),
                "price_deviation_avg": _f(r[4]),
                "missing_pct_avg": _f(r[5]),
            }
            for r in cur.fetchall()
        ]

        # 价格偏差直方图：只看挂牌行，桶宽 10%，看这个网格是整体洼地还是内部分化。
        cur.execute(
            "SELECT price_deviation FROM dws_spatial_feature "
            "WHERE zone_id=%s AND entity_type='listing' AND price_deviation IS NOT NULL",
            (zone_id,),
        )
        buckets = [
            "<-30%",
            "-30~-20%",
            "-20~-10%",
            "-10~0%",
            "0~10%",
            "10~20%",
            "20~30%",
            ">30%",
        ]
        hist = dict.fromkeys(buckets, 0)
        for (dev,) in cur.fetchall():
            d = float(dev)
            if d < -0.30:
                hist["<-30%"] += 1
            elif d < -0.20:
                hist["-30~-20%"] += 1
            elif d < -0.10:
                hist["-20~-10%"] += 1
            elif d < 0:
                hist["-10~0%"] += 1
            elif d < 0.10:
                hist["0~10%"] += 1
            elif d < 0.20:
                hist["10~20%"] += 1
            elif d < 0.30:
                hist["20~30%"] += 1
            else:
                hist[">30%"] += 1

        # 5km 内的贷款：抵押物坐标在 dws_spatial_feature，距离在内存里算（200 行级）。
        nearby = []
        if zone["center_lng"] is not None:
            loans = _load_loans(cur, bcur)
            for ln in loans:
                if ln["lat"] is None or ln["lng"] is None:
                    continue
                d = _km(ln["lng"], ln["lat"], zone["center_lng"], zone["center_lat"])
                if d <= 5.0:
                    nearby.append(
                        {
                            "loan_id": ln["loan_id"],
                            "collateral_id": ln["collateral_id"],
                            "distance_km": round(d, 2),
                            "risk_class": ln["risk_class"],
                            "ltv": ln["ltv"],
                            "balance": ln["balance"],
                            "is_high_risk_zone": ln["is_high_risk_zone"],
                            "property_addr": ln["property_addr"],
                        }
                    )
            nearby.sort(key=lambda x: x["distance_km"])

        cur.close()
        bcur.close()
        return {
            "zone": zone,
            "entities": entities,
            "deviation_hist": [{"bucket": b, "count": hist[b]} for b in buckets],
            "nearby_loans": nearby[:20],
            "nearby_total": len(nearby),
        }
    finally:
        crawl.close()
        biz.close()


PAGE = {
    "id": "spatial",
    "label": "高危地图与黑名单",
    # 风控策略经理是主用户；DA 需要看口径，admin 全量可见。贷后不参与空间策略，故不开。
    "roles": {"admin", "risk", "da"},
    "order": 50,
    "js": "p5_spatial.js",
    "routes": {
        ("GET", "/api/spatial"): overview,
        ("GET", "/api/spatial/zone"): zone_detail,
    },
}
