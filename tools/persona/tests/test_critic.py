"""critic.py 单元测试：ks_report 正确性 + calibrate 收敛。"""

import numpy as np
import persona_testmods as m


# ------------------------------------------------------------------ ks_report
def _personas_from_features(feats):
    """把三特征数组包成 Personas（用于构造「与基准相同/不同」的输入）。"""
    return m.generator.Personas(
        income_monthly=feats[0],
        debt_ratio=feats[1],
        credit_score=feats[2],
        bias="test",
        seed=0,
    )


def test_ks_report_identical_distribution(bench):
    """与基准完全相同的分布 → 三特征与违约率 KS 全为 0。"""
    personas = _personas_from_features(
        [bench.income_monthly.copy(), bench.debt_ratio.copy(), bench.credit_score.copy()]
    )
    rep = m.critic.ks_report(bench, personas)
    for key in m.benchmark.FEATURE_KEYS:
        assert rep["features"][key]["ks"] == 0.0
        assert rep["features"][key]["passed_ks_le_0_05"] is True
    assert rep["default_prob"]["ks"] == 0.0


def test_ks_report_detects_shift(bench):
    """把负债率整体抬高（违约风险升）→ 违约率 KS 明显超线，检测得到。"""
    shifted = _personas_from_features(
        [
            bench.income_monthly.copy(),
            np.clip(bench.debt_ratio + 0.3, 0.0, 1.0),
            bench.credit_score.copy(),
        ]
    )
    rep = m.critic.ks_report(bench, shifted)
    assert rep["default_prob"]["ks"] > 0.05
    assert rep["features"]["debt_ratio"]["ks"] > 0.05


def test_ks_report_structure(bench, naive):
    """报告结构齐全：n、每特征 KS、违约率 KS。"""
    rep = m.critic.ks_report(bench, naive)
    assert rep["n_benchmark"] == 200
    assert rep["n_synthetic"] == naive.n
    assert set(rep["features"]) == {"income_monthly", "debt_ratio", "credit_score"}
    for v in rep["features"].values():
        assert set(v) == {"ks", "pvalue", "passed_ks_le_0_05"}
    assert set(rep["default_prob"]) == {"ks", "pvalue", "passed_ks_le_0_05"}


# ------------------------------------------------------------------ calibrate
def test_calibrate_converges(bench, naive):
    """naive 输入经校准后违约率 KS ≤ 0.05，轮数在 20 内，轨迹单调收敛。"""
    calibrated, trajectory = m.critic.calibrate(bench, naive)
    rep = m.critic.ks_report(bench, calibrated)

    # 基线超线、最终达标
    assert trajectory[0] > 0.05
    assert trajectory[-1] <= 0.05
    assert rep["default_prob"]["ks"] <= 0.05
    assert len(trajectory) - 1 <= m.critic.DEFAULT_MAX_ROUNDS

    # 轨迹整体单调下降（校准是逼近基准，不来回震荡）
    assert trajectory == sorted(trajectory, reverse=True) or all(
        trajectory[i] >= trajectory[i + 1] for i in range(len(trajectory) - 1)
    )
    # 校准只作用于特征：违约概率仍由固定公式重算（用校准后特征重算应一致）
    np.testing.assert_allclose(
        calibrated.default_prob,
        m.benchmark.derive_default_prob(calibrated.debt_ratio, calibrated.credit_score),
    )


def test_calibrate_corrects_features_toward_benchmark(bench, naive):
    """校准后各特征分布应明显贴近基准（美化变换被修正）。"""
    calibrated, trajectory = m.critic.calibrate(bench, naive)
    # 违约率 KS 收敛即证明特征修正生效
    assert trajectory[-1] <= 0.05
    # 特征均值应回到基准附近（收入不再上移、负债率不再下移）
    naive_income_bias = abs(naive.income_monthly.mean() - bench.income_monthly.mean())
    cal_income_bias = abs(calibrated.income_monthly.mean() - bench.income_monthly.mean())
    naive_debt_bias = abs(naive.debt_ratio.mean() - bench.debt_ratio.mean())
    cal_debt_bias = abs(calibrated.debt_ratio.mean() - bench.debt_ratio.mean())
    assert cal_income_bias < naive_income_bias
    assert cal_debt_bias < naive_debt_bias
    # 校准后特征均值应贴近基准（相对偏差 < 5%）
    assert cal_income_bias / bench.income_monthly.mean() < 0.05
    assert cal_debt_bias / bench.debt_ratio.mean() < 0.05


def test_calibrate_deterministic(bench, naive):
    """同输入两次校准结果完全一致（可复现）。"""
    a, ta = m.critic.calibrate(bench, naive)
    b, tb = m.critic.calibrate(bench, naive)
    assert ta == tb
    for key in ("income_monthly", "debt_ratio", "credit_score"):
        np.testing.assert_array_equal(a.features[key], b.features[key])
    np.testing.assert_array_equal(a.default_prob, b.default_prob)


def test_calibrate_already_ok_no_rounds(bench, realistic):
    """输入已达标（realistic）→ 原样返回，0 轮修正。"""
    out, trajectory = m.critic.calibrate(bench, realistic)
    assert len(trajectory) == 1
    assert trajectory[0] <= 0.05
    for key in ("income_monthly", "debt_ratio", "credit_score"):
        np.testing.assert_array_equal(out.features[key], realistic.features[key])


def test_calibrate_respects_max_rounds(bench):
    """把「永远不合格」的输入限制在 max_rounds 轮内返回（不无限循环）。"""
    import numpy as np

    extreme = _personas_from_features(
        [
            bench.income_monthly * 5.0,
            np.ones_like(bench.debt_ratio) * 0.05,
            bench.credit_score,
        ]
    )
    # max_rounds=1 时最多跑 1 轮；极端输入 1 轮未必收敛，但必须正常返回
    _, trajectory = m.critic.calibrate(bench, extreme, max_rounds=1)
    assert len(trajectory) == 2
    assert trajectory[-1] > 0.05
