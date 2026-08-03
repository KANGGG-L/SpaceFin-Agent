#!/usr/bin/env python3

"""
房源出租数据解析模块（出租 14 字段 schema）

安居客出租列表页位于独立子域 `{city}.zu.anjuke.com/fangyuan/p{n}/`，
需 DrissionPage 渲染（JS 动态 + 反爬验证码，首次人工过码后会话重放）。
卡片结构：`div.zu-itemmod`，详情 URL 在 `link` 属性。

抽取：户型拆解(室/厅)、面积、朝向、楼层/地铁、月租金(元/月)、租期类型、小区名。
经纬度在 ETL 阶段由 geocoder 补全（拉取阶段用 NOOP_GEOCODER 跳过）。
"""

import csv
import re

from lxml import etree

RENT_HEADERS = [
    "title",
    "community",
    "district",
    "bedrooms",
    "halls",
    "bathrooms",
    "area_sqm",
    "direction",
    "floor",
    "monthly_rent_yuan",
    "rent_type",
    "latitude",
    "longitude",
    "url",
]


class _NoopGeocoder:
    """拉取阶段跳过地理编码的桩：geocode 返回 (None, None)，ETL 阶段再补。"""

    def geocode(self, community_name, full_text=""):
        return None, None


NOOP_GEOCODER = _NoopGeocoder()


def parse_rent_schema_housing(html_content, district_tag="bj", geocoder=None):
    """解析安居客出租列表页（zu.anjuke.com），返回出租 schema 记录列表。

    卡片 `div.zu-itemmod`，详情 URL 从 `link` 属性提取；
    其余字段从卡片全文正则抽取。
    """
    if geocoder is None:
        geocoder = NOOP_GEOCODER

    houses = []
    tree = etree.HTML(html_content)
    # 出租卡片：zu-itemmod（主 class 或含子类）
    cards = tree.xpath("//div[contains(@class, 'zu-itemmod')]")
    if not cards:
        cards = tree.xpath("//div[@class='property']")
    if not cards:
        cards = tree.xpath("//div[contains(@class, 'property-card')]")

    seen_keys = set()

    for card in cards:
        try:
            # 详情 URL：优先 link 属性
            link_attr = card.get("link", "")
            url = ""
            if link_attr:
                # 提取干净详情 URL（去掉 tracking 参数）
                m = re.search(r"(https?://[^\s?]+/fangyuan/\d+)", link_attr)
                if m:
                    url = m.group(1)
            if not url:
                href_nodes = card.xpath(".//a[contains(@href, '/fangyuan/')]/@href | .//a/@href")
                url = href_nodes[0] if href_nodes else ""

            full_text = " ".join([t.strip() for t in card.xpath(".//text()") if t.strip()])

            # 标题：卡片第一段有意义的文本（跳过"安选/实图/已认证"等标记）
            title = ""
            title_nodes = card.xpath(
                ".//div[contains(@class, 'title')]/text() | .//a[contains(@class, 'title')]/text() | .//h3/text()"
            )
            if title_nodes:
                title = title_nodes[0].strip()
            if not title:
                # 取卡片首段文本：跳过常见标记词
                skip_words = ("安选", "实图", "已认证", "精装", "主卧", "次卧", "独卫")
                tokens = [t for t in full_text.split() if t and t not in skip_words]
                if tokens:
                    title = tokens[0]
            if not title:
                continue

            # 小区名：comm-name 节点或正则
            community = ""
            comm_nodes = card.xpath(
                ".//span[contains(@class, 'comm-name')]/text() | .//div[contains(@class, 'comm-name')]/text() | .//a[contains(@class, 'comm')]/text()"
            )
            if comm_nodes:
                community = comm_nodes[0].strip()
            if not community:
                comm_match = re.search(
                    r"([^\s|]+(?:小区|家园|花园|新村|公寓|名邸|雅苑|城|华庭|苑|村))", full_text
                )
                if comm_match:
                    community = comm_match.group(1)

            # 月租金：N 元/月
            monthly_rent_yuan = None
            rent_match = re.search(r"(\d+)\s*元/月", full_text)
            if rent_match:
                monthly_rent_yuan = int(rent_match.group(1))
            else:
                price_nodes = card.xpath(
                    ".//span[contains(@class, 'price')]/text() | .//span[contains(@class, 'num')]/text()"
                )
                if price_nodes:
                    pm = re.search(r"\d+", price_nodes[0])
                    if pm:
                        monthly_rent_yuan = int(pm.group(0))

            dedup_key = url if url else f"{title}_{community}_{monthly_rent_yuan}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            rooms_match = re.search(r"(\d+)\s*室", full_text)
            bedrooms = int(rooms_match.group(1)) if rooms_match else 0

            halls_match = re.search(r"(\d+)\s*厅", full_text)
            halls = int(halls_match.group(1)) if halls_match else 0

            baths_match = re.search(r"(\d+)\s*卫", full_text)
            bathrooms = int(baths_match.group(1)) if baths_match else 0

            area_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:㎡|平米|m²)", full_text)
            area_sqm = float(area_match.group(1)) if area_match else None

            dir_match = re.search(r"(南北|东南|西南|东北|西北|南|北|东|西)", full_text)
            direction = dir_match.group(1) if dir_match else ""

            # 楼层/地铁线索
            floor = ""
            floor_match = re.search(r"((?:高层|中层|低层|顶层|底层)|(\d+)/\d+层)", full_text)
            if floor_match:
                floor = floor_match.group(1) or floor_match.group(0)
            elif re.search(r"\d+号线", full_text):
                m2 = re.search(r"((?:\d+/)*\d+号线)", full_text)
                floor = f"地铁{m2.group(1)}" if m2 else ""

            rent_type = ""
            if "合租" in full_text:
                rent_type = "合租"
            elif "整租" in full_text:
                rent_type = "整租"

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
                    "monthly_rent_yuan": monthly_rent_yuan,
                    "rent_type": rent_type,
                    "latitude": lat,
                    "longitude": lng,
                    "url": url,
                }
            )
        except Exception:
            continue

    return houses


def save_rent_csv(houses, filename="anjuke_rent_houses.csv"):
    with open(filename, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RENT_HEADERS)
        writer.writeheader()
        for h in houses:
            writer.writerow({k: h.get(k) for k in RENT_HEADERS})
    print(f"[+] Saved {len(houses)} rent records into {filename}")
