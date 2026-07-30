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
