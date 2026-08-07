"""地理编码测试：O(1) 查找 / 模糊匹配 / 区域兜底 / 增量沉淀。"""

import json

from anjuke_crawler.geocoder import LocalGeocoder


def test_exact_match():
    g = LocalGeocoder()
    lat, lng = g.geocode("证大家园")
    assert lat and lng
    assert abs(lat - 31.28) < 0.1


def test_fuzzy_contains_match():
    g = LocalGeocoder()
    # "联洋XX苑" 应命中 "联洋"
    lat, lng = g.geocode("联洋年华苑")
    assert lat is not None


def test_region_fallback_from_full_text():
    g = LocalGeocoder()
    lat, lng = g.geocode("某未知小区", full_text="位于陆家嘴板块，交通便利")
    assert lat is not None  # 从全文兜底命中 陆家嘴


def test_unknown_returns_none():
    g = LocalGeocoder()
    assert g.geocode("不存在的小区xyz", full_text="无区域关键词abc") == (None, None)


def test_empty_name_returns_none():
    g = LocalGeocoder()
    assert g.geocode("") == (None, None)


def test_guangdong_district_present():
    g = LocalGeocoder()
    lat, lng = g.geocode("天河")
    assert lat and lng and abs(lng - 113.36) < 0.1


def test_add_and_save(tmp_path):
    db = tmp_path / "coords.json"
    g = LocalGeocoder(db_file=str(db))
    g.add("测试小区", 30.5, 114.3)
    g.save_local_db()
    data = json.load(open(db, encoding="utf-8"))
    assert data["测试小区"] == [30.5, 114.3]
    # 新实例能从落盘文件加载
    g2 = LocalGeocoder(db_file=str(db))
    assert g2.geocode("测试小区") == (30.5, 114.3)
