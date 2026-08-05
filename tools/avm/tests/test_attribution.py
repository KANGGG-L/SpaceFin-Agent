"""attribution.py：permutation importance 特征归因报告（C-02）。

纯函数用例（零 DB、零真实模型产物）：用假线性 log 回归器验证返回结构、
Top-N 排序、feature_names 匹配、method 字段诚实标注、优雅失败路径。
"""

import numpy as np
import pytest
from avmmods import attribution

# 与 train.FEATURE_NAMES 一致的 28 特征名（测试只需数量与首特征名正确）
NAMES = (
    ["area", "log_area", "bed", "hall", "bath", "rooms", "area_per_bed", "age"]
    + ["floor_level", "floor_total", "floor_ratio", "direction", "parking", "city_code"]
    + ["lat", "lng"]
    + ["comm_mean", "comm_median", "comm_n", "comm_minus_city"]
    + ["city_mean", "city_median", "city_n"]
    + ["nn_med_k3", "nn_med_k8", "nn_med_k20", "nn_med_k50", "nn_dist"]
)
N_FEATURES = len(NAMES)


class _LinearLogReg:
    """y_log = x @ w；predict 返回 log 单价，exp(predict) = 单价 = y。

    只有权重非零的特征影响预测 → permutation importance 应集中到它们。
    实现空 fit / 声明 regressor 类型以满足 sklearn 的 estimator 校验。
    """

    _estimator_type = "regressor"

    def __init__(self, weights):
        self.w = np.array(weights, dtype=float)

    def fit(self, x, y=None, **kw):
        return self

    def predict(self, x):
        x = np.asarray(x, dtype=float)
        x = np.nan_to_num(x, nan=0.0)
        return x @ self.w


def _dominant_data(n=120, seed=7):
    """x 与 y（单价口径）：只有 area（w=1.0）对预测有影响。"""
    rng = np.random.RandomState(seed)
    x = rng.normal(size=(n, N_FEATURES))
    w = np.zeros(N_FEATURES)
    w[NAMES.index("area")] = 1.0
    reg = _LinearLogReg(w)
    y = np.exp(reg.predict(x))
    return reg, x, y


def _report(**kw):
    reg, x, y = _dominant_data()
    return attribution.compute_attribution(reg, x, y, NAMES, n_repeats=3, seed=42, **kw)


# ================================================================ 优雅失败路径


def test_returns_none_when_model_missing():
    assert attribution.compute_attribution(None, np.zeros((5, 28)), np.ones(5), NAMES) is None


def test_returns_none_when_estimator_missing_in_artifact():
    """产物 dict 里没有 model 键（异常产物）→ None，不抛栈。"""
    assert attribution.compute_attribution({}, np.zeros((5, 28)), np.ones(5), NAMES) is None


def test_returns_none_when_feature_count_mismatch():
    """x 列数与 feature_names 数不一致 → None（优雅失败，提示重训）。"""
    reg, x, y = _dominant_data()
    assert attribution.compute_attribution(reg, x[:, :-1], y, NAMES) is None


def test_returns_none_when_xy_length_mismatch():
    reg, x, y = _dominant_data()
    assert attribution.compute_attribution(reg, x, y[:-1], NAMES) is None


def test_returns_none_when_data_missing():
    reg, x, y = _dominant_data()
    assert attribution.compute_attribution(reg, x, None, NAMES) is None
    assert attribution.compute_attribution(reg, None, y, NAMES) is None


def test_negative_repeats_raises_valueerror():
    reg, x, y = _dominant_data()
    with pytest.raises(ValueError):
        attribution.compute_attribution(reg, x, y, NAMES, n_repeats=0)


# ================================================================ 报告结构与诚实标注


def test_report_has_expected_keys():
    rep = _report(version="2026-08-05-test")
    for key in (
        "version",
        "method",
        "n_repeats",
        "seed",
        "scoring",
        "top_features",
        "full_importances",
        "generated_at",
    ):
        assert key in rep, f"缺少字段 {key}"
    assert rep["version"] == "2026-08-05-test"


def test_method_honest_permutation_importance():
    """方法诚实标注为 permutation_importance（非 SHAP）。"""
    rep = _report()
    assert rep["method"] == "permutation_importance"


def test_scoring_field_is_neg_mape():
    rep = _report()
    assert rep["scoring"] == "neg_mean_absolute_percentage_error"


def test_accepts_artifact_dict_with_model_key():
    reg, x, y = _dominant_data()
    rep = attribution.compute_attribution({"model": reg}, x, y, NAMES, n_repeats=3)
    assert rep is not None
    assert rep["top_features"][0]["feature"] == "area"


# ================================================================ 排序 / Top-N / 命名


def test_top_features_sorted_descending():
    rep = _report()
    means = [t["importance_mean"] for t in rep["top_features"]]
    assert means == sorted(means, reverse=True)
    assert len(rep["top_features"]) == N_FEATURES


def test_dominant_feature_ranked_first():
    """只有 area 有真实信号 → area 必须排第一且 importance 为正。"""
    rep = _report()
    top = rep["top_features"][0]
    assert top["feature"] == "area"
    assert top["importance_mean"] > 0.0
    assert rep["full_importances"]["area"]["importance_mean"] > 0.0


def test_full_importances_covers_all_features():
    rep = _report(top_n=3)
    assert set(rep["full_importances"]) == set(NAMES)  # 不随 top_n 截断


def test_top_n_limits_top_features():
    rep = _report(top_n=5)
    assert len(rep["top_features"]) == 5
    assert len(rep["full_importances"]) == N_FEATURES


def test_chinese_name_and_english_fallback():
    rep = _report()
    by_feat = {t["feature"]: t for t in rep["top_features"]}
    assert by_feat["area"]["chinese_name"] == "面积"
    assert by_feat["area"]["description"]  # 有说明
    # 未收录特征名回退英文名
    assert attribution.feature_meta("no_such_feature")["chinese_name"] == "no_such_feature"


def test_generated_at_is_iso_like():
    rep = _report()
    assert len(rep["generated_at"]) >= 10
    assert rep["generated_at"][4] == "-"
