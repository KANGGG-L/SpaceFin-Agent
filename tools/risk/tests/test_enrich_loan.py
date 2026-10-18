"""风险引擎单笔打宽（risk_engine.enrich_loan）：估值回退链 → LTV → 五级 → 预警。

覆盖的业务规则：
- 估值三级回退：AVM(S2) → DWD 行情 → 业务库 true_market_price，且命中标记互斥。
- LTV = 余额 / 估值，估值为 0 或缺失时不得抛异常。
- AC-03 预警：LTV 两档——严格大于警示线 0.75 → 'warn'；严格大于强预警线 0.85 → 'strong'；
  恰好 = 阈值不触发；strong 覆盖 warn；非低置信才报。
- AC-04 低置信：空间特征缺失率 **严格大于** 75（0–100 标度，恰好 = 75 不低置信）→ 抑制
  自动预警。0–1 小数的归一在 store.load_collaterals() 入口完成（SPF-AC04 修复），引擎只认 0–100。
- 高危区叠加：正常/关注档处高危区至少升到「关注」。
- R-UNW-03 异常估值：仅 AVM 命中时算偏差，严格大于 30% 才标异常。
- R-UBQ-01 血缘：每行都带 model_version，取不到版本号即 'unknown'。
"""

import pytest
from riskmods import config, risk_engine

CITY_MAP = config.CITY_MAP


def enrich(loan, collateral, *, dwd=None, model=None):
    """薄封装：固定 customer=None（行业规则等客户特征路径有独立用例覆盖）。"""
    return risk_engine.enrich_loan(loan, collateral, None, dwd or {}, CITY_MAP, avm_model=model)


# ================================================================ 估值回退链


def test_avm_hit_uses_avm_valuation_and_leaves_dwd_unflagged(loan, collateral, patch_avm):
    """回退链第一级命中就该短路。

    dwd_hit 必须为 False：main.py 用 `len(rows)-avm_hits-dwd_hits` 算兜底笔数，
    两个标记同时为真会让 fallback 统计变负数（risk_engine 里专门有注释防这个回归）。
    """
    patch_avm(8_000_000.0)
    dwd = {("gz", "广州市天河区"): 90_000.0}

    row = enrich(loan, collateral, dwd=dwd)

    assert row["market_valuation"] == 8_000_000.0
    assert row["avm_hit"] is True
    assert row["dwd_hit"] is False


def test_falls_back_to_dwd_market_price_when_avm_misses(loan, collateral, patch_avm):
    """第二级：DWD 单价 × 面积。命中键是 (城市码, 区域)。"""
    patch_avm(None)
    dwd = {("gz", "天河"): 90_000.0}

    row = enrich(loan, collateral, dwd=dwd)

    assert row["market_valuation"] == pytest.approx(9_000_000.0)  # 90000 × 100㎡
    assert row["avm_hit"] is False
    assert row["dwd_hit"] is True


def test_falls_back_to_true_market_price_when_avm_and_dwd_miss(loan, collateral, patch_avm):
    """第三级兜底：true_market_price，两个命中标记都为假。"""
    patch_avm(None)

    row = enrich(loan, collateral, dwd={})

    assert row["market_valuation"] == 10_000_000.0
    assert row["avm_hit"] is False
    assert row["dwd_hit"] is False


def test_all_valuation_tiers_missing_yields_zero_valuation_and_none_ltv(
    loan, collateral, patch_avm
):
    """兜底价也没有 → 估值 0.0。此时 LTV 无意义，必须是 None 而不是 inf/异常。"""
    patch_avm(None)
    collateral["true_market_price"] = None

    row = enrich(loan, collateral, dwd={})

    assert row["market_valuation"] == 0.0
    assert row["ltv"] is None
    assert row["risk_class"] == "次级"  # classify(None) 的保守回退


def test_zero_valuation_does_not_raise_division_error(loan, collateral, patch_avm):
    """分母为 0 是真实脏数据场景（抵押物被注销/估值写 0），必须静默降级。"""
    patch_avm(None)
    collateral["true_market_price"] = 0.0

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] is None
    assert row["alert"] is False  # LTV 算不出来就不能报 LTV 预警


# ================================================================ LTV 计算


def test_ltv_is_balance_over_valuation_rounded_to_four_decimals(loan, collateral, patch_avm):
    patch_avm(None)
    loan["balance"] = 3_333_333.0
    collateral["true_market_price"] = 10_000_000.0

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.3333


def test_missing_balance_treated_as_zero_exposure(loan, collateral, patch_avm):
    """balance 为 None（台账未回填）→ 视作 0 敞口，而不是 None 导致 TypeError。"""
    patch_avm(None)
    loan["balance"] = None

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.0
    assert row["risk_class"] == "正常"


# ================================================================ AC-03 两档预警
#
# 两档口径：LTV 严格大于警示线 0.75 → 'warn'；严格大于强预警线 0.85 → 'strong'
# （strong 覆盖 warn）；恰好 = 阈值不触发。alert 布尔 = alert_level is not None，兼容下游。


def test_ltv_exactly_at_warn_line_does_not_trigger(loan, collateral, patch_avm):
    """警示线语义「严格大于」：LTV == 0.75 压线不报警，alert_level 为 None。"""
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 7_500_000.0  # LTV = 0.7500

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.75
    assert row["alert"] is False
    assert row["alert_level"] is None


def test_ltv_between_warn_and_red_raises_warn(loan, collateral, patch_avm):
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 8_000_000.0  # LTV = 0.80，> 警示线且 <= 强预警线

    row = enrich(loan, collateral, dwd={})

    assert row["alert"] is True
    assert row["alert_level"] == "warn"


def test_ltv_at_red_line_is_warn_not_strong(loan, collateral, patch_avm):
    """强预警线也是「严格大于」：LTV == 0.85 不触发 strong，但仍 > 警示线 → warn。"""
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 8_500_000.0  # LTV = 0.8500

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.85
    assert row["alert"] is True
    assert row["alert_level"] == "warn"  # 不是 strong
    assert row["risk_class"] == "次级"


def test_ltv_just_over_red_line_raises_strong(loan, collateral, patch_avm):
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 8_501_000.0  # LTV = 0.8501

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.8501
    assert row["alert"] is True
    assert row["alert_level"] == "strong"
    assert row["risk_class"] == "可疑"


def test_ltv_over_red_line_raises_strong(loan, collateral, patch_avm):
    """LTV=0.90 远超强预警线 → strong（strong 覆盖 warn）。"""
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 9_000_000.0  # LTV = 0.90

    row = enrich(loan, collateral, dwd={})

    assert row["alert"] is True
    assert row["alert_level"] == "strong"


def test_warn_line_threshold_is_config_driven(loan, collateral, patch_avm, monkeypatch):
    """阈值走 config.LTV_WARN_LINE：调低警示线应立刻多出 warn 预警。"""
    patch_avm(None)
    loan["balance"] = 5_000_000.0  # LTV = 0.5
    assert enrich(loan, collateral, dwd={})["alert_level"] is None

    monkeypatch.setattr(config, "LTV_WARN_LINE", 0.4)
    assert enrich(loan, collateral, dwd={})["alert_level"] == "warn"


def test_red_line_threshold_is_config_driven(loan, collateral, patch_avm, monkeypatch):
    """阈值走 config.LTV_RED_LINE，不是散落的魔法数字——调低强预警线应立刻多出 strong 预警。"""
    patch_avm(None)
    loan["balance"] = 5_000_000.0  # LTV = 0.5
    assert enrich(loan, collateral, dwd={})["alert_level"] is None

    monkeypatch.setattr(config, "LTV_RED_LINE", 0.4)
    assert enrich(loan, collateral, dwd={})["alert_level"] == "strong"


# ================================================================ AC-04 低置信抑制
#
# 标度契约：enrich_loan() 读到的 collateral["spatial_feat_missing_pct"] 必须是
# **0–100 的百分数**，因为它要和 config.LOW_CONF_MISSING_PCT（默认 75.0）直接比较。
# 0–1 小数的归一发生在 store.load_collaterals()（数据入口唯一处，见 SPF-AC04 修复说明）。
# 阈值 75 与 tools/spatial/main.py 的 missing_ge75「严重缺失」口径一致。


def test_percent_scale_missing_pct_above_threshold_marks_low_confidence(
    loan, collateral, patch_avm
):
    """标度契约的正向锚点：80.0 读作「缺失 80%」，超过 75.0 阈值。"""
    patch_avm(None)
    collateral["spatial_feat_missing_pct"] = 80.0

    assert enrich(loan, collateral, dwd={})["low_confidence"] is True


def test_missing_pct_exactly_at_threshold_is_not_low_confidence(loan, collateral, patch_avm):
    """低置信语义是「严格大于」：缺失率恰好 = 75 不标记，自动预警不被抑制。"""
    patch_avm(None)
    loan["balance"] = 9_500_000.0  # LTV 0.95，远超强预警线
    collateral["spatial_feat_missing_pct"] = 75.0

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is False  # 恰好 = 75 不低置信
    assert row["ltv"] == 0.95
    assert row["alert"] is True  # 未被抑制
    assert row["alert_level"] == "strong"


def test_missing_pct_just_over_threshold_marks_low_confidence_and_suppresses_alert(
    loan, collateral, patch_avm
):
    """缺失率 75.1 **严格大于** 75 → 低置信，抑制自动预警转人工核查。"""
    patch_avm(None)
    loan["balance"] = 9_500_000.0  # LTV 0.95，远超强预警线
    collateral["spatial_feat_missing_pct"] = 75.1

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is True
    assert row["ltv"] == 0.95
    assert row["alert"] is False  # 被抑制
    assert row["alert_level"] is None
    assert row["risk_class"] == "可疑"  # 但分类照常降级，敞口不被隐藏


def test_missing_pct_just_below_threshold_still_alerts(loan, collateral, patch_avm):
    """边界是 `>`：74.9% 不算低置信。"""
    patch_avm(None)
    loan["balance"] = 9_500_000.0
    collateral["spatial_feat_missing_pct"] = 74.9

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is False
    assert row["alert"] is True
    assert row["alert_level"] == "strong"


def test_none_missing_pct_treated_as_zero(loan, collateral, patch_avm):
    patch_avm(None)
    collateral["spatial_feat_missing_pct"] = None

    assert enrich(loan, collateral, dwd={})["low_confidence"] is False


def test_fraction_scale_input_is_below_threshold_by_engine_contract(loan, collateral, patch_avm):
    """引擎契约：入参必须是 0–100 百分数。

    0.30（0–1 小数标度）在引擎里读作「缺失 0.3%」，低于 75 阈值 → 不标低置信。
    这是**修复后的正确行为**：0–1 小数的归一化在 store.load_collaterals() 入口完成
    （SPF-AC04 修复，见 store.py 该函数 docstring），引擎本身只认 0–100。
    若直接从引擎绕过 store 传 0.30，说明调用方漏了归一化，这个用例会帮忙兜住。
    """
    patch_avm(None)
    loan["balance"] = 9_500_000.0
    collateral["spatial_feat_missing_pct"] = 0.30

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is False
    assert row["alert"] is True


# ================================================================ 无抵押物


def test_missing_collateral_is_classified_as_loss_without_alert(loan):
    """抵押物缺失 = 风险敞口不可计量，按最保守的「损失」处理。

    同时 alert 必须为 False：没有 LTV 就没有 LTV 预警，该场景走的是数据质量问题
    而不是贷后保全预警。
    """
    row = risk_engine.enrich_loan(loan, None, None, {}, CITY_MAP, avm_model=None)

    assert row["risk_class"] == "损失"
    assert row["market_valuation"] is None
    assert row["ltv"] is None
    assert row["low_confidence"] is True
    assert row["alert"] is False
    assert row["alert_level"] is None
    assert row["avm_hit"] is False and row["dwd_hit"] is False


def test_missing_collateral_still_carries_model_version(loan):
    """R-UBQ-01：没有估值也要带模型版本，否则 DWS 里这批行血缘断链。"""
    row = risk_engine.enrich_loan(loan, None, None, {}, CITY_MAP, avm_model={"version": "s2-r4"})

    assert row["model_version"] == "s2-r4"
    assert row["valuation_deviation_pct"] is None
    assert row["abnormal_valuation"] is False


def test_missing_collateral_preserves_original_loan_fields(loan):
    """打宽是 `{**loan, ...}`，原台账字段不能被吃掉。"""
    row = risk_engine.enrich_loan(loan, None, None, {}, CITY_MAP)

    assert row["loan_id"] == 1001
    assert row["customer_id"] == 9001
    assert row["balance"] == 5_000_000.0


# ================================================================ 高危区叠加


@pytest.mark.parametrize(("balance", "base_class"), [(3_000_000.0, "正常"), (7_000_000.0, "关注")])
def test_high_risk_zone_lifts_normal_and_attention_to_attention(
    loan, collateral, patch_avm, balance, base_class
):
    patch_avm(None)
    loan["balance"] = balance
    collateral["is_high_risk_zone"] = 0
    assert enrich(loan, collateral, dwd={})["risk_class"] == base_class

    collateral["is_high_risk_zone"] = 1
    assert enrich(loan, collateral, dwd={})["risk_class"] == "关注"


@pytest.mark.parametrize(("balance", "expected"), [(8_000_000.0, "次级"), (9_500_000.0, "可疑")])
def test_high_risk_zone_never_upgrades_worse_classes(
    loan, collateral, patch_avm, balance, expected
):
    """叠加规则只上调不下调——否则可疑贷款会被「洗」成关注。"""
    patch_avm(None)
    loan["balance"] = balance
    collateral["is_high_risk_zone"] = 1

    assert enrich(loan, collateral, dwd={})["risk_class"] == expected


def test_high_risk_zone_flag_passes_through(loan, collateral, patch_avm):
    patch_avm(None)
    collateral["is_high_risk_zone"] = 1

    assert enrich(loan, collateral, dwd={})["is_high_risk_zone"] == 1


# ================================================================ P6 空间惩罚项（A1：城市粒度）
#
# 城市码由 collateral.property_addr 解析：默认 fixture 地址「广州市天河区…」→ 城市码 gz。
# 与 p6_policy._impact 口径一致：A1 规则不带 ltv_cap，整城市有 LTV 的贷款全部命中并提级。


def test_city_strict_policy_bumps_one_tier(loan, collateral):
    r = risk_engine.enrich_loan(
        loan,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("city", "gz"): {"action": "strict"}},
    )
    assert r["risk_class"] == "关注"  # 正常 -> 关注


def test_city_strict_policy_bumps_further_when_already_关注(loan, collateral):
    loan2 = dict(loan, balance=8_000_000.0)  # LTV 0.8 -> 次级
    r = risk_engine.enrich_loan(
        loan2,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("city", "gz"): {"action": "strict"}},
    )
    assert r["risk_class"] == "可疑"  # 次级 -> 可疑


def test_city_notice_policy_floors_at_关注(loan, collateral):
    r = risk_engine.enrich_loan(
        loan,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("city", "gz"): {"action": "notice"}},
    )
    assert r["risk_class"] == "关注"  # 正常 -> 关注（最轻）


def test_city_notice_policy_does_not_lower_existing_次级(loan, collateral):
    loan2 = dict(loan, balance=9_000_000.0)  # LTV 0.9 -> 可疑
    r = risk_engine.enrich_loan(
        loan2,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("city", "gz"): {"action": "notice"}},
    )
    assert r["risk_class"] == "可疑"  # 不被 notice 下压


def test_other_city_policy_does_not_affect_loan(loan, collateral):
    r = risk_engine.enrich_loan(
        loan,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("city", "sh"): {"action": "strict"}},
    )
    assert r["risk_class"] == "正常"  # 非本城规则不生效


def test_zone_policy_ignored_when_center_missing(loan, collateral):
    # A2：zone 规则 center=None -> 无坐标可判，跳过（与 A1 行为一致：不动分类）
    r = risk_engine.enrich_loan(
        loan,
        collateral,
        None,
        {},
        config.CITY_MAP,
        policy_map={("zone", "z1"): {"action": "strict", "ltv_cap": None, "center": None}},
    )
    assert r["risk_class"] == "正常"
    assert r["is_rejected"] is False


def test_no_policy_map_leaves_class_unchanged(loan, collateral):
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP)
    assert r["risk_class"] == "正常"


# ================================================================ P6 空间惩罚项（A2：zone + ltv_cap + reject）
#
# 抵押物 fixture 默认 lat=23.13 lng=113.32，地址「广州市…」→ 城市码 gz，LTV 0.5（正常档）。


def test_zone_rule_applies_within_5km(loan, collateral):
    # 同坐标 zone 中心 -> haversine=0 <= 5km 命中
    pm = {("zone", "z9"): {"action": "strict", "ltv_cap": None, "center": (23.13, 113.32)}}
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)
    assert r["risk_class"] == "关注"  # 正常 -> 关注


def test_zone_rule_ignored_beyond_5km(loan, collateral):
    pm = {
        ("zone", "z9"): {"action": "strict", "ltv_cap": None, "center": (40.0, 116.0)}
    }  # 北京，远超 5km
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)
    assert r["risk_class"] == "正常"


def test_ltv_cap_only_hits_over_cap(loan, collateral):
    # 默认 LTV 0.5；cap=0.6 时不命中，cap=0.4 时命中
    low = {("city", "gz"): {"action": "strict", "ltv_cap": 0.6, "center": None}}
    high = {("city", "gz"): {"action": "strict", "ltv_cap": 0.4, "center": None}}
    assert (
        risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=low)[
            "risk_class"
        ]
        == "正常"
    )
    assert (
        risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=high)[
            "risk_class"
        ]
        == "关注"
    )


def test_reject_sets_flag_not_class(loan, collateral):
    pm = {("city", "gz"): {"action": "reject", "ltv_cap": 0.4, "center": None}}  # LTV0.5>0.4 命中
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)
    assert r["is_rejected"] is True
    assert r["risk_class"] == "正常"  # 拒贷不改分类


def test_reject_not_set_when_below_cap(loan, collateral):
    pm = {
        ("city", "gz"): {"action": "reject", "ltv_cap": 0.6, "center": None}
    }  # LTV0.5<=0.6 不命中
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)
    assert r["is_rejected"] is False


def test_zone_and_city_rules_can_stack(loan, collateral):
    pm = {
        ("city", "gz"): {"action": "notice", "ltv_cap": None, "center": None},
        ("zone", "z9"): {"action": "strict", "ltv_cap": None, "center": (23.13, 113.32)},
    }
    r = risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)
    assert r["risk_class"] == "次级"  # 正常->关注(notice) 再 ->次级(strict)


# ================================================================ P6 空间惩罚项（行业粒度，US-01）
#
# 行业规则键 ("industry", 行业码)，贷款经 customer["industry"] 命中；
# 默认 LTV 0.5（正常档），与 city/zone 规则同一套 _apply_policy_rule 语义。


def test_industry_rule_promotes_matching_customer(loan, collateral):
    pm = {("industry", "manufacturing"): {"action": "strict", "ltv_cap": None, "center": None}}
    r = risk_engine.enrich_loan(
        loan, collateral, {"industry": "manufacturing"}, {}, config.CITY_MAP, policy_map=pm
    )
    assert r["risk_class"] == "关注"  # 正常 -> 关注


def test_industry_rule_skips_other_industry(loan, collateral):
    pm = {("industry", "manufacturing"): {"action": "strict", "ltv_cap": None, "center": None}}
    r = risk_engine.enrich_loan(
        loan, collateral, {"industry": "education"}, {}, config.CITY_MAP, policy_map=pm
    )
    assert r["risk_class"] == "正常"  # 非目标行业不受影响


def test_industry_rule_skips_when_customer_missing(loan, collateral):
    # 无客户档（CDC 孤儿贷款）或行业为空 → 不命中任何行业规则
    pm = {("industry", "manufacturing"): {"action": "strict", "ltv_cap": None, "center": None}}
    assert (
        risk_engine.enrich_loan(loan, collateral, None, {}, config.CITY_MAP, policy_map=pm)[
            "risk_class"
        ]
        == "正常"
    )
    assert (
        risk_engine.enrich_loan(
            loan, collateral, {"industry": None}, {}, config.CITY_MAP, policy_map=pm
        )["risk_class"]
        == "正常"
    )


def test_industry_rule_respects_ltv_cap(loan, collateral):
    # 默认 LTV 0.5：cap 0.6 不命中，cap 0.4 命中（严格大于才命中）
    low = {("industry", "manufacturing"): {"action": "strict", "ltv_cap": 0.6, "center": None}}
    high = {("industry", "manufacturing"): {"action": "strict", "ltv_cap": 0.4, "center": None}}
    cust = {"industry": "manufacturing"}
    assert (
        risk_engine.enrich_loan(loan, collateral, cust, {}, config.CITY_MAP, policy_map=low)[
            "risk_class"
        ]
        == "正常"
    )
    assert (
        risk_engine.enrich_loan(loan, collateral, cust, {}, config.CITY_MAP, policy_map=high)[
            "risk_class"
        ]
        == "关注"
    )


def test_industry_reject_sets_flag_not_class(loan, collateral):
    pm = {("industry", "manufacturing"): {"action": "reject", "ltv_cap": 0.4, "center": None}}
    r = risk_engine.enrich_loan(
        loan, collateral, {"industry": "manufacturing"}, {}, config.CITY_MAP, policy_map=pm
    )
    assert r["is_rejected"] is True
    assert r["risk_class"] == "正常"  # 拒贷不改分类，与 city/zone 口径一致


def test_city_and_industry_rules_can_stack(loan, collateral):
    pm = {
        ("city", "gz"): {"action": "notice", "ltv_cap": None, "center": None},
        ("industry", "manufacturing"): {"action": "strict", "ltv_cap": None, "center": None},
    }
    r = risk_engine.enrich_loan(
        loan, collateral, {"industry": "manufacturing"}, {}, config.CITY_MAP, policy_map=pm
    )
    assert r["risk_class"] == "次级"  # 正常->关注(notice) 再 ->次级(strict)


# ================================================================ R-UNW-03 异常估值


def test_avm_deviation_over_threshold_marks_abnormal_valuation(loan, collateral, patch_avm):
    """基准是业务库 true_market_price；偏差 = |AVM - 基准| / 基准。"""
    patch_avm(6_000_000.0)  # 基准 1000 万 → 偏差 40%

    row = enrich(loan, collateral, dwd={})

    assert row["valuation_deviation_pct"] == 0.4
    assert row["abnormal_valuation"] is True


def test_deviation_exactly_at_threshold_is_not_abnormal(loan, collateral, patch_avm):
    """B-04 边界：PRD 约定「>30% 才标」，恰好 30% 放行。"""
    patch_avm(7_000_000.0)  # 偏差正好 0.30

    row = enrich(loan, collateral, dwd={})

    assert row["valuation_deviation_pct"] == 0.3
    assert row["abnormal_valuation"] is False


def test_deviation_uses_absolute_value_for_over_and_under_valuation(loan, collateral, patch_avm):
    """高估 40% 与低估 40% 都要拦——单边判断会漏掉一半异常。"""
    patch_avm(14_000_000.0)

    row = enrich(loan, collateral, dwd={})

    assert row["valuation_deviation_pct"] == 0.4
    assert row["abnormal_valuation"] is True


def test_deviation_is_none_when_avm_misses(loan, collateral, patch_avm):
    """回退链的值与基准同源，算出来偏差恒为 0，会把异常噪声化。

    故未命中 AVM 时 deviation 必须是 None（不可判），不能是 0.0（判过且正常）。
    """
    patch_avm(None)

    row = enrich(loan, collateral, dwd={})

    assert row["valuation_deviation_pct"] is None
    assert row["abnormal_valuation"] is False


def test_deviation_is_none_when_baseline_price_missing(loan, collateral, patch_avm):
    """true_market_price 为 0/None → 除零风险，偏差置 None 而不是抛异常。"""
    patch_avm(8_000_000.0)
    collateral["true_market_price"] = 0.0

    row = enrich(loan, collateral, dwd={})

    assert row["valuation_deviation_pct"] is None
    assert row["abnormal_valuation"] is False


def test_deviation_threshold_is_config_driven(loan, collateral, patch_avm, monkeypatch):
    patch_avm(8_500_000.0)  # 偏差 15%
    assert enrich(loan, collateral, dwd={})["abnormal_valuation"] is False

    monkeypatch.setattr(config, "VALUATION_DEVIATION_THRESHOLD", 0.10)
    assert enrich(loan, collateral, dwd={})["abnormal_valuation"] is True


# ================================================================ R-UBQ-01 血缘


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ({"version": "s2-r4"}, "s2-r4"),
        ({"version": ""}, "unknown"),  # 空版本号视同缺失
        ({"model": "x"}, "unknown"),  # 产物里没有 version 键
        (None, "unknown"),  # 模型未加载
    ],
)
def test_every_row_carries_model_version_defaulting_to_unknown(
    loan, collateral, patch_avm, model, expected
):
    patch_avm(None)

    assert enrich(loan, collateral, dwd={}, model=model)["model_version"] == expected
