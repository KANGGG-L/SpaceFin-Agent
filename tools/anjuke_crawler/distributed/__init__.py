"""
分布式采集子包：Redis 任务队列 + asyncio 多 worker 横向扩展（对应反爬演进阶段六）。

任务流：producer 把 城市×页 任务推入 Redis 队列 → 多个 worker（k8s 副本集）用 asyncio
并行消费、抓取、解析，结果写入 Redis hash → exporter 汇总去重导出 CSV。
应对单 IP 频次限制：横向扩 worker + 每 Pod 轮换出口 IP。

注：本子包依赖 `redis`。为避免在仅做离线解析/单机抓取时强依赖 redis，这里用 PEP 562
惰性导入——只有真正访问 producer/worker/exporter 时才 import（届时需要已安装 redis）。
"""

_LAZY = {
    "seed_cluster_tasks": ".producer",
    "main_worker_loop": ".worker",
    "export_cluster_results": ".exporter",
}
__all__ = list(_LAZY)


def __getattr__(name):
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
