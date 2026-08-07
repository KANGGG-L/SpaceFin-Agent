"""url_key.py 单测：URL 规范化去重键的五种形态与边界。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from url_key import make_url_key


def test_sale_anjuke_strips_query():
    url = "https://guangzhou.anjuke.com/prop/view/S4656241945109512?auction=201&stats_key=abc&from=from_esf_List_screen&index=1"
    assert make_url_key(url) == "sale:anjuke:S4656241945109512"


def test_sale_anjuke_same_house_different_query_same_key():
    base = "https://guangzhou.anjuke.com/prop/view/S4656241945109512"
    assert make_url_key(base + "?a=1") == make_url_key(base + "?b=2&c=3")
    assert make_url_key(base + "?a=1") == make_url_key(base)


def test_sale_58():
    url = "https://gz.58.com/xinfang/huxing/59071435-897433.html?soj_info=%7B%22x%22%3A1%7D"
    assert make_url_key(url) == "sale:58:59071435-897433"


def test_rent_zu():
    url = "https://gz.zu.anjuke.com/fangyuan/4688937808519174"
    assert make_url_key(url) == "rent:zu:4688937808519174"


def test_rent_gfangyuan_normalized_to_zu():
    url = "https://mz.zu.anjuke.com/gfangyuan/2580079256313869?isauction=6&shangquan_id=29864&legoFeeUrl=https%3A%2F%2Flegoclick.58.com"
    assert make_url_key(url) == "rent:zu:2580079256313869"


def test_unknown_falls_back_to_md5():
    url = "https://example.com/unknown/foo/bar"
    key = make_url_key(url)
    assert key.startswith("md5:")
    assert len(key) == 4 + 32  # "md5:" + 32 hex


def test_empty_url_returns_md5():
    key = make_url_key("")
    assert key.startswith("md5:")
    assert len(key) == 4 + 32


def test_sale_58_vs_anjuke_not_merged():
    # 同城 sale 任务里 58 新房与安居客二手是不同房源，不得合并
    k58 = make_url_key("https://gz.58.com/xinfang/huxing/59071435-897433.html")
    kanjuke = make_url_key("https://guangzhou.anjuke.com/prop/view/S59071435897433")
    assert k58 != kanjuke
    assert k58.startswith("sale:58:")
    assert kanjuke.startswith("sale:anjuke:")


def test_key_length_within_varchar64():
    urls = [
        "https://guangzhou.anjuke.com/prop/view/S4656241945109512?long=query",
        "https://gz.58.com/xinfang/huxing/59071435-897433.html?soj=1",
        "https://gz.zu.anjuke.com/fangyuan/4688937808519174",
        "https://mz.zu.anjuke.com/gfangyuan/2580079256313869?x=1",
        "https://example.com/unknown/" + "a" * 300,
        "",
    ]
    for u in urls:
        assert len(make_url_key(u)) < 64, make_url_key(u)
