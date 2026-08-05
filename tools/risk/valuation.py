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
import re
import statistics
import sys

import config

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
    """从 spacefin_crawler 读 sale DWD，按 (城市码, 地名) 聚合单位价中位数。

    返回 {(city_code, name): median_unit_price_yuan}。

    **列名陷阱**：`crawl_housing_sale.district` 存的是**城市码**（gz/sz/fs…），
    `community` 存的才是市内地名——两列的名字都和内容对不上，SELECT 里的 AS 别名
    按真实语义重命名，不要照列名理解。

    `community` 绝大多数是**小区名**（去重 1 万个，如「恒大城」「保利紫云府」），
    只有极少数是区/镇级聚合（禅城 / 惠城 / 清城…）。抵押物地址解析出的是**区名**，
    与小区名不同粒度，故本级回退天然低命中——见 `valuation_from_dwd`。

    `DWD_MIN_SAMPLES` 门槛过滤掉样本不足的键：库里 78% 的键只有 1 行，
    单条挂牌的「中位数」不是行情，放进来只会造出离谱估值。
    """
    out = {}
    cur = conn.cursor()
    cur.execute(
        """
        SELECT district AS city_code, community AS name, unit_price_yuan
        FROM crawl_housing_sale
        WHERE district IS NOT NULL AND unit_price_yuan > 0
        """
    )
    buckets: dict[tuple, list] = {}
    for city, name, up in cur.fetchall():
        if not name:
            continue
        buckets.setdefault((city, name), []).append(float(up))
    cur.close()
    for k, vals in buckets.items():
        if len(vals) >= config.DWD_MIN_SAMPLES:
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


# 地址解析三段式：剥市名前缀 → 取首个政区 token → 剥政区后缀。
# 分三步而不是一条大正则，是因为「后缀要不要剥」依赖剥完后剩几个字（见下方 ≥2 字守卫）。
_CITY_PREFIX_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}市")
_ADMIN_TOKEN_RE = re.compile(r"^([\u4e00-\u9fa5]{1,6}?(?:区|县|市|街道|镇))")
_ADMIN_SUFFIX_RE = re.compile(r"(?:区|县|市|街道|镇)$")


def _district_from_addr(addr: str) -> str | None:
    """从地址文本提取区/县/镇级地名：「广州市天河区体育西路 1 号」→「天河」。合成地址返回 None。

    去掉政区后缀是为了对齐 DWD 键——`crawl_housing_sale.community` 里的粗粒度地名一律
    不带后缀（禅城 / 惠城 / 清城，库里不存在任何「…区」形式的 community）。

    **≥2 字守卫**：剥完后缀若只剩 1 个字，说明后缀本身是地名的一部分，退回不剥的原 token。
    中国没有单字区名——「西区」(中山)、「城区」(汕尾)、「梅县」(梅州) 剥成「西」「城」「梅」
    都不再是地名。这条守卫不是为凑命中，是为不造出假地名。

    命中率的天花板不在本函数：见 `valuation_from_dwd` 的说明。
    """
    if not addr or addr.startswith("合成地址"):
        return None
    body = _CITY_PREFIX_RE.sub("", addr, count=1)
    m = _ADMIN_TOKEN_RE.match(body)
    if not m:
        return None
    token = m.group(1)
    stripped = _ADMIN_SUFFIX_RE.sub("", token)
    return stripped if len(stripped) >= 2 else token


def valuation_from_dwd(dwd_unit: dict, collateral: dict, city_map: dict) -> float | None:
    """用 DWD 行情估抵押物：unit_price × area。命中返回元/㎡×㎡，未命中返回 None。

    **本级命中率天然很低，这是数据粒度差异，不是 bug**（2026-08-05 实测 22/200 → 11%）：
    抵押物地址只能解析到**区级**（天河 / 南海 / 斗门…，200 笔覆盖 64 个 (城市,区) 组合），
    而 DWD 的 `community` 是**小区级**（1 万个去重值）。两套地名体系只在少数几个
    「区名恰好被当作 community 落库」的键上相交——实测仅 6 个键（惠城 / 清城 / 榕城 /
    源城 / 江城 / 禅城），且这 6 个都是 255–919 行的真区级聚合，命中质量可靠
    （DWD 估值 / true_market_price 中位 0.95）。

    要提高本级命中率，正确做法是补齐 DWD 的行政区字段（爬虫侧解析 city+district），
    而不是在这里维护「区名 → 小区名」的映射表——那是用假映射掩盖数据缺口。
    """
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
