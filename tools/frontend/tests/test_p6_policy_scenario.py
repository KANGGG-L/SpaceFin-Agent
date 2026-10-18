"""P6 情景冲击打通的单元测试（零 DB：parse_scenario 纯函数 + _impact 直喂内存快照）。

覆盖「情景假设与拟议规则真正打通」的定量腿：
1. parse_scenario：房价下跌模板 → 结构化冲击；利率/收入类 → None（仅叙事腿）；
2. _impact 带 shock：冲击后 LTV（÷(1−pct)）参与命中判定，shock 对比块数字正确；
3. _impact 不带 shock：行为与打通前完全一致（纯规则推演不受影响）。

handler 层（handle_preview 的 admin 闸门）只是「role == admin 才解析」一行判断，
不另起 DB mock 测它。
"""

import pages.p6_policy as p6

# ---------------- parse_scenario ----------------


def test_parse_price_drop_with_city():
    s = p6.parse_scenario("广州核心区房价下跌 10%")
    # 「核心区」是泛词不是行政区（不在 DISTRICT_MAP 白名单）→ 仍是城市级情景
    assert s == {
        "kind": "price_drop",
        "pct": 0.10,
        "direction": "down",
        "city": "gz",
        "district": None,
        "industry": None,
        "label": "房价下跌 10%",
        "label_key": "房价下跌 {pct}%",
        "label_params": {"pct": "10"},
    }


def test_parse_price_drop_without_city_is_global():
    # 未指明城市 / 「全国」→ city=None，作用于任意作用域
    assert p6.parse_scenario("房价下跌 10%")["city"] is None
    assert p6.parse_scenario("全国房价下跌 20%")["city"] is None


def test_parse_price_drop_variants():
    for text, pct in [
        ("全国房价下跌 20%", 0.20),
        ("房价下跌8.5%", 0.085),
        ("抵押物价格下行 12%", 0.12),
        ("挂牌价异动下探 15%", 0.15),
    ]:
        s = p6.parse_scenario(text)
        assert s is not None, text
        assert s["kind"] == "price_drop"
        assert abs(s["pct"] - pct) < 1e-9


def test_parse_rate_and_income_scenarios():
    # 利率类 → rate_change（带符号百分点）；收入类 → income_drop
    s = p6.parse_scenario("某社区消费贷利率下调 1%")
    assert s["kind"] == "rate_change"
    assert s["pp"] == -1.0
    assert "下调" in s["label"]

    up = p6.parse_scenario("LPR 上调 0.5 个百分点")
    assert up["kind"] == "rate_change"
    assert up["pp"] == 0.5

    inc = p6.parse_scenario("深圳互联网行业平均收入下降 15%")
    assert inc == {
        "kind": "income_drop",
        "pct": 0.15,
        "direction": "down",
        "city": "sz",
        "district": None,
        "industry": "it_software",
        "label": "收入下降 15%",
        "label_key": "收入下降 {pct}%",
        "label_params": {"pct": "15"},
    }


def test_parse_unparseable_scenario_returns_none():
    # 失业率不是利率（不含「利率」关键字）；空文本无从解析 → 仅走叙事腿
    assert p6.parse_scenario("制造业失业率上升 3 个百分点") is None
    assert p6.parse_scenario("") is None
    assert p6.parse_scenario(None) is None


def test_parse_degenerate_scenarios_return_none():
    # 跌/降 100% 及以上没有推演意义
    assert p6.parse_scenario("房价下跌 100%") is None
    assert p6.parse_scenario("收入下降 150%") is None


def test_parse_price_up():
    # 上涨 10% → direction up、pct 存负值（对齐 ltv/(1-pct)，负 pct → LTV 下降）
    s = p6.parse_scenario("广州房价上涨 10%")
    assert s["kind"] == "price_up"
    assert abs(s["pct"] - (-0.10)) < 1e-9
    assert s["direction"] == "up"
    assert s["city"] == "gz"
    assert s["label"] == "房价上涨 10%"
    assert s["label_key"] == "房价上涨 {pct}%"
    assert p6.parse_scenario("楼价反弹 15%")["kind"] == "price_up"
    assert p6.parse_scenario("挂牌价回升 12%")["direction"] == "up"


def test_parse_income_up():
    s = p6.parse_scenario("互联网行业收入提升 20%")
    assert s["kind"] == "income_up"
    assert abs(s["pct"] - (-0.20)) < 1e-9
    assert s["direction"] == "up"
    assert s["industry"] == "it_software"
    assert s["label"] == "收入提升 20%"
    assert p6.parse_scenario("制造业收入增加 5%")["kind"] == "income_up"


def test_parse_degenerate_up_return_none():
    # 涨 100% 及以上没有推演意义（与下行同口径）
    assert p6.parse_scenario("房价上涨 100%") is None
    assert p6.parse_scenario("收入提升 150%") is None


# ---------------- _impact 带 shock ----------------

ZONE_ID = "price-gz-113.30-23.14"


def _snapshot():
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

    def loan(lid, ltv):
        return {
            "loan_id": lid,
            "collateral_id": lid,
            "balance": 1_000_000.0,
            "ltv": ltv,
            "risk_class": "关注",
            "low_confidence": 0,
            "is_high_risk_zone": 0,
            "lat": 23.14,
            "lng": 113.30,
            "addr": "广州市黄埔区某路1号",
            "city": "gz",
        }

    # 阈值 0.70、红线 0.85、冲击 −10%（LTV ÷0.9）：
    # L1 0.68→0.756  基线不命中 → 冲击后命中（新增受限）
    # L6 0.64→0.711  基线不命中 → 冲击后命中（新增受限）
    # L2 0.80→0.889  基线命中（新增受限）→ 冲击后越过红线（提级处置）
    # L3 0.90→1.000  基线命中（已超红线）→ 冲击后仍超红线
    # L4 None        缺失 LTV，任何口径都不命中
    loans = [loan(1, 0.68), loan(6, 0.64), loan(2, 0.80), loan(3, 0.90), loan(4, None)]
    return {"loans": loans, "zones": zones}


def _run(shock_pct=None):
    shock = {"kind": "price_drop", "pct": shock_pct, "label": "房价下跌 X%"} if shock_pct else None
    return p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=shock)


def test_impact_baseline_without_shock():
    out = _run()
    assert out["hit"]["loans"] == 2  # L2 + L3
    assert out["newly_hit"]["loans"] == 1  # L2（0.80 ≤ 红线 0.85）
    assert out["already_alert"]["loans"] == 1  # L3
    assert "shock" not in out


def test_impact_with_shock_recompute_and_comparison():
    out = _run(shock_pct=0.10)
    # 冲击后命中：L1 / L6 / L2 / L3（L4 缺 LTV 恒不命中）
    assert out["hit"]["loans"] == 4
    assert out["newly_hit"]["loans"] == 2  # L1 0.756 + L6 0.711（均 ≤ 0.85）
    assert out["already_alert"]["loans"] == 2  # L2 0.889 + L3 1.0
    # 对比块：冲击前 vs 冲击后
    s = out["shock"]
    assert s["baseline_hit"]["loans"] == 2
    assert s["baseline_newly_hit"]["loans"] == 1
    assert s["extra_hit_loans"] == 2  # L1 + L6 因冲击新落入命中
    assert s["extra_newly_hit_loans"] == 1  # 新增受限 2 − 基线 1（L2 冲击后翻为提级处置）
    # 传导分布：中位数位移 + 红线穿越对比 + 增量余额（过程链的原始数字）
    assert abs(s["ltv_median_before"] - 0.74) < 1e-6
    assert abs(s["ltv_median_after"] - (0.68 / 0.9 + 0.80 / 0.9) / 2) < 1e-6
    assert s["baseline_alert"]["loans"] == 1  # 仅 L3 原本已超红线
    assert abs(s["extra_newly_balance"] - 1_000_000.0) < 1e-6
    # 参数传导依据：分位数 + 冲击敏感带（可复核口径）
    assert abs(s["ltv_p90_before"] - 0.90) < 1e-6
    assert abs(s["ltv_p90_after"] - 1.0) < 1e-6
    assert s["flip_band"] == [0.63, 0.70]
    assert s["flip_count"] == 2  # 敏感带 (0.63, 0.70] 内恰 L1/L6，应等于 extra_hit_loans
    # 明细行展示的是冲击后 LTV：L1 = 0.68 / 0.9 ≈ 0.7556
    row1 = next(r for r in out["rows"] if r["loan_id"] == 1)
    assert abs(row1["ltv"] - 0.68 / 0.9) < 1e-3


def test_impact_up_adds_exited_red():
    # 上行冲击（房价上涨 10%，pct=-0.10）只降险：不新增命中/不提级，
    # 统计「脱险」与「风险敞口下降」。L3 0.90→0.90/1.10≈0.818≤红线0.85 → 脱险 1 笔。
    shock = {
        "kind": "price_up",
        "pct": -0.10,
        "direction": "up",
        "label": "房价上涨 10%",
        "city": None,
        "district": None,
        "industry": None,
    }
    out = p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=shock)
    assert out["newly_hit"]["loans"] == 0
    assert out["already_alert"]["loans"] == 0
    s = out["shock"]
    assert s["direction"] == "up"
    assert s["exited_red"]["loans"] == 1  # L3 原越红线、冲击后降回红线以下
    assert abs(s["exited_red"]["balance"] - 1_000_000.0) < 1e-6
    assert s["risk_reduced_balance"] >= 0
    assert s["extra_hit_loans"] == 0
    # LTV 有效值下降：中位数位移方向与下行相反（after < before）
    assert s["ltv_median_after"] < s["ltv_median_before"]


# ---------------- _process_chain（推演过程：定量传导链） ----------------


def test_process_chain_price_drop_has_real_numbers():
    shock = {"kind": "price_drop", "pct": 0.10, "label": "房价下跌 10%"}
    imp = p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=shock)
    steps = p6._process_chain(imp, shock, None)
    assert [s["step"] for s in steps] == ["情景解析", "传导测算（逐笔）", "规则评估"]
    # 每个数字都可从命中明细复算：作用域存量、中位数位移、阈值穿越、增量余额
    assert "5 笔存量" in steps[0]["detail"] and "÷(1−0.10)" in steps[0]["detail"]
    assert "×1.11" in steps[0]["basis"] and "+11.1%" in steps[0]["basis"]  # 换算依据
    assert "0.74" in steps[1]["detail"] and "2 → 4 笔" in steps[1]["detail"]
    assert "1 → 2 笔" in steps[1]["detail"]  # 红线 0.85 穿越
    assert "P90" in steps[1]["detail"]
    assert "+1 笔" in steps[2]["detail"] and "1,000,000" in steps[2]["detail"]
    assert "敏感带" in steps[2]["basis"] and "恰 2 笔" in steps[2]["basis"]  # 可复核口径


def test_process_chain_math_scenario_two_steps():
    mb = p6.scenario_math({"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"})
    imp = p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70)
    steps = p6._process_chain(imp, None, mb)
    assert [s["step"] for s in steps] == ["情景解析", "传导测算（示意）"]
    assert "不经过抵押物价值" in steps[0]["detail"]
    assert "17.6%" in steps[1]["detail"]
    assert "负担率" in steps[1]["basis"] and "1/(1−0.15)" in steps[1]["basis"]  # 公式依据


def test_process_chain_no_scenario_empty():
    imp = p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70)
    assert p6._process_chain(imp, None, None) == []


# ---------------- 城市核对：情景与作用域不同城 → 零影响 ----------------


def test_resolve_shock_same_city_applies():
    sc = {"kind": "price_drop", "pct": 0.10, "city": "gz", "label": "房价下跌 10%"}
    shock, note = p6._resolve_shock(sc, "zone", ZONE_ID, _snapshot()["zones"])  # 网格属 gz
    assert shock is sc and note is None
    shock, note = p6._resolve_shock(sc, "city", "gz", {})
    assert shock is sc and note is None


def test_resolve_shock_city_mismatch_zero_impact():
    sc = {"kind": "price_drop", "pct": 0.10, "city": "gz", "label": "房价下跌 10%"}
    # 广州房价下跌 × 深圳城市作用域 / 深圳网格 → 必须零影响并给出核对说明
    shock, note = p6._resolve_shock(sc, "city", "sz", {})
    assert shock is None
    assert "广州" in note and "无影响" in note


def test_resolve_shock_global_and_unknown_city_apply():
    sc_none = {"kind": "price_drop", "pct": 0.10, "city": None, "label": "房价下跌 10%"}
    shock, note = p6._resolve_shock(sc_none, "city", "sz", {})
    assert shock is sc_none and note is None  # 全国/未指明 → 任意作用域
    sc_sz = {"kind": "price_drop", "pct": 0.10, "city": "sz", "label": "x"}
    # 网格画像缺城市信息 → 无法判定不符，不误杀（与未指明城市同一处理）
    zone_no_city = {**_snapshot()["zones"][ZONE_ID], "city": None}
    shock, note = p6._resolve_shock(sc_sz, "zone", ZONE_ID, zone_no_city)
    assert shock is sc_sz and note is None


def test_process_chain_mismatch_single_step():
    imp = p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70)
    steps = p6._process_chain(imp, None, None, mismatch="情景限定广州…无影响")
    assert [s["step"] for s in steps] == ["情景核对"]
    assert "无影响" in steps[0]["detail"]


# ---------------- scenario_math（利率/收入类传导测算） ----------------


def test_scenario_math_rate_down_shows_payment_delta():
    m = p6.scenario_math({"kind": "rate_change", "pp": -1.0, "label": "贷款利率下调 1 个百分点"})
    assert "月供参考" in m["lines"][0]
    assert "→" in m["lines"][0]
    assert "-" in m["lines"][0]  # 下调 → 月供下降（带符号增量）
    assert "4.2%" in m["note"]  # 示意口径必须标注基准利率


def test_scenario_math_income_drop_inflates_burden():
    m = p6.scenario_math({"kind": "income_drop", "pct": 0.15, "label": "收入下降 15%"})
    # 1/(1−0.15) − 1 ≈ 17.6%
    assert "17.6%" in m["lines"][0]
    assert "负担率" in m["lines"][0]


def test_scenario_math_clamps_extreme_rate_cut():
    # 降 10pp 会把名义利率打成负数：钳到正利率下限，不给荒诞月供
    m = p6.scenario_math({"kind": "rate_change", "pp": -10.0, "label": "x"})
    assert m is not None and m["lines"]


def test_scenario_math_ignores_price_shock_and_none():
    # price_drop 走 _impact 逐笔重算，不该进传导测算；None 同样
    assert p6.scenario_math({"kind": "price_drop", "pct": 0.1, "label": "x"}) is None
    assert p6.scenario_math(None) is None


# ---------------- 模拟对象选取：叙事必须落在真实贷款上 ----------------


def test_pick_profile_prefers_shocked_highest_ltv_hit():
    snapshot = _snapshot()
    shock = {"kind": "price_drop", "pct": 0.10, "label": "x", "city": None}
    p = p6.pick_profile_loan(snapshot, "zone", ZONE_ID, 0.70, shock)
    # 冲击后命中笔里 LTV 最高的是 L3（0.90 → 1.0）
    assert p["loan_id"] == 3
    assert p["is_hit"] is True
    assert p["city"] == "gz"
    assert p["ltv"] == 0.90


def test_pick_profile_no_hit_falls_back_to_scope():
    snapshot = _snapshot()
    # 阈值 1.5 → 无命中（LTV 全部低于），退化为作用域内 LTV 最高的 L3
    p = p6.pick_profile_loan(snapshot, "zone", ZONE_ID, 1.5, None)
    assert p["loan_id"] == 3 and p["is_hit"] is False


def test_pick_profile_out_of_scope_empty():
    snapshot = _snapshot()
    zones = dict(snapshot["zones"])
    zones["price-sz-114.00-22.50"] = {
        **zones[ZONE_ID],
        "zone_id": "price-sz-114.00-22.50",
        "city": "sz",
        "center_lat": 22.50,
        "center_lng": 114.00,
    }
    assert p6.pick_profile_loan(snapshot, "zone", "price-sz-114.00-22.50", 0.70, None) is None


# ---------------- _sensitivity 上行（脱险）阶梯 ----------------


def test_sensitivity_up_ladder():
    shock = {
        "kind": "price_up",
        "pct": -0.10,
        "direction": "up",
        "label": "房价上涨 10%",
        "city": None,
        "district": None,
        "industry": None,
    }
    rows = p6._sensitivity(_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock)
    assert rows and len(rows) == len(p6.SENSITIVITY_UP_PCTS)
    for r in rows:
        assert "房价上涨" in r["label"]
        assert r["newly_hit_loans"] == 0  # 上行不新增命中
        assert r["risk_reduced_balance"] >= 0
        assert "exited_red_loans" in r
    # 档位单调：涨幅越大，脱险笔数不降、敞口下降量不降（L3 0.90 在 10% 涨幅处脱险）
    exits = [r["exited_red_loans"] for r in rows]
    rrb = [r["risk_reduced_balance"] for r in rows]
    assert exits == sorted(exits) and rrb == sorted(rrb)
    assert rows[-1]["exited_red_loans"] >= 1
