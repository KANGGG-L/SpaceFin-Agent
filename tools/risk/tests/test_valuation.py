"""估值层（valuation.py）：城市码解析、DWD 行情估值、AVM 守卫、血缘版本号。

本文件不加载 tools/avm 的真实模型：`valuation._avm_module` 一律用替身 monkeypatch，
只验证 valuation.py 自己的守卫与回退语义。
"""

import pytest
from riskmods import FakeConn, config, valuation

CITY_MAP = config.CITY_MAP


# ================================================================ 城市码解析


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("广州市天河区体育西路 1 号", "gz"),
        ("深圳市南山区科技园", "sz"),
        ("佛山市禅城区", "fs"),
        ("云浮市云城区", "yf"),
    ],
)
def test_city_code_extracted_from_guangdong_address(addr, expected):
    assert valuation._city_code_from_addr(addr, CITY_MAP) == expected


@pytest.mark.parametrize("addr", ["", None, "上海市浦东新区世纪大道", "合成地址-0421"])
def test_non_guangdong_address_yields_no_city_code(addr):
    """业务库当前是上海合成地址——取不到城市码正是 AVM/DWD 双双未命中的根因。"""
    assert valuation._city_code_from_addr(addr, CITY_MAP) is None


def test_city_match_is_first_key_in_map_not_leftmost_in_address():
    """⚠️ 已知脆弱点：纯子串匹配，谁在 CITY_MAP 里排前面谁赢。

    「阳江市江门路」的真实城市是阳江(yj)，但 CITY_MAP 里「江门」排在「阳江」之前，
    于是被判成江门(jm)。真实广东地址里街道名撞城市名的情况存在，会把估值打到错误
    城市的行情上。这里钉住当前行为，避免无声变化；修法应是按匹配位置最靠左 /
    最长匹配优先，属业务代码改动，未在本轮处理。
    """
    assert list(CITY_MAP).index("江门") < list(CITY_MAP).index("阳江")
    assert valuation._city_code_from_addr("阳江市江门路 5 号", CITY_MAP) == "jm"


# ================================================================ 区域解析（DWD 键的一半）


def test_synthetic_address_is_excluded_from_district_parsing():
    """合成地址没有真实行政区，硬解会造出假键。"""
    assert valuation._district_from_addr("合成地址上海市浦东新区") is None


@pytest.mark.parametrize("addr", ["", None, "广州市天河体育中心"])
def test_address_without_district_marker_yields_none(addr):
    assert valuation._district_from_addr(addr) is None


def test_district_parsing_strips_city_prefix_to_match_dwd_keys():
    """曾经的缺陷：旧正则 `([\\u4e00-\\u9fa5]{1,8}?区)` 从地址开头惰性展开，把「广州市」

    前缀一并吃进匹配，返回「广州市天河区」。DWD 键里的地名一律不带市名也不带「区」
    后缀（禅城 / 惠城 / 清城），两边永不相等 → DWD 这一级回退是死代码。
    """
    assert valuation._district_from_addr("广州市天河区体育西路 1 号") == "天河"


@pytest.mark.parametrize(
    ("addr", "expected", "why"),
    [
        ("佛山市禅城区金色家园38号", "禅城", "常规「X市Y区」"),
        ("惠州市大亚湾区翡翠绿洲144号", "大亚湾", "三字区名"),
        ("东莞市万江街道绿地国际花都189号", "万江", "直筒子市的街道级地名"),
        ("江门市台山市某路", "台山", "县级市后缀同样要剥"),
        ("天河区体育西路", "天河", "地址不带市名前缀也要能解析"),
    ],
)
def test_district_parsing_strips_administrative_suffix(addr, expected, why):
    assert valuation._district_from_addr(addr) == expected, why


@pytest.mark.parametrize(
    ("addr", "expected", "why"),
    [
        ("中山市西区街道锦绣华庭186号", "西区", "剥成「西」不再是地名"),
        ("汕尾市城区水岸朗晴花园150号", "城区", "剥成「城」不再是地名"),
        ("梅州市梅县区某路", "梅县", "剥成「梅」不再是地名"),
    ],
)
def test_single_char_residue_keeps_administrative_suffix(addr, expected, why):
    """≥2 字守卫：剥完后缀只剩 1 个字说明后缀是地名的一部分，退回原 token。

    中国没有单字区名。这条守卫是为了不造出「西」「城」「梅」这类假地名去撞键，
    而不是为了凑命中——它对实测命中率没有任何贡献（这 3 个键 DWD 里都没有）。
    """
    assert valuation._district_from_addr(addr) == expected, why


# ================================================================ DWD 行情估值


def test_dwd_hit_values_unit_price_times_area():
    col = {"property_addr": "广州市天河区某路", "area": 80.0}
    dwd = {("gz", "天河"): 90_000.0}

    assert valuation.valuation_from_dwd(dwd, col, CITY_MAP) == 7_200_000.0


def test_dwd_key_shape_matches_loader_output():
    """回归钉子：解析侧产出的键必须与 load_dwd_unit_prices 的键同形。

    这两处曾经不同形（解析侧「广州市天河区」vs 加载侧「天河」），是 DWD 回退级
    成为死代码的直接原因。任一侧单独改动都会重新打破对齐。
    """
    addr = "广州市天河区某路"
    parsed_key = (
        valuation._city_code_from_addr(addr, CITY_MAP),
        valuation._district_from_addr(addr),
    )
    loaded = valuation.load_dwd_unit_prices(
        FakeConn().queue_result([("gz", "天河", 90_000.0)] * config.DWD_MIN_SAMPLES)
    )

    assert parsed_key in loaded


def test_dwd_miss_when_key_absent():
    col = {"property_addr": "广州市天河区某路", "area": 80.0}

    assert valuation.valuation_from_dwd({("sz", "南山"): 90_000.0}, col, CITY_MAP) is None


@pytest.mark.parametrize(
    ("addr", "area", "why"),
    [
        ("上海市浦东新区某路", 80.0, "城市码解析不出来"),
        ("广州市天河区某路", None, "面积缺失"),
        ("广州市天河区某路", 0, "面积为零"),
        ("广州市体育西路", 80.0, "地址里没有区"),
        ("", 80.0, "地址为空"),
    ],
)
def test_dwd_valuation_requires_all_preconditions(addr, area, why):
    """任一要素缺失都返回 None 交给下一级回退，而不是拿残缺输入硬算。"""
    col = {"property_addr": addr, "area": area}
    dwd = {("gz", "天河"): 90_000.0}

    assert valuation.valuation_from_dwd(dwd, col, CITY_MAP) is None, why


def test_zero_unit_price_treated_as_dwd_miss():
    """单价 0 是脏数据，用它算出的估值 0 会让下游 LTV 直接失真。"""
    col = {"property_addr": "广州市天河区某路", "area": 80.0}

    assert valuation.valuation_from_dwd({("gz", "天河"): 0.0}, col, CITY_MAP) is None


# ================================================================ DWD 行情加载（按 (城市,社区) 取中位数）


@pytest.fixture
def no_min_samples(monkeypatch):
    """样本量门槛置 1：只想验证聚合口径的用例不必堆 20 行造数。"""
    monkeypatch.setattr(config, "DWD_MIN_SAMPLES", 1)


def test_dwd_prices_aggregate_to_median_per_city_community(no_min_samples):
    conn = FakeConn().queue_result(
        [
            ("gz", "天河", 90_000.0),
            ("gz", "天河", 100_000.0),
            ("gz", "天河", 110_000.0),
            ("sz", "南山", 120_000.0),
        ]
    )

    out = valuation.load_dwd_unit_prices(conn)

    assert out[("gz", "天河")] == 100_000.0  # 中位数而非均值
    assert out[("sz", "南山")] == 120_000.0


def test_dwd_prices_skip_rows_without_community(no_min_samples):
    """社区为空无法构成 (城市,社区) 键，这类行只会污染桶。"""
    conn = FakeConn().queue_result(
        [("fs", None, 30_000.0), ("fs", "", 31_000.0), ("fs", "禅城", 32_000.0)]
    )

    out = valuation.load_dwd_unit_prices(conn)

    assert out == {("fs", "禅城"): 32_000.0}


def test_dwd_prices_drop_keys_below_min_samples(monkeypatch):
    """样本不足的键不是行情：库里 78% 的 (城市,社区) 只有 1 行。

    实测反例 (dg, 东城) 只有 1 行 57,066 元/㎡ 的同名小区，被当成区级行情用会把
    抵押物估到基准的 7.4 倍。门槛 20 与 tools/spatial 的 MIN_ZONE_SAMPLES 同源。
    """
    monkeypatch.setattr(config, "DWD_MIN_SAMPLES", 3)
    conn = FakeConn().queue_result(
        [("dg", "东城", 57_066.0)]  # 1 行 → 丢弃
        + [
            ("fs", "禅城", 13_000.0),
            ("fs", "禅城", 13_400.0),
            ("fs", "禅城", 14_000.0),
        ]  # 3 行 → 保留
    )

    out = valuation.load_dwd_unit_prices(conn)

    assert ("dg", "东城") not in out
    assert out == {("fs", "禅城"): 13_400.0}


def test_dwd_prices_empty_table_yields_empty_map():
    assert valuation.load_dwd_unit_prices(FakeConn().queue_result([])) == {}


# ================================================================ AVM 守卫


class _StubAvmModule:
    """tools/avm/predict 的替身。tools/avm 正被其它 agent 改动，测试不依赖真实实现。"""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def estimate_total_price(self, model, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def stub_avm(monkeypatch):
    def _install(**kw):
        stub = _StubAvmModule(**kw)
        monkeypatch.setattr(valuation, "_avm_module", lambda: stub)
        return stub

    return _install


def test_avm_hit_passes_city_code_and_area_through(stub_avm):
    stub = stub_avm(result=8_800_000.0)
    col = {"property_addr": "深圳市南山区某路", "area": 88.0, "age": 5, "lat": 22.5, "lng": 113.9}

    assert valuation.valuation_from_avm(object(), col, CITY_MAP) == 8_800_000.0
    assert stub.calls[0]["city_code"] == "sz"
    assert stub.calls[0]["area_sqm"] == 88.0
    # 地址粒度不够时不硬塞小区名，让模型回退城市中位价
    assert stub.calls[0]["community"] is None


def test_avm_not_called_when_model_absent(stub_avm):
    stub = stub_avm(result=1.0)
    col = {"property_addr": "深圳市南山区某路", "area": 88.0}

    assert valuation.valuation_from_avm(None, col, CITY_MAP) is None
    assert stub.calls == []


@pytest.mark.parametrize(
    ("addr", "area", "why"),
    [
        ("上海市浦东新区某路", 88.0, "无广东城市码时套全局中位价会让 LTV 失真"),
        ("深圳市南山区某路", None, "面积缺失"),
        ("深圳市南山区某路", 0, "面积为零"),
        ("深圳市南山区某路", -10, "面积为负"),
    ],
)
def test_avm_not_called_when_preconditions_unmet(stub_avm, addr, area, why):
    stub = stub_avm(result=1.0)
    col = {"property_addr": addr, "area": area}

    assert valuation.valuation_from_avm(object(), col, CITY_MAP) is None, why
    assert stub.calls == [], why


def test_avm_exception_degrades_to_miss_instead_of_crashing(stub_avm):
    """AVM 是可选增强项，它坏了应该退回 DWD/兜底价，不能让整批风险计算失败。"""
    stub_avm(raises=RuntimeError("model corrupted"))
    col = {"property_addr": "深圳市南山区某路", "area": 88.0}

    assert valuation.valuation_from_avm(object(), col, CITY_MAP) is None


def test_avm_import_failure_degrades_to_miss(monkeypatch):
    monkeypatch.setattr(valuation, "_avm_module", lambda: None)
    col = {"property_addr": "深圳市南山区某路", "area": 88.0}

    assert valuation.valuation_from_avm(object(), col, CITY_MAP) is None


# ================================================================ R-UBQ-01 血缘版本号


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ({"version": "2026-08-05-r1"}, "2026-08-05-r1"),
        ({"version": 3}, "3"),  # 非字符串版本号转字符串
        ({"version": None}, "unknown"),
        ({"version": ""}, "unknown"),
        ({}, "unknown"),
        (None, "unknown"),
        ("not-a-dict", "unknown"),  # 产物结构不符预期时不能瞎猜
        (object(), "unknown"),
    ],
)
def test_model_version_extraction_and_fallback(model, expected):
    assert valuation.model_version(model) == expected


def test_load_avm_model_returns_none_when_artifact_missing(monkeypatch, tmp_path):
    """模型文件不存在是常态（未训练环境），必须静默返回 None 而不是抛异常。"""
    monkeypatch.setattr(valuation.os.path, "exists", lambda p: False)

    assert valuation.load_avm_model() is None
