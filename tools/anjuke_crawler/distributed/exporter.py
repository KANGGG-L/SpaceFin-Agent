#!/usr/bin/env python3

"""
集群数据导出与监控

从 Redis hash anjuke_results 汇总去重后的记录，导出为 CSV（比 parser.SCHEMA_HEADERS
多一列 pod_name，标记记录来自哪个 worker Pod，便于分布式抓取的可观测性）。
"""

import csv
import json
import os

import redis

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

HEADERS = [
    "title",
    "community",
    "district",
    "bedrooms",
    "halls",
    "bathrooms",
    "area_sqm",
    "direction",
    "floor",
    "building_year",
    "building_age",
    "parking_count",
    "total_price_wan",
    "unit_price_yuan",
    "latitude",
    "longitude",
    "pod_name",
    "url",
]


def export_cluster_results(
    filename="anjuke_k8s_cluster_results.csv", redis_host=REDIS_HOST, redis_port=REDIS_PORT
):
    r = redis.Redis(host=redis_host, port=redis_port, db=0)

    remaining_tasks = r.llen("anjuke_task_queue")
    results_raw = r.hgetall("anjuke_results")

    print("=" * 64)
    print("  Kubernetes Cluster Distributed Crawling Status")
    print("=" * 64)
    print(f"[+] Remaining Queue Tasks: {remaining_tasks}")
    print(f"[+] Aggregated Unique Records: {len(results_raw)}")

    if not results_raw:
        print("[-] No records in cluster results.")
        return 0

    records = []
    for _, v in results_raw.items():
        try:
            records.append(json.loads(v.decode("utf-8")))
        except Exception:
            continue

    with open(filename, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k) for k in HEADERS})

    print(f"[+] Exported {len(records)} clean records into {filename}!\n")
    return len(records)


if __name__ == "__main__":
    export_cluster_results()
