"""P6 性能优化（快照缓存 + 坐标桶剪枝）的专项测试。

覆盖 agent 新增但此前无任何测试的保护代码：
1. `_zone_loan_index` 桶剪枝命中集合必须与旧暴力双循环完全一致（口径不变是
   本优化的核心承诺，一旦漂移就是数据事故）；
2. `_load_snapshot` 快照缓存的「指纹相同复用 / 指纹变化重建」语义；
3. `_snapshot_fingerprint` 对行数 / 聚合值变化的敏感度（FakeConn mock 两条
   UNION 指纹查询）；
4. `_parse_rule` 的 ltv_cap 边界（与前端 0.01–2 校验对齐）。

除指纹用 FakeConn 外全部零 DB（直喂内存快照 / monkeypatch）。
"""

import random

import pages.p6_policy as p6
from fakeconn import FakeConn

# ---------------- 工具：构造带坐标的快照 ----------------

_GD_BBOX = {"lat_lo": 20.0, "lat_hi": 25.5, "lng_lo": 109.5, "lng_hi": 118.0}


def _make_loans(n, seed=42):
    """确定性伪随机 n 笔贷款，坐标落在广东范围，约 8% 无坐标。"""
    rng = random.Random(seed)
    loans = []
    for i in range(n):
        if rng.random() < 0.08:
            loans.append({"loan_id": i, "lat": None, "lng": None})
        else:
            loans.append(
                {
                    "loan_id": i,
                    "lat": round(rng.uniform(_GD_BBOX["lat_lo"], _GD_BBOX["lat_hi"]), 6),
                    "lng": round(rng.uniform(_GD_BBOX["lng_lo"], _GD_BBOX["lng_hi"]), 6),
                }
            )
    return loans


def _make_zones(n, seed=7):
    """确定性伪随机 n 个网格，约 8% 无中心坐标。"""
    rng = random.Random(seed)
    zones = {}
    for i in range(n):
        if rng.random() < 0.08:
            zones[f"zone-{i}"] = {"zone_id": f"zone-{i}", "center_lat": None, "center_lng": None}
        else:
            zones[f"zone-{i}"] = {
                "zone_id": f"zone-{i}",
                "center_lat": round(rng.uniform(_GD_BBOX["lat_lo"], _GD_BBOX["lat_hi"]), 6),
                "center_lng": round(rng.uniform(_GD_BBOX["lng_lo"], _GD_BBOX["lng_hi"]), 6),
            }
    return zones


def _bruteforce(loans, zones):
    """旧实现：每笔贷款 × 每个网格，逐笔 haversine <= 5km。"""
    out = {}
    for zid, zone in zones.items():
        zlat, zlng = zone["center_lat"], zone["center_lng"]
        hits = []
        if zlat is not None:
            for i, loan in enumerate(loans):
                if loan["lat"] is None:
                    continue
                if (
                    p6._haversine_km(loan["lat"], loan["lng"], zlat, zlng)
                    <= p6.ZONE_MATCH_RADIUS_KM
                ):
                    hits.append(i)
        out[zid] = hits
    return out


# ---------------- 1. 桶剪枝 = 暴力双循环（核心口径承诺） ----------------


def test_zone_loan_index_equals_bruteforce():
    loans = _make_loans(500)
    zones = _make_zones(60)
    coord_index = {}
    for idx, loan in enumerate(loans):
        if loan["lat"] is not None:
            coord_index.setdefault(p6._bucket_key(loan["lat"], loan["lng"]), []).append(idx)

    new = p6._zone_loan_index(loans, zones, coord_index)
    old = _bruteforce(loans, zones)

    assert set(new) == set(old)
    for zid in zones:
        assert new[zid] == old[zid], f"网格 {zid} 命中集合不一致"


def test_zone_loan_index_missing_coords_consistent():
    """无坐标网格 → 空命中；无坐标贷款 → 不参与（新旧同口径）。"""
    loans = [
        {"loan_id": 0, "lat": None, "lng": None},
        {"loan_id": 1, "lat": 23.14, "lng": 113.30},
        {"loan_id": 2, "lat": 23.141, "lng": 113.301},
    ]
    zones = {
        "price-gz-113.30-23.14": {"center_lat": 23.14, "center_lng": 113.30, "zone_id": "z1"},
        "no-coord": {"center_lat": None, "center_lng": None, "zone_id": "z2"},
    }
    coord_index = {}
    for idx, loan in enumerate(loans):
        if loan["lat"] is not None:
            coord_index.setdefault(p6._bucket_key(loan["lat"], loan["lng"]), []).append(idx)

    new = p6._zone_loan_index(loans, zones, coord_index)
    old = _bruteforce(loans, zones)
    assert new == old
    assert new["no-coord"] == []
    # 无坐标贷款即使距离网格中心 0 也不该进命中（与旧实现一致）
    assert new["price-gz-113.30-23.14"] == [1, 2]


def test_zone_loan_index_bucket_boundary():
    """坐标恰在桶边界（lat/lng 为 0.1 的整数倍）两侧时不应漏命中。

    桶键用 floor：23.0 和 23.0999 落同桶，23.1 落下一桶；两桶都扫到才能命中
    5km 内的邻居。该用例直接验证「桶区间包含邻桶」的剪枝不丢点。
    """
    loans = [
        {"loan_id": 0, "lat": 23.0999, "lng": 113.30},
        {"loan_id": 1, "lat": 23.1, "lng": 113.30},
    ]
    zones = {"z": {"zone_id": "z", "center_lat": 23.09995, "center_lng": 113.30}}
    coord_index = {}
    for idx, loan in enumerate(loans):
        coord_index.setdefault(p6._bucket_key(loan["lat"], loan["lng"]), []).append(idx)
    new = p6._zone_loan_index(loans, zones, coord_index)
    assert new["z"] == [0, 1]  # 两笔都在网格中心 5km 内


# ---------------- 2. 快照缓存语义（monkeypatch，零 DB） ----------------


def _reset_cache():
    p6._SNAPSHOT_CACHE["fp"] = None
    p6._SNAPSHOT_CACHE["snapshot"] = None


def test_snapshot_cache_reuses_same_object(monkeypatch):
    _reset_cache()
    calls = []

    def fake_build():
        calls.append(1)
        return {"marker": len(calls)}

    monkeypatch.setattr(p6, "_build_snapshot", fake_build)
    monkeypatch.setattr(p6, "_snapshot_fingerprint", lambda: "fp-A")
    try:
        s1 = p6._load_snapshot()
        s2 = p6._load_snapshot()
        assert s1 is s2, "指纹未变时应复用同一快照对象"
        assert s1["marker"] == 1
        assert len(calls) == 1, "快照只应构建一次"
    finally:
        _reset_cache()


def test_snapshot_cache_rebuilds_on_fingerprint_change(monkeypatch):
    _reset_cache()
    calls = []

    def fake_build():
        calls.append(1)
        return {"marker": len(calls)}

    fp = {"v": "fp-A"}

    monkeypatch.setattr(p6, "_build_snapshot", fake_build)
    monkeypatch.setattr(p6, "_snapshot_fingerprint", lambda: fp["v"])
    try:
        s1 = p6._load_snapshot()
        fp["v"] = "fp-B"  # 数据库数据变了 → 指纹变
        s2 = p6._load_snapshot()
        assert s1 is not s2, "指纹变化后必须重建快照"
        assert s1["marker"] == 1 and s2["marker"] == 2
        assert len(calls) == 2
    finally:
        _reset_cache()


# ---------------- 3. 指纹敏感度（FakeConn mock 两条 UNION 查询） ----------------


def _fp_conns(rows_crawl, rows_biz):
    crawl = FakeConn().when(
        "dws_risk_class",
        rows_crawl,
        ["src", "n", "etl"],
    )
    biz = FakeConn().when(
        "collateral",
        rows_biz,
        ["src", "n", "s1", "s2", "s3"],
    )
    return crawl, biz


def test_fingerprint_same_data_same_fp(monkeypatch):
    rows_c = [
        ("dws_risk_class", 100, 1000),
        ("dws_spatial_feature", 50, 500),
        ("ads_spatial_zone", 20, 200),
    ]
    rows_b = [
        ("collateral", 100, 1234, 0, 0),
        ("customer", 100, 6000, 0, 0),
        ("loan", 100, 0, 0, 0),
    ]
    crawl, biz = _fp_conns(rows_c, rows_b)
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: crawl)
    monkeypatch.setattr(p6.db, "biz_conn", lambda: biz)
    assert p6._snapshot_fingerprint() == p6._snapshot_fingerprint()


def test_fingerprint_changes_on_row_count(monkeypatch):
    rows_c = [
        ("dws_risk_class", 100, 1000),
        ("dws_spatial_feature", 50, 500),
        ("ads_spatial_zone", 20, 200),
    ]
    rows_b1 = [
        ("collateral", 100, 1234, 0, 0),
        ("customer", 100, 6000, 0, 0),
        ("loan", 100, 0, 0, 0),
    ]
    rows_b2 = [
        ("collateral", 101, 1234, 0, 0),
        ("customer", 100, 6000, 0, 0),
        ("loan", 100, 0, 0, 0),
    ]

    c1, b1 = _fp_conns(rows_c, rows_b1)
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: c1)
    monkeypatch.setattr(p6.db, "biz_conn", lambda: b1)
    fp1 = p6._snapshot_fingerprint()

    c2, b2 = _fp_conns(rows_c, rows_b2)  # collateral 行数 100 → 101
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: c2)
    monkeypatch.setattr(p6.db, "biz_conn", lambda: b2)
    fp2 = p6._snapshot_fingerprint()
    assert fp1 != fp2, "行数变化必须让指纹变化（否则 UPDATE 后缓存不失效）"


def test_fingerprint_changes_on_aggregate_value(monkeypatch):
    rows_c = [
        ("dws_risk_class", 100, 1000),
        ("dws_spatial_feature", 50, 500),
        ("ads_spatial_zone", 20, 200),
    ]
    rows_b1 = [
        ("collateral", 100, 1234, 0, 0),
        ("customer", 100, 6000, 0, 0),
        ("loan", 100, 0, 0, 0),
    ]
    rows_b2 = [
        ("collateral", 100, 9999, 0, 0),
        ("customer", 100, 6000, 0, 0),
        ("loan", 100, 0, 0, 0),
    ]

    c1, b1 = _fp_conns(rows_c, rows_b1)
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: c1)
    monkeypatch.setattr(p6.db, "biz_conn", lambda: b1)
    fp1 = p6._snapshot_fingerprint()

    c2, b2 = _fp_conns(rows_c, rows_b2)  # collateral 地址长度和 1234 → 9999
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: c2)
    monkeypatch.setattr(p6.db, "biz_conn", lambda: b2)
    fp2 = p6._snapshot_fingerprint()
    assert fp1 != fp2, "原地 UPDATE 地址后聚合值变化，指纹必须变化"


# ---------------- 4. ltv_cap 校验与前端对齐（0.01–2） ----------------


def _parse(body, zones):
    return p6._parse_rule(body, zones)


def test_parse_rule_ltv_cap_lower_bound_aligned_with_frontend():
    zones = {"price-gz-113.30-23.14": {"center_lat": 23.14, "center_lng": 113.30, "city": "gz"}}
    base = {"scope_type": "zone", "scope_value": "price-gz-113.30-23.14", "action": "strict"}

    # 前端拦 < 0.01；后端必须同口径（0.005 应被拒），否则前后端判罚不一致
    try:
        _parse({**base, "ltv_cap": 0.005}, zones)
        assert False, "0.005 应被拒（与前端 0.01 下限对齐）"
    except ValueError as exc:
        assert "ltv_cap" in str(exc)

    # 0.01 与 2.0 都是合法边界
    assert _parse({**base, "ltv_cap": 0.01}, zones)[3] == 0.01
    assert _parse({**base, "ltv_cap": 2.0}, zones)[3] == 2.0

    # 超过 2 被拒
    try:
        _parse({**base, "ltv_cap": 2.01}, zones)
        assert False, "2.01 应被拒"
    except ValueError:
        pass

    # 留空 = 整区命中（不设阈值）；仅 notice 动作允许无阈值（strict/reject 必填）
    notice = {**base, "action": "notice"}
    assert _parse({**notice, "ltv_cap": None}, zones)[3] is None
    assert _parse({**notice, "ltv_cap": ""}, zones)[3] is None
