"""抵押物估值：AVM 模型 → DWD 真实行情 → 业务库 true_market_price 三级回退。

匹配策略：
1. **AVM（S2）**：tools/avm 训练的 GBDT+空间特征模型。必须能解析出广东城市码才有意义
   ——AVM 对未知城市会回退全局中位价，而业务库当前是上海合成地址（无广东城市码），
   强行套全局中位价会让 LTV 失真，故与 DWD 一样以城市码为命中前提。
2. **DWD 行情**：(city, district) 中位单价 × 面积。
3. **true_market_price**：业务库合成价，兜底。

未命中 AVM/DWD 时返回 None，由调用方回退下一级。
"""

import os
import statistics
import sys

_AVM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "avm")
_avm_predict = None  # 模块级惰性加载：AVM 依赖缺失时首次调用返回 None，不炸风险引擎


def _avm_module():
    global _avm_predict
    if _avm_predict is None:
        if _AVM_DIR not in sys.path:
            sys.path.insert(0, _AVM_DIR)
        try:
            import predict as avm_predict  # noqa: PLC0415

            _avm_predict = avm_predict
        except Exception:
            return None
    return _avm_predict


def load_avm_model() -> object | None:
    """惰性加载 AVM 模型（tools/avm 产物，output/avm/model.joblib）。

    AVM 的 sklearn/joblib 是惰性导入：模型缺失或依赖缺失时返回 None，不抛异常，
    让估值链回退到 DWD/true_market_price。模型文件名固定，路径相对仓库根。
    """
    model_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "output",
        "avm",
        "model.joblib",
    )
    if not os.path.exists(model_path):
        return None
    mod = _avm_module()
    if mod is None:
        return None
    try:
        return mod.load_model(model_path)
    except Exception:
        return None


def model_version(model: object | None) -> str:
    """取 AVM 模型产物版本号；模型缺失或产物无 version 键 → 'unknown'（R-UBQ-01）。

    版本号是血缘分组的锚点：Worker A 重训后在产物 dict 加 version 键，这里只读兼容。
    产物没有 version 说明估值结论无法追溯到具体训练版本，正是 E-06「不可溯源」的触发条件。
    """
    if model is None:
        return "unknown"
    if not isinstance(model, dict):
        return "unknown"
    ver = model.get("version")
    return str(ver) if ver else "unknown"


def valuation_from_avm(model, collateral: dict, city_map: dict) -> float | None:
    """用 AVM 估抵押物总价（元）。城市码缺失（如合成地址）返回 None，保持原回退语义。

    property_addr 含广东城市名才能命中；community 从地址里提不出来时传 None，
    由模型回退城市中位价。坐标直接用 collateral 的 lat/lng（种子数据为上海坐标，
    与广东模型空间带不符，命中城市码前坐标不参与判断）。
    """
    if model is None:
        return None
    addr = (collateral.get("property_addr") or "").strip()
    code = _city_code_from_addr(addr, city_map)
    area = collateral.get("area")
    if not code or not area or float(area) <= 0:
        return None
    mod = _avm_module()
    if mod is None:
        return None
    try:
        return mod.estimate_total_price(
            model,
            city_code=code,
            community=None,  # 地址粒度不够时让模型回退城市中位
            area_sqm=float(area),
            building_age=collateral.get("age"),
            bedrooms=None,
            latitude=collateral.get("lat"),
            longitude=collateral.get("lng"),
        )
    except Exception:
        return None


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
