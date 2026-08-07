"""空间基础几何（features.py）：等距圆柱投影、球面距离、网格划分。

这些是整个 S3 的坐标底座，投影或网格错一点，邻域查询与区块归属会整体偏移。
"""

import math

import pytest
from spatmods import config, features

# ================================================================ 投影


def test_reference_point_projects_to_origin():
    """投影参考点 (23.5N, 113.5E) 必须落在公里坐标原点。"""
    assert features.project_latlng(23.5, 113.5) == (0.0, 0.0)


def test_east_and_north_are_positive_axes():
    x_east, _ = features.project_latlng(23.5, 114.5)
    _, y_north = features.project_latlng(24.5, 113.5)

    assert x_east > 0
    assert y_north > 0


def test_one_degree_latitude_is_about_110km():
    _, y = features.project_latlng(24.5, 113.5)

    assert y == pytest.approx(110.57, abs=0.01)


def test_one_degree_longitude_is_shortened_by_latitude():
    """经度方向要乘 cos(参考纬度)，否则广东一带东西向距离会被高估约 8%。"""
    x, _ = features.project_latlng(23.5, 114.5)

    assert x == pytest.approx(111.32 * math.cos(math.radians(23.5)), abs=0.01)
    assert x < 111.32


def test_projection_is_linear_and_symmetric():
    east, _ = features.project_latlng(23.5, 114.5)
    west, _ = features.project_latlng(23.5, 112.5)

    assert east == pytest.approx(-west)


# ================================================================ 球面距离


def test_distance_to_self_is_zero():
    assert features.haversine_km(23.13, 113.26, 23.13, 113.26) == pytest.approx(0.0, abs=1e-9)


def test_guangzhou_to_shenzhen_is_about_100km():
    """广州—深圳直线约 100km，用真实城市中心校准球面公式。"""
    gz_lng, gz_lat = config.CITY_CENTER["gz"]
    sz_lng, sz_lat = config.CITY_CENTER["sz"]

    d = features.haversine_km(gz_lat, gz_lng, sz_lat, sz_lng)

    assert 95.0 < d < 110.0


def test_distance_is_symmetric():
    a = features.haversine_km(23.1, 113.2, 22.5, 114.0)
    b = features.haversine_km(22.5, 114.0, 23.1, 113.2)

    assert a == pytest.approx(b)


def test_one_degree_latitude_arc_is_about_111km():
    assert features.haversine_km(23.0, 113.0, 24.0, 113.0) == pytest.approx(111.19, abs=0.5)


# ================================================================ 网格


def test_grid_key_floors_to_grid_corner():
    """网格键取左下角，同一网格内的点必须得到同一个键。"""
    assert features.config.grid_key(113.3271, 23.1349) == config.grid_key(113.3350, 23.1399)


def test_grid_key_returns_lower_left_corner():
    glng, glat = config.grid_key(113.3271, 23.1349)

    assert glng <= 113.3271 < glng + config.GRID_DEG
    assert glat <= 23.1349 < glat + config.GRID_DEG


def test_grid_key_on_nominal_boundary_falls_into_lower_cell():
    """⚠️ 浮点精度：113.32/0.02 在二进制里是 5665.9999…，floor 后落到前一格。

    也就是说「正好压在网格线上」的点归属下一格而不是本格。结果确定且可复现，
    本身不算错，但会让一簇坐标恰好跨在 0.02° 线上的挂牌被劈成两格
    （见 test_features.py 的 test_cluster_on_grid_boundary_is_split_into_two_cells）。
    """
    glng, glat = config.grid_key(113.32, 23.14)

    assert glng == pytest.approx(113.30)
    assert glat == pytest.approx(23.14)


def test_adjacent_grids_differ_by_one_cell():
    a = config.grid_key(113.3100, 23.1300)
    b = config.grid_key(113.3300, 23.1300)

    assert b[0] - a[0] == pytest.approx(config.GRID_DEG)


def test_grid_key_handles_negative_coordinates():
    """floor 语义对负经度也要成立（虽然广东用不到，但别在别处炸）。"""
    glng, _ = config.grid_key(-0.01, 23.13)

    assert glng <= -0.01
