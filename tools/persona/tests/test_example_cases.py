"""R2-12 · build_report 新增「具体案例 · 修正前 vs 修正后」example_cases。

护栏（改前即绿）：build_report 既有顶层键结构与语义不得破坏——只增 example_cases；
判据（改前必红）：example_cases 存在且数量在 [3,5]（n≥3）、逐值对齐数组、schema 字段
齐全、同 seed 确定性、选择算法可复现、无 PII、n<3 时为 []。
"""

import json
import re

import numpy as np
import persona_testmods as m

# build_report 既有全部顶层键：R2-12 只允许增 example_cases，不得删改。
EXISTING_TOP_KEYS = (
    "tool",
    "h3",
    "p3_pain",
    "acceptance",
    "generated_at",
    "params",
    "benchmark",
    "naive",
    "calibrated",
    "calibration",
    "comparison_default_prob",
    "uncalibrated_flag",
    "honest_declaration",
)

# 每案例 schema 字段：label/index/两个违约率/delta/特征前后/基准均值/reason。
CASE_KEYS = (
    "label",
    "index",
    "default_prob_naive",
    "default_prob_calibrated",
    "delta",
    "features_naive",
    "features_calibrated",
    "benchmark_mean",
    "reason",
)


def _build(bench, n=1000, seed=42):
    """按默认流水线生成 naive → calibrate → build_report。"""
    naive = m.generator.generate_personas(n=n, bias="naive", seed=seed, benchmark_data=bench)
    calibrated, trajectory = m.critic.calibrate(bench, naive)
    return m.main.build_report(bench, naive, calibrated, trajectory, seed, n)


def _recompute_selection(naive_dp, cal_dp, bench_dp, max_cases=5):
    """按规格 5 条判据独立重算应选中的 index 集合（并列取最小 index）。"""
    delta = cal_dp - naive_dp
    n = len(delta)
    criteria = (
        -delta,  # ① max-delta：美化最重、修正最猛
        delta,  # ② min-delta：美化影响最小的边界
        np.abs(delta - np.median(delta)),  # ③ median-delta：典型修正幅度
        np.abs(cal_dp - np.median(bench_dp)),  # ④ 修正后最像"真实同类"
        np.abs(naive_dp - np.median(naive_dp)),  # ⑤ 修正前最典型的被美化者
    )
    selected = []
    for s in criteria:
        order = np.lexsort((np.arange(n), s))  # 判据值升序、并列取最小 index
        for i in order:
            i = int(i)
            if i not in selected:
                selected.append(i)
                break
    if len(selected) < 3:  # 兜底：全退化时按 index 升序补足 3 条
        for i in range(n):
            if i not in selected:
                selected.append(i)
            if len(selected) >= 3:
                break
    return set(selected[:max_cases])


# ---------------------------------------------------------------- 护栏
def test_report_top_level_keys_unchanged(bench):
    """R2-12 只增 example_cases：既有全部顶层键必须原样保留（改前即绿）。"""
    report = _build(bench)
    for key in EXISTING_TOP_KEYS:
        assert key in report, f"build_report 丢了既有顶层键：{key}"
    # 结构语义不被破坏：naive/calibrated/calibration/comparison 仍在原位置。
    assert report["naive"]["calibration_status"] == "未校准"
    assert report["calibrated"]["calibration_status"] == "校准"
    assert "trajectory" in report["calibration"]
    assert set(report["comparison_default_prob"]) == {"benchmark", "naive", "calibrated"}


# ---------------------------------------------------------------- 判据
def test_example_cases_present_and_count(bench):
    """顶层 example_cases 必须存在，且 n=1000 时数量在 [3,5]（改前红）。"""
    report = _build(bench)
    cases = report["example_cases"]
    assert isinstance(cases, list), "example_cases 应为列表"
    assert 3 <= len(cases) <= 5, f"n=1000 时案例数应在 [3,5]，实际 {len(cases)}"


def test_example_cases_delta_matches_arrays(bench):
    """每条 case 的 naive/calibrated/delta 与数组在 index 处逐值一致（改前红）。"""
    report = _build(bench)
    naive = m.generator.generate_personas(n=1000, bias="naive", seed=42, benchmark_data=bench)
    calibrated, _ = m.critic.calibrate(bench, naive)
    for c in report["example_cases"]:
        i = c["index"]
        assert 0 <= i < 1000
        np.testing.assert_allclose(c["default_prob_naive"], naive.default_prob[i])
        np.testing.assert_allclose(c["default_prob_calibrated"], calibrated.default_prob[i])
        np.testing.assert_allclose(c["delta"], calibrated.default_prob[i] - naive.default_prob[i])


def test_example_cases_fields_complete(bench):
    """schema 字段齐全、benchmark_mean 与基准违约率均值同值、特征三键齐全（改前红）。"""
    report = _build(bench)
    for c in report["example_cases"]:
        for key in CASE_KEYS:
            assert key in c, f"案例缺 schema 字段：{key}"
        assert c["benchmark_mean"] == float(bench.default_prob.mean())
        for side in ("features_naive", "features_calibrated"):
            assert set(c[side]) == {"income_monthly", "debt_ratio", "credit_score"}
        assert c["delta"] == c["default_prob_calibrated"] - c["default_prob_naive"]


def test_example_cases_deterministic(bench):
    """同 seed 两遍逐字节一致（确定性，改前红）。"""
    a = _build(bench)
    b = _build(bench)
    assert json.dumps(a["example_cases"], ensure_ascii=False, sort_keys=True) == json.dumps(
        b["example_cases"], ensure_ascii=False, sort_keys=True
    )


def test_example_cases_reproduce_selection(bench):
    """按算法重算，断言 case 的 index 集合与规则一致（改前红）。"""
    naive = m.generator.generate_personas(n=1000, bias="naive", seed=42, benchmark_data=bench)
    calibrated, _ = m.critic.calibrate(bench, naive)
    report = m.main.build_report(bench, naive, calibrated, [], 42, 1000)
    expected = _recompute_selection(
        np.asarray(naive.default_prob),
        np.asarray(calibrated.default_prob),
        np.asarray(bench.default_prob),
    )
    assert {c["index"] for c in report["example_cases"]} == expected


def test_example_cases_no_pii_fields(bench):
    """案例只标识 label/index，禁止 customer_id/customer_name 等 PII（改前红）。"""
    report = _build(bench)
    for c in report["example_cases"]:
        keys = set(c) | set(c["features_naive"]) | set(c["features_calibrated"])
        assert not (keys & {"customer_id", "customer_name", "name", "address"}), (
            f"案例混入 PII 字段：{keys & {'customer_id', 'customer_name', 'name', 'address'}}"
        )
        assert re.fullmatch(r"案例 [A-Z]", c["label"]), f"label 格式异常：{c['label']!r}"


def test_example_cases_small_n_empty(bench):
    """n=2（<3）时 example_cases 必须为 []（改前红）。"""
    report = _build(bench, n=2)
    assert report["example_cases"] == []
