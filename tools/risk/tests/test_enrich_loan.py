"""风险引擎单笔打宽（risk_engine.enrich_loan）：估值回退链 → LTV → 五级 → 预警。

覆盖的业务规则：
- 估值三级回退：AVM(S2) → DWD 行情 → 业务库 true_market_price，且命中标记互斥。
- LTV = 余额 / 估值，估值为 0 或缺失时不得抛异常。
- AC-03 预警：LTV **严格大于**红线 0.85 且非低置信才报警。
- AC-04 低置信：空间特征缺失率 >= 75%（0–100 标度）→ 抑制自动预警。0–1 小数的归一
  在 store.load_collaterals() 入口完成（SPF-AC04 修复），引擎只认 0–100。
- 高危区叠加：正常/关注档处高危区至少升到「关注」。
- R-UNW-03 异常估值：仅 AVM 命中时算偏差，严格大于 30% 才标异常。
- R-UBQ-01 血缘：每行都带 model_version，取不到版本号即 'unknown'。
"""

import pytest
from riskmods import config, risk_engine

CITY_MAP = config.CITY_MAP


def enrich(loan, collateral, *, dwd=None, model=None):
    """薄封装：固定 customer=None（当前风险口径不使用客户特征）。"""
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


# ================================================================ AC-03 红线预警


def test_ltv_exactly_at_red_line_does_not_alert(loan, collateral, patch_avm):
    """红线语义是「严格大于」：LTV == 0.85 压线不报警，分类停在「次级」。"""
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 8_500_000.0  # LTV = 0.8500

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.85
    assert row["alert"] is False
    assert row["risk_class"] == "次级"


def test_ltv_just_over_red_line_alerts_and_downgrades_to_doubtful(loan, collateral, patch_avm):
    patch_avm(None)
    collateral["true_market_price"] = 10_000_000.0
    loan["balance"] = 8_501_000.0  # LTV = 0.8501

    row = enrich(loan, collateral, dwd={})

    assert row["ltv"] == 0.8501
    assert row["alert"] is True
    assert row["risk_class"] == "可疑"


def test_red_line_threshold_is_config_driven(loan, collateral, patch_avm, monkeypatch):
    """阈值走 config.LTV_RED_LINE，不是散落的魔法数字——调低红线应立刻多出预警。"""
    patch_avm(None)
    loan["balance"] = 5_000_000.0  # LTV = 0.5
    assert enrich(loan, collateral, dwd={})["alert"] is False

    monkeypatch.setattr(config, "LTV_RED_LINE", 0.4)
    assert enrich(loan, collateral, dwd={})["alert"] is True


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


def test_missing_pct_at_threshold_marks_low_confidence_and_suppresses_alert(
    loan, collateral, patch_avm
):
    """缺失率 >= 75% 时空间特征几乎不可用，估值不可信，宁可不报也不能误报给贷后。"""
    patch_avm(None)
    loan["balance"] = 9_500_000.0  # LTV 0.95，远超红线
    collateral["spatial_feat_missing_pct"] = 75.0

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is True
    assert row["ltv"] == 0.95
    assert row["alert"] is False  # 被抑制
    assert row["risk_class"] == "可疑"  # 但分类照常降级，敞口不被隐藏


def test_missing_pct_just_below_threshold_still_alerts(loan, collateral, patch_avm):
    """边界是 `>=`：74.9% 不算低置信。"""
    patch_avm(None)
    loan["balance"] = 9_500_000.0
    collateral["spatial_feat_missing_pct"] = 74.9

    row = enrich(loan, collateral, dwd={})

    assert row["low_confidence"] is False
    assert row["alert"] is True


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
