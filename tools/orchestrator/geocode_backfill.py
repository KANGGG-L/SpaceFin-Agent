#!/usr/bin/env python3
"""
DWD 坐标补全脚本（独立于 ETL，补全从 ETL 解耦——见 docs/tech/components/crawler-etl.md）。

背景：坐标补全依赖 community_coords 词典（由 geocode_fill.py 用腾讯 geocoder 缓慢填充，
6000/天）。若把补全内联在 ETL，则每次 ETL run 都要扫 DWD null 行，且新输入持续进来会
拖累主链路。本脚本将"补全 DWD"独立出来，可 cron/手动/Airflow 收尾调度，与 ETL 互不阻塞。

流程：
1. 读 community_coords 词典中 status='hit' 的 (city, community) -> (lat, lng)
2. 扫 DWD 两表 geocode_status IN ('pending','miss') 且 lat/lng 为 null 的行
   （miss 也要扫：geocode_fill 允许 miss 小区 7 天后重查并可能转 hit）
3. 用词典精确命中（city+community）→ 批量 UPDATE，置 geocode_status='hit'
4. 词典中明确 miss 的小区 → 该 DWD 行置 geocode_status='miss'（不再重复尝试）
5. 剩余（词典尚无记录）保持 pending，等 geocode_fill 补词典后再跑本脚本

用法：
    python geocode_backfill.py [--limit N] [--dry-run]
    # --limit 0 = 不限；--dry-run 只统计可补行不写库
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymysql
from etl import _mysql_params, load_env

_TABLE = {"sale": "crawl_housing_sale", "rent": "crawl_housing_rent"}
_HEADER_COMMUNITY = {"sale": "community", "rent": "community"}
_HEADER_DISTRICT = {"sale": "district", "rent": "district"}


def load_hit_dict(conn) -> dict:
    """读词典 hit：(city, community) -> (lat, lng)。"""
    d = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT city, community, lat, lng FROM community_coords "
            "WHERE status='hit' AND lat IS NOT NULL"
        )
        for city, community, lat, lng in cur.fetchall():
            d[(city, community)] = (float(lat), float(lng))
    return d


def load_miss_dict(conn) -> set:
    """读词典 miss：(city, community) 集合（查过但无结果，不再重试）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT city, community FROM community_coords WHERE status='miss'")
        return {(c, comm) for c, comm in cur.fetchall()}


def update_coords(conn, table: str, updates: list, chunk: int = 500) -> None:
    """批量写回坐标：多值 CASE WHEN 单语句更新一块，避免 executemany 逐条往返。

    updates: [(url_key, lat, lng), ...]；表名来自内部白名单 _TABLE，其余全部参数化。
    """
    for i in range(0, len(updates), chunk):
        part = updates[i : i + chunk]
        cases = " ".join(["WHEN %s THEN %s"] * len(part))
        marks = ",".join(["%s"] * len(part))
        sql = (
            f"UPDATE {table} SET "
            f"latitude = CASE url_key {cases} END, "
            f"longitude = CASE url_key {cases} END, "
            f"geocode_status='hit' "
            f"WHERE url_key IN ({marks})"
        )
        params = []
        for url_key, lat, _lng in part:
            params += [url_key, lat]
        for url_key, _lat, lng in part:
            params += [url_key, lng]
        params += [u[0] for u in part]
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def backfill_table(
    conn, typ: str, hit_dict: dict, miss_dict: set, limit: int, dry_run: bool
) -> dict:
    """补全一张 DWD 表。返回 {scanned, updated, marked_miss}。"""
    table = _TABLE[typ]
    comm_col = _HEADER_COMMUNITY[typ]
    dist_col = _HEADER_DISTRICT[typ]
    res = {"scanned": 0, "updated": 0, "marked_miss": 0}
    # 同时扫 miss 行：geocode_fill 允许 miss 小区 7 天后重查并可能转 hit，
    # 只扫 pending 会让"曾 miss 但词典现已 hit"的行永远补不上坐标。
    sql = (
        f"SELECT url_key, {dist_col}, {comm_col}, geocode_status FROM {table} "
        f"WHERE geocode_status IN ('pending','miss') "
        f"AND (latitude IS NULL OR longitude IS NULL)"
    )
    if limit > 0:
        sql += f" LIMIT {limit}"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    res["scanned"] = len(rows)

    updates = []
    miss_keys = []
    for url_key, district, community, status in rows:
        if district is None or community is None:
            continue
        key = (district, community)
        if key in hit_dict:
            lat, lng = hit_dict[key]
            updates.append((url_key, lat, lng))
        elif key in miss_dict and status != "miss":
            # 词典仍 miss：pending 行标 miss；已是 miss 的行保持不动
            miss_keys.append(url_key)
    res["updated"] = len(updates)
    res["marked_miss"] = len(miss_keys)

    if not dry_run:
        if updates:
            update_coords(conn, table, updates)
        if miss_keys:
            with conn.cursor() as cur:
                # 分批（避免 SQL 超长）
                for i in range(0, len(miss_keys), 1000):
                    chunk = miss_keys[i : i + 1000]
                    marks = ",".join(["%s"] * len(chunk))
                    cur.execute(
                        f"UPDATE {table} SET geocode_status='miss' WHERE url_key IN ({marks})",
                        chunk,
                    )
        conn.commit()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="每表最多处理 N 行（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只统计可补行不写库")
    args = ap.parse_args()

    env = load_env()
    conn = pymysql.connect(**_mysql_params(None, env), autocommit=False, charset="utf8mb4")

    hit_dict = load_hit_dict(conn)
    miss_dict = load_miss_dict(conn)
    print(f"[geocode_backfill] 词典 hit {len(hit_dict)} / miss {len(miss_dict)}")

    t0 = time.time()
    total = {"scanned": 0, "updated": 0, "marked_miss": 0}
    for typ in ("sale", "rent"):
        r = backfill_table(conn, typ, hit_dict, miss_dict, args.limit, args.dry_run)
        print(
            f"[geocode_backfill] {typ}: 扫描 {r['scanned']} / 补全 {r['updated']} "
            f"/ 标 miss {r['marked_miss']}"
        )
        for k in total:
            total[k] += r[k]

    conn.close()
    print("=" * 50)
    mode = "dry-run" if args.dry_run else "完成"
    print(
        f"[geocode_backfill] {mode}：扫描 {total['scanned']} → 补全 {total['updated']} "
        f"/ miss {total['marked_miss']}，耗时 {time.time() - t0:.1f}s"
    )
    print("  剩余 pending 行等 geocode_fill 补词典后重跑本脚本")


if __name__ == "__main__":
    main()
