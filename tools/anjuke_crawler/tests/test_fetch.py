"""抓取层测试：ProxyClient 契约对接（mock proxy_pool）+ Fetcher 拦截检测逻辑。

不打真实安居客（避免网络变量与 IP 频次消耗）；网络抓取的正确性由 test_pipeline 的
编排 + fixture 解析覆盖。
"""

from anjuke_crawler.fetch import CITY_SUBDOMAIN, Fetcher, ProxyClient
from anjuke_crawler.fetch.fetcher import FetchResult

# ---- ProxyClient 对接 proxy_pool 契约 ----


def test_proxy_disabled_returns_none():
    pc = ProxyClient(api_base=None)
    assert pc.enabled is False
    assert pc.get_proxy() is None
    assert pc.pop_proxy() is None
    pc.delete_proxy({"http": "http://1.2.3.4:80"})  # 不应抛异常


def test_proxy_get_from_pool(mock_proxy_server):
    base, store = mock_proxy_server
    pc = ProxyClient(api_base=base)
    assert pc.enabled is True
    proxy = pc.get_proxy()
    assert proxy is not None
    assert proxy["http"].startswith("http://10.0.0.")
    assert proxy["https"] == proxy["http"]


def test_proxy_delete_removes(mock_proxy_server):
    base, store = mock_proxy_server
    pc = ProxyClient(api_base=base)
    pc.delete_proxy({"http": "http://10.0.0.1:8080"})
    remaining = store.zrange("proxies", 0, -1)
    assert b"10.0.0.1:8080" not in remaining


def test_proxy_pool_drain(mock_proxy_server):
    base, store = mock_proxy_server
    pc = ProxyClient(api_base=base)
    got = [pc.get_proxy() for _ in range(5)]  # 池里只有 3 个
    valid = [p for p in got if p]
    assert len(valid) == 3
    assert got[-1] is None  # 抽干后返回 None


# ---- Fetcher 拦截检测逻辑（纯函数，不发网络请求）----


def test_is_blocked_detects_deny():
    f = Fetcher()
    assert f._is_blocked("https://callback.58.com/antibot/deny.do?x", "<html></html>") is True


def test_is_blocked_detects_verifycode():
    f = Fetcher()
    assert f._is_blocked("https://callback.58.com/antibot/verifycode?x", "<html></html>") is True


def test_is_blocked_detects_gateway_div():
    f = Fetcher()
    html = '<div id="@@xxzlGatewayUrl">https://callback.58.com/antibot/deny.do</div>'
    assert f._is_blocked("https://guangzhou.anjuke.com/sale/p1/", html) is True


def test_is_blocked_passes_clean_listing():
    f = Fetcher()
    assert f._is_blocked("https://guangzhou.anjuke.com/sale/p1/", "<html>正常列表</html>") is False


def test_city_subdomain_mapping():
    assert CITY_SUBDOMAIN["gz"] == "guangzhou"
    assert CITY_SUBDOMAIN["sz"] == "shenzhen"
    assert len(CITY_SUBDOMAIN) >= 5  # 广东 5 城 + 其它


def test_fetch_result_blocked_flag():
    assert FetchResult(ok=False, status=200, final_url="x", html="").blocked is True
    assert FetchResult(ok=True, status=200, final_url="x", html="").blocked is False
