"""P0-2 方案 A：房源城市归属以 URL 子域为准（不看文件名）。

回归的实测缺陷：最新观测日 47.4% 的 url_key，其 URL 子域不在广东 21 城
（nantong×1059、anshan×943、beijing×876…），而 etl 当时按**文件名城市码**记归属，
把南通房源记成广东房源 —— 按城流转率的分母被污染一半。

判据设计（每条都能区分真成功与假成功）：
- 省外子域**必须落省外桶**，不许 fallback 回文件名城市（否则污染照旧，测试却绿）；
- 58 域名必须能解析（实测 484 行 `*.58.com` 曾被判不可解析，其中 fs/gz 共 264 行
  是真广东，漏了这层等于方案 A 误杀 264 行）；
- `resolve_city` 是 total 函数：任何输入都返回 (str, str)，不抛异常、不返回 None。

etl.load_rows 的集成分桶也放在本文件（不并入 test_etl_observe.py）：那边的用例按
basename 断言分桶键，与「按 URL 重算归属」的期望集不同源，混在一个文件里会让
「改了 etl 却只在半个文件里改期望」成为新的假绿入口。
"""

import importlib.util
import json
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ORCH_DIR)

from url_city import (  # noqa: E402
    R_EMPTY_URL,
    R_UNPARSED_HOST,
    R_URL_FOREIGN,
    R_URL_HIT,
    R_URL_HIT_OVER_FILENAME,
    UNKNOWN_CITY,
    city_subdomain,
    parse_host,
    resolve_city,
    subdomain_to_code,
)

# 真实形态：anjuke 子域是全拼，58 子域是短码
URL_GZ = "https://guangzhou.anjuke.com/prop/view/S4656241945109512?soj_info=abc"
URL_SZ = "https://shenzhen.anjuke.com/prop/view/S4656241945109513"
URL_NANTONG = "https://nantong.anjuke.com/prop/view/A7423039077?from=from_esf_List_screen"
URL_58_FS = "https://fs.58.com/xinfang/huxing/59071435-897433.html"
URL_58_GZ = "https://gz.58.com/xinfang/huxing/59071436-897434.html"
URL_58_SU = "https://su.58.com/xinfang/huxing/59071437-897435.html"
URL_58_NB = "https://nb.58.com/xinfang/huxing/59071438-897436.html"
URL_ZU_GZ = "https://guangzhou.zu.anjuke.com/fangyuan/4688937808519174"
URL_ZU_MZ = "https://mz.zu.anjuke.com/gfangyuan/2580079256313869?auction=201"


# ----------------------------------------------------------------------------
# parse_host
# ----------------------------------------------------------------------------


def test_parse_host_reads_subdomain_from_both_registrars():
    assert parse_host(URL_GZ) == ("anjuke", "guangzhou")
    assert parse_host(URL_58_FS) == ("58", "fs")
    # 出租子域天然满足 anjuke 分支，parts[0] 就是城市，无需特判
    assert parse_host(URL_ZU_GZ) == ("anjuke", "guangzhou")
    assert parse_host(URL_ZU_MZ) == ("anjuke", "mz")


def test_parse_host_returns_none_pair_when_no_city_subdomain():
    """不可解析必须显式返回 (None, None)，好让调用方分清「省外」与「不知道」。"""
    assert parse_host("") == (None, None)
    assert parse_host(None) == (None, None)
    # 只有二级域名：不能把 'anjuke' 当成城市
    assert parse_host("https://anjuke.com/prop/view/S1") == (None, None)
    # 非目标站点
    assert parse_host("https://www.example.com/prop/view/S1") == (None, None)
    assert parse_host("not a url at all") == (None, None)


# ----------------------------------------------------------------------------
# resolve_city：三层兜底
# ----------------------------------------------------------------------------


def test_resolve_city_prefers_url_subdomain_over_filename():
    """1184 键那种「子域≠文件名城市」：以子域为准重写归属，不是保留文件名。"""
    assert resolve_city(URL_SZ, "gz") == ("sz", R_URL_HIT_OVER_FILENAME)
    # 一致时不该报冲突
    assert resolve_city(URL_GZ, "gz") == ("gz", R_URL_HIT)
    assert resolve_city(URL_GZ) == ("gz", R_URL_HIT)


def test_resolve_city_foreign_subdomain_is_dropped_not_reassigned():
    """核心负例：省外键落到省外桶，**不许** fallback 回文件名城市。"""
    city, reason = resolve_city(URL_NANTONG, "gz")
    assert city == "nantong"  # 不是 'gz'
    assert reason == R_URL_FOREIGN


def test_resolve_city_58_hosts_including_the_264_real_guangdong_rows():
    """实测 484 行 `*.58.com` 曾被判不可解析，其中 fs/gz 共 264 行是真广东。

    少了 58 解析这层，方案 A 会把这 264 行误杀进 unknown 桶。
    """
    assert resolve_city(URL_58_FS, "fs") == ("fs", R_URL_HIT)
    assert resolve_city(URL_58_GZ, "gz") == ("gz", R_URL_HIT)
    # 58 短码里非广东的那部分仍按省外桶存，不静默并进广东
    assert resolve_city(URL_58_SU, "gz") == ("su", R_URL_FOREIGN)
    assert resolve_city(URL_58_NB, "gz") == ("nb", R_URL_FOREIGN)


def test_resolve_city_zu_anjuke_maps_subdomain_to_city():
    assert resolve_city(URL_ZU_GZ, "gz") == ("gz", R_URL_HIT)
    assert resolve_city(URL_ZU_MZ, "mz") == ("mz", R_URL_HIT)


def test_resolve_city_unparsed_and_empty_url_fall_back_to_unknown_with_reason():
    assert resolve_city("", "gz") == (UNKNOWN_CITY, R_EMPTY_URL)
    assert resolve_city(None, "gz") == (UNKNOWN_CITY, R_EMPTY_URL)
    assert resolve_city("https://www.example.com/x", "gz") == (UNKNOWN_CITY, R_UNPARSED_HOST)
    assert resolve_city("not a url at all", "gz") == (UNKNOWN_CITY, R_UNPARSED_HOST)


def test_resolve_city_is_total_for_any_input():
    """照 make_url_key 的 total 口径：不抛异常、不丢行、不返回 None。"""
    nasty = [
        None,
        "",
        "  ",
        "not a url",
        "://",
        "http://",
        "https://[::1",
        "ftp://gz.anjuke.com/x",
        "HTTPS://GUANGZHOU.ANJUKE.COM/prop/view/S1",  # 大小写/协议归一化
        "https://gz.anjuke.com:8080/prop/view/S1",  # 带端口
        12345,
        [],
        {},
        object(),
        b"https://gz.anjuke.com/prop/view/S1",
    ]
    for url in nasty:
        city, reason = resolve_city(url)
        assert isinstance(city, str) and city, f"{url!r} → city={city!r}"
        assert isinstance(reason, str) and reason, f"{url!r} → reason={reason!r}"


def test_whitelist_is_the_same_21_cities_etl_admits():
    """白名单唯一数据源是 worker.CITY_SUBDOMAIN；与 etl.CITY_NAMES 漂移即归属口径分裂。"""
    spec = importlib.util.spec_from_file_location(
        "etl_under_city_attribution_test", os.path.join(ORCH_DIR, "etl.py")
    )
    etl = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = etl
    spec.loader.exec_module(etl)

    assert set(city_subdomain()) == set(etl.CITY_NAMES)
    assert len(subdomain_to_code()) == 42  # 全拼 ∪ 码


# ----------------------------------------------------------------------------
# etl.load_rows：归属落地到 (city,type) 桶与 district 列
# ----------------------------------------------------------------------------


def _write_jsonl(raw_dir, rel, rows):
    path = os.path.join(raw_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _row(url, **extra):
    r = {"url": url, "title": "三居", "community": "某小区", "district": "gz"}
    r.update(extra)
    return r


@pytest.fixture(scope="module")
def etl():
    spec = importlib.util.spec_from_file_location(
        "etl_load_rows_city_test", os.path.join(ORCH_DIR, "etl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_load_rows_buckets_by_resolved_city_and_writes_district(tmp_path, etl):
    """gz 文件里的 sz 行进 sz 桶；省外行进省外桶；畸行进 unknown 桶 —— 且不进 gz。"""
    raw = str(tmp_path / "raw")
    _write_jsonl(
        raw,
        "ds=2026-09-02/gz_sale_worker-1.jsonl",
        [_row(URL_SZ), _row(URL_NANTONG), _row("https://www.example.com/x"), _row(URL_GZ)],
    )

    rows_by_task, raw_count = etl.load_rows(["ds=2026-09-02/gz_sale_worker-1.jsonl"], raw)

    assert raw_count == 4  # 重分桶不许丢行
    assert set(rows_by_task) == {
        ("sz", "sale"),
        ("nantong", "sale"),
        ("unknown", "sale"),
        ("gz", "sale"),
    }
    by_city = {c: r for (c, _t), r in rows_by_task.items()}
    # DWD 没有 city 列，城市码就落在 district（worker 的 district_tag=city）
    assert by_city["sz"][0]["district"] == "sz"
    assert by_city["nantong"][0]["district"] == "nantong"
    assert by_city["unknown"][0]["district"] == UNKNOWN_CITY


def test_load_rows_keeps_filename_whitelist_and_page_evidence(tmp_path, etl):
    """文件名城市码仍要拦非法文件名（它决定「派了什么活」），且页码证据不受影响。"""
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_worker-1.jsonl", [_row(URL_GZ, _page=7)])
    _write_jsonl(raw, "ds=2026-09-02/nowhere_sale_worker-1.jsonl", [_row(URL_GZ)])

    rows_by_task, raw_count = etl.load_rows(
        ["ds=2026-09-02/gz_sale_worker-1.jsonl", "ds=2026-09-02/nowhere_sale_worker-1.jsonl"], raw
    )

    assert list(rows_by_task) == [("gz", "sale")]
    assert raw_count == 1
    assert rows_by_task[("gz", "sale")][0]["last_seen_page"] == 7


def test_geocode_buckets_skips_foreign_and_unknown_cities(etl):
    """省外/unknown 桶不许进 geocode：community_coords 按 (city,community) 隔离，
    拿省外城市码去查只会登记一批永远查不到的 pending，污染跨城词典。"""
    calls = []

    class _Geocoder:
        def geocode(self, community, title, city=None):
            calls.append(city)
            return (None, None)

    def _r(district):
        return [{"community": "某小区", "district": district, "latitude": None, "longitude": None}]

    rows_by_task = {
        ("gz", "sale"): _r("gz"),
        ("nantong", "sale"): _r("nantong"),
        ("unknown", "sale"): _r("unknown"),
    }

    attempted, hit = etl.geocode_buckets(rows_by_task, _Geocoder(), {})

    assert calls == ["gz"]  # 只有 21 城桶被问过
    assert attempted == 1 and hit == 0


def test_schema_widens_city_code_for_foreign_pinyin():
    """省外全拼最长实测 12 字符（shuangyashan），Doris 列窄了会静默截断。"""
    lake_dir = os.path.join(ORCH_DIR, "..", "lake")
    spec = importlib.util.spec_from_file_location(
        "lake_schema_city_test", os.path.join(lake_dir, "schema.py")
    )
    schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema)

    for tbl in ("dwd_housing_sale", "dws_city_price_stats", "ads_city_avg_price"):
        cols = dict(
            (c, t)
            for c, t, _ in schema.DWD_TABLES.get(
                tbl, schema.DWS_TABLES.get(tbl, schema.ADS_TABLES.get(tbl))
            )["columns"]
        )
        assert cols["city_code"].startswith("VARCHAR(")
        assert int(cols["city_code"][len("VARCHAR(") : -1]) >= 32, f"{tbl}.city_code 太窄"
