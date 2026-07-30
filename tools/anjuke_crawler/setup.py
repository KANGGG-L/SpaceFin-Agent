from setuptools import find_packages, setup

setup(
    name="anjuke_crawler",
    version="1.0.0",
    description="Anjuke second-hand housing crawler built for SpaceFin Agent data-source "
    "exploration (curl_cffi anti-bot bypass + proxy_pool + k8s distributed). "
    "Technical PoC only.",
    packages=find_packages(),
    install_requires=[
        "curl_cffi>=0.5.0",  # 抓取层：TLS/JA3 指纹伪装
        "lxml>=4.9.0",  # 解析层：XPath 卡片抽取
        "redis>=4.5.0",  # 分布式层：任务队列 + 结果聚合
        "requests>=2.28.0",
        "DrissionPage>=4.0.0",  # 隐身抓取层：持久化 profile 会话重放（需本机 Chrome）
    ],
    python_requires=">=3.8",
)
