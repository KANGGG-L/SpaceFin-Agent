#!/usr/bin/env python3
"""
ETL 后置脚本：处理全量测试的 raw JSONL → 去重 → 补地理编码 → 输出 CSV + 评测报告。

拉取阶段 worker 只做轻量解析（跳过 geocode，经纬度为 None）；本脚本在**所有数据
拉取完成后**统一执行：
1. 读 `output/guangdong/raw/*.jsonl`（每 worker 一个文件，含重复）；
2. 按 URL 去重合并（幂等，重跑安全）；
3. 补地理编码（真实 LocalGeocoder）；
4. 按 city:type 输出每城 CSV + 汇总 CSV；
5. 输出 etl_report.json（正确性 + 性能评测）。

用法：
    python tools/orchestrator/etl.py [--raw-dir output/guangdong/raw] [--out-dir output/guangdong]
"""

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from anjuke_crawler.geocoder import LocalGeocoder
from anjuke_crawler.parse import RENT_HEADERS, SCHEMA_HEADERS

CITY_NAMES = {
    "gz": "广州",
    "sz": "深圳",
    "zh": "珠海",
    "st": "汕头",
    "fs": "佛山",
    "sg": "韶关",
    "zj": "湛江",
    "zq": "肇庆",
    "jm": "江门",
    "mm": "茂名",
    "hui": "惠州",
    "mz": "梅州",
    "sw": "汕尾",
    "hy": "河源",
    "yj": "阳江",
    "qy": "清远",
    "dg": "东莞",
    "zs": "中山",
    "cz": "潮州",
    "jy": "揭阳",
    "yf": "云浮",
}
CITY_ORDER = list(CITY_NAMES.keys())


def _load_raw(raw_dir):
    """读所有 raw JSONL，返回 {(city,type): [rows]}。"""
    rows_by_task = {}
    total_lines = 0
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(raw_dir, fname)
        # 文件名格式: {city}_{type}_{worker_id}.jsonl
        parts = fname.split("_")
        if len(parts) < 2:
            continue
        city, typ = parts[0], parts[1]
        key = (city, typ)
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    row.pop("_source", None)  # 去掉拉取阶段附加字段
                    rows_by_task.setdefault(key, []).append(row)
                    total_lines += 1
                except json.JSONDecodeError:
                    continue
    return rows_by_task, total_lines


def _dedup(rows):
    """按 URL 去重（无 URL 的按 title+community 去重）。"""
    seen = set()
    out = []
    for r in rows:
        key = r.get("url") or f"{r.get('title')}_{r.get('community')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _field_completeness(rows, headers):
    """每字段非空率。"""
    n = len(rows)
    if n == 0:
        return {h: 0.0 for h in headers}
    return {h: round(sum(1 for r in rows if r.get(h) not in (None, "", 0)) / n, 4) for h in headers}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="output/guangdong/raw")
    ap.add_argument("--out-dir", default="output/guangdong")
    ap.add_argument("--geocoder-db", default=None, help="LocalGeocoder db 路径（可选）")
    args = ap.parse_args()

    t0 = time.time()
    os.makedirs(args.out_dir, exist_ok=True)
    geocoder = LocalGeocoder()
    if args.geocoder_db and os.path.exists(args.geocoder_db):
        try:
            with open(args.geocoder_db, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if isinstance(v, list) and len(v) == 2:
                    geocoder.add(k, v[0], v[1])
        except Exception as e:
            print(f"[etl] geocoder db load warn: {e}")

    rows_by_task, total_raw = _load_raw(args.raw_dir)
    print(f"[etl] raw 行数: {total_raw}（含重复），任务数: {len(rows_by_task)}")

    # 按 city:type 去重 + 补 geocode
    report = {
        "input_raw": total_raw,
        "tasks": {},
        "summary": {},
    }
    geocode_hits = 0
    geocode_total = 0
    unique_total = 0

    all_sale = {}
    all_rent = {}
    t_geocode0 = time.time()
    for key, rows in sorted(rows_by_task.items()):
        city, typ = key
        unique = _dedup(rows)
        headers = SCHEMA_HEADERS if typ == "sale" else RENT_HEADERS
        # 补地理编码
        for r in unique:
            if r.get("latitude") is None or r.get("longitude") is None:
                lat, lng = geocoder.geocode(r.get("community", ""), r.get("title", ""))
                r["latitude"], r["longitude"] = lat, lng
                geocode_total += 1
                if lat is not None:
                    geocode_hits += 1
        unique_total += len(unique)
        report["tasks"][f"{city}:{typ}"] = {
            "raw": len(rows),
            "unique": len(unique),
            "dup": len(rows) - len(unique),
            "dup_rate": round((len(rows) - len(unique)) / len(rows), 4) if rows else 0,
            "completeness": _field_completeness(unique, headers),
        }
        if typ == "sale":
            all_sale[city] = unique
        else:
            all_rent[city] = unique

    t_geocode1 = time.time()
    geocode_time = t_geocode1 - t_geocode0

    # 输出每城 CSV（sale / fangyuan 分开）
    for city in CITY_ORDER:
        for typ, store, headers in (
            ("sale", all_sale, SCHEMA_HEADERS),
            ("fangyuan", all_rent, RENT_HEADERS),
        ):
            rows = store.get(city, [])
            fpath = os.path.join(args.out_dir, f"{city}_{typ}_proxy.csv")
            with open(fpath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in headers})

    # 汇总 CSV
    sale_total = sum(len(v) for v in all_sale.values())
    rent_total = sum(len(v) for v in all_rent.values())
    with open(
        os.path.join(args.out_dir, "guangdong_all_cities.csv"),
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA_HEADERS)
        writer.writeheader()
        for city in CITY_ORDER:
            for r in all_sale.get(city, []):
                writer.writerow({k: r.get(k, "") for k in SCHEMA_HEADERS})

    elapsed = time.time() - t0
    report["summary"] = {
        "sale_unique": sale_total,
        "rent_unique": rent_total,
        "total_unique": unique_total,
        "total_raw": total_raw,
        "overall_dup_rate": round((total_raw - unique_total) / total_raw, 4) if total_raw else 0,
        "geocode_attempts": geocode_total,
        "geocode_hits": geocode_hits,
        "geocode_hit_rate": round(geocode_hits / geocode_total, 4) if geocode_total else 0,
        "elapsed_sec": round(elapsed, 2),
        "throughput_rows_per_sec": round(unique_total / elapsed, 2) if elapsed else 0,
        "geocode_time_sec": round(geocode_time, 2),
    }

    with open(os.path.join(args.out_dir, "etl_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 60)
    print("[etl] 完成")
    print(f"  sale 唯一: {sale_total} 条")
    print(f"  fangyuan 唯一: {rent_total} 条")
    print(
        f"  总唯一: {unique_total} 条 / raw {total_raw} 行（重复率 {report['summary']['overall_dup_rate']}）"
    )
    print(f"  geocode 命中率: {report['summary']['geocode_hit_rate']}")
    print(f"  耗时 {elapsed:.1f}s，吞吐 {report['summary']['throughput_rows_per_sec']} 条/s")
    print(f"  报告: {os.path.join(args.out_dir, 'etl_report.json')}")


if __name__ == "__main__":
    main()
