"""空间特征计算（features.py）：邻域价格面、高危区规则、POI/通勤代理、缺失率。

高危区两条规则是可解释风控的核心产物，阈值与样本量门槛必须钉死：
    A 价格洼地：区块中位单价 <= 城市中位 * (1 - 0.25) 且样本 >= 20
    B LTV 集中：网格内 LTV 中位 > 红线 0.85 且样本 >= 3
"""

import numpy as np
import pytest
import spatmods
from spatmods import config, features


def surface(rows):
    return features.build_price_surface(rows)


def q(lat, lng):
    return np.array([features.project_latlng(lat, lng)], dtype=np.float64)


# ================================================================ 邻域中位价


def test_neighborhood_median_uses_median_not_mean(gz_listings):
    tree, coords, prices = surface(gz_listings)

    out = features.neighborhood_median(
        tree, coords, prices, q(23.1300, 113.3200), config.NEIGHBOR_RADIUS_KM
    )

    assert out[0] == pytest.approx(float(np.median(prices)))


def test_neighborhood_median_is_nan_when_too_few_samples():
    """样本 < MIN_NEIGHBORS 不足以支撑「局部价格基准」，必须缺失而不是硬算。"""
    rows = [spatmods.sale_row(23.13 + i * 0.0001, 113.32, 90_000.0) for i in range(3)]
    tree, coords, prices = surface(rows)

    out = features.neighborhood_median(
        tree, coords, prices, q(23.13, 113.32), config.NEIGHBOR_RADIUS_KM
    )

    assert np.isnan(out[0])


def test_neighborhood_median_excludes_self_to_avoid_self_validation():
    """挂牌行查自身邻域时要剔除自己，否则是「用自己验证自己」。

    价格构造成 [离群, 10, 10, 10, 20, 20]：含自身时中位为 15，剔除自身后为 10，
    两者不同才说明剔除真的生效了。
    """
    prices_in = [999_999.0, 10.0, 10.0, 10.0, 20.0, 20.0]
    rows = [spatmods.sale_row(23.135 + i * 0.0001, 113.325, p) for i, p in enumerate(prices_in)]
    tree, coords, prices = surface(rows)
    qs = np.array([features.project_latlng(r["lat"], r["lng"]) for r in rows], dtype=np.float64)

    with_self = features.neighborhood_median(tree, coords, prices, qs, config.NEIGHBOR_RADIUS_KM)
    without_self = features.neighborhood_median(
        tree, coords, prices, qs, config.NEIGHBOR_RADIUS_KM, exclude_indices=np.arange(len(rows))
    )

    assert with_self[0] == 15.0
    assert without_self[0] == 10.0


def test_neighborhood_median_is_nan_outside_radius(gz_listings):
    """半径外的查询点没有邻居 → 缺失。"""
    tree, coords, prices = surface(gz_listings)

    out = features.neighborhood_median(
        tree, coords, prices, q(31.23, 121.47), config.NEIGHBOR_RADIUS_KM
    )

    assert np.isnan(out[0])  # 上海坐标落在广东价格面之外


# ================================================================ 高危区 A：价格洼地


def zone_rows(city, lat, lng, price, n):
    """在 (lat, lng) 所属网格的**中心**放 n 个样本，全部落在同一格。

    必须显式吸附到格心：直接用 23.13/113.32 这类整点会正好压在 0.02° 网格线上，
    浮点 floor 会把这簇样本劈进相邻两格，每格都够不到 MIN_ZONE_SAMPLES，
    区块于是一个都建不出来（见 test_cluster_on_grid_boundary_is_split_into_two_cells）。
    """
    glng, glat = config.grid_key(lng, lat)
    clng, clat = glng + config.GRID_DEG / 2, glat + config.GRID_DEG / 2
    return [spatmods.sale_row(clat + i * 1e-5, clng + i * 1e-5, price, city=city) for i in range(n)]


def test_price_zone_requires_minimum_samples():
    """样本不足的网格不构成区块，避免 1-2 行撑起一个画像。"""
    rows = zone_rows("gz", 23.13, 113.32, 90_000.0, config.MIN_ZONE_SAMPLES - 1)

    zones, index = features.build_price_zones(rows)

    assert zones == [] and index == {}


def test_price_zone_built_at_exactly_minimum_samples():
    rows = zone_rows("gz", 23.13, 113.32, 90_000.0, config.MIN_ZONE_SAMPLES)

    zones, index = features.build_price_zones(rows)

    assert len(zones) == 1
    assert zones[0]["sample_count"] == config.MIN_ZONE_SAMPLES
    assert list(index.values()) == [zones[0]["zone_id"]]


def test_price_low_zone_is_flagged_high_risk():
    """洼地网格（中位价 = 城市中位的 50%）应命中规则 A。"""
    rows = zone_rows("gz", 23.13, 113.32, 50_000.0, 20) + zone_rows(
        "gz", 23.50, 113.60, 150_000.0, 20
    )

    zones, _ = features.build_price_zones(rows)
    low = min(zones, key=lambda z: z["median_unit_price"])

    assert low["is_high_risk_zone"] == 1
    assert low["high_risk_rule"] == "price_low"


def test_price_zone_at_normal_level_is_not_high_risk():
    rows = zone_rows("gz", 23.13, 113.32, 100_000.0, 20)

    zones, _ = features.build_price_zones(rows)

    assert zones[0]["is_high_risk_zone"] == 0
    assert zones[0]["high_risk_rule"] is None


def test_price_low_rule_boundary_is_inclusive():
    """规则 A 用的是 `<=`：恰好等于城市中位 *(1-0.25) 也算洼地。

    构造两个等样本网格，价格 75 与 125，城市中位落在 100，
    则低价网格 75 == 100*(1-0.25)，压线命中。
    """
    rows = zone_rows("gz", 23.13, 113.32, 75.0, 20) + zone_rows("gz", 23.50, 113.60, 125.0, 20)

    zones, _ = features.build_price_zones(rows)
    low = min(zones, key=lambda z: z["median_unit_price"])

    assert low["median_unit_price"] == 75.0
    assert low["is_high_risk_zone"] == 1


def test_price_deviation_vs_city_median_is_recorded():
    rows = zone_rows("gz", 23.13, 113.32, 50.0, 20) + zone_rows("gz", 23.50, 113.60, 150.0, 20)

    zones, _ = features.build_price_zones(rows)
    low = min(zones, key=lambda z: z["median_unit_price"])

    assert low["price_dev_vs_city"] == pytest.approx(-0.5)  # 比城市中位低 50%


def test_price_zone_id_encodes_city_and_grid():
    """zone_id 要能反查所属城市与网格，否则区块画像不可解释。"""
    rows = zone_rows("gz", 23.13, 113.32, 90_000.0, 20)

    zones, _ = features.build_price_zones(rows)

    assert zones[0]["zone_id"].startswith("price-gz-")
    assert zones[0]["zone_type"] == "price"


def test_price_zones_are_split_per_city():
    """同一网格坐标但不同城市码要分成两个区块，城市中位基准也各算各的。"""
    rows = zone_rows("gz", 23.13, 113.32, 90_000.0, 20) + zone_rows(
        "sz", 23.13, 113.32, 90_000.0, 20
    )

    zones, _ = features.build_price_zones(rows)

    assert {z["city"] for z in zones} == {"gz", "sz"}


def test_cluster_on_grid_boundary_is_split_into_two_cells():
    """⚠️ 可解释性缺口：正好跨在 0.02° 网格线上的一簇挂牌会被劈成两格。

    这里 20 条样本横跨经度 113.32（网格线），两侧各约一半，都够不到
    MIN_ZONE_SAMPLES=20，于是**一个区块都建不出来**——不会报错，只是这片
    区域悄悄没有了价格面画像和高危区判定。真实房源沿主干道分布时容易踩到。
    """
    rows = [
        spatmods.sale_row(23.135, 113.32 - 5e-5 + i * 1e-5, 90_000.0, city="gz") for i in range(20)
    ]

    assert len({config.grid_key(r["lng"], r["lat"]) for r in rows}) == 2

    zones, _ = features.build_price_zones(rows)

    assert zones == []


# ================================================================ 高危区 B：LTV 集中


def collateral(cid, ltv, lat=31.23, lng=121.47):
    return {"collateral_id": cid, "lat": lat, "lng": lng, "ltv": ltv}


def test_ltv_zone_requires_minimum_samples():
    cols = [collateral(i, 0.95) for i in range(config.MIN_LTV_ZONE_SAMPLES - 1)]

    zones, index = features.build_ltv_zones(cols)

    assert zones == [] and index == {}


def test_ltv_zone_flagged_when_median_exceeds_red_line():
    cols = [collateral(i, 0.95) for i in range(3)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["is_high_risk_zone"] == 1
    assert zones[0]["high_risk_rule"] == "ltv_high"
    assert zones[0]["median_ltv"] == 0.95


def test_ltv_zone_at_exactly_red_line_is_not_high_risk():
    """规则 B 是「严格大于」红线，与 tools/risk 的预警口径一致。"""
    cols = [collateral(i, config.LTV_RED_LINE) for i in range(3)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["median_ltv"] == config.LTV_RED_LINE
    assert zones[0]["is_high_risk_zone"] == 0


def test_ltv_zone_uses_median_so_single_outlier_does_not_flip_it():
    """中位数抗离群：一笔 3.0 的极端 LTV 不该把整个网格打成高危。"""
    cols = [collateral(0, 0.3), collateral(1, 0.4), collateral(2, 3.0)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["median_ltv"] == 0.4
    assert zones[0]["is_high_risk_zone"] == 0


def test_ltv_zone_skips_collateral_without_coordinates():
    cols = [collateral(i, 0.95) for i in range(3)]
    cols.append({"collateral_id": 99, "lat": None, "lng": None, "ltv": 0.99})

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["sample_count"] == 3


def test_ltv_zone_id_encodes_grid_and_has_no_city():
    """抵押物是上海合成坐标，与广东 DWD 网格不重叠，故 LTV 区块不带城市。"""
    cols = [collateral(i, 0.95) for i in range(3)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["zone_id"].startswith("ltv-")
    assert zones[0]["zone_type"] == "ltv"
    assert zones[0]["city"] is None


def test_ltv_zone_all_none_ltv_yields_no_zone_instead_of_crashing():
    """曾经的缺陷：有坐标但 LTV 为 None 的抵押物让整条 S3 链路抛 TypeError。

    enrich_ltv 在 true_market_price 缺失/为 0 时把 ltv 置 None（合理），
    build_ltv_zones 却直接 float(c["ltv"]) 无守卫，main.py 两步之间也无过滤。
    现在全为 None → 无观测 → 不产生区块，而不是崩。
    """
    cols = [collateral(i, None) for i in range(3)]

    zones, zone_index = features.build_ltv_zones(cols)

    assert zones == []
    assert zone_index == {}


def test_ltv_zone_skips_collateral_without_ltv():
    """LTV 缺失的抵押物排除出统计，不计入 sample_count 分母。"""
    cols = [collateral(i, 0.95) for i in range(3)] + [collateral(9, None)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["sample_count"] == 3


def test_none_ltv_does_not_help_pass_min_sample_gate():
    """分母口径的要害：2 条缺失 + 1 条真实观测，不足以撑起一个区块画像。

    若把 None 计入分母，这里会用单点「中位数」造出一个高危区——正是
    MIN_LTV_ZONE_SAMPLES 要防的事。
    """
    assert config.MIN_LTV_ZONE_SAMPLES == 3
    cols = [collateral(0, 0.95), collateral(1, None), collateral(2, None)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones == []


def test_none_ltv_excluded_from_median_not_treated_as_zero():
    """排除 ≠ 当成 0。当成 0 会把中位数往下拽，高危区被漏报。"""
    cols = [collateral(i, 0.95) for i in range(3)] + [collateral(i, None) for i in range(3, 7)]

    zones, _ = features.build_ltv_zones(cols)

    assert zones[0]["median_ltv"] == 0.95
    assert zones[0]["is_high_risk_zone"] == 1


# ================================================================ POI 密度代理


def test_poi_density_is_count_over_circle_area(gz_listings):
    tree, coords, _ = surface(gz_listings)

    dens = features.poi_density_proxy(tree, coords, q(23.1300, 113.3200))

    expected = len(gz_listings) / (np.pi * config.POI_RADIUS_KM**2)
    assert dens[0] == pytest.approx(expected)


def test_poi_density_is_nan_outside_coverage(gz_listings):
    """半径内无挂牌且最近挂牌超出覆盖半径 → 无覆盖（缺失），不是密度 0。"""
    tree, coords, _ = surface(gz_listings)

    dens = features.poi_density_proxy(tree, coords, q(31.23, 121.47))

    assert np.isnan(dens[0])


def test_poi_density_zero_within_coverage_is_not_missing(gz_listings):
    """覆盖半径内但 2km 采样圈内无挂牌 → 真的是 0 密度，与「缺失」语义不同。"""
    tree, coords, _ = surface(gz_listings)
    # 距离簇约 10km：在 POI_COVERAGE_KM(20) 内、POI_RADIUS_KM(2) 外
    dens = features.poi_density_proxy(tree, coords, q(23.2200, 113.3200))

    assert dens[0] == 0.0
    assert not np.isnan(dens[0])


# ================================================================ 通勤代理


def test_commute_time_is_distance_over_speed():
    gz_lng, gz_lat = config.CITY_CENTER["gz"]

    assert features.commute_minutes(gz_lat, gz_lng, "gz") == 0.0


def test_commute_time_grows_with_distance():
    gz_lng, gz_lat = config.CITY_CENTER["gz"]

    near = features.commute_minutes(gz_lat + 0.05, gz_lng, "gz")
    far = features.commute_minutes(gz_lat + 0.20, gz_lng, "gz")

    assert 0 < near < far


def test_commute_time_matches_speed_setting():
    gz_lng, gz_lat = config.CITY_CENTER["gz"]
    d = features.haversine_km(gz_lat + 0.1, gz_lng, gz_lat, gz_lng)

    got = features.commute_minutes(gz_lat + 0.1, gz_lng, "gz")

    assert got == pytest.approx(d / config.COMMUTE_SPEED_KMH * 60.0, abs=0.01)


def test_commute_without_city_code_picks_nearest_center():
    """抵押物无城市归属时取最近的城市中心，而不是直接判缺失。"""
    sz_lng, sz_lat = config.CITY_CENTER["sz"]

    assert features.commute_minutes(sz_lat, sz_lng, None) == 0.0


def test_commute_with_unknown_city_code_falls_back_to_nearest():
    sz_lng, sz_lat = config.CITY_CENTER["sz"]

    assert features.commute_minutes(sz_lat, sz_lng, "not-a-city") == 0.0


def test_commute_is_none_outside_coverage():
    """上海坐标离广东任何城市中心都超过 60km → 通勤缺失。"""
    assert features.commute_minutes(31.23, 121.47, None) is None


def test_commute_declared_city_is_used_even_if_farther():
    """显式给了城市码就用该城市中心，不去挑更近的别的城市。"""
    gz_lng, gz_lat = config.CITY_CENTER["gz"]

    to_gz = features.commute_minutes(gz_lat, gz_lng, "gz")
    to_nearest = features.commute_minutes(gz_lat, gz_lng, None)

    assert to_gz == 0.0
    assert to_nearest == 0.0  # 广州中心的最近城市中心就是广州自己


# ================================================================ 缺失率


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        ({"poi_density": 1.0, "commute_min": 2.0, "zone_id": "z", "price_deviation": 0.1}, 0.0),
        ({"poi_density": None, "commute_min": 2.0, "zone_id": "z", "price_deviation": 0.1}, 25.0),
        ({"poi_density": None, "commute_min": None, "zone_id": "z", "price_deviation": 0.1}, 50.0),
        (
            {"poi_density": None, "commute_min": None, "zone_id": None, "price_deviation": None},
            100.0,
        ),
    ],
)
def test_missing_pct_counts_four_spatial_features(rec, expected):
    assert features._missing_pct(rec) == expected


def test_missing_pct_treats_zero_as_present():
    """0 是有效取值（如密度 0、偏差 0），不能被当成缺失。"""
    rec = {"poi_density": 0.0, "commute_min": 0.0, "zone_id": "z", "price_deviation": 0.0}

    assert features._missing_pct(rec) == 0.0


def test_collateral_missing_pct_is_high_enough_to_trigger_low_confidence():
    """AC-04 联动：抵押物落在广东价格面之外时缺失率应 >= 25%，从而被风险引擎判低置信。"""
    rows = [spatmods.sale_row(23.13 + i * 1e-4, 113.32, 90_000.0) for i in range(25)]
    tree, coords, prices = surface(rows)
    cols = [
        {"collateral_id": 1, "lat": 31.23, "lng": 121.47, "true_market_price": 1e7, "area": 100.0}
    ]

    feats = features.build_collateral_features(cols, tree, coords, prices, {})

    assert feats[0]["spatial_feat_missing_pct"] >= 25.0


# ================================================================ LTV 回填


def test_enrich_ltv_divides_balance_by_market_price():
    cols = [{"collateral_id": 1, "true_market_price": 1_000_000.0}]

    features.enrich_ltv(cols, [{"collateral_id": 1, "balance": 800_000.0}])

    assert cols[0]["ltv"] == pytest.approx(0.8)


def test_enrich_ltv_sums_balances_for_one_collateral_many_loans():
    """一物多贷：敞口是所有贷款余额合计，不是取其中一笔。"""
    cols = [{"collateral_id": 1, "true_market_price": 1_000_000.0}]
    loans = [{"collateral_id": 1, "balance": 300_000.0}, {"collateral_id": 1, "balance": 500_000.0}]

    features.enrich_ltv(cols, loans)

    assert cols[0]["ltv"] == pytest.approx(0.8)


def test_enrich_ltv_is_zero_when_no_loan_attached():
    cols = [{"collateral_id": 1, "true_market_price": 1_000_000.0}]

    features.enrich_ltv(cols, [])

    assert cols[0]["ltv"] == 0.0


@pytest.mark.parametrize("price", [None, 0.0])
def test_enrich_ltv_is_none_without_valid_market_price(price):
    """分母无效 → LTV 不可算，置 None 而不是 0（0 会被误读成「无风险」）。"""
    cols = [{"collateral_id": 1, "true_market_price": price}]

    features.enrich_ltv(cols, [{"collateral_id": 1, "balance": 800_000.0}])

    assert cols[0]["ltv"] is None


def test_enrich_ltv_treats_null_balance_as_zero():
    cols = [{"collateral_id": 1, "true_market_price": 1_000_000.0}]

    features.enrich_ltv(cols, [{"collateral_id": 1, "balance": None}])

    assert cols[0]["ltv"] == 0.0
