"""generator.py 单元测试：bias 开关行为 + 可复现性。"""

import numpy as np
import persona_testmods as m


def test_bias_naive_optimistic(bench, naive):
    """bias=naive 是乐观画像：违约率被系统性压低，KS 明显超线。"""
    rep = m.critic.ks_report(bench, naive)
    # 美化偏见导致的违约率分布 KS 应明显大于 0.05（可被检测）
    assert rep["default_prob"]["ks"] > 0.05
    # 系统性低估：均值显著低于基准
    assert naive.default_prob.mean() < bench.default_prob.mean() * 0.95
    # 特征被美化：收入上移、负债率下移
    assert naive.income_monthly.mean() > bench.income_monthly.mean() * 1.15
    assert naive.debt_ratio.mean() < bench.debt_ratio.mean() * 0.85


def test_bias_realistic_close_to_benchmark(bench, realistic):
    """bias=realistic 贴近基准：违约率分布 KS ≤ 0.05。"""
    rep = m.critic.ks_report(bench, realistic)
    assert rep["default_prob"]["ks"] <= 0.05
    assert abs(realistic.default_prob.mean() - bench.default_prob.mean()) < 0.01


def test_bias_switch_naive_worse_than_realistic(bench, naive, realistic):
    """bias 开关行为：naive 的违约率 KS 应明显差于 realistic。"""
    naive_ks = m.critic.ks_report(bench, naive)["default_prob"]["ks"]
    realistic_ks = m.critic.ks_report(bench, realistic)["default_prob"]["ks"]
    assert naive_ks > realistic_ks + 0.1


def test_reproducible_same_seed(bench):
    """固定 seed 两次生成结果完全一致（确定性）。"""
    a = m.generator.generate_personas(n=500, bias="naive", seed=7, benchmark_data=bench)
    b = m.generator.generate_personas(n=500, bias="naive", seed=7, benchmark_data=bench)
    for key in ("income_monthly", "debt_ratio", "credit_score"):
        np.testing.assert_array_equal(a.features[key], b.features[key])
    np.testing.assert_array_equal(a.default_prob, b.default_prob)
    assert a.seed == b.seed == 7


def test_reproducible_different_seed_differs(bench):
    """不同 seed 结果不同（种子确实生效）。"""
    a = m.generator.generate_personas(n=500, bias="naive", seed=1, benchmark_data=bench)
    b = m.generator.generate_personas(n=500, bias="naive", seed=2, benchmark_data=bench)
    assert not np.array_equal(a.income_monthly, b.income_monthly)


def test_unknown_bias_raises(bench):
    import pytest

    with pytest.raises(ValueError):
        m.generator.generate_personas(n=10, bias="other", benchmark_data=bench)
