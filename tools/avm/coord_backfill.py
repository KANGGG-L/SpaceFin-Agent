"""离线坐标回填：为训练行缺失的经纬度补位置信号（S2 第二轮）。

背景：crawl_housing_sale 仅约 31% 行带经纬度，且集中在一半城市（cz/fs/gz/hui/
hy/jm/jy/mm/mz/dg）；sz/zs/zh/yf 等 11 城全表无坐标，模型对无坐标行只能靠
城市中位 + 小区编码估计，无法区分核心区/郊区价差（sz MAPE 27.6%、gz 34.4% 的
结构性来源）。本轮在不消耗腾讯 geocoder 配额的前提下，用三个离线词典回填：

1. **community_coords 表 hit 记录**：geocode_fill 每日跑的腾讯 geocoder 词典，
   键为爬虫原始 community 名（含整句噪音标签），命中直接回填（坐标是真实解析值）。
2. **dws_spatial_feature 的 community 实体**：spatial 层已入库的小区坐标（同样
   按原始 community 名）。
3. **区中心点词典**：从 anjuke_crawler.geocoder.OFFLINE_COMMUNITY_DB 提取
   gz/sz/fs/dg/zh 五城行政区中心点，用「小区名/标题含区名」规则给无坐标行一个
   粗略位置（粒度是区，不是小区，但足以区分核心区/郊区价格带）。

另外兼容 output/avm/coord_cache.json（腾讯 geocoder 批量补坐标的产物，见
coord_fill.py）——有则优先用，实现「词典 → 历史坐标 → 区中心」三级回填。

回填坐标进入特征前会再经 GD 围栏校验，越界视为脏坐标不采纳。

所有词典查询只在模块层做一次（连接一次 DB），训练/预测期间零网络调用。
"""

from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COORD_CACHE_PATH = os.path.join(REPO_ROOT, "output", "avm", "coord_cache.json")

# 广东 21 城行政区中心点（经纬度 WGS-84，来自 anjuke_crawler.geocoder 离线词典，
# 仅覆盖有坐标的 5 城；其余城市无词典，不强造位置）。
DISTRICT_CENTERS: dict[str, dict[str, tuple[float, float]]] = {
    "gz": {
        "天河": (23.1246, 113.3612),
        "越秀": (23.1291, 113.2665),
        "海珠": (23.0900, 113.3170),
        "白云": (23.1576, 113.2730),
        "荔湾": (23.1249, 113.2188),
        "黄埔": (23.1304, 113.4805),
        "番禺": (22.9370, 113.3845),
        "南沙": (22.8016, 113.5253),
        "花都": (23.4360, 113.2200),
        "从化": (23.5453, 113.5913),
        "增城": (23.2614, 113.8105),
    },
    "sz": {
        "福田": (22.5410, 114.0546),
        "南山": (22.5333, 113.9304),
        "罗湖": (22.5484, 114.1315),
        "宝安": (22.5553, 113.8830),
        "龙岗": (22.7197, 114.2470),
        "龙华": (22.6850, 114.0367),
        "盐田": (22.5570, 114.2370),
        "光明": (22.7488, 113.9360),
        "坪山": (22.7090, 114.3460),
    },
    "fs": {
        "禅城": (23.0196, 113.1227),
        "南海": (23.0289, 113.1429),
        "顺德": (22.8351, 113.2930),
        "三水": (23.1555, 112.8967),
        "高明": (22.9000, 112.8925),
    },
    "dg": {
        "莞城": (23.0430, 113.7518),
        "南城": (23.0207, 113.7518),
        "松山湖": (22.9281, 113.8950),
    },
    "zh": {
        "香洲": (22.2710, 113.5441),
        "斗门": (22.2090, 113.2967),
        "金湾": (22.1469, 113.3648),
        "横琴": (22.0987, 113.5440),
        "高新": (22.3500, 113.6100),
        "吉大": (22.2500, 113.5780),
    },
}

GD_BOX = (19.9, 25.6, 109.4, 117.6)  # 与 data_clean 同源


def _in_gd(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = GD_BOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _load_tencent_cache() -> dict[tuple[str, str], tuple[float, float]]:
    """读腾讯 geocoder 批量补坐标的缓存（coord_fill.py 产物），结构按城市嵌套。"""
    if not os.path.exists(COORD_CACHE_PATH):
        return {}
    try:
        with open(COORD_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for city, comms in data.items():
        for comm, (lat, lng) in comms.items():
            if _in_gd(float(lat), float(lng)):
                out[(city, comm)] = (float(lat), float(lng))
    return out


def load_coord_dict(env: dict) -> dict[tuple[str, str], tuple[float, float]]:
    """从 DB 词典 + 本地缓存构建 (city, community) -> (lat, lng) 全量词典。

    调用方需传入已读好的 .env（train.py 复用其 DB 连接参数）。
    """
    import pymysql

    out: dict[tuple[str, str], tuple[float, float]] = {}
    try:
        conn = pymysql.connect(
            host=env.get("MYSQL_HOST", "127.0.0.1"),
            port=int(env.get("MYSQL_PORT", "3306")),
            user=env.get("MYSQL_APP_USER", "spacefin_crawler_app"),
            password=env.get("MYSQL_APP_PASSWORD", ""),
            database="spacefin_crawler",
            charset="utf8mb4",
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT city, community, lat, lng FROM community_coords "
                "WHERE status='hit' AND lat IS NOT NULL"
            )
            for city, comm, lat, lng in cur.fetchall():
                if comm and _in_gd(float(lat), float(lng)):
                    out[(city, comm.strip())] = (float(lat), float(lng))
            cur.execute(
                "SELECT entity_id, district, lat, lng FROM dws_spatial_feature "
                "WHERE entity_type='community' AND lat IS NOT NULL"
            )
            for eid, city, lat, lng in cur.fetchall():
                # entity_id 形如 "cz|，新景花园"
                _, _, comm = (eid or "").partition("|")
                if comm and _in_gd(float(lat), float(lng)):
                    out[(city, comm.strip())] = (float(lat), float(lng))
        conn.close()
    except Exception:
        pass  # 词典构建失败不阻断训练：无坐标行保持 NaN
    out.update(_load_tencent_cache())
    return out


def backfill_coords(
    rows: list[dict], coord_dict: dict[tuple[str, str], tuple[float, float]]
) -> dict:
    """就地给缺失经纬度的行回填坐标（不改已有坐标行）。

    匹配优先级（只回填「已确认在本市」的坐标）：
      1. 原始 community（爬虫标签）精确命中词典；
      2. 归一后 community 精确命中词典；
      3. 小区名含本城行政区名 → 区中心点；
      4. 标题含「区名+区」→ 区中心点（保守，防「望南海景」这类误配）。

    返回回填统计。越界坐标（GD 围栏外）不采纳。
    """
    stats = {"n_backfilled": 0, "by_city": {}, "by_source": {"dict": 0, "district": 0}}
    for r in rows:
        if r["lat"] == r["lat"]:
            continue  # 已有坐标
        city = r["city"]
        raw = r.get("raw_comm") or ""
        comm = r["comm"] or ""
        hit = None
        src = None
        for name in (raw, comm):
            if not name:
                continue
            c = coord_dict.get((city, name))
            if c:
                hit, src = c, "dict"
                break
        if hit is None:
            # 区中心点：小区名含区名，或标题含「区名+区」/纯区名（区名在省内城市
            # 语境无歧义：天河/龙岗/松山湖 等不会出现在省外或描述性文本里）
            title = r.get("title") or ""
            for d, c in DISTRICT_CENTERS.get(city, {}).items():
                if (d + "区") in title or d in title or d in comm:
                    hit, src = c, "district"
                    break
        if hit is None:
            continue
        lat, lng = hit
        if not _in_gd(lat, lng):
            continue
        r["lat"], r["lng"] = lat, lng
        stats["n_backfilled"] += 1
        stats["by_city"][city] = stats["by_city"].get(city, 0) + 1
        stats["by_source"][src] += 1
    return stats
