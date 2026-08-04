"""S3 L2 空间特征计算核心：价格面 / 高危区 / POI 密度代理 / 通勤近似。

为什么单机实现而非 Sedona：规划文档 S3 原案是 Sedona 分布式空间连接，但本机
4 核 15G，DWD 有坐标的行仅约 1.3 万，规模远够不上分布式。scipy cKDTree 的
球面邻域查询在内存中秒级完成，语义与 Sedona 的「半径连接 + 聚合」一致，
故 MVP 用单机实现（见 docs/tech/开发计划.md R-tech-1 降级结论）。

坐标投影：广东 21 城集中在约 (20N-26N, 108E-118E)，用等距圆柱投影换算为
公里坐标（以 (23.5N, 113.5E) 为参考原点），误差在城市尺度可忽略。
"""

from __future__ import annotations

import math

import config
import numpy as np
from scipy.spatial import cKDTree

# 等距圆柱投影参考点（广东中部，经度/纬度）与公里换算常数
_REF_LAT, _REF_LNG = 23.5, 113.5
_KM_PER_DEG_LAT = 110.57
_KM_PER_DEG_LNG = 111.32 * math.cos(math.radians(_REF_LAT))


def project_latlng(lat: float, lng: float) -> tuple[float, float]:
    """经纬度 → 公里坐标 (x, y)，x 向东、y 向北。"""
    x = (lng - _REF_LNG) * _KM_PER_DEG_LNG
    y = (lat - _REF_LAT) * _KM_PER_DEG_LAT
    return float(x), float(y)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面直线距离（公里）。通勤代理用，cKDTree 邻域查询用投影代替。"""
    r = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# 空间价格面：邻域中位单价 + 单行价格偏差
# ---------------------------------------------------------------------------


def build_price_surface(rows: list[dict], radius_km: float = config.NEIGHBOR_RADIUS_KM):
    """对 DWD 挂牌行建 cKDTree，返回 (tree, coords_km, prices)。

    rows 须含 lat/lng/unit_price_yuan，且已被过滤为有坐标、有有效单价。
    返回的 tree 用于对任意点（挂牌行或抵押物）做半径邻域查询。
    """
    coords = np.array([project_latlng(r["lat"], r["lng"]) for r in rows], dtype=np.float64)
    prices = np.array([float(r["unit_price_yuan"]) for r in rows], dtype=np.float64)
    tree = cKDTree(coords)
    return tree, coords, prices


def neighborhood_median(
    tree: cKDTree,
    coords: np.ndarray,
    prices: np.ndarray,
    q_coords: np.ndarray,
    radius_km: float,
    min_samples: int = config.MIN_NEIGHBORS,
    exclude_indices: np.ndarray | None = None,
) -> np.ndarray:
    """每个查询点在半径内的邻域中位单价（默认不含自身）。

    exclude_indices 与 q_coords 等长：每个查询点要剔除的树内索引（挂牌行查
    自身邻域时传自身索引，避免「用自己验证自己」）。邻域样本 < min_samples
    时返回 NaN——样本太少不足以支撑「局部价格基准」，对应空间特征缺失。
    """
    neigh = tree.query_ball_point(q_coords, r=radius_km)
    out = np.full(len(q_coords), np.nan)
    for i, idx in enumerate(neigh):
        if exclude_indices is not None:
            self_i = int(exclude_indices[i])
            idx = [j for j in idx if j != self_i]
        if len(idx) < min_samples:
            continue
        out[i] = float(np.median(prices[idx]))
    return out


# ---------------------------------------------------------------------------
# 空间区块画像 + 高危区判定
# ---------------------------------------------------------------------------


def build_price_zones(
    rows: list[dict], radius_km: float = config.NEIGHBOR_RADIUS_KM
) -> tuple[list[dict], dict]:
    """按 (city, 网格) 聚合挂牌行，构建价格面区块画像，并标记高危区。

    返回 (zones, zone_index)：
    - zones: 区块画像行列表，每行含网格键/中心/样本量/中位单价/与城市中位偏差/高危标记
    - zone_index: {(city, grid_key): zone_id}，供挂牌行反查所属区块
    """
    # 城市中位单价：用该城市全部挂牌行（含无坐标行）的中位，作为「城市均价」基准
    city_med: dict[str, float] = {}
    city_bucket: dict[str, list[float]] = {}
    for r in rows:
        city_bucket.setdefault(r["district"], []).append(float(r["unit_price_yuan"]))
    for c, vals in city_bucket.items():
        city_med[c] = float(np.median(vals))

    bucket: dict[tuple, list[dict]] = {}
    for r in rows:
        glng, glat = config.grid_key(r["lng"], r["lat"])
        bucket.setdefault((r["district"], glng, glat), []).append(r)

    zones: list[dict] = []
    zone_index: dict[tuple, str] = {}
    for (city, glng, glat), members in sorted(bucket.items()):
        n = len(members)
        # 样本不足的网格不构成区块（避免 1-2 行撑起一个画像）
        if n < config.MIN_ZONE_SAMPLES:
            continue
        med_price = float(np.median([m["unit_price_yuan"] for m in members]))
        ctr_lat = float(np.mean([m["lat"] for m in members]))
        ctr_lng = float(np.mean([m["lng"] for m in members]))
        cm = city_med.get(city)
        dev = (med_price / cm - 1.0) if cm and cm > 0 else None
        # 高危规则 A：区块中位显著低于城市中位（价格洼地）且样本足够
        high_risk = bool(cm and med_price <= cm * (1 - config.PRICE_LOW_RATIO))
        zone_id = f"price-{city}-{glng:.2f}-{glat:.2f}"
        zones.append(
            {
                "zone_id": zone_id,
                "zone_type": "price",
                "city": city,
                "center_lng": round(ctr_lng, 6),
                "center_lat": round(ctr_lat, 6),
                "sample_count": n,
                "median_unit_price": round(med_price, 2),
                "median_ltv": None,
                "price_dev_vs_city": round(dev, 4) if dev is not None else None,
                "is_high_risk_zone": int(high_risk),
                "high_risk_rule": "price_low" if high_risk else None,
            }
        )
        zone_index[(city, glng, glat)] = zone_id
    return zones, zone_index


def build_ltv_zones(collaterals: list[dict]) -> tuple[list[dict], dict]:
    """按抵押物坐标做网格聚合，构建 LTV 集中型高危区块。

    规则 B：网格内抵押物 LTV 中位 > 红线且样本足够 → 该网格为高危区。
    抵押物当前为上海合成坐标，与广东 DWD 网格不重叠，故 LTV 区块与价格区块
    分属两套网格体系，各自独立画像（zone_type 区分）。
    """
    bucket: dict[tuple, list[float]] = {}
    for c in collaterals:
        if c.get("lat") is None or c.get("lng") is None:
            continue
        glng, glat = config.grid_key(c["lng"], c["lat"])
        bucket.setdefault((glng, glat), []).append(float(c["ltv"]))

    zones: list[dict] = []
    zone_index: dict[tuple, str] = {}
    for (glng, glat), ltvs in sorted(bucket.items()):
        n = len(ltvs)
        if n < config.MIN_LTV_ZONE_SAMPLES:
            continue
        med_ltv = float(np.median(ltvs))
        high_risk = med_ltv > config.LTV_RED_LINE
        zone_id = f"ltv-{glng:.2f}-{glat:.2f}"
        zones.append(
            {
                "zone_id": zone_id,
                "zone_type": "ltv",
                "city": None,
                "center_lng": round(glng + config.GRID_DEG / 2, 6),
                "center_lat": round(glat + config.GRID_DEG / 2, 6),
                "sample_count": n,
                "median_unit_price": None,
                "median_ltv": round(med_ltv, 4),
                "price_dev_vs_city": None,
                "is_high_risk_zone": int(high_risk),
                "high_risk_rule": "ltv_high" if high_risk else None,
            }
        )
        zone_index[(glng, glat)] = zone_id
    return zones, zone_index


# ---------------------------------------------------------------------------
# POI 密度代理 与 通勤近似
# ---------------------------------------------------------------------------


def poi_density_proxy(
    tree: cKDTree,
    coords_km: np.ndarray,
    q_coords: np.ndarray,
    radius_km: float = config.POI_RADIUS_KM,
) -> np.ndarray:
    """POI 密度代理：半径内 DWD 挂牌数 / 圆面积（个/平方公里）。

    这是「挂牌房源密度」的代理指标，不是真实 POI 数据（本平台无 POI 图层，
    用挂牌行近似人流/配套热度）。若查询点半径内无挂牌，返回 NaN 表示
    「本地无覆盖」而非「密度为 0」——两者语义不同，前者是数据缺失。
    """
    cnt = np.array([len(i) for i in tree.query_ball_point(q_coords, r=radius_km)])
    area = math.pi * radius_km * radius_km
    dens = cnt / area
    # 半径内 0 行且最近挂牌超出覆盖半径 → 视为无覆盖（缺失）
    nearest = np.ravel(tree.query(q_coords, k=1)[0])
    dens[nearest > config.POI_COVERAGE_KM] = np.nan
    return dens


def commute_minutes(lat: float, lng: float, city_code: str | None) -> float | None:
    """通勤近似：到最近城市中心的直线距离 / 平均通勤速度（无路网数据）。

    city_code 为 None（如抵押物无城市归属）时，取广东 21 城中最近的城市中心。
    超过 COMMUTE_COVERAGE_KM 视为不在任何城市覆盖内 → 返回 None（缺失）。
    """
    centers: list[tuple[float, float]] = []
    if city_code and city_code in config.CITY_CENTER:
        centers.append(config.CITY_CENTER[city_code])
    else:
        centers = list(config.CITY_CENTER.values())
    best = min(haversine_km(lat, lng, clat, clng) for (clng, clat) in centers)
    if best > config.COMMUTE_COVERAGE_KM:
        return None
    return round(best / config.COMMUTE_SPEED_KMH * 60.0, 2)


# ---------------------------------------------------------------------------
# DWS 特征组装
# ---------------------------------------------------------------------------


def build_listing_features(
    rows: list[dict],
    tree: cKDTree,
    coords: np.ndarray,
    prices: np.ndarray,
    zone_index: dict,
) -> list[dict]:
    """为每条有坐标挂牌行计算空间特征（实体键：url_key）。

    特征口径：价格偏差 = (本行单价 - 邻域中位) / 邻域中位，正值价格高地、
    负值价格洼地。spatial_feat_missing_pct 为 4 个空间特征中缺失的占比。
    """
    q_coords = np.array([project_latlng(r["lat"], r["lng"]) for r in rows], dtype=np.float64)
    self_idx = np.arange(len(rows))  # 查询点即树内点，剔除自身避免自证
    neigh_med = neighborhood_median(
        tree, coords, prices, q_coords, config.NEIGHBOR_RADIUS_KM, exclude_indices=self_idx
    )

    poi = poi_density_proxy(tree, coords, q_coords)
    out = []
    for r, nm, pd in zip(rows, neigh_med, poi, strict=True):
        comm = commute_minutes(r["lat"], r["lng"], r["district"])
        glng, glat = config.grid_key(r["lng"], r["lat"])
        zid = zone_index.get((r["district"], glng, glat))
        price_dev = (float(r["unit_price_yuan"]) - nm) / nm if not math.isnan(nm) else None
        out.append(_feature_row(r, r["url_key"], "listing", comm, pd, zid, price_dev))
    return out


def build_community_features(
    rows: list[dict],
    tree: cKDTree,
    coords: np.ndarray,
    prices: np.ndarray,
    zone_index: dict,
) -> list[dict]:
    """按 (city, community) 聚合挂牌行，输出小区级空间特征。

    小区坐标取成员挂牌的中位经纬度，小区价格取中位单价；邻域查询时剔除
    本小区成员（避免自证），反映「周边小区」价格水平。
    """
    buckets: dict[tuple, list[dict]] = {}
    for r in rows:
        if not r.get("community"):
            continue
        buckets.setdefault((r["district"], r["community"]), []).append(r)

    out = []
    for (city, comm), members in buckets.items():
        ctr_lat = float(np.median([m["lat"] for m in members]))
        ctr_lng = float(np.median([m["lng"] for m in members]))
        med_price = float(np.median([m["unit_price_yuan"] for m in members]))
        q = np.array([project_latlng(ctr_lat, ctr_lng)], dtype=np.float64)

        # 邻域查询，剔除本小区成员索引
        idx = set(tree.query_ball_point(q, r=config.NEIGHBOR_RADIUS_KM)[0])
        self_idx = {
            i for i, r in enumerate(rows) if (r["district"], r.get("community")) == (city, comm)
        }
        ext = idx - self_idx
        if len(ext) >= config.MIN_NEIGHBORS:
            nm = float(np.median(prices[sorted(ext)]))
        else:
            nm = np.nan

        poi = float(poi_density_proxy(tree, coords, q)[0])
        comm_min = commute_minutes(ctr_lat, ctr_lng, city)
        glng, glat = config.grid_key(ctr_lng, ctr_lat)
        zid = zone_index.get((city, glng, glat))
        price_dev = (med_price - nm) / nm if not math.isnan(nm) else None
        rec = {
            "entity_type": "community",
            "entity_id": f"{city}|{comm}",
            "district": city,
            "lat": ctr_lat,
            "lng": ctr_lng,
            "poi_density": round(poi, 4) if not math.isnan(poi) else None,
            "commute_min": comm_min,
            "zone_id": zid,
            "price_deviation": round(price_dev, 4) if price_dev is not None else None,
        }
        rec["spatial_feat_missing_pct"] = _missing_pct(rec)
        out.append(rec)
    return out


def build_collateral_features(
    collaterals: list[dict],
    tree: cKDTree,
    coords: np.ndarray,
    prices: np.ndarray,
    ltv_zone_index: dict,
) -> list[dict]:
    """为抵押物计算空间特征（实体键：collateral_id）。

    抵押物坐标当前为上海合成值，落在广东 DWD 网格外 → POI 密度/通勤/价格偏差
    多为缺失，spatial_feat_missing_pct 高，符合「低置信标记」的设计意图
    （见 tools/risk/risk_engine.py AC-04）。
    """
    out = []
    for c in collaterals:
        lat, lng = c.get("lat"), c.get("lng")
        q = np.array([project_latlng(lat, lng)], dtype=np.float64) if lat is not None else None
        poi = float(poi_density_proxy(tree, coords, q)[0]) if lat is not None else np.nan
        comm = commute_minutes(lat, lng, None) if lat is not None else None
        glng, glat = config.grid_key(lng, lat) if lng is not None else (np.nan, np.nan)
        zid = ltv_zone_index.get((glng, glat))

        # 价格偏差：用抵押物自身隐含单价（市值/面积）对比 DWD 邻域中位
        nm = np.nan
        if lat is not None:
            nm = neighborhood_median(tree, coords, prices, q, config.NEIGHBOR_RADIUS_KM)[0]
        unit_price = None
        area = c.get("area")
        if c.get("true_market_price") and area and float(area) > 0:
            unit_price = float(c["true_market_price"]) / float(area)
        price_dev = (unit_price - nm) / nm if (not math.isnan(nm) and unit_price) else None

        rec = {
            "entity_type": "collateral",
            "entity_id": str(c["collateral_id"]),
            "district": None,
            "lat": lat,
            "lng": lng,
            "poi_density": round(poi, 4) if not math.isnan(poi) else None,
            "commute_min": comm,
            "zone_id": zid,
            "price_deviation": round(price_dev, 4) if price_dev is not None else None,
        }
        rec["spatial_feat_missing_pct"] = _missing_pct(rec)
        out.append(rec)
    return out


def _feature_row(
    r: dict,
    entity_id: str,
    entity_type: str,
    comm: float | None,
    poi: float,
    zid: str | None,
    price_dev: float | None,
) -> dict:
    rec = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "district": r["district"],
        "lat": r["lat"],
        "lng": r["lng"],
        "poi_density": round(poi, 4) if not math.isnan(poi) else None,
        "commute_min": comm,
        "zone_id": zid,
        "price_deviation": round(price_dev, 4) if price_dev is not None else None,
    }
    rec["spatial_feat_missing_pct"] = _missing_pct(rec)
    return rec


def _missing_pct(rec: dict) -> float:
    """4 个空间特征（POI 密度/通勤/高危区/价格偏差）中缺失的占比（0-100）。

    高危区缺失 = 该点不在任何区块内（zone_id is None）。
    """
    feats = [rec["poi_density"], rec["commute_min"], rec["zone_id"], rec["price_deviation"]]
    miss = sum(1 for f in feats if f is None)
    return round(miss / len(feats) * 100.0, 2)


def enrich_ltv(collaterals: list[dict], loans: list[dict]) -> list[dict]:
    """抵押物 LTV = 贷款余额 / 抵押物市值。一物多贷取余额合计。

    LTV 用于高危规则 B 的网格聚合；市值取业务库 true_market_price（合成值）。
    """
    bal_by_coll: dict[int, float] = {}
    for ln in loans:
        cid = int(ln["collateral_id"])
        bal_by_coll[cid] = bal_by_coll.get(cid, 0.0) + float(ln["balance"] or 0.0)
    for c in collaterals:
        cid = int(c["collateral_id"])
        price = c.get("true_market_price")
        ltv = (bal_by_coll.get(cid, 0.0) / float(price)) if price and float(price) > 0 else None
        c["ltv"] = ltv
    return collaterals
