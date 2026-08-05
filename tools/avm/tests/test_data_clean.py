"""data_clean.py：四层外市清洗规则 + title 归一小区名。

本文件只测纯函数（detect_foreign / url_city_code / parse_community_from_title），
不调用 clean_rows_with_stats（它会重写并落盘 community_vocab.json，测试不能碰
仓库产物）。
"""

import pytest
from avmmods import data_clean as dc


def _df(
    district,
    community=None,
    title=None,
    lat=None,
    lng=None,
    up=None,
    url=None,
    price_caps=None,
    price_floors=None,
):
    """detect_foreign 的简写。"""
    return dc.detect_foreign(
        district,
        community,
        title,
        lat,
        lng,
        up,
        price_caps=price_caps,
        price_floors=price_floors,
        url=url,
    )


# ================================================================ ① 坐标围栏


@pytest.mark.parametrize(
    ("lat", "lng", "why"),
    [
        (41.8, 116.4, "北京纬度，在广东围栏外"),
        (22.0, 105.0, "广西方向（lng < 109.4）"),
    ],
)
def test_fence_rejects_external_coords(lat, lng, why):
    assert _df("zs", lat=lat, lng=lng) == "coord_outside_gd", why


@pytest.mark.parametrize(
    ("lat", "lng", "why"),
    [
        (23.13, 113.32, "广州市中心"),
        (19.9, 117.6, "南/东界值（含边界，inclusive）"),
    ],
)
def test_fence_keeps_guangdong_coords(lat, lng, why):
    assert _df("gz", lat=lat, lng=lng) is None, why


def test_fence_skipped_when_coords_incomplete():
    """坐标缺任一即跳过围栏层，不误杀（继续走后续层判定）。"""
    assert _df("gz", title="天河区某小区三房", lat=23.13, lng=None) is None


# ================================================================ ② URL 子域城市


@pytest.mark.parametrize(
    ("district", "url", "expected", "why"),
    [
        ("zs", "https://beijing.anjuke.com/fang/123", "url_mismatch:bj", "北京页面错标成 zs"),
        ("dg", "http://deyang.58.com/", "url_mismatch:dy", "四川德阳 58 页面错标成 dg"),
        ("zs", "https://zhongshan.anjuke.com/", None, "子域 == 标签城市，本地"),
    ],
)
def test_url_subdomain_detects_external_city(district, url, expected, why):
    assert _df(district, url=url) == expected, why


def test_url_www_no_signal_does_not_kill():
    """www/m 等通用子域解析不出城市码 → 无信号，不误杀。"""
    assert dc.url_city_code("https://www.anjuke.com/") is None
    assert _df("gz", url="https://www.anjuke.com/") is None


# ================================================================ ③ 文字标记


@pytest.mark.parametrize(
    ("district", "title", "expected", "why"),
    [
        ("zs", "海淀区中关村附近小区急售", "text:海淀", "北京区县名"),
        ("dg", "集中供暖大三居 南北通透", "text:集中供暖", "北方专属词"),
    ],
)
def test_text_markers_reject_external_listings(district, title, expected, why):
    assert _df(district, title=title) == expected, why


@pytest.mark.parametrize(
    ("district", "title", "why"),
    [
        ("jy", "保利壹号公馆望京灶大桥", "望京 例外「望京灶」"),
        ("mm", "茂名北京东二路 三房", "北京东 例外「北京东二路」"),
        ("gz", "天河区北京路地铁口三房", "北京路（广州）无对应标记不误杀"),
    ],
)
def test_text_markers_exceptions_keep_guangdong_locals(district, title, why):
    assert _df(district, title=title) is None, why


# ================================================================ ④ 城市价格上/下限


@pytest.mark.parametrize(
    ("district", "up", "expected", "why"),
    [
        ("yf", 20000.0, "price_over_15000", "云浮 >1.5 万天花板"),
        ("sz", 3000.0, "price_below_12000", "深圳 <1.2 万地板"),
    ],
)
def test_price_bounds_reject_external_high_low(district, up, expected, why):
    assert _df(district, up=up) == expected, why


@pytest.mark.parametrize(
    ("district", "up", "why"),
    [
        ("gz", 50000.0, "广州 5 万在区间内"),
        ("yf", 10000.0, "云浮 1 万在区间内"),
        ("gz", None, "无单价不走价格层"),
    ],
)
def test_price_bounds_keep_local_prices(district, up, why):
    assert _df(district, up=up) is None, why


def test_price_bounds_respect_custom_caps_floors():
    """调用方（训练/服务）可注入自定义上下限，判定逻辑不变；上限先于下限。"""
    assert _df("gz", up=50000.0, price_caps={"gz": 40000.0}) == "price_over_40000"
    assert _df("gz", up=3000.0, price_floors={"gz": 4500.0}) == "price_below_4500"


# ================================================================ 层间优先级


@pytest.mark.parametrize(
    ("district", "title", "lat", "lng", "up", "url", "expected", "why"),
    [
        (
            "zs",
            None,
            41.8,
            116.4,
            20000.0,
            "https://beijing.anjuke.com/",
            "coord_outside_gd",
            "围栏最先，URL 其次",
        ),
        (
            "zs",
            "海淀区中关村小区",
            None,
            None,
            20000.0,
            "https://beijing.anjuke.com/",
            "url_mismatch:bj",
            "URL 先于文字标记",
        ),
        (
            "yf",
            None,
            None,
            None,
            20000.0,
            "https://beijing.anjuke.com/",
            "url_mismatch:bj",
            "URL 先于价格上限",
        ),
    ],
)
def test_layer_priority_order(district, title, lat, lng, up, url, expected, why):
    assert _df(district, title=title, lat=lat, lng=lng, up=up, url=url) == expected, why


# ================================================================ title 归一小区名


@pytest.mark.parametrize(
    ("title", "expected", "why"),
    [
        ("宏天广场中心区采光好 242 平方 5 房 2 厅", "宏天广场", "双字尾缀「广场」"),
        ("海博熙泰三期 江景房 三房", "海博熙泰", "XX三期 期数模式"),
        ("新收紫麟城二期 三房两厅", "紫麟城", "期数模式剥营销前缀「新收」"),
        ("万科启城家园 三房两厅", "万科启城", "尾缀变体「家园」归一"),
    ],
)
def test_parse_community_extracts_community(empty_vocab, title, expected, why):
    assert dc.parse_community_from_title(title) == expected, why


@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("刚需小三居 大两居 望花园 拎包入住", "整句都是户型/描述词"),
        ("望花园", "纯描述词"),
        ("碧桂园 精装三房", "开发商品牌（精确命中黑名单）"),
        (None, "空标题"),
    ],
)
def test_parse_community_returns_none_for_non_community(empty_vocab, title, why):
    assert dc.parse_community_from_title(title) is None, why


def test_parse_community_uses_installed_vocab_as_fallback(empty_vocab):
    """尾缀规则解不出的专有名词走语料词典（gazetteer）路径。"""
    dc.install_name_vocab({"悦泰春天"}, {"悦泰春天": "悦泰春天"}, {"悦泰春天": 12})
    assert dc.parse_community_from_title("悦泰春天 3房2厅 精装") == "悦泰春天"


def test_parse_community_never_picks_description_with_vocab_installed(empty_vocab):
    """词典只装合法楼盘名时，纯描述标题依然解析不出小区名。

    真实语料词典由 build_name_vocab 统计生成，构词层就过滤了描述词
    （_vocab_cand_ok 拒绝含 DESC_WORDS 的串），噪音标签不会进入词典。
    """
    dc.install_name_vocab({"悦泰春天"}, {"悦泰春天": "悦泰春天"}, {"悦泰春天": 5})
    assert dc.parse_community_from_title("刚需小三居 大两居 拎包入住") is None
    assert dc.parse_community_from_title("望花园 精装修 三房") is None
