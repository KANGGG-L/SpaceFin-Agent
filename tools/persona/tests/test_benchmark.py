"""benchmark.py 单元测试（零 DB）。"""

import os

import numpy as np
import persona_testmods as m


def test_load_benchmark_snapshot(bench):
    """快照能离线加载，n=200，三特征数组与派生违约概率齐全。"""
    assert bench.n == 200
    for key in ("income_monthly", "debt_ratio", "credit_score"):
        arr = bench.features[key]
        assert isinstance(arr, np.ndarray)
        assert arr.shape == (200,)
        assert np.all(np.isfinite(arr))
    p = bench.default_prob
    assert p.shape == (200,)
    assert np.all((p > 0.0) & (p < 1.0))


def test_snapshot_json_consistent_with_code_formula():
    """快照 JSON 内嵌的 derived_default_prob 与代码固定公式重算一致。"""
    import json

    snap = os.path.join(m.PERSONA_DIR, "benchmark_customer.json")
    with open(snap, encoding="utf-8") as f:
        doc = json.load(f)
    feats = doc["features"]
    recomputed = m.benchmark.derive_default_prob(feats["debt_ratio"], feats["credit_score"])
    stored = np.asarray(doc["derived_default_prob"], dtype=float)
    np.testing.assert_allclose(recomputed, stored, atol=1e-4)
    # 快照自描述公式与代码常量一致
    assert doc["formula"]["b0"] == m.benchmark.LOGISTIC["b0"]
    assert doc["formula"]["b1"] == m.benchmark.LOGISTIC["b1"]
    assert doc["formula"]["b2"] == m.benchmark.LOGISTIC["b2"]


def test_derive_default_prob_monotonic():
    """违约率随 debt_ratio 单调升、随 credit_score 单调降（H3 行为模型前提）。"""
    score = 680.0
    debt_up = m.benchmark.derive_default_prob([0.1, 0.3, 0.5, 0.7], score)
    assert np.all(np.diff(debt_up) > 0)

    debt = 0.4
    score_up = m.benchmark.derive_default_prob(debt, [500.0, 600.0, 700.0, 800.0])
    assert np.all(np.diff(score_up) < 0)


def test_formula_base_rate_reasonable(bench):
    """基准违约率均值与分布要有足够区分度（美化偏见才可被检测）。"""
    p = bench.default_prob
    assert 0.20 <= p.mean() <= 0.50
    assert np.percentile(p, 90) - np.percentile(p, 10) > 0.2
