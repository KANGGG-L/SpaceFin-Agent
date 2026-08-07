"""隐身抓取层测试：模块导入 + 配置 + 拦截检测。

DrissionPage 真实启动 Chrome 很慢且依赖显示环境，默认不做 live 启动；
设 ANJUKE_TEST_LIVE=1 才跑有头/无头冒烟（见末尾）。
"""

import os

import pytest
from anjuke_crawler.fetch import stealth
from anjuke_crawler.fetch.stealth import scrape_listing, scrape_listing_to_records


def test_stealth_importable():
    assert callable(scrape_listing)
    assert callable(scrape_listing_to_records)


def test_stealth_config_defaults():
    assert stealth.PROFILE_DIR.endswith("chrome_profile")
    assert "webdriver" in stealth.STEALTH_JS  # 反检测脚本含隐藏 webdriver


def test_is_blocked_logic():
    class FakePage:
        def __init__(self, url):
            self.url = url

    assert stealth._is_blocked(FakePage("https://callback.58.com/antibot/deny.do")) is True
    assert stealth._is_blocked(FakePage("https://security.anjuke.com/x")) is True
    assert stealth._is_blocked(FakePage("https://guangzhou.anjuke.com/sale/p1/")) is False


@pytest.mark.skipif(
    os.environ.get("ANJUKE_TEST_LIVE") != "1",
    reason="live Chrome 冒烟默认跳过（设 ANJUKE_TEST_LIVE=1 启用）",
)
def test_stealth_live_headless_smoke():
    html = scrape_listing("gz", 1, headless=True)
    assert isinstance(html, str)
