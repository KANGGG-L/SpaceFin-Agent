"""抵押物估值：优先用房产 DWD 真实行情（crawl_housing_sale），未命中回退业务库 true_market_price。

DWD 匹配轴为 (city_code, district)。collateral.property_addr 目前为合成地址（无城市），
命中率为 0；当业务数据与广东 DWD 对齐后（地址含城市/区域）此路径自动生效。
"""

import statistics


def load_dwd_unit_prices(conn) -> dict:
    """从 spacefin_crawler 读 sale DWD，按 (city, district) 聚合单位价中位数。

    返回 {(city, district): median_unit_price_yuan}。
    """
    out = {}
    cur = conn.cursor()
    cur.execute(
        """
        SELECT district AS city, community AS district, unit_price_yuan
        FROM crawl_housing_sale
        WHERE district IS NOT NULL AND unit_price_yuan > 0
        """
    )
    buckets: dict[tuple, list] = {}
    for city, district, up in cur.fetchall():
        if not district:
            continue
        buckets.setdefault((city, district), []).append(float(up))
    cur.close()
    for k, vals in buckets.items():
        if vals:
            out[k] = statistics.median(vals)
    return out


def _city_code_from_addr(addr: str, city_map: dict) -> str | None:
    """从地址文本提取城市码。city_map: {中文城市名或简称: code}。"""
    if not addr:
        return None
    for name, code in city_map.items():
        if name in addr:
            return code
    return None


def _district_from_addr(addr: str) -> str | None:
    """从地址文本提取区域：形如「XX市天河区」→ 天河。合成地址返回 None。"""
    if not addr or addr.startswith("合成地址"):
        return None
    # 匹配「X区」（不含「X市X区」里的市字干扰，直接找第一个…区）
    import re

    m = re.search(r"([\u4e00-\u9fa5]{1,8}?区)", addr)
    return m.group(1) if m else None


def valuation_from_dwd(dwd_unit: dict, collateral: dict, city_map: dict) -> float | None:
    """用 DWD 行情估抵押物：unit_price × area。命中返回元/㎡×㎡，未命中返回 None。"""
    addr = (collateral.get("property_addr") or "").strip()
    code = _city_code_from_addr(addr, city_map)
    district = _district_from_addr(addr)
    area = collateral.get("area")
    if not code or not district or not area:
        return None
    up = dwd_unit.get((code, district))
    if not up:
        return None
    return round(up * float(area), 2)
