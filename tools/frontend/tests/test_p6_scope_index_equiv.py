"""P6 作用域候选 _scope_loan_indices 的索引等价性测试（零 DB）。

性能改造把「每笔贷款 × haversine」换成「快照预计算 zone_loans 查表」，但
判定必须与旧实现逐字同口径——这里用真实构建器 _zone_loan_index 造索引，
再对每个网格断言「索引答案 == _in_scope 暴力全筛」，把等价性钉死成回归
用例而不是口头承诺。

覆盖：
1. 随机坐标下（固定种子），每个网格的索引命中集与暴力全筛逐字相等；
2. 索引答不出该网格（zone_loans 里没有这个 zid）时回落全量筛——结论一致，
   绝不能静默返回空集冒充真实结果；
3. 合成快照（无 zone_loans 键，测试 / 无索引调用方的形态）一律走全量筛；
4. 下标升序是硬约束：cover.balance 是浮点连加，顺序换了 round 到分位的
   尾数就会漂；
5. city / industry 作用域不走索引，语义直接由 _in_scope 定义（顺带钉住
   SCOPE_ALL_CITY 命中全部城市、industry 为 None 不命中任何行业）。
"""

import random

import pages.p6_policy as p6


def _loan(idx, lat, lng, city="gz", industry=None):
    return {
        "loan_id": 9000 + idx,
        "collateral_id": 9000 + idx,
        "balance": 1_000_000.0,
        "ltv": 0.6,
        "risk_class": "关注",
        "low_confidence": 0,
        "is_high_risk_zone": 0,
        "lat": lat,
        "lng": lng,
        "addr": None,
        "city": city,
        "industry": industry,
    }


def _zone(zid, lat, lng, city="gz"):
    return {
        "zone_id": zid,
        "zone_type": "price",
        "city": city,
        "center_lat": lat,
        "center_lng": lng,
        "sample_count": 10,
        "median_unit_price": 30000.0,
        "median_ltv": None,
        "price_dev_vs_city": None,
        "is_high_risk_zone": 0,
        "high_risk_rule": None,
    }


def _build(loans, zones):
    """按 _build_snapshot 的同一套工序造快照（坐标分桶 → 网格命中索引）。"""
    coord_index = {}
    for idx, loan in enumerate(loans):
        if loan["lat"] is not None and loan["lng"] is not None:
            coord_index.setdefault(p6._bucket_key(loan["lat"], loan["lng"]), []).append(idx)
    return {
        "loans": loans,
        "zones": zones,
        "coord_index": coord_index,
        "zone_loans": p6._zone_loan_index(loans, zones, coord_index),
    }


def _brute_zone(snapshot, zid):
    return [
        i
        for i, ln in enumerate(snapshot["loans"])
        if p6._in_scope(ln, "zone", zid, snapshot["zones"])
    ]


def test_zone_scope_uses_index_equiv():
    """固定种子随机撒点：每个网格 索引命中集 == 暴力全筛（逐字不变）。"""
    rng = random.Random(20240601)
    # 中心网格贴着真实广州坐标；贷款坐标在 ±0.5°（≈±55km）内撒，既有命中也有落空。
    zones = {
        f"price-gz-{i}": _zone(
            f"price-gz-{i}", 23.14 + rng.uniform(-0.1, 0.1), 113.30 + rng.uniform(-0.1, 0.1)
        )
        for i in range(6)
    }
    loans = [
        _loan(i, 23.14 + rng.uniform(-0.5, 0.5), 113.30 + rng.uniform(-0.5, 0.5))
        for i in range(300)
    ]
    # 少量无坐标贷款：不入 coord_index，_in_scope 也判 False，两侧必须一致。
    loans[7]["lat"] = loans[7]["lng"] = None
    loans[42]["lat"] = loans[42]["lng"] = None
    snap = _build(loans, zones)

    assert snap["zone_loans"] is not None
    for zid in zones:
        got = p6._scope_loan_indices(snap, "zone", zid)
        assert got == _brute_zone(snap, zid), zid
        assert got == snap["zone_loans"][zid], zid


def test_index_miss_falls_back_to_full_scan():
    """zone_loans 里查无此网格 → 回落全量筛，结论与暴力筛一致（非空集冒充）。"""
    loans = [_loan(i, 23.14, 113.30) for i in range(5)]
    zones = {"price-gz-0": _zone("price-gz-0", 23.14, 113.30)}
    snap = _build(loans, zones)
    # 篡改快照：抹掉这个网格的索引条目，模拟「索引答不出」的调用形态。
    snap["zone_loans"] = {"price-gz-other": [0, 1]}

    got = p6._scope_loan_indices(snap, "zone", "price-gz-0")
    assert got == _brute_zone(snap, "price-gz-0") == [0, 1, 2, 3, 4]


def test_snapshot_without_zone_loans_key_falls_back():
    """合成快照（无 zone_loans 键）不炸、不走索引，语义与全量筛逐字一致。"""
    loans = [_loan(i, 23.14 + i * 0.001, 113.30) for i in range(4)]
    zones = {"price-gz-0": _zone("price-gz-0", 23.14, 113.30)}
    snap = {"loans": loans, "zones": zones}  # 无 zone_loans / coord_index

    got = p6._scope_loan_indices(snap, "zone", "price-gz-0")
    assert got == [0, 1, 2, 3]
    # 距离超出 5km 半径的贷款不命中。
    far = {"price-gz-far": _zone("price-gz-far", 23.14 + 0.2, 113.30)}
    snap2 = {"loans": loans, "zones": far}
    assert p6._scope_loan_indices(snap2, "zone", "price-gz-far") == []


def test_scope_indices_are_ascending():
    """下标升序是硬约束：cover.balance 浮点连加的顺序不能被桶序打乱。"""
    rng = random.Random(7)
    zones = {"price-gz-0": _zone("price-gz-0", 23.14, 113.30)}
    loans = [
        _loan(i, 23.14 + rng.uniform(-0.03, 0.03), 113.30 + rng.uniform(-0.03, 0.03))
        for i in range(50)
    ]
    snap = _build(loans, zones)
    got = p6._scope_loan_indices(snap, "zone", "price-gz-0")
    assert got == sorted(got)
    assert got  # 撒点半径远小于 5km，必然全部命中


def test_city_and_industry_scopes_still_brute_force():
    """city / industry 不吃索引：ALL_CITY 命中全部城市、None 行业不命中。"""
    loans = [
        _loan(0, 23.14, 113.30, city="gz", industry="制造"),
        _loan(1, 23.14, 113.30, city="sz", industry="制造"),
        _loan(2, 23.14, 113.30, city="gz", industry=None),
    ]
    zones = {"price-gz-0": _zone("price-gz-0", 23.14, 113.30)}
    snap = _build(loans, zones)

    assert p6._scope_loan_indices(snap, "city", p6.SCOPE_ALL_CITY) == [0, 1, 2]
    assert p6._scope_loan_indices(snap, "city", "gz") == [0, 2]
    assert p6._scope_loan_indices(snap, "city", "sz") == [1]
    assert p6._scope_loan_indices(snap, "industry", "制造") == [0, 1]
    assert p6._scope_loan_indices(snap, "industry", "批发零售") == []
