"""P6 情景地域闸门：区级限定词解析 + record 级冲击筛选。

产品口径：市场情景只影响情景限定地域内的 record——「广州房贷利率上升」只影响
广州的 record，「越秀区的房贷利率上升」只影响越秀区的 record。改动前区级限定词
完全不被识别（越秀区情景 = 全国情景，冲击打到作用域内全部 record 上），
且城市级情景挂在行业作用域（跨城市）上会把外市 record 也放大 LTV。

覆盖：
1. parse_scenario：区级限定词白名单解析（含「越秀区的房贷利率上升」这类
   区级 × 利率情景）、泛词不误判（核心区 / 城区 / 小区）；
2. _loan_in_shock_geo：record 级地域闸门三分支；
3. _impact：区级冲击只放大地域内 record 的 LTV（shock_added / geo_scope /
   敏感带 / 中位数全部只数地域内的笔），城市情景 × 行业作用域不再波及外市；
4. _resolve_shock：区级情景 × 异城作用域 → 零影响且说明带区名；
5. _process_chain：区级情景明示作用面与域外笔口径；
6. _sensitivity：作用域内无情景地域存量 → 不给误导性的零冲击平线；
7. handle_stress：区限定只统计该区存量，scope_note 带区名。
"""

import pages.p6_policy as p6
import pytest


@pytest.fixture(autouse=True)
def _no_boundary_data(monkeypatch):
    # 区名限定情景的坐标闸门映射默认按「边界数据不可用」stub（_district_loan_map
    # 返回 (None, None)）：本文件单测不走 _load_snapshot，绝不能依赖本地缓存文件 /
    # 外网；映射 None 让 _loan_in_shock_geo 走地址子串匹配——即本文件既有的地址
    # 口径全部保留为「边界不可用时的兜底路径」用例。坐标优先口径由
    # test_p6_income_dti.py 的注入映射用例覆盖。
    monkeypatch.setattr(p6, "_district_loan_map", lambda snap, city: (None, None))


# ---------------- 1. parse_scenario：区级限定词 ----------------


def test_parse_district_price_drop():
    s = p6.parse_scenario("越秀区房价下跌 10%")
    # 区名即城市归属：不写「广州」也归 gz
    assert s["kind"] == "price_drop"
    assert s["district"] == "越秀区"
    assert s["city"] == "gz"
    assert abs(s["pct"] - 0.10) < 1e-9


def test_parse_district_with_city_prefix():
    s = p6.parse_scenario("广州市天河区房价下跌 20%")
    assert s["district"] == "天河区"
    assert s["city"] == "gz"


def test_parse_district_rate_scenario():
    # 区级 × 利率类：利率情景不产生 record 级定量影响，但地域口径必须随情景下发
    s = p6.parse_scenario("越秀区的房贷利率上升 1 个百分点")
    assert s["kind"] == "rate_change"
    assert s["pp"] == 1.0
    assert s["district"] == "越秀区"
    assert s["city"] == "gz"


def test_parse_district_wins_over_conflicting_city():
    # 自相矛盾输入（深圳越秀区）按更具体的区所在城市解释
    s = p6.parse_scenario("深圳越秀区房价下跌 10%")
    assert s["district"] == "越秀区"
    assert s["city"] == "gz"


def test_parse_generic_qu_word_not_district():
    # 泛词不在白名单：仍是城市级 / 全国情景，不得误判成区级
    assert p6.parse_scenario("广州核心区房价下跌 10%")["district"] is None
    assert p6.parse_scenario("全国城区房价下跌 10%")["district"] is None
    assert p6.parse_scenario("某小区房价下跌 10%")["district"] is None


# ---------------- 2. _loan_in_shock_geo：record 级闸门 ----------------


def _loan(lid, ltv, addr, city="gz", balance=1_000_000.0, industry=None, lat=23.14, lng=113.30):
    return {
        "loan_id": lid,
        "collateral_id": lid,
        "balance": balance,
        "ltv": ltv,
        "risk_class": "关注",
        "low_confidence": 0,
        "is_high_risk_zone": 0,
        "lat": lat,
        "lng": lng,
        "addr": addr,
        "city": city,
        "industry": industry,
    }


def test_geo_gate_branches():
    district_sc = {
        "kind": "price_drop",
        "pct": 0.1,
        "city": "gz",
        "district": "越秀区",
        "label": "x",
    }
    assert p6._loan_in_shock_geo({"addr": "广州市越秀区中山一路2号"}, district_sc)
    assert not p6._loan_in_shock_geo({"addr": "广州市天河区体育西路1号"}, district_sc)
    assert not p6._loan_in_shock_geo({"addr": None}, district_sc)
    city_sc = {"kind": "price_drop", "pct": 0.1, "city": "gz", "district": None, "label": "x"}
    assert p6._loan_in_shock_geo({"addr": "广州市天河区体育西路1号", "city": "gz"}, city_sc)
    assert not p6._loan_in_shock_geo({"addr": "深圳市福田区x路1号", "city": "sz"}, city_sc)
    # 无地域限定（含全国）→ 任意 record 都在冲击面内
    global_sc = {"kind": "price_drop", "pct": 0.1, "city": None, "district": None, "label": "x"}
    assert p6._loan_in_shock_geo({"addr": "上海市浦东新区x路1号", "city": None}, global_sc)
    assert p6._loan_in_shock_geo({"addr": "x"}, None)


# ---------------- 3. _impact：区级冲击只放大地域内 record ----------------

ZONE_ID = "price-gz-113.30-23.14"


def _geo_snapshot():
    zones = {
        ZONE_ID: {
            "zone_id": ZONE_ID,
            "zone_type": "price",
            "city": "gz",
            "center_lat": 23.14,
            "center_lng": 113.30,
            "sample_count": 10,
            "median_unit_price": 30000.0,
            "median_ltv": 0.7,
            "price_dev_vs_city": -0.1,
            "is_high_risk_zone": 1,
            "high_risk_rule": "price_low",
        }
    }
    loans = [
        # 阈值 0.70、红线 0.85、冲击 −10%（÷0.9）：
        # L1 0.68 越秀 → 0.756 冲击后命中（shock_added）
        # L2 0.80 天河 → 不在地域内，按原 LTV 0.80 判定（基线命中，非因情景新增）
        # L3 0.90 越秀 → 1.0 恒越红线
        _loan(1, 0.68, "广州市越秀区中山一路1号"),
        _loan(2, 0.80, "广州市天河区体育西路2号"),
        _loan(3, 0.90, "广州市越秀区解放北路3号"),
    ]
    return {"loans": loans, "zones": zones}


_DISTRICT_SHOCK = {
    "kind": "price_drop",
    "pct": 0.10,
    "city": "gz",
    "district": "越秀区",
    "label": "房价下跌 10%",
}


def test_impact_district_shock_only_amplifies_matching_records():
    out = p6._impact(_geo_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=_DISTRICT_SHOCK)
    # 命中 3 笔（L1 冲击后 0.756、L2 原值 0.80、L3 冲击后 1.0）
    assert out["hit"]["loans"] == 3
    # 新增受限 = L1（0.756）+ L2（0.80）= 2；已预警 = L3
    assert out["newly_hit"]["loans"] == 2
    assert out["already_alert"]["loans"] == 1
    s = out["shock"]
    # 情景地域覆盖：仅越秀的 2 笔；因冲击新增命中的只有 L1
    assert s["geo_scope"]["loans"] == 2
    assert s["extra_hit_loans"] == 1
    assert s["flip_count"] == 1  # 敏感带只数地域内的笔，与 extra_hit_loans 自洽
    row1 = next(r for r in out["rows"] if r["loan_id"] == 1)
    row2 = next(r for r in out["rows"] if r["loan_id"] == 2)
    assert abs(row1["ltv"] - 0.68 / 0.9) < 1e-3 and row1["shock_added"] is True
    # 天河的笔：ltv == ltv_base（未被放大），也绝不是「因情景新增」
    assert row2["ltv"] == row2["ltv_base"] == 0.80 and row2["shock_added"] is False
    # 中位数位移只统计地域内的笔：[0.68, 0.90] → [0.756, 1.0]，中位 0.79 → 0.878
    assert abs(s["ltv_median_before"] - 0.79) < 1e-6
    assert abs(s["ltv_median_after"] - (0.68 / 0.9 + 1.0) / 2) < 1e-6


def test_impact_city_scenario_not_leak_across_industry_scope():
    # 「广州房价下跌」挂在行业作用域（跨城市）上：只冲击该行业内广州的 record，
    # 深圳同行业笔的 LTV 不得被放大（改动前这里会误伤外市）。
    snapshot = {
        "loans": [
            _loan(1, 0.68, "广州市越秀区x路1号", industry="tech"),
            _loan(2, 0.68, "深圳市南山区科技园2号", city="sz", industry="tech"),
        ],
        "zones": {},
    }
    shock = {"kind": "price_drop", "pct": 0.10, "city": "gz", "district": None, "label": "x"}
    out = p6._impact(snapshot, "industry", "tech", "reject", 0.65, shock=shock)
    rows = {r["loan_id"]: r for r in out["rows"]}
    assert abs(rows[1]["ltv"] - 0.68 / 0.9) < 1e-3  # 广州笔被放大
    assert rows[2]["ltv"] == 0.68  # 深圳笔按原 LTV，判定不变
    assert rows[2]["shock_added"] is False


# ---------------- 4. _resolve_shock：区级情景 × 异城作用域 ----------------


def test_resolve_district_shock_city_mismatch():
    sc = _DISTRICT_SHOCK
    shock, note = p6._resolve_shock(sc, "city", "sz", {})
    assert shock is None
    # 核对说明要点名区名，用户才知道是哪个地域对不上
    assert "越秀区" in note and "无影响" in note


def test_resolve_district_shock_same_city_passes_to_record_gate():
    # 同城：作用域核对放行，收窄交给 record 级闸门（_impact / _loan_in_shock_geo）
    shock, note = p6._resolve_shock(_DISTRICT_SHOCK, "city", "gz", {})
    assert shock is _DISTRICT_SHOCK and note is None


# ---------------- 5. _process_chain：区级情景明示作用面 ----------------


def test_process_chain_district_shock_states_scope():
    imp = p6._impact(_geo_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=_DISTRICT_SHOCK)
    steps = p6._process_chain(imp, _DISTRICT_SHOCK, None)
    head = steps[0]["detail"]
    assert "限越秀区" in head and "2 笔存量" in head
    assert "其余 1 笔不在情景地域" in head  # 天河那笔的口径要明说
    assert "越秀区内 LTV 中位数" in steps[1]["detail"]


def test_process_chain_district_zero_hits_single_explanatory_step():
    # 区级情景 × 作用域内没有该区存量：没有笔被冲击、分位样本为空——
    # 曾在此处 NoneFormat 崩溃（500），必须退化为单独一步说明而不是硬格式化。
    only_tianhe = {
        "loans": [_loan(2, 0.80, "广州市天河区体育西路2号")],
        "zones": _geo_snapshot()["zones"],
    }
    shock = dict(_DISTRICT_SHOCK)
    imp = p6._impact(only_tianhe, "zone", ZONE_ID, "reject", 0.70, shock=shock)
    steps = p6._process_chain(imp, shock, None)
    assert [s["step"] for s in steps] == ["情景解析"]
    assert "越秀区" in steps[0]["detail"] and "无影响" in steps[0]["detail"]


def test_process_chain_district_all_missing_ltv_skips_distribution_step():
    # 地域内的笔全部缺估值（有坐标无 LTV）：分位对为空 → 中位数为 None，
    # 传导分布步跳过（不崩），规则评估步如实呈现 0 增量。
    snap = {
        "loans": [
            _loan(1, None, "广州市越秀区中山一路1号"),
            _loan(2, 0.80, "广州市天河区体育西路2号"),
        ],
        "zones": _geo_snapshot()["zones"],
    }
    shock = dict(_DISTRICT_SHOCK)
    imp = p6._impact(snap, "zone", ZONE_ID, "reject", 0.70, shock=shock)
    steps = p6._process_chain(imp, shock, None)
    assert [s["step"] for s in steps] == ["情景解析", "规则评估"]


# ---------------- 6. _sensitivity：无地域存量不给零冲击平线 ----------------


def test_sensitivity_suppressed_when_district_absent_in_scope():
    shock = dict(_DISTRICT_SHOCK)
    # 作用域内全是天河地址 → 区级情景每档都零冲击 → 不给误导性平线
    only_tianhe = {
        "loans": [_loan(2, 0.80, "广州市天河区体育西路2号")],
        "zones": _geo_snapshot()["zones"],
    }
    rows = p6._sensitivity(only_tianhe, "zone", ZONE_ID, "reject", 0.70, shock)
    assert rows is None


def test_sensitivity_runs_when_district_present():
    rows = p6._sensitivity(_geo_snapshot(), "zone", ZONE_ID, "reject", 0.70, dict(_DISTRICT_SHOCK))
    assert rows is not None and len(rows) == len(p6.SENSITIVITY_PCTS)
    # 恰等于本次情景 pct 的档位（0.10）应与 _impact 直跑同口径
    row = next(r for r in rows if abs(r["pct"] - 0.10) < 1e-9)
    assert row["hit_loans"] == 3


# ---------------- 7. handle_stress：区限定只统计该区存量 ----------------


def _stress(monkeypatch, body, snapshot, role="admin"):
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snapshot)
    ctx = type(
        "_Ctx",
        (),
        {
            "body": body,
            "ip": "127.0.0.1",
            "user": {"user": "tester", "role": role, "label": role},
        },
    )()
    return p6.handle_stress(ctx)


def test_stress_district_scope_only_counts_matching_records(monkeypatch):
    snap = {
        "loans": [
            _loan(1, 0.80, "广州市越秀区中山一路1号"),
            _loan(2, 0.80, "广州市天河区体育西路2号", balance=3_000_000.0),
            _loan(3, 0.95, "深圳市福田区深南大道3号", city="sz"),
        ],
        "zones": {},
    }
    out = _stress(monkeypatch, {"hypothesis": "越秀区房价下跌 10%"}, snap)
    # 只有越秀那笔进统计：天河 / 深圳的 LTV 不得被越秀区的房价冲击放大
    assert out["totals"]["loans"] == 1
    assert out["totals"]["balance"] == 1_000_000.0
    assert sum(b["loans_before"] for b in out["ltv_hist"]) == 1
    assert out["stats"]["red_after"]["loans"] == 1  # 0.80 → 0.889 翻线
    assert out["shock"]["district"] == "越秀区"
    assert out["shock"]["city"] == "gz"
    assert out["scope_note"] == "仅统计广州越秀区存量"


def test_stress_city_scope_note_unchanged(monkeypatch):
    snap = {"loans": [_loan(1, 0.80, "广州市天河区x路1号")], "zones": {}}
    out = _stress(monkeypatch, {"hypothesis": "广州房价下跌 10%"}, snap)
    assert out["shock"]["district"] is None
    assert out["scope_note"] == "仅统计广州存量"
    assert out["totals"]["loans"] == 1
