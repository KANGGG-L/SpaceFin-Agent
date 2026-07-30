#!/usr/bin/env python3
"""
端到端流水线演示：crawler → proxy_pool → 分布式队列（k8s 逻辑）

本脚本在**无真实 redis-server / k8s 集群 / 公网代理**的环境下，用替身把三段链路全部跑通，
证明各组件可拼装成完整流水线。替身与真实件的对应关系（诚实标注）：

| 段 | 真实件 | 本演示替身 | 为何用替身 |
|----|--------|-----------|-----------|
| 分布式队列 | Redis 服务 + k8s worker 副本集 | fakeredis（内存 Redis 协议实现） | 沙箱无 redis-server/编译器/sudo，也无 k8s 集群 |
| 代理池 | jhao104/proxy_pool（REST API + 真代理） | 复刻 /get//pop//delete/ 契约的 mock 服务 | 真实 proxy_pool 需常驻 Redis + 公网代理源 |
| 抓取层 | curl_cffi 真抓安居客 | 注入包内 fixture HTML | 隔离"每 IP 1 页"的网络变量，专注验证编排 |

运行：
    python -m anjuke_crawler.scripts.demo_pipeline
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import fakeredis
import redis

from anjuke_crawler.distributed import exporter, producer, worker
from anjuke_crawler.fetch import Fetcher, ProxyClient

FIXTURE_PATH = "anjuke_crawler/tests/sample_listing.html"


def segment_1_distributed_queue():
    print("\n" + "=" * 64)
    print(" 段 1/3 · 分布式队列（producer → worker → exporter）")
    print("=" * 64)
    srv = fakeredis.FakeServer()
    redis.Redis = lambda *a, **k: fakeredis.FakeRedis(server=srv)
    worker.r = fakeredis.FakeRedis(server=srv)

    fixture = open(FIXTURE_PATH, encoding="utf-8").read()

    class FakeResp:
        status_code, url, text = 200, "https://guangzhou.anjuke.com/sale/p1/", fixture

    async def fake_get(self, url, **kw):
        return FakeResp()

    worker.requests.AsyncSession.get = fake_get  # mock 抓取，隔离网络变量

    n = producer.seed_cluster_tasks(cities=["gz", "sz"], pages=2)
    print(f"[producer] 播种 {n} 个 城市×页 任务")
    asyncio.run(worker.main_worker_loop())
    print(
        f"[worker] 队列剩余={worker.r.llen('anjuke_task_queue')}，聚合去重={worker.r.hgetall('anjuke_results').__len__()}"
    )
    out = exporter.export_cluster_results(filename="/tmp/demo_dist.csv")
    print(f"[exporter] 导出 {out} 条 -> /tmp/demo_dist.csv")


def segment_2_proxy_pool():
    print("\n" + "=" * 64)
    print(" 段 2/3 · proxy_pool 代理池对接（REST 契约 + 用坏即扔）")
    print("=" * 64)
    store = fakeredis.FakeRedis()
    for p in ["127.0.0.9:8080", "127.0.0.9:8081"]:
        store.zadd("proxies", {p: 1.0})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith(("/get/", "/pop/")):
                p = store.zpopmax("proxies", 1)
                self._json({"proxy": p[0][0].decode()} if p else {})
            elif self.path.startswith("/delete/"):
                store.zrem("proxies", self.path.split("proxy=")[-1])
                print("    [mock proxy_pool] 剔除失效代理")
                self._json({"code": 0})
            else:
                self._json({})

    # 端口 0 = 系统分配空闲端口，避免与真实 proxy_pool（默认 :5010）冲突
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[mock proxy_pool] 起在 {base}（复刻 jhao104/proxy_pool 契约）")

    pc = ProxyClient(api_base=base)
    print(f"[ProxyClient] /get/ 取到代理: {pc.get_proxy()}")
    res = Fetcher(proxy_client=ProxyClient(api_base=base)).fetch_listing("gz", 1)
    print(
        f"[crawler via proxy] note={res.note}（127.0.0.9 非真代理→连接失败→自动 /delete/ 剔除，演示容错闭环）"
    )
    httpd.shutdown()


def segment_3_crawler_offline():
    print("\n" + "=" * 64)
    print(" 段 3/3 · 抓取层离线解析（17/18 字段双 schema）")
    print("=" * 64)
    from anjuke_crawler.parse import parse_advanced_housing_data, parse_numeric_schema_housing

    html = open(FIXTURE_PATH, encoding="utf-8").read()
    a = parse_numeric_schema_housing(html, "sh_pudong")
    b = parse_advanced_housing_data(html)
    print(f"[parser numeric]  17 字段：{len(a)} 条")
    print(
        f"[parser advanced] 18 字段：{len(b)} 条（样例 Layout={b[0]['Layout']} 车位={b[0]['Parking_Desc']}）"
    )


if __name__ == "__main__":
    print("安居客采集端到端流水线演示（替身环境，见文件头诚实标注）")
    segment_1_distributed_queue()
    segment_2_proxy_pool()
    segment_3_crawler_offline()
    print("\n[OK] 三段链路（分布式队列 / 代理池对接 / 抓取解析）全部跑通。")
    print("     真实部署：redis-server + jhao104/proxy_pool + k8s 集群（见 k8s_manifests/）。")
