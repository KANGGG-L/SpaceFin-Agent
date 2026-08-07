"""
抓取子包：把房源列表页从安居客取回来的三种手段（对应反爬演进阶段）。

- fetcher  阶段三：curl_cffi TLS/JA3 指纹伪装 + Session 预热
- stealth  阶段三+：DrissionPage 持久化 profile 会话重放 + 代理自动切换
- proxy    阶段四：jhao104/proxy_pool 动态代理池 REST 客户端
"""

from .fetcher import CITY_SUBDOMAIN, Fetcher, FetchResult
from .proxy import ProxyClient
from .stealth import scrape_listing, scrape_listing_to_records

__all__ = [
    "ProxyClient",
    "Fetcher",
    "FetchResult",
    "CITY_SUBDOMAIN",
    "scrape_listing",
    "scrape_listing_to_records",
]
