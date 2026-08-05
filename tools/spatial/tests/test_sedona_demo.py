"""G5 · Sedona 等价性演示测试（纯 Python，无 Spark 集群）。

验证 run_sedona_demo(synthetic=True)：
1. 返回结构完整，含 pyspark_available / neighbor_counts_match 等键；
2. pyspark 探测结果与实际环境一致（可用即为 True，缺失则 False 且给出提示）；
3. 邻居计数与 cKDTree 基准一致（neighbor_counts_match=True，max_count_diff=0）；
4. 两套实现的每查询 neighbor 计数都等于 k。
"""

import sedona_demo as sd


def test_demo_returns_full_struct():
    out = sd.run_sedona_demo()
    for key in (
        "pyspark_available",
        "mode",
        "note",
        "k",
        "n_points",
        "neighbor_counts_match",
        "max_count_diff",
        "sedona_counts",
        "ckd_counts",
        "sedona_latency_ms",
        "ckd_latency_ms",
    ):
        assert key in out
    assert out["mode"] == "synthetic"


def test_pyspark_detection_matches_env():
    import importlib.util

    expected = importlib.util.find_spec("pyspark") is not None
    out = sd.run_sedona_demo()
    assert out["pyspark_available"] == expected
    # 不可用时应带有明确降级提示，不静默。
    if not expected:
        assert "不可用" in out["note"]


def test_neighbor_counts_match_ckd():
    out = sd.run_sedona_demo(n_points=200, k=5, seed=42)
    assert out["neighbor_counts_match"] is True
    assert out["max_count_diff"] == 0
    # 每个查询点两侧都返回恰好 k 个邻居。
    assert all(c == 5 for c in out["sedona_counts"])
    assert all(c == 5 for c in out["ckd_counts"])
    # 两套计数数组逐点相等。
    assert out["sedona_counts"] == out["ckd_counts"]


def test_non_synthetic_mode_rejected():
    import pytest

    with pytest.raises(ValueError):
        sd.run_sedona_demo(synthetic=False)
