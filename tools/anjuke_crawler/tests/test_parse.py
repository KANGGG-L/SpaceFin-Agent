"""解析层测试：numeric（17 字段）+ advanced（18 字段）。"""

import csv

from anjuke_crawler.parse import (
    ADVANCED_HEADERS,
    SCHEMA_HEADERS,
    parse_advanced_housing_data,
    parse_numeric_schema_housing,
    save_numeric_schema_csv,
)


def test_numeric_parses_71_unique(fixture_html):
    rows = parse_numeric_schema_housing(fixture_html, district_tag="sh_pudong")
    assert len(rows) == 71  # 精确 div[@class='property'] 去重后应为 71，非 4x


def test_numeric_schema_17_fields(fixture_html):
    assert len(SCHEMA_HEADERS) == 17
    rows = parse_numeric_schema_housing(fixture_html)
    assert set(rows[0].keys()) == set(SCHEMA_HEADERS)


def test_numeric_dedup_by_url(fixture_html):
    rows = parse_numeric_schema_housing(fixture_html)
    urls = [r["url"] for r in rows if r["url"]]
    assert len(urls) == len(set(urls))  # 无重复 url


def test_numeric_field_types(fixture_html):
    rows = parse_numeric_schema_housing(fixture_html, district_tag="sh_pudong")
    r = next(x for x in rows if x["total_price_wan"])
    assert isinstance(r["bedrooms"], int)
    assert isinstance(r["total_price_wan"], float)
    assert r["district"] == "sh_pudong"  # district_tag 透传


def test_numeric_csv_roundtrip(fixture_html, tmp_path):
    rows = parse_numeric_schema_housing(fixture_html)
    out = tmp_path / "num.csv"
    save_numeric_schema_csv(rows, str(out))
    back = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert len(back) == 71
    assert list(back[0].keys()) == SCHEMA_HEADERS


def test_advanced_18_fields_with_layout_parking(fixture_html):
    assert len(ADVANCED_HEADERS) == 18
    rows = parse_advanced_housing_data(fixture_html)
    assert len(rows) == 71
    r = rows[0]
    assert "Layout" in r and "Has_Parking" in r and "Parking_Desc" in r
    assert isinstance(r["Has_Parking"], bool)


def test_advanced_layout_format(fixture_html):
    rows = parse_advanced_housing_data(fixture_html)
    r = next(x for x in rows if x["Rooms"])
    assert "室" in r["Layout"]  # 形如 "3室2厅2卫"
