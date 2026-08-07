#!/usr/bin/env python3

"""
集群异步 Worker 节点

从 Redis 队列 lpop 任务 → curl_cffi(impersonate=chrome) 抓取 → parser 解析 →
hset 写入 Redis hash anjuke_results（以 url 去重）。asyncio 协程串行消费，
多 Pod 横向扩展靠 k8s 副本集（见 k8s_manifests/worker-deployment.yaml）。

注：本 worker 默认直连（不带 proxy）。生产可持续抓取应在 fetch 处接 ProxyClient
轮换 IP，以规避单 IP 频次限制。
"""

import asyncio
import json
import os

import redis
from curl_cffi import requests

from ..geocoder import LocalGeocoder
from ..parse import parse_numeric_schema_housing

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
POD_NAME = os.getenv("POD_NAME", "worker-pod-local")

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
geocoder = LocalGeocoder()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def fetch_task(session, task_payload):
    data = json.loads(task_payload)
    url = data["url"]
    district = data.get("district", data.get("city", ""))

    headers = {
        "User-Agent": UA,
        "Referer": "https://www.anjuke.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        response = await session.get(url, headers=headers, timeout=12)
        if response.status_code == 200 and "deny.do" not in response.url:
            records = parse_numeric_schema_housing(
                response.text, district_tag=district, geocoder=geocoder
            )
            if records:
                for record in records:
                    record["pod_name"] = POD_NAME
                    item_id = record["url"] or f"{record['title']}_{record['district']}"
                    r.hset("anjuke_results", item_id, json.dumps(record, ensure_ascii=False))
                print(f"[{POD_NAME}] Extracted {len(records)} items from {url}")
            else:
                print(f"[{POD_NAME}] Parsed 0 items from {url} (结构未命中或被软拦截)")
        else:
            print(f"[{POD_NAME}] Page blocked or error on {url} -> {response.url[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"[{POD_NAME}] Async fetch error on {url}: {e}")


async def main_worker_loop():
    print(f"[{POD_NAME}] Starting Asyncio Worker Loop (Redis {REDIS_HOST}:{REDIS_PORT})...")
    async with requests.AsyncSession(impersonate="chrome") as session:
        while True:
            task_raw = r.lpop("anjuke_task_queue")
            if not task_raw:
                await asyncio.sleep(2)
                if r.llen("anjuke_task_queue") == 0:
                    break
                continue
            await fetch_task(session, task_raw.decode("utf-8"))
            await asyncio.sleep(1)
    print(f"[{POD_NAME}] Queue drained, worker exit.")


if __name__ == "__main__":
    asyncio.run(main_worker_loop())
