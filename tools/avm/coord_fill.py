"""腾讯 geocoder 批量补坐标：把缺失经纬度的小区解析成 (lat, lng) 落缓存。

背景：crawl_housing_sale 约 2/3 行无坐标，sz/zs/zh/yf 等 11 城全表无坐标，
模型对无坐标行只能靠城市中位估计（gz/sz 段 MAPE 33%/28% 的主要来源；S6 已
剔除 zs/yf/zh/dg 四城 100% 外市错标数据，sz/gz 仍无坐标）。
本脚本按「行数降序」优先解析行数最多的小区，命中写 output/avm/coord_cache.json
（城市 -> 小区名 -> [lat, lng]，WGS-84），train.py 训练时自动加载回填。

与 tools/orchestrator/geocode_fill.py 的区别：
- 签名修复：geocode_fill 对参数值做 quote 后参与 MD5，腾讯按解码值验签会返回
  status=111；本脚本按腾讯实际行为用「原始值拼签名串 + 编码 URL」，实测可用。
- 输入归一：直接消费 data_clean 归一后的小区名（geocode_fill 用的是爬虫原始
  整句标签，解析命中率低）。
- 增量缓存：每次 hit 立即写盘，中断不丢进度；重跑只处理缓存缺失的小区。

配额：腾讯 geocoder 每 key 每日 6000 次（超了返回 status=121），脚本自动暂停
保留进度，次日重跑即可续传。耗时：0.2s/条 × ~6800 小区 ≈ 22 分钟/天。

用法：
    tools/orchestrator/.venv/bin/python tools/avm/coord_fill.py [--daily-limit N]
    # 建议每日跑一次直到全量覆盖；--dry-run 只看待解析清单

需要 .env 提供 TENCENT_MAP_KEY / TENCENT_MAP_SK（SK 开启时同腾讯控制台一致）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "avm"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "orchestrator"))
sys.path.insert(0, REPO_ROOT)

COORD_CACHE_PATH = os.path.join(REPO_ROOT, "output", "avm", "coord_cache.json")

TENCENT_GEOCODE_URL = "https://apis.map.qq.com/ws/geocoder/v1"
GEOCODE_PATH = "/ws/geocoder/v1"

# 解析优先级：先 sz（贡献最大误差）→ gz 缺坐标 → zs/zh/yf 污染段 → 其余按行数
CITY_PRIORITY = ["sz", "gz", "zs", "zh", "yf"]
EXTRA_SKIP = {"mz", "hy", "cz"}  # 已多数有坐标/误差低，最后兜底时再处理


def load_env() -> dict:
    env = {}
    path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def crawl_params(env: dict) -> dict:
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": env.get("MYSQL_APP_USER", "spacefin_crawler_app"),
        "password": env.get("MYSQL_APP_PASSWORD", ""),
        "database": "spacefin_crawler",
    }


def city_names() -> dict:
    """城市码 -> 中文名（geocoder region 参数用）。"""
    from etl import CITY_NAMES

    return CITY_NAMES


def query_tencent(key: str, address: str, region: str, sk: str = "") -> tuple:
    """查腾讯 geocoder，返回 (lat, lng, level, status)。

    签名坑：腾讯按「参数解码值」验签，签名串必须用原始值（不做百分号编码），
    URL 再按 RFC3986 编码；两者不一致会 status=111（geocode_fill 当前实现就栽在这）。
    address 需先清洗掉 '+'/'&' 等会让 URL/签名不一致的字符。
    """
    params = {
        "key": key,
        "address": address,
        "region": region,
        "policy": 1,
        "output": "json",
    }
    if sk:
        # 签名串：参数名升序，值用原始文本拼接（不编码）
        sorted_qs = "&".join(f"{k}={str(params[k])}" for k in sorted(params))
        sig = hashlib.md5((GEOCODE_PATH + "?" + sorted_qs + sk).encode("utf-8")).hexdigest()
        params["sig"] = sig
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in sorted(params.items()))
    url = f"{TENCENT_GEOCODE_URL}?{qs}"
    try:
        req = Request(url, headers={"User-Agent": "SpaceFinAVM/1.0"})
        with urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
    except Exception:
        return None, None, None, -1
    import json as _json

    j = _json.loads(data)
    status = j.get("status")
    if status != 0:
        return None, None, None, status
    result = j.get("result") or {}
    loc = result.get("location") or {}
    level = result.get("level") or 0
    if not loc:
        return None, None, None, 0
    if level < 10:  # 只采纳小区/大厦/POI 级
        return None, None, level, 0
    return float(loc["lat"]), float(loc["lng"]), level, 0


def collect_pending() -> list[tuple[str, str, int]]:
    """收集待解析 (city, community, 行数)：先跑清洗得到归一小区名，再剔除
    已有坐标/已有缓存/已在词典命中的小区。返回按优先级+行数排序的清单。"""
    import pymysql

    env = load_env()
    conn = pymysql.connect(**crawl_params(env), charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(
        "SELECT title, community, district, bedrooms, halls, bathrooms, area_sqm, "
        "direction, floor, building_age, parking_count, total_price_wan, "
        "unit_price_yuan, latitude, longitude, url FROM crawl_housing_sale"
    )
    cols = [d[0] for d in cur.description]
    raw_rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    conn.close()

    from data_clean import clean_rows_with_stats

    cleaned, _ = clean_rows_with_stats(raw_rows, parse_all=True)

    # 统计：城市码 -> 小区名 -> 缺失坐标行数

    need = {}
    have_coord_comm = set()
    for r in raw_rows:
        dist = r["district"]
        comm = (r["community"] or "").strip()
        if r["latitude"] is not None and r["longitude"] is not None and comm:
            have_coord_comm.add((dist, comm))
    for r in cleaned:
        dist = r["district"]
        comm = (r["community"] or "").strip()
        if not comm:
            continue
        if r["latitude"] is not None and r["longitude"] is not None:
            have_coord_comm.add((dist, comm))
        else:
            need[(dist, comm)] = need.get((dist, comm), 0) + 1
    # 去掉已有坐标（含同一小区其它行有坐标的情况：同名小区坐标一致）
    for k in have_coord_comm:
        need.pop(k, None)

    cache = load_cache()
    rows = [(city, comm, n) for (city, comm), n in need.items() if comm not in cache.get(city, {})]
    rows.sort(key=lambda x: (CITY_PRIORITY.index(x[0]) if x[0] in CITY_PRIORITY else 99, -x[2]))
    return rows


def load_cache() -> dict:
    if not os.path.exists(COORD_CACHE_PATH):
        return {}
    try:
        with open(COORD_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(COORD_CACHE_PATH), exist_ok=True)
    with open(COORD_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-limit", type=int, default=6000)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    key = env.get("TENCENT_MAP_KEY", "")
    sk = env.get("TENCENT_MAP_SK", "")
    if not key and not args.dry_run:
        raise SystemExit("[coord_fill] .env 缺 TENCENT_MAP_KEY")
    names = city_names()

    pending = collect_pending()
    print(f"[coord_fill] 待解析 {len(pending)} 个小区（含跨城市同名）")
    if args.dry_run:
        for city, comm, n in pending[:30]:
            print(f"  {city} {comm} x{n}")
        return

    cache = load_cache()
    hit = miss = err = 0
    t0 = time.time()
    for city, comm, _n in pending:
        if hit + miss + err >= args.daily_limit:
            print(f"[coord_fill] 已达今日配额 {args.daily_limit}，暂停（缓存已保存，次日续跑）")
            break
        region = names.get(city, city)
        address = f"{region}{comm}"
        lat, lng, level, status = query_tencent(key, address, region, sk=sk)
        if status == -1:
            err += 1
            time.sleep(1)
            continue
        if status == 121:  # 配额耗尽：终止，剩余待明天
            print("[coord_fill] 腾讯配额已满（status=121），暂停")
            break
        if status != 0:
            # 系统/参数错误：记 miss 不空耗配额
            if status == 348:
                cache.setdefault(city, {})[comm] = None
                save_cache(cache)
            miss += 1
            if miss <= 5:
                print(f"[coord_fill] 失败 status={status} ({city},{comm})")
            time.sleep(args.sleep)
            continue
        if lat is not None and level >= 10:
            cache.setdefault(city, {})[comm] = [lat, lng]
            save_cache(cache)
            hit += 1
        else:
            miss += 1
        time.sleep(args.sleep)
        if (hit + miss + err) % 200 == 0:
            print(f"  进度 {hit + miss + err}/{len(pending)} hit={hit} miss={miss} err={err}")

    print("=" * 50)
    print(f"[coord_fill] 完成：hit={hit} miss={miss} err={err} 缓存 {COORD_CACHE_PATH}")
    print(f"  耗时 {(time.time() - t0):.0f}s；剩余小区次日重跑即可")


if __name__ == "__main__":
    main()
