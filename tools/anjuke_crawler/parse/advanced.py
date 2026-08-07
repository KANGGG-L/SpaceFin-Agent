#!/usr/bin/env python3

"""
增强版房源特征解析模块（18 字段，比 parser.py 的 17 字段数值 schema 更丰富）

在 parser.py 基础上额外产出：
- Layout         户型字符串（如 "3室2厅2卫"）
- Has_Parking    是否含车位（bool）
- Parking_Desc   车位描述（"送产权车位" / "包含/有车位" / "无说明"）

用途：需要更细粒度特征（车位、完整户型串）的分析场景；若只要纯数值 schema 用 parser.py。
卡片级去重与 parser.py 一致（精确 `//div[@class='property']`）。
"""

import csv
import re
from datetime import datetime

from lxml import etree

from ..geocoder import LocalGeocoder

CURRENT_YEAR = datetime.now().year
local_geocoder = LocalGeocoder()

ADVANCED_HEADERS = [
    "Title",
    "Community",
    "Rooms",
    "Halls",
    "Bathrooms",
    "Layout",
    "Area_sqm",
    "Direction",
    "Floor",
    "Building_Year",
    "Building_Age",
    "Has_Parking",
    "Parking_Desc",
    "Total_Price_Wan",
    "Unit_Price_Yuan",
    "Latitude",
    "Longitude",
    "URL",
]


def parse_advanced_housing_data(html_content, use_local_geocoding=True, geocoder=None):
    if geocoder is None:
        geocoder = local_geocoder

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
            price = price_nodes[0].strip() if price_nodes else ""
            if not price:
                price_match = re.search(r"(\d+(?:\.\d+)?)\s*万", full_text)
                if price_match:
                    price = price_match.group(1)

            unit_nodes = card.xpath(
                ".//span[contains(@class, 'property-price-average')]/text() | .//span[contains(@class, 'unit-price')]/text()"
            )
            unit_price = (
                unit_nodes[0].strip().replace("元/㎡", "").replace("元/平米", "").strip()
                if unit_nodes
                else ""
            )
            if not unit_price:
                unit_match = re.search(r"(\d+)\s*元/㎡", full_text)
                if unit_match:
                    unit_price = unit_match.group(1)

            dedup_key = url if url else f"{title}_{community}_{price}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            rooms_match = re.search(r"(\d+)\s*室", full_text) or re.search(r"(\d+)\s*房", full_text)
            rooms = int(rooms_match.group(1)) if rooms_match else None

            halls_match = re.search(r"(\d+)\s*厅", full_text)
            halls = int(halls_match.group(1)) if halls_match else None

            baths_match = re.search(r"(\d+)\s*卫", full_text)
            baths = int(baths_match.group(1)) if baths_match else None

            layout_str = ""
            if rooms is not None:
                layout_str += f"{rooms}室"
            if halls is not None:
                layout_str += f"{halls}厅"
            if baths is not None:
                layout_str += f"{baths}卫"

            area_match = re.search(r"(\d+(?:\.\d+)?)\s*㎡", full_text) or re.search(
                r"(\d+(?:\.\d+)?)\s*平米", full_text
            )
            area = float(area_match.group(1)) if area_match else None

            dir_match = re.search(r"(南北|东南|西南|东北|西北|南|北|东|西)", full_text)
            direction = dir_match.group(1) if dir_match else ""

            floor_match = re.search(r"((?:高层|中层|低层|顶层|底层)\([^\)]+\)|共\d+层)", full_text)
            floor = floor_match.group(1) if floor_match else ""

            year_match = re.search(r"(\d{4})\s*年", full_text)
            building_year = int(year_match.group(1)) if year_match else None
            building_age = (CURRENT_YEAR - building_year) if building_year else None

            has_parking = False
            parking_desc = "无说明"
            if any(k in full_text for k in ["车位", "产权车位", "赠送车位", "车库", "含车位"]):
                has_parking = True
                parking_match = re.search(
                    r"(送[^\s]*车位|产权车位|含车位|带车位|有车位)", full_text
                )
                parking_desc = parking_match.group(1) if parking_match else "包含/有车位"

            lat, lng = None, None
            if use_local_geocoding:
                lat, lng = geocoder.geocode(community, full_text)

            houses.append(
                {
                    "Title": title,
                    "Community": community,
                    "Rooms": rooms,
                    "Halls": halls,
                    "Bathrooms": baths,
                    "Layout": layout_str,
                    "Area_sqm": area,
                    "Direction": direction,
                    "Floor": floor,
                    "Building_Year": building_year,
                    "Building_Age": building_age,
                    "Has_Parking": has_parking,
                    "Parking_Desc": parking_desc,
                    "Total_Price_Wan": float(price)
                    if price and price.replace(".", "", 1).isdigit()
                    else None,
                    "Unit_Price_Yuan": int(unit_price)
                    if unit_price and unit_price.isdigit()
                    else None,
                    "Latitude": lat,
                    "Longitude": lng,
                    "URL": url,
                }
            )
        except Exception:
            continue

    return houses


def save_advanced_csv(houses, filename="anjuke_advanced_houses.csv"):
    with open(filename, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ADVANCED_HEADERS)
        writer.writeheader()
        for h in houses:
            writer.writerow({k: h.get(k) for k in ADVANCED_HEADERS})
    print(f"[+] Saved {len(houses)} clean unique records into {filename}")
