"""predict.py：模型缺失/信息不足回退、小区→城市→全局中位回退语义、特征构建。

全部用例用 conftest.make_model 构造的假 model dict（含 CaptureRegressor），
不加载 output/avm 真实产物、不连数据库。
"""

import math

import numpy as np
import pytest
from avmmods import predict
from conftest import make_model

# _build_row 产出的 28 维特征行里与本文件用例相关的索引
IDX = {
    "area": 0,
    "log_area": 1,
    "bed": 2,
    "age": 7,
    "city_code": 13,
    "lat": 14,
    "lng": 15,
    "comm_mean": 16,
    "comm_median": 17,
    "comm_n": 18,
    "comm_minus_city": 19,
    "city_mean": 20,
    "city_median": 21,
    "city_n": 22,
}

# 假模型里的参考价
GLOBAL_MED = 40000.0
GZ_CITY_MED = 42000.0
GZ_CITY_MEAN = 43000.0
COMM_MED = 48000.0
COMM_MEAN = 50000.0
COMM_N = 20
AREA = 89.5


def _row(model):
    """取假回归器最近一次 predict 收到的特征行。"""
    return model["model"].rows[-1]


# ================================================================ 模型缺失 / 信息不足


def test_estimate_returns_none_when_model_missing():
    """模型缺失（未训练环境常态，load_model 返回 None）→ 直接 None，由调用方回退。"""
    assert predict.estimate_total_price(None, city_code="gz", area_sqm=80) is None


@pytest.mark.parametrize(
    ("city", "area", "why"),
    [
        ("", 80.0, "城市码为空"),
        ("gz", None, "面积缺失"),
        ("gz", 0, "面积为零"),
        ("gz", -10.0, "面积为负"),
    ],
)
def test_estimate_returns_none_when_input_insufficient(model, city, area, why):
    assert predict.estimate_total_price(model, city_code=city, area_sqm=area) is None, why


# ================================================================ 回退语义


def test_known_community_returns_smoothed_comm_median_times_area(model):
    """小区命中：总价 = EB 收缩后的小区中位 × 面积（smooth_k=10, n=20 → 46000）。"""
    v = predict.estimate_total_price(model, city_code="gz", community="天河城", area_sqm=AREA)
    assert v == pytest.approx(46000.0 * AREA, abs=0.01)


def test_known_community_without_smoothing_uses_raw_comm_median():
    """smooth_k=0（老模型/不收缩）时用原始小区中位。"""
    m = make_model(smooth_k=0.0)
    v = predict.estimate_total_price(m, city_code="gz", community="天河城", area_sqm=AREA)
    assert v == pytest.approx(COMM_MED * AREA, abs=0.01)
    assert _row(m)[IDX["comm_median"]] == pytest.approx(COMM_MED)


def test_eb_mode_uses_per_city_shrinkage_k():
    """smooth_mode=eb 时按城市读 encoders.eb_k（老产物无此键 → 视为 fixed）。"""
    m = make_model(smooth_k=10.0, smooth_mode="eb", eb_k={"gz": 5.0})
    v = predict.estimate_total_price(m, city_code="gz", community="天河城", area_sqm=AREA)
    exp_med = (COMM_N * COMM_MED + 5.0 * GZ_CITY_MED) / (COMM_N + 5.0)
    assert v == pytest.approx(exp_med * AREA, abs=0.01)


def test_unknown_community_falls_back_to_city_median(model):
    """小区未知：comm 特征置空，模型收到城市中位 → 总价 = 城市中位 × 面积。"""
    v = predict.estimate_total_price(model, city_code="gz", area_sqm=AREA)
    assert v == pytest.approx(GZ_CITY_MED * AREA, abs=0.01)
    row = _row(model)
    assert np.isnan(row[IDX["comm_mean"]])
    assert np.isnan(row[IDX["comm_median"]])
    assert row[IDX["comm_n"]] == 0.0
    assert row[IDX["city_median"]] == pytest.approx(GZ_CITY_MED)


def test_unknown_city_falls_back_to_global_median(model):
    """城市未知：city 编码退化为 (g, g, 0)，模型收到全局中位 → 总价 = 全局中位 × 面积。"""
    v = predict.estimate_total_price(model, city_code="zz", community="天河城", area_sqm=AREA)
    assert v == pytest.approx(GLOBAL_MED * AREA, abs=0.01)
    row = _row(model)
    assert row[IDX["city_code"]] == -1.0  # 未见类别码（HistGBR 容忍）
    assert row[IDX["city_mean"]] == pytest.approx(GLOBAL_MED)
    assert row[IDX["city_median"]] == pytest.approx(GLOBAL_MED)
    assert row[IDX["city_n"]] == 0.0


def test_legacy_artifact_without_smooth_k_still_works():
    """老产物无 smooth_k 键 → 视为不收缩（向后兼容），不抛异常。"""
    m = make_model()
    del m["smooth_k"]
    v = predict.estimate_total_price(m, city_code="gz", community="天河城", area_sqm=AREA)
    assert v == pytest.approx(COMM_MED * AREA, abs=0.01)


# ================================================================ 特征构建


def test_area_and_attributes_forwarded_into_row(model):
    predict.estimate_total_price(
        model,
        city_code="gz",
        community="天河城",
        area_sqm=AREA,
        building_age=8,
        bedrooms=3,
        latitude=23.13,
        longitude=113.32,
    )
    row = _row(model)
    assert row[IDX["area"]] == pytest.approx(AREA)
    assert row[IDX["log_area"]] == pytest.approx(math.log(AREA))
    assert row[IDX["bed"]] == pytest.approx(3.0)
    assert row[IDX["age"]] == pytest.approx(8.0)
    assert row[IDX["lat"]] == pytest.approx(23.13)
    assert row[IDX["lng"]] == pytest.approx(113.32)
    assert row.shape == (28,)


def test_partial_coords_treated_as_no_coords(model):
    """只有单边坐标视为无坐标（训练侧同口径），lat/lng 置 NaN。"""
    predict.estimate_total_price(
        model, city_code="gz", area_sqm=AREA, latitude=23.13, longitude=None
    )
    row = _row(model)
    assert np.isnan(row[IDX["lat"]])
    assert np.isnan(row[IDX["lng"]])


# ================================================================ load_model


def test_load_model_returns_none_when_file_missing(tmp_path):
    assert predict.load_model(str(tmp_path / "no-such.joblib")) is None


def test_load_model_returns_none_when_artifact_corrupt(tmp_path):
    p = tmp_path / "corrupt.joblib"
    p.write_bytes(b"this is not a joblib payload")
    assert predict.load_model(str(p)) is None
