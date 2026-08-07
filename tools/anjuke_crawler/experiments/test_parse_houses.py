#!/usr/bin/env python3
"""
实验：列表页卡片解析探针（lxml，早期版）

用途：验证 XPath 能从安居客列表页抽出结构化字段。这是 parser.py / parser_advanced.py
的前身——注意它用了 `contains(@class,'property')`，会把子节点重复计入（每套 4 次），
正式解析器已修为精确 `//div[@class='property']`（见 parser.py 头部说明）。
依赖：lxml。
"""

from lxml import etree


def parse_html_file(filepath="../tests/sample_listing.html"):
    with open(filepath, encoding="utf-8") as f:
        html = f.read()

    tree = etree.HTML(html)
    cards = tree.xpath("//div[contains(@class, 'property')]")  # 早期版：含重复 bug
    print(f"[+] 命中 {len(cards)} 个 property 相关元素（含嵌套子节点，未去重）")

    houses = []
    for card in cards:
        try:
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

            comm = card.xpath(".//div[contains(@class, 'property-content-info-comm-name')]/text()")
            comm = comm[0].strip() if comm else ""
            price = card.xpath(".//span[contains(@class, 'property-price-total-num')]/text()")
            price = price[0].strip() if price else ""
            unit = card.xpath(".//span[contains(@class, 'property-price-average')]/text()")
            unit = unit[0].strip() if unit else ""
            link = card.xpath(".//a/@href")
            url = link[0] if link else ""

            houses.append(
                {
                    "Title": title,
                    "Community": comm,
                    "Total Price (万)": price,
                    "Unit Price": unit,
                    "URL": url,
                }
            )
        except Exception:
            continue
    return houses


if __name__ == "__main__":
    houses = parse_html_file()
    print(f"\n[=== 抽取 {len(houses)} 条（含重复）===]\n")
    for i, h in enumerate(houses[:5], 1):
        print(
            f"#{i}: [{h['Community']}] {h['Title']} - {h['Total Price (万)']}万 ({h['Unit Price']})"
        )
