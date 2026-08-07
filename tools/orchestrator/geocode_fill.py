#!/usr/bin/env python3
"""
批量补全小区坐标词典（腾讯位置服务 geocoder → community_coords 表）。

背景：ETL geocode 命中率低（2.54%）因离线词典只覆盖 5 城。本脚本用腾讯
WebService API（/ws/geocoder/v1）对词典表中 status='pending' 或
miss 超 7 天的 (city, community) 批量查询坐标，命中写回 WGS-84。

设计（用户确认）：
- 单小区只调一次 API：status='hit' 的行不再查；miss 每周允许重查一次
- 断点续跑天然由 status 驱动（重跑只处理 pending/过期 miss）
- 跨城重名隔离：查询带 region=城市名
- 坐标系：腾讯返回 GCJ-02，写入前转 WGS-84（统一口径，见 geocoder.py 说明）

用法：
    python geocode_fill.py [--limit N] [--sleep 0.2] [--dry-run]
    # --limit 0 = 不限；--sleep 每请求间隔秒（默认 0.2 = 5 req/s，6000/天配额充裕）

.env 需要：TENCENT_MAP_KEY=xxx（腾讯位置服务 WebService key）
          TENCENT_MAP_SK=xxx（若控制台开启签名校验 SN，则必须提供 SecretKey）
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pymysql
from etl import CITY_NAMES, _mysql_params, load_env

# 腾讯位置服务 geocoder 端点（查小区坐标；签名计算用 GEOCODE_PATH，
# URL 与 PATH 都无尾斜杠——尾斜杠会导致签名校验失败 status=111）
TENCENT_GEOCODE_URL = "https://apis.map.qq.com/ws/geocoder/v1"
GEOCODE_PATH = "/ws/geocoder/v1"

# GCJ-02 → WGS-84（标准算法，误差 <1m）
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 火星坐标 → WGS-84 真实坐标。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lng - dlng, lat - dlat


def clean_address(region: str, community: str, aggressive: bool = False) -> str:
    """把 community_coords.community（常是整段房源标题）清洗成可地理编码的小区名。

    community 字段实际存的是爬虫抓来的房源标题，腾讯 geocoder 对"城市+小区名"
    能解析，但对以下情况报 348 参数错误：
      - 整段营销标题（含 出售/四房/单价…）
      - 重复城市前缀（region 已拼一次，community 里又带一次）
      - '+' 等符号（还会让签名 MD5 不匹配 → 111）
    aggressive=True 时进一步抽取标题前段并去掉营销/户型词，用于 348 的二次重试。
    返回空串表示无法清洗（调用方应跳过重试）。
    """
    s = (community or "").strip()
    s = re.sub(r"@\S*$", "", s)  # 去尾部 @xxx（防御）
    # 去开头城市前缀：'茂名，' / '茂名!' / '茂名 ' 等
    s = re.sub(rf"^[{re.escape(region)}]+[\s，,、！!。\.#@\-]*", "", s)
    # 去会让签名/参数报错的非法符号
    s = re.sub(r"[\+#%&*=@|\\/`~]+", "", s)
    if aggressive:
        # 小区名通常在标题最前，取首个分隔符前的片段
        head = re.split(r"[，,、 　!！?？@]+", s)[0]
        # 去常见营销/户型词，保留地名核心（不删 花园/小区/城/苑 等地名成分）
        head = re.sub(
            r"(出售|出租|急售|低价|笋盘|一口价|可谈|价格|总价|单价|万元|万|㎡|平米|平方|字头|"
            r"包税|满五|唯一|精装|装修|简装|毛坯|电梯|楼梯|步梯|楼层|南北|南向|北向|望|送|带|"
            r"含|近|旁|附近|门口|户型|房|室|厅|卫|栋|幢|层|楼|号|期|楼龄|年)",
            "",
            head,
        )
        s = head
    s = re.sub(r"\s+", "", s)  # 折叠空白
    return s


def _http_get(url: str, timeout: int = 30, retries: int = 3):
    """HTTP GET，自动适配代理环境 + 重试。

    不硬编码代理是否启用——由当前环境决定：
    1) 默认方式（尊重系统/环境代理）先试；
    2) 失败后绕过代理直连兜底。
    每次尝试失败都会重试（间歇性 SSL 握手超时/网络抖动）。
    """
    from urllib.request import ProxyHandler, build_opener

    req = Request(url, headers={"User-Agent": "SpaceFinETL/1.0"})
    last_err = None
    for _attempt in range(retries):
        # 1) 默认方式：尊重系统代理/环境变量（ProxyHandler() 不带参 = 用系统代理）
        for opener in (None, build_opener(ProxyHandler({}))):
            try:
                if opener is None:
                    with urlopen(req, timeout=timeout) as resp:
                        return resp.read().decode("utf-8")
                else:
                    with opener.open(req, timeout=timeout) as resp:
                        return resp.read().decode("utf-8")
            except Exception as e:
                last_err = e
    raise last_err


def query_tencent(key: str, address: str, region: str, sk: str = ""):
    """调用腾讯 geocoder（/ws/geocoder/v1）查小区坐标。

    返回 (lat_wgs84, lng_wgs84, level, status_code)。
    status_code 为腾讯返回的原始 status（0=成功）；status!=0 属系统类错误
    （鉴权/限频/配额等），调用方不应记 miss、应保留 pending 次日重跑。

    为何用 geocoder 而非 place/search：
    - place/search 每天配额仅 200（无法跑 23k 条）
    - geocoder 每天 6000 配额（并发 5/s）；对"城市+小区名"拼地址可正常解析（纯小区名会 348）
    - 只采纳 level>=10（小区/大厦/POI 级），过滤 level<10 的区县/城市级误配
    签名：参数按名升序 → md5("/ws/geocoder/v1?" + 原始参数串 + SK) → 附 sig。
    """
    import hashlib
    import json

    params = {
        "key": key,
        "address": address,  # 调用方传"城市名+小区名"
        "region": region,
        "policy": 1,  # 宽松策略：允许地址缺失省市区
        "output": "json",
    }
    if sk:
        # 注意：签名必须与最终 URL 的编码一致——值须经 quote 编码后再参与 MD5，
        # 否则 '+' 等特殊字符会让签名与服务端不一致 → status=111 签名验证失败
        sorted_qs = "&".join(f"{k}={quote(str(params[k]))}" for k in sorted(params))
        params["sig"] = hashlib.md5(
            (GEOCODE_PATH + "?" + sorted_qs + sk).encode("utf-8")
        ).hexdigest()
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in sorted(params.items()))
    url = f"{TENCENT_GEOCODE_URL}?{qs}"

    data = _http_get(url)

    j = json.loads(data)
    status = j.get("status")
    if status != 0:
        if status in (110, 111, 112, 348):
            print(
                f"[geocode_fill] 腾讯报错 status={status} msg={j.get('message')} "
                f"({address}@{region})"
            )
        # 系统类错误：返回原始 status，交由调用方按 err 处理（不记 miss、不锁 7 天）
        return None, None, None, status

    result = j.get("result") or {}
    loc = result.get("location") or {}
    level = result.get("level") or 0
    if not loc:
        return None, None, None, 0
    # 只采纳小区/大厦/POI 级（level>=10）；区县(2)/城市(1)/乡镇(3)级不采纳
    if level < 10:
        return None, None, level, 0
    lng_gcj, lat_gcj = loc["lng"], loc["lat"]
    lng_wgs, lat_wgs = gcj02_to_wgs84(lng_gcj, lat_gcj)
    return lat_wgs, lng_wgs, level, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理 N 条（0=不限）")
    ap.add_argument(
        "--daily-limit",
        type=int,
        default=6000,
        help="每日腾讯 geocoder 配额上限（默认 6000/天），跑满即停、次日续跑",
    )
    ap.add_argument("--sleep", type=float, default=0.2, help="每请求间隔秒（默认 0.2=5 req/s）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待处理行，不调 API")
    args = ap.parse_args()

    env = load_env()
    key = env.get("TENCENT_MAP_KEY", "")
    sk = env.get("TENCENT_MAP_SK", "")
    if not key and not args.dry_run:
        raise SystemExit("[geocode_fill] .env 缺 TENCENT_MAP_KEY")
    if sk:
        print("[geocode_fill] 已启用腾讯签名校验（sig）")

    params = _mysql_params(None, env)
    conn = pymysql.connect(**params, autocommit=False, charset="utf8mb4")

    # 待处理：pending 或 miss 且超 7 天（last_queried_at 为 NULL 视为未查过，可查）
    sql = (
        "SELECT city, community FROM community_coords "
        "WHERE status='pending' OR (status='miss' AND "
        "(last_queried_at IS NULL OR last_queried_at < DATE_SUB(NOW(), INTERVAL 7 DAY))) "
        "ORDER BY city, community"
    )
    if args.limit > 0:
        sql += f" LIMIT {args.limit}"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    print(f"[geocode_fill] 待处理 {len(rows)} 条（pending + 超期 miss）")

    if args.dry_run:
        for city, community in rows[:20]:
            print(f"  {city} {community}")
        conn.close()
        return

    hit = miss = err = 0
    t0 = time.time()
    # 每日配额保护：统计今日已消耗（last_queried_at 今日的 hit+miss），跑满 daily-limit 即停
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM community_coords WHERE last_queried_at >= CURDATE()")
        used_today = cur.fetchone()[0]
    remaining_today = max(0, args.daily_limit - used_today)
    print(
        f"[geocode_fill] 今日已用 {used_today}/{args.daily_limit}，本 run 可再处理 {remaining_today} 条"
    )
    if remaining_today == 0:
        print("[geocode_fill] 今日配额已用完，退出（pending 保留，明日续跑）")
        conn.close()
        return

    for city, community in rows:
        if hit + miss + err >= remaining_today:
            print(f"[geocode_fill] 已达今日配额 {args.daily_limit}，暂停（pending 保留，明日续跑）")
            break
        try:
            region = CITY_NAMES.get(city, city)
            # geocoder 需"城市名+小区名"拼地址（纯小区名报 348）
            address = f"{region}{community}"
            lat, lng, level, status_code = query_tencent(key, address, region, sk=sk)
            # 348 参数错误：多半是 community 为整段标题/含重复城市前缀/非法符号，
            # 清洗后重试一次（不影响已 hit 的行——它们首查即成功，不会进这里）
            if status_code == 348:
                clean = clean_address(region, community)
                if clean and clean != community:
                    address = f"{region}{clean}"
                    lat, lng, level, status_code = query_tencent(key, address, region, sk=sk)
                # 仍 348：进一步抽取标题前段+去营销词再试一次
                if status_code == 348:
                    clean2 = clean_address(region, community, aggressive=True)
                    if clean2 and clean2 != clean:
                        address = f"{region}{clean2}"
                        lat, lng, level, status_code = query_tencent(key, address, region, sk=sk)
        except Exception as e:
            # 网络/限流等异常：不中断整体，跳过待下次重跑（不写 miss、不更新 last_queried_at）
            err += 1
            if err <= 5 or err % 50 == 0:
                print(f"[geocode_fill] 单条失败({city},{community}): {type(e).__name__} {e}")
            time.sleep(1)
            continue
        if status_code != 0:
            if status_code == 348:
                # 清洗后仍 348 = 永久坏输入，记 miss 不再每日空耗配额（原逻辑会一直 pending 重试）
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE community_coords SET status='miss', "
                        "query_count=query_count+1, last_queried_at=NOW() "
                        "WHERE city=%s AND community=%s",
                        (city, community),
                    )
                    conn.commit()
                miss += 1
                if miss <= 5 or miss % 50 == 0:
                    print(f"[geocode_fill] 参数错误记 miss ({city},{community}) addr={address}")
                continue
            # 其它系统类错误（鉴权 110/111、限频 120、配额耗尽等）：
            # 不记 miss、不更新 last_queried_at，保留 pending 次日重跑，避免临时故障误判
            err += 1
            if err <= 5 or err % 50 == 0:
                print(f"[geocode_fill] 腾讯系统错误 status={status_code} ({city},{community})")
            time.sleep(1)
            continue
        if lat is not None and level >= 10:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE community_coords SET lat=%s, lng=%s, status='hit', source='tencent', "
                    "level=%s, query_count=query_count+1, last_queried_at=NOW() "
                    "WHERE city=%s AND community=%s",
                    (lat, lng, level, city, community),
                )
            hit += 1
        elif lat is not None:
            # 查到了但精度不足（level<10，非小区/大厦级），仍记为 miss 不采纳
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE community_coords SET status='miss', level=%s, "
                    "query_count=query_count+1, last_queried_at=NOW() "
                    "WHERE city=%s AND community=%s",
                    (level, city, community),
                )
            miss += 1
        else:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE community_coords SET status='miss', "
                    "query_count=query_count+1, last_queried_at=NOW() "
                    "WHERE city=%s AND community=%s",
                    (city, community),
                )
            miss += 1
        # 长跑可能触发 MySQL wait_timeout 断连，commit 前 ping 自动重连
        conn.ping(reconnect=True)
        conn.commit()
        time.sleep(args.sleep)
        if (hit + miss + err) % 500 == 0:
            print(
                f"  进度 {hit + miss + err}/{len(rows)} hit={hit} miss={miss} err={err} "
                f"{(time.time() - t0) / (hit + miss + err):.2f}s/条"
            )

    conn.close()
    print("=" * 50)
    print(f"[geocode_fill] 完成：{len(rows)} 条 → hit {hit} / miss {miss} / err {err}")
    print(f"  耗时 {(time.time() - t0):.0f}s（{len(rows) / max(time.time() - t0, 1):.1f} 条/s）")


if __name__ == "__main__":
    main()
