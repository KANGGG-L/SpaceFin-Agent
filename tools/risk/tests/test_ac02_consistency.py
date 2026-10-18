"""AC-02 回归钉子：日终五级分类与「手工抽样」一致率 ≥ 99.9%（R-STA-01）。

用固化标定样本（不随机、可复现）批量跑 enrich_loan，把每行引擎输出的 risk_class
与一份**独立**的 PRD 阈值参考实现逐笔比对，统计一致率。

参考实现硬编码 PRD 定义的五级 LTV 上界（正常 0.60 / 关注 0.75 / 次级 0.85 / 可疑
1.00，超 1.00 为损失）与高危区叠加规则（正常/关注处高危区至少升「关注」），不调用
config.classify——这样一旦 config 阈值或 risk_engine 的分类/叠加逻辑相对 PRD 漂移，
一致率会跌破 99.9% 而告警，而非与自身比对永远 100%。

标定样本刻意覆盖全部边界点（0.60/0.75/0.85/1.00 及其 ±1e-9）与高低危区组合，
确保「恰好命中阈值」「高危区升档」这些易回归点被钉死。
"""

from riskmods import config, risk_engine

CITY_MAP = config.CITY_MAP

# PRD 五级 LTV 上界（与 config.CLASS_LTV_UPPER 默认值同源，但此处独立硬编码，
# 作为「手工抽样」口径的参照基准）。
_PRD_NORMAL = 0.60
_PRD_ATTN = 0.75
_PRD_SUBSTD = 0.85
_PRD_DOUBT = 1.00


def _ref_risk_class(ltv, is_high_risk_zone):
    """独立参考实现：PRD 五级口径，不依赖 config.classify。"""
    if ltv is None or ltv < 0:
        cls = "次级"
    elif ltv <= _PRD_NORMAL:
        cls = "正常"
    elif ltv <= _PRD_ATTN:
        cls = "关注"
    elif ltv <= _PRD_SUBSTD:
        cls = "次级"
    elif ltv <= _PRD_DOUBT:
        cls = "可疑"
    else:
        cls = "损失"
    if is_high_risk_zone and cls in ("正常", "关注"):
        cls = "关注"
    return cls


def _enrich(ltv, is_high_risk_zone):
    """构造一笔 LTV 确定的贷款 + 抵押物，走 enrich_loan。

    抵押物不带城市码/面积，使 AVM/DWD 双双未命中，估值回退 true_market_price，
    LTV = balance / true_market_price 完全由入参决定，避免外部估值口径干扰分类比对。
    price=1.0、balance=ltv 使引擎内部 LTV = round(ltv, 4)，与参考口径同精度。
    """
    price = 1.0
    loan = {"loan_id": 1, "balance": ltv, "customer_id": "C1"}
    collateral = {
        "true_market_price": price,
        "is_high_risk_zone": is_high_risk_zone,
    }
    row = risk_engine.enrich_loan(loan, collateral, None, {}, CITY_MAP, avm_model=None)
    return row["risk_class"]


def _calibration_samples():
    """固化标定样本：边界点 + 边界紧邻（4 位小数可分辨）+ 密集扫描 + 高低危区组合。"""
    ltv_targets = []
    # 边界及其紧邻点（4 位小数可分辨：恰好阈值 / 阈值 -0.0001 / 阈值 +0.0001）
    for b in (_PRD_NORMAL, _PRD_ATTN, _PRD_SUBSTD, _PRD_DOUBT):
        ltv_targets += [b, round(b - 0.0001, 4), round(b + 0.0001, 4)]
    # 0 到 1.5 密集扫描（步长 0.01）
    ltv_targets += [round(x * 0.01, 2) for x in range(0, 151)]
    # 远高于阈值的尾部
    ltv_targets += [1.2, 1.5, 2.0]
    samples = []
    for ltv in ltv_targets:
        if ltv < 0:
            continue
        for high_risk in (False, True):
            samples.append((ltv, high_risk))
    return samples


def test_ac02_five_class_consistency_rate_at_least_99_9_pct():
    """固化样本上，引擎五级分类与 PRD 参考口径一致率 ≥ 99.9%。

    参考口径用引擎内部同精度 LTV（round(ltv, 4)），避免浮点舍入造成假不一致。
    """
    samples = _calibration_samples()
    mismatches = 0
    for ltv, high_risk in samples:
        got = _enrich(ltv, high_risk)
        exp = _ref_risk_class(round(ltv, 4), high_risk)
        if got != exp:
            mismatches += 1
    rate = (len(samples) - mismatches) / len(samples)
    assert rate >= 0.999, f"AC-02 一致率 {rate:.4f} < 0.999（{mismatches}/{len(samples)} 笔不一致）"


def test_ac02_boundary_points_exactly_match_reference():
    """边界点钉死：恰好 = 阈值时落在该档（<= 语义），高危区正常/关注升「关注」。"""
    for b, expect in (
        (_PRD_NORMAL, "正常"),
        (_PRD_ATTN, "关注"),
        (_PRD_SUBSTD, "次级"),
        (_PRD_DOUBT, "可疑"),
    ):
        assert _enrich(b, False) == expect
        # 高危区：正常/关注档升到关注，次级及以上不受影响
        if expect in ("正常", "关注"):
            assert _enrich(b, True) == "关注"
        else:
            assert _enrich(b, True) == expect
    # 严格大于阈值（4 位小数可分辨）→ 落入下一档
    assert _enrich(round(_PRD_NORMAL + 0.0001, 4), False) == "关注"
    assert _enrich(round(_PRD_ATTN + 0.0001, 4), False) == "次级"
    assert _enrich(round(_PRD_SUBSTD + 0.0001, 4), False) == "可疑"
    assert _enrich(round(_PRD_DOUBT + 0.0001, 4), False) == "损失"
