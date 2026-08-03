#!/usr/bin/env python3

"""
本地离线地理编码模块

做法：把小区/板块坐标加载进内存 dict，O(1) 哈希查找，单条约 1.2ms，
规避在线地理编码（高德/OSM）每次 200~300ms 的网络延迟。
词典覆盖上海板块 + 广东 5 城（gz/sz/fs/dg/zh）区中心点，支持本地 JSON 增量扩展。
"""

import json
import os

# 离线坐标词典：小区/板块名 -> (lat, lng)
OFFLINE_COMMUNITY_DB = {
    # ---- 上海浦东小区 + 板块 ----
    "证大家园": (31.2849285, 121.5921211),
    "爱法新城": (31.2797389, 121.5818131),
    "浦发东悦城": (31.1051138, 121.5806232),
    "印象春城": (31.1245123, 121.5789123),
    "桃园新城中虹家园": (31.3412389, 121.4812391),
    "中虹家园": (31.3412389, 121.4812391),
    "金谊河畔": (31.1451234, 121.5123941),
    "云台路": (31.1712394, 121.4912391),
    "巨峰路": (31.2835371, 121.5922421),
    "周浦": (31.1123941, 121.5812394),
    "金桥": (31.2612394, 121.5912394),
    "陆家嘴": (31.2394123, 121.4912394),
    "张江": (31.1912394, 121.5912394),
    "三林": (31.1512394, 121.5012394),
    "川沙": (31.1812394, 121.7012394),
    "惠南": (31.0512394, 121.7512394),
    "联洋": (31.2212394, 121.5512394),
    "碧云": (31.2412394, 121.5812394),
    # ---- 广东 5 城区中心点 ----
    # 广州
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
    # 深圳
    "福田": (22.5410, 114.0546),
    "南山": (22.5333, 113.9304),
    "罗湖": (22.5484, 114.1315),
    "宝安": (22.5553, 113.8830),
    "龙岗": (22.7197, 114.2470),
    "龙华": (22.6850, 114.0367),
    # 佛山
    "禅城": (23.0196, 113.1227),
    "南海": (23.0289, 113.1429),
    "顺德": (22.8351, 113.2930),
    # 东莞
    "莞城": (23.0430, 113.7518),
    "南城": (23.0207, 113.7518),
    "松山湖": (22.9281, 113.8950),
    # 珠海
    "香洲": (22.2710, 113.5441),
    "斗门": (22.2090, 113.2967),
    "金湾": (22.1469, 113.3648),
}


class LocalGeocoder:
    def __init__(self, db_file="community_coords.json"):
        self.db_file = db_file
        self.coords_db = OFFLINE_COMMUNITY_DB.copy()
        self.load_local_db()

    def load_local_db(self):
        """可选：从本地 JSON 增量加载坐标，覆盖/扩展内置词典。"""
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self.coords_db[k] = (v[0], v[1])
            except Exception:
                pass

    def save_local_db(self):
        """把当前内存词典落盘为 JSON，便于把抓取过程中新发现的小区坐标沉淀下来。"""
        with open(self.db_file, "w", encoding="utf-8") as f:
            json.dump(self.coords_db, f, ensure_ascii=False, indent=2)

    def add(self, community_name, lat, lng):
        """新增/更新一个小区坐标到内存词典（配合 save_local_db 持久化）。"""
        self.coords_db[community_name.strip()] = (lat, lng)

    def geocode(self, community_name, full_text=""):
        if not community_name:
            return None, None

        comm_clean = community_name.strip()
        # 1) 精确命中
        if comm_clean in self.coords_db:
            return self.coords_db[comm_clean]

        # 2) 双向包含（小区名带板块前缀，如"联洋XX苑"）
        for name, coords in self.coords_db.items():
            if name in comm_clean or comm_clean in name:
                return coords

        # 3) 区域关键词兜底（从全文找区/板块）
        for region, coords in self.coords_db.items():
            if region in full_text:
                return coords

        return None, None


class DbGeocoder:
    """数据库版 geocoder：坐标词典存 MySQL community_coords 表。

    与 LocalGeocoder 的区别：
    - 键为 (city, community) 复合主键，隔离跨城重名（如"东城"在 12 城是不同位置）
    - 状态机：pending=待查 / hit=已解析 / miss=查无
    - 单小区只调一次外部 API：geocode() 命中 hit 即返回并累计 query_count；
      miss 不重查（除非超 7 天）；无记录则 INSERT pending 交给 geocode_fill.py 处理
    - 内存缓存 _cache 避免重复 SQL（每次 run 只查一次表）
    """

    def __init__(self, conn, city_names: dict):
        self.conn = conn
        self.city_names = city_names or {}
        self._cache = {}  # (city, community) -> (lat, lng)
        self._by_city = {}  # city -> [(community, (lat, lng))]，包含匹配只扫本城
        self._match_memo = {}  # (city, community) -> 命中的词典键 / None（run 内 memoize）
        self._known = set()  # (city, community) 已在库中（hit/pending/miss 均记录，防重复登记）
        self._pending_buf = set()  # 待 INSERT 的 pending 键（批量 flush）
        self._touch_buf = {}  # (city, community) -> 命中次数（批量 flush）
        self._loaded = False

    # ---- 加载 ----
    def load_all(self):
        """全量加载词典到内存（表小，一次 SELECT 即可）。"""
        if self._loaded:
            return
        with self.conn.cursor() as cur:
            cur.execute("SELECT city, community, lat, lng, status FROM community_coords")
            for city, community, lat, lng, status in cur.fetchall():
                key = (city, community)
                self._known.add(key)
                if status == "hit":
                    coords = (float(lat), float(lng))
                    self._cache[key] = coords
                    self._by_city.setdefault(city, []).append((community, coords))
        self._loaded = True

    def flush(self):
        """批量提交累积的 pending 登记与 hit 命中计数。"""
        if self._pending_buf:
            self._flush_pending()
        if self._touch_buf:
            with self.conn.cursor() as cur:
                for (city, community), n in self._touch_buf.items():
                    cur.execute(
                        "UPDATE community_coords SET query_count = query_count + %s "
                        "WHERE city=%s AND community=%s AND status='hit'",
                        (n, city, community),
                    )
            self._touch_buf.clear()
        self.conn.commit()

    def _flush_pending(self, chunk=500):
        """pending 登记用多值 INSERT 分块提交（pymysql executemany 是逐条循环，34k 条会 ~170s）。"""
        buf = list(self._pending_buf)
        with self.conn.cursor() as cur:
            for i in range(0, len(buf), chunk):
                batch = buf[i : i + chunk]
                values = ",".join(["(%s, %s, 'pending')"] * len(batch))
                flat = [v for pair in batch for v in pair]
                cur.execute(
                    f"INSERT IGNORE INTO community_coords (city, community, status) "
                    f"VALUES {values}",
                    flat,
                )
        self._pending_buf.clear()

    # ---- 主查询 ----
    def geocode(self, community_name, full_text="", city=None):
        """按 (city, community) 查词典。city 缺失时退化为不带城匹配（兼容旧调用）。"""
        if not community_name:
            return None, None
        comm_clean = community_name.strip()
        if not comm_clean:
            return None, None

        # 防御：city 必须是已知城市代码，否则视为脏数据不登记 pending
        valid_city = city is not None and city in self.city_names
        if city is not None and not valid_city:
            return None, None

        self.load_all()

        # memo：run 内同一 (city, community) 只扫一次词典，命中计数照常累积
        memo_key = (city, comm_clean)
        if memo_key in self._match_memo:
            hit_key = self._match_memo[memo_key]
            if hit_key is not None:
                self._bump_hit(hit_key)
                return self._cache[hit_key]
        # 1) 带城市精确命中
        elif city:
            if memo_key in self._cache:
                self._match_memo[memo_key] = memo_key
                self._bump_hit(memo_key)
                return self._cache[memo_key]
            # 双向包含（小区名带板块前缀）；只扫本城桶，保持跨城隔离
            for name, coords in self._by_city.get(city, ()):
                if name in comm_clean or comm_clean in name:
                    hit_key = (city, name)
                    self._match_memo[memo_key] = hit_key
                    self._bump_hit(hit_key)
                    return coords
            self._match_memo[memo_key] = None

        # 2) 不带城退化：全库匹配（仅当调用方未提供 city）
        else:
            for (c, name), coords in self._cache.items():
                if name == comm_clean:
                    self._match_memo[memo_key] = (c, name)
                    self._bump_hit((c, name))
                    return coords
            for (c, name), coords in self._cache.items():
                if name in comm_clean or comm_clean in name:
                    self._match_memo[memo_key] = (c, name)
                    self._bump_hit((c, name))
                    return coords
            self._match_memo[memo_key] = None

        # 3) 未命中：登记 pending（若库中无该 (city,community) 记录），交 geocode_fill 处理
        if city:
            key = (city, comm_clean)
            if key not in self._known:
                self._known.add(key)
                self._pending_buf.add(key)
        return None, None

    # ---- 内部 ----
    def _bump_hit(self, key):
        """命中 hit：累积 query_count（批量 flush，上限防无限增长）。"""
        if key[0] is None:
            return
        self._touch_buf[key] = self._touch_buf.get(key, 0) + 1
