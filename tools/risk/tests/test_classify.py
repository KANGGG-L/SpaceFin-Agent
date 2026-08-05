"""五级分类判定（config.classify）：档位边界与降级语义。

分类口径（CLASS_LTV_UPPER，「<= 上界」归本级，超过「可疑」上界归损失）：
    正常 <= 0.60 < 关注 <= 0.75 < 次级 <= 0.85 < 可疑 <= 1.00 < 损失

这些是纯函数，边界值必须逐个钉死——LTV 差 0.0001 就跨档，直接影响拨备计提。
"""

import pytest
from riskmods import config

# ---------------------------------------------------------------- 档位主干


@pytest.mark.parametrize(
    ("ltv", "expected"),
    [
        (0.30, "正常"),
        (0.70, "关注"),
        (0.80, "次级"),
        (0.90, "可疑"),
        (1.50, "损失"),
    ],
)
def test_classify_midpoints_map_to_expected_class(ltv, expected):
    assert config.classify(ltv) == expected


# ---------------------------------------------------------------- 边界值


@pytest.mark.parametrize(
    ("upper", "expected"),
    [
        (0.60, "正常"),
        (0.75, "关注"),
        (0.85, "次级"),
        (1.00, "可疑"),
    ],
)
def test_classify_value_equal_to_upper_bound_stays_in_class(upper, expected):
    """判定用的是 `<=`：LTV 恰好压线仍算本级，不上浮。"""
    assert config.classify(upper) == expected


@pytest.mark.parametrize(
    ("just_over", "expected"),
    [
        (0.6001, "关注"),
        (0.7501, "次级"),
        (0.8501, "可疑"),
        (1.0001, "损失"),
    ],
)
def test_classify_one_step_over_upper_bound_moves_to_next_class(just_over, expected):
    """LTV 保留 4 位小数（risk_engine 里 round(...,4)），0.0001 就是最小可分辨步进。"""
    assert config.classify(just_over) == expected


def test_classify_zero_ltv_is_normal():
    """LTV=0（余额已还清）是正常，不能因为「非典型」被丢进保守档。"""
    assert config.classify(0.0) == "正常"


# ---------------------------------------------------------------- 缺失/非法输入


def test_classify_none_ltv_falls_back_to_substandard():
    """估值缺失导致 LTV 算不出来时，既不能乐观归正常，也不该直接判损失。

    次级是 config.classify 明确写死的保守回退档。
    """
    assert config.classify(None) == "次级"


@pytest.mark.parametrize("bad", [-0.01, -1.0, -999.0])
def test_classify_negative_ltv_falls_back_to_substandard(bad):
    """负 LTV 只可能来自脏数据（负余额/负估值），按保守档处理而不是当成「正常」。"""
    assert config.classify(bad) == "次级"


# ---------------------------------------------------------------- 口径自洽


def test_class_upper_bounds_are_strictly_increasing():
    """正常 < 关注 < 次级 < 可疑，任何一档被调乱都会让 classify 的顺序遍历失效。"""
    uppers = [config.CLASS_LTV_UPPER[c] for c in config.CLASS_ORDER[:-1]]
    assert uppers == sorted(uppers)
    assert len(set(uppers)) == len(uppers)


def test_class_order_covers_all_tiers_with_loss_last():
    assert config.CLASS_ORDER == ["正常", "关注", "次级", "可疑", "损失"]
    # 除「损失」外每一档都必须有上界，否则 classify 的循环会 KeyError
    assert set(config.CLASS_ORDER[:-1]) == set(config.CLASS_LTV_UPPER)


def test_substandard_upper_bound_equals_ltv_red_line():
    """口径一致性约束（AC-03）：预警红线 0.85 同时是「次级」的上界。

    两者由不同环境变量控制（RISK_LTV_SUBSTD / RISK_LTV_RED_LINE），只改一个就会
    出现「已进可疑档但没触发预警」或「还在次级档却已预警」的错配。这里钉住默认口径。
    """
    assert config.CLASS_LTV_UPPER["次级"] == config.LTV_RED_LINE == 0.85


def test_first_tier_above_red_line_is_doubtful():
    """超红线的贷款至少是「可疑」——预警与分类降级必须同时发生。"""
    over = config.LTV_RED_LINE + 0.0001
    assert config.classify(over) == "可疑"
