#!/usr/bin/env python3
"""在架流转率（Active Listing Turnover）纯函数（计划 S4）。

不连库、不读 env、不 import etl/census——判定必须能脱离 DB/Redis/网络验收，否则只能用
「跑一遍看数字漂不漂亮」来验收，那正是假绿的入口（与 `market_state.decide_removal`、
`census.judge` 同一条纪律）。

指标定义（口径已定，勿改）：
    在架流转率 = (下架数 + 新挂数) / ((在架(t0) + 在架(t1)) / 2)
    下架率     = 下架数 / 平均在架数
    新挂率     = 新挂数 / 平均在架数
    在架数     = 当日被**合格观测**覆盖且该房源出现 —— 严格按 `status == 'active'` 计数

- `t0` = 当日开始、`t1` = 当日结束。用池恒等式反推 t0：
  `在架(t0) = 在架(t1) - 新挂 + 下架`（下架的在 t0 还在池里，新挂的在 t0 还没进池）。
- **分母为 0 一律返回 `None`，不是 `0.0`**：0.0 会被读成「零流转」的健康信号，而真相是
  「今天没有合格观测，什么都不知道」。前端据此显示「无合格观测」。
- `absent` / `unknown` 两个状态**既不进分子也不进分母**：它们是「不知道」，不是「没了」。
  这是「没看就等于不知道，不等于没了」在指标侧的落地点。

毛版 / 净版（D-4：对外默认净版，毛版留副标注）：
- 毛版按 `url_key` 照实算：房东删了重发会拿到新 ID，看起来就是「一套下架 + 一套新挂」。
- 净版用 `unit_sig`（小区+户型+面积+楼层段+价格段哈希）抵消同 ds 内的重挂对子：
  **一个 removed 配一个「当日新挂」的 active、且 unit_sig 相同** 才算一对重挂。
  只配「新挂」而不配任何 active，否则会把长期在架的同户型房源当成重挂抵消掉。
- **重挂不合并主键**：抵消只发生在计数层面，`url_key` 一个都不合并。
- 分母两版相同：抵消的是「一进一出」，池子大小没变，
  故 `net = (gross分子 - 2*抵消数) / 同一分母`，天然满足 **毛 ≥ 净**。

行契约（`rows` 是**已按当日过滤**的逐 key 切片，来自 `dwd_listing_market_state`）：
    city / typ          分组维度（city 是解析后的归属：21 城短码 / 省外子域原文 / unknown）
    status              active|absent|unknown|removed
    ds / first_seen_date 同值即判为当日新挂
    unit_sig            可为 None；为 None 的 key 只进毛版，不参与净版抵消
    total_price_wan     或已算好的 price_bucket（二选一，优先后者）
    complete            该 (city,typ) 当日是否完备（来自 crawl_census），**必须显式传 1**。
                        缺省 0（缺省即未知：不知道合不合格 = 不合格），组内**任一**行
                        不合格即整天剔除。宁可让指标返回 `None`，也不静默用不合格数据
                        算出一个数——后者会让「看起来合理的流转率」实际是噪声。

注意口径边界：`absent`（连续缺席但未达 N 天）既不在架也没下架，会从在架池里消失，
使**分母偏小、流转率偏高**。这是「在架 = 当日出现」这一定义的直接后果（S3 数据里
absent 为 0 时尚未暴露）。故 metrics 里外露 `absent` 计数：分母的口径必须透明可见，
否则读者看到流转率升高时，分不清是市场真变化还是缺席变多。
"""

from __future__ import annotations

import math
from collections import Counter

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
STATUS_ABSENT = "absent"

UNKNOWN_BUCKET = "unknown"
# 半开区间 [lo, hi)：保证「不重不漏」——每个有限且非负的价格恰好落进一个桶
_PRICE_BUCKETS = ((0, 50), (50, 100), (100, 200), (200, 300), (300, 500), (500, 1000))
_TOP_BUCKET = "1000+"


def price_bucket(total_price_wan) -> str:
    """总价（万元）→ 价格段标签。分桶必须是一个划分：不重、不漏。

    不可解析 / 负数 / NaN 一律 `unknown`：它们确实存在，只是没有价格证据，
    单列一桶比混进任一价格段都安全（混进去会污染该段的均价与流转率）。
    """
    try:
        v = float(total_price_wan)
    except (TypeError, ValueError):
        return UNKNOWN_BUCKET
    if math.isnan(v) or v < 0:
        return UNKNOWN_BUCKET
    for lo, hi in _PRICE_BUCKETS:
        if v < hi:
            return f"{lo}-{hi}"
    return _TOP_BUCKET


def _is_new(row: dict) -> bool:
    """当日新挂：首见日 == 该行所属观测日（两个字段都来自同一行，无需外部传 ds）。"""
    ds, first = row.get("ds"), row.get("first_seen_date")
    return ds is not None and first is not None and first == ds


def _rate(numerator: float, denominator: float):
    """分母为 0 → None（不是 0.0）。这是本模块最重要的一条防假绿约定。"""
    return None if not denominator else numerator / denominator


def _cancel_pairs(removed_rows: list, new_rows: list) -> int:
    """净版抵消数：同 unit_sig 的「一 removed 一 当日新挂 active」配对数。

    `unit_sig` 为 None 的 key 没有指纹，无法判断是不是重挂 → 不参与配对（只进毛版）。
    """
    removed_sigs = Counter(r.get("unit_sig") for r in removed_rows if r.get("unit_sig"))
    new_sigs = Counter(r.get("unit_sig") for r in new_rows if r.get("unit_sig"))
    return sum(min(n, new_sigs[sig]) for sig, n in removed_sigs.items())


def _metrics(city, typ, bucket, rows: list) -> dict:
    # 整天剔除：该 (city,typ) 当日不完备 → 分子分母一律不计。
    # 缺省 0（缺字段 = 不知道合不合格 = 不合格）：乐观缺省会让不合格的 city-day 静默
    # 进分母，算出一个看起来合理、实际失真的流转率，且没有任何报错。
    complete = 1 if all(r.get("complete", 0) for r in rows) else 0
    active_rows = [r for r in rows if r.get("status") == STATUS_ACTIVE]
    removed_rows = [r for r in rows if r.get("status") == STATUS_REMOVED]
    absent_rows = [r for r in rows if r.get("status") == STATUS_ABSENT]
    new_rows = [r for r in active_rows if _is_new(r)]

    if not complete:
        # 整天剔除时所有计数一律归零（`keys` 仍保留行数供对账）：不合格的 city-day
        # 不提供任何证据，连「有多少缺席」都不该被当成结论。
        active_rows, removed_rows, new_rows, absent_rows = [], [], [], []

    n_active = len(active_rows)
    n_removed = len(removed_rows)
    n_new = len(new_rows)
    active_t0 = n_active - n_new + n_removed
    denom = (active_t0 + n_active) / 2
    cancelled = _cancel_pairs(removed_rows, new_rows)

    return {
        "city": city,
        "typ": typ,
        "price_bucket": bucket,
        "keys": len(rows),
        "complete": complete,
        "active_t1": n_active,
        "active_t0": active_t0,
        "removed": n_removed,
        "new": n_new,
        # 诊断计数：不进分子也不进分母，但必须外露——分母偏小的偏差方向要看得见
        "absent": len(absent_rows),
        "cancelled": cancelled,
        "denominator": denom,
        "gross": _rate(n_removed + n_new, denom),
        "net": _rate(n_removed + n_new - 2 * cancelled, denom),
        "removal_rate": _rate(n_removed, denom),
        "new_rate": _rate(n_new, denom),
        "net_removal_rate": _rate(n_removed - cancelled, denom),
        "net_new_rate": _rate(n_new - cancelled, denom),
    }


def compute_turnover(rows) -> dict:
    """按 `city × typ × 价格段` 分组算在架流转率。

    -> {(city, typ, price_bucket): {gross, net, removal_rate, new_rate,
                                    net_removal_rate, net_new_rate,
                                    denominator, active_t0, active_t1,
                                    removed, new, absent, cancelled,
                                    keys, complete}}

    `gross/net/removal_rate/new_rate/net_*` 在分母为 0 时是 `None`；
    `complete=0` 的组（该 city-day 无合格观测，或 `complete` 字段缺失）所有计数归零、
    全部比率为 `None`。`absent` 是不进任何计算的诊断计数，用于看出「分母偏小」的偏差。
    """
    groups: dict = {}
    for r in rows:
        bucket = r.get("price_bucket") or price_bucket(r.get("total_price_wan"))
        groups.setdefault((r.get("city"), r.get("typ"), bucket), []).append(r)

    out = {}
    for key in sorted(groups, key=lambda g: tuple(str(x) for x in g)):
        city, typ, bucket = key
        out[key] = _metrics(city, typ, bucket, groups[key])
    return out
