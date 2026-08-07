"""
安居客二手房数据采集包

本项目自建的数据源探索组件：为论证"公开房源能否稳定爬取"，针对安居客反爬网关
逐阶段攻克，沉淀为可复现的采集链路。按职责分包：

    fetch/              抓取层
        fetcher.py        阶段三——curl_cffi TLS/JA3 指纹伪装 + Session 预热
        stealth.py        阶段三+——DrissionPage 持久化 profile 会话重放 + 代理自动切换
        proxy.py          阶段四——动态代理池 REST 客户端（应对 IP 频次限制）
    parse/              解析层
        numeric.py        纯数值 17 字段 schema（XPath 卡片去重 + regex 兜底抽取）
        advanced.py       18 字段增强 schema（额外含户型串/车位描述）
    geocoder.py         地理编码——本地离线 O(1) 哈希，规避在线编码 200ms/次延迟
    distributed/        分布式层（阶段六）——Redis 任务队列 + asyncio 多 worker 横向扩展
        producer.py / worker.py / exporter.py
    main.py             CLI——parse / parse-advanced / geocode / crawl / stealth
    k8s_manifests/      Redis + Worker 副本集 k8s 清单
    experiments/        实验归档——阶段一/二各反爬绕过手段的尝试脚本与结论

反爬演进的完整论证见 docs/tech/components/anjuke-crawler.md 与
docs/poc/data-source/anjuke-exploratory/README.md。

⚠️ 合规红线（docs/product/01/数据现状摸底.md §3.3）：
- 公开房产/空间数据：以爬虫获取，**数据源锁定安居客**，本包即其采集实现；
- 个人贷款数据：一律合成 populate，绝不采集真实个人信息。
安居客 TOS 限制批量抓取、绕过反爬网关存在法律风险（已知悉并接受）——缓解：控制频次、
代理轮换、仅采公开非 PII 数据、投产前法律审查。抓取产物落 output/（已 gitignore）。
"""

from .fetch import Fetcher, ProxyClient, scrape_listing, scrape_listing_to_records
from .geocoder import LocalGeocoder
from .parse import (
    parse_advanced_housing_data,
    parse_numeric_schema_housing,
    save_advanced_csv,
    save_numeric_schema_csv,
)

__version__ = "1.0.0"
__all__ = [
    "LocalGeocoder",
    "parse_numeric_schema_housing",
    "save_numeric_schema_csv",
    "parse_advanced_housing_data",
    "save_advanced_csv",
    "ProxyClient",
    "Fetcher",
    "scrape_listing",
    "scrape_listing_to_records",
]
