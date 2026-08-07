#!/usr/bin/env python3

"""
集群任务生产者

把 城市 × 页 的抓取任务序列化后推入 Redis 列表 anjuke_task_queue，供 worker 消费。
默认广东 5 城（gz/sz/fs/dg/zh），可用 --cities / --pages 覆盖。
"""

import argparse
import json
import os

import redis

from ..fetch.fetcher import CITY_SUBDOMAIN

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))


def seed_cluster_tasks(cities=None, pages=3, redis_host=REDIS_HOST, redis_port=REDIS_PORT):
    cities = cities or ["gz", "sz", "fs", "dg", "zh"]
    print(f"[+] Connecting to Redis Task Queue at {redis_host}:{redis_port}...")
    r = redis.Redis(host=redis_host, port=redis_port, db=0)

    tasks = []
    for city in cities:
        sub = CITY_SUBDOMAIN[city]
        for p in range(1, pages + 1):
            url = f"https://{sub}.anjuke.com/sale/p{p}/"
            tasks.append(json.dumps({"city": city, "district": city, "page": p, "url": url}))

    r.delete("anjuke_task_queue")
    for t in tasks:
        r.rpush("anjuke_task_queue", t)

    queue_len = r.llen("anjuke_task_queue")
    print(f"[+] Seeded {queue_len} distributed tasks into 'anjuke_task_queue'!")
    return queue_len


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", default="gz,sz,fs,dg,zh")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()
    seed_cluster_tasks(
        cities=[c.strip() for c in args.cities.split(",") if c.strip()], pages=args.pages
    )
