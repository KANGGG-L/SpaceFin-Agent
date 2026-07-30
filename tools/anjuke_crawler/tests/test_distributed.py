"""分布式层测试：producer → worker → exporter 队列编排（fakeredis + mock 抓取）。

验证真实的 lpop/hset/hgetall/去重/导出代码路径；Redis 服务用 fakeredis 替身，
抓取层注入 fixture HTML 以隔离网络变量。
"""

import asyncio
import csv

import fakeredis
import redis
from anjuke_crawler.distributed import exporter, producer, worker


def _wire_fakeredis(monkeypatch, fixture_html):
    srv = fakeredis.FakeServer()
    conn = fakeredis.FakeRedis(server=srv)
    monkeypatch.setattr(redis, "Redis", lambda *a, **k: fakeredis.FakeRedis(server=srv))
    monkeypatch.setattr(worker, "r", conn)

    class FakeResp:
        status_code = 200
        url = "https://guangzhou.anjuke.com/sale/p1/"
        text = fixture_html

    async def fake_get(self, url, **kw):
        return FakeResp()

    monkeypatch.setattr(worker.requests.AsyncSession, "get", fake_get)
    return conn


def test_producer_seeds_tasks(monkeypatch, fixture_html):
    conn = _wire_fakeredis(monkeypatch, fixture_html)
    n = producer.seed_cluster_tasks(cities=["gz", "sz"], pages=2)
    assert n == 4
    assert conn.llen("anjuke_task_queue") == 4


def test_producer_task_payload(monkeypatch, fixture_html):
    import json

    conn = _wire_fakeredis(monkeypatch, fixture_html)
    producer.seed_cluster_tasks(cities=["gz"], pages=1)
    payload = json.loads(conn.lpop("anjuke_task_queue").decode())
    assert payload["city"] == "gz"
    assert "guangzhou.anjuke.com/sale/p1" in payload["url"]


def test_worker_consumes_and_aggregates(monkeypatch, fixture_html):
    conn = _wire_fakeredis(monkeypatch, fixture_html)
    producer.seed_cluster_tasks(cities=["gz", "sz"], pages=2)
    asyncio.run(worker.main_worker_loop())
    assert conn.llen("anjuke_task_queue") == 0  # 全部消费
    results = conn.hgetall("anjuke_results")
    assert len(results) == 71  # 4 任务各抓同 fixture，url 去重后 71


def test_exporter_writes_csv(monkeypatch, fixture_html, tmp_path):
    _wire_fakeredis(monkeypatch, fixture_html)
    producer.seed_cluster_tasks(cities=["gz"], pages=2)
    asyncio.run(worker.main_worker_loop())
    out = tmp_path / "cluster.csv"
    n = exporter.export_cluster_results(filename=str(out))
    assert n == 71
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert len(rows) == 71
    assert "pod_name" in rows[0]  # 分布式可观测列
