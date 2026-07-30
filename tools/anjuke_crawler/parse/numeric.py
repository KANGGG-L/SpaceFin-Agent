#!/usr/bin/env python3

"""
房源数据解析模块（纯数值 17 字段 schema）

抽取：户型拆解(室/厅/卫)、面积、朝向、楼层、建造年/房龄、车位数、总价(万)、
单价(元/㎡)、经纬度(离线地理编码)。输出可直接喂给 L3 AVM 估值。

卡片级去重（本项目踩坑后修复）：必须用精确 `//div[@class='property']` 而非
`contains(@class,'property')`——后者会连带匹配 property-content / property-price
等嵌套子节点，导致每套房源被重复解析 4 次。
"""

import csv
import re
from datetime import datetime

from lxml import etree

from ..geocoder import LocalGeocoder

CURRENT_YEAR = datetime.now().year
default_geocoder = LocalGeocoder()

SCHEMA_HEADERS = [
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
    "url",
]


def parse_numeric_schema_housing(html_content, district_tag="pudong", geocoder=None):
    if geocoder is None:
        geocoder = default_geocoder

    houses = []
    tree = etree.HTML(html_content)
    cards = tree.xpath("//div[@class='property']")
    if not cards:
        cards = tree.xpath("//div[contains(@class, 'property-card')]")

    seen_keys = set()

    for card in cards:
        try:
            full_text = " ".join([t.strip() for t in card.xpath(".//text()") if t.strip()])

            title_nodes = card.xpath(
                ".//div[contains(@class, 'property-content-title-name')]/text() | .//h3/text() | .//a[contains(@class, 'title')]/text()"
            )
            title = title_nodes[0].strip() if title_nodes else ""
            if not title:
                link_node = card.xpath(".//a[contains(@href, '/prop/')]")
                if link_node:
                    title = link_node[0].xpath("string(.)").strip()
            if not title:
                continue

            link_nodes = card.xpath(".//a[contains(@href, '/prop/')]/@href | .//a/@href")
            url = link_nodes[0] if link_nodes else ""

            comm_nodes = card.xpath(
                ".//div[contains(@class, 'property-content-info-comm-name')]/text() | .//span[contains(@class, 'community')]/text()"
            )
            community = comm_nodes[0].strip() if comm_nodes else ""
            if not community:
                comm_match = re.search(
                    r"([^\s]+(?:小区|家园|花园|新村|公寓|名邸|雅苑|城|华庭|苑|村))", full_text
                )
                if comm_match:
                    community = comm_match.group(1)

            price_nodes = card.xpath(
                ".//span[contains(@class, 'property-price-total-num')]/text() | .//span[contains(@class, 'price')]/text()"
            )
            price_str = price_nodes[0].strip() if price_nodes else ""
            if not price_str:
                price_match = re.search(r"(\d+(?:\.\d+)?)\s*万", full_text)
                if price_match:
                    price_str = price_match.group(1)
            total_price_wan = (
                float(price_str) if price_str and price_str.replace(".", "", 1).isdigit() else None
            )

            unit_nodes = card.xpath(
                ".//span[contains(@class, 'property-price-average')]/text() | .//span[contains(@class, 'unit-price')]/text()"
            )
            unit_str = (
                unit_nodes[0].strip().replace("元/㎡", "").replace("元/平米", "").strip()
                if unit_nodes
                else ""
            )
            if not unit_str:
                unit_match = re.search(r"(\d+)\s*元/㎡", full_text)
                if unit_match:
                    unit_str = unit_match.group(1)
            unit_price_yuan = int(unit_str) if unit_str and unit_str.isdigit() else None

            dedup_key = url if url else f"{title}_{community}_{total_price_wan}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            rooms_match = re.search(r"(\d+)\s*室", full_text) or re.search(r"(\d+)\s*房", full_text)
            bedrooms = int(rooms_match.group(1)) if rooms_match else 0

            halls_match = re.search(r"(\d+)\s*厅", full_text)
            halls = int(halls_match.group(1)) if halls_match else 0

            baths_match = re.search(r"(\d+)\s*卫", full_text)
            bathrooms = int(baths_match.group(1)) if baths_match else 0

            area_match = re.search(r"(\d+(?:\.\d+)?)\s*㎡", full_text) or re.search(
                r"(\d+(?:\.\d+)?)\s*平米", full_text
            )
            area_sqm = float(area_match.group(1)) if area_match else None

            dir_match = re.search(r"(南北|东南|西南|东北|西北|南|北|东|西)", full_text)
            direction = dir_match.group(1) if dir_match else ""

            floor_match = re.search(r"((?:高层|中层|低层|顶层|底层)\([^\)]+\)|共\d+层)", full_text)
            floor = floor_match.group(1) if floor_match else ""

            year_match = re.search(r"(\d{4})\s*年", full_text)
            building_year = int(year_match.group(1)) if year_match else None
            building_age = (CURRENT_YEAR - building_year) if building_year else None

            parking_count = 0
            if any(k in full_text for k in ["双车位", "2车位", "两个车位"]):
                parking_count = 2
            elif any(k in full_text for k in ["车位", "产权车位", "赠送车位", "车库", "含车位"]):
                parking_count = 1

            lat, lng = geocoder.geocode(community, full_text)

            houses.append(
                {
                    "title": title,
                    "community": community,
                    "district": district_tag,
                    "bedrooms": bedrooms,
                    "halls": halls,
                    "bathrooms": bathrooms,
                    "area_sqm": area_sqm,
                    "direction": direction,
                    "floor": floor,
                    "building_year": building_year,
                    "building_age": building_age,
                    "parking_count": parking_count,
                    "total_price_wan": total_price_wan,
                    "unit_price_yuan": unit_price_yuan,
                    "latitude": lat,
                    "longitude": lng,
                    "url": url,
                }
            )
        except Exception:
            continue

    return houses


def save_numeric_schema_csv(houses, filename="anjuke_numeric_schema_houses.csv"):
    with open(filename, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA_HEADERS)
        writer.writeheader()
        for h in houses:
            writer.writerow({k: h.get(k) for k in SCHEMA_HEADERS})
    print(f"[+] Saved {len(houses)} clean numeric-schema records into {filename}")
