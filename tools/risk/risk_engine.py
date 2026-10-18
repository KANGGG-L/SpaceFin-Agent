"""风险引擎：估值 → LTV → 五级分类 → 贷后保全预警（AC-02/03/04）。

- 估值：三级回退，见 valuation.py——AVM（S2 模型）→ DWD 行情 → true_market_price。
- LTV = 贷款余额 / 抵押物估值。
- 五级分类：按 LTV 阈值(config.CLASS_LTV_UPPER)分档。
- 低置信(AC-04)：spatial_feat_missing_pct **严格大于** 75（0–100 标度，恰好=75 不低置信）
  → 标记 low_confidence，不触发自动预警。
- 预警(AC-03)：LTV 非空且非低置信时按两档触发——LTV 严格大于警示线 0.75 → 'warn'；
  严格大于强预警线 0.85 → 'strong'（strong 覆盖 warn）；alert 布尔保持兼容下游。
- 异常估值(R-UNW-03)：AVM 命中时按 |AVM 估值 - true_market_price| / true_market_price 算偏差，
  严格大于 30%（config.VALUATION_DEVIATION_THRESHOLD）→ abnormal_valuation，走人工核查。
- 血缘(R-UBQ-01)：每行带 model_version，模型缺失/无版本号 → 'unknown' 并触发「不可溯源」告警。
"""

from __future__ import annotations

import math

import config  # noqa: F401  (运行入口 tools/risk/main.py 已把本目录加入 sys.path)
import valuation

# 哨兵：未传 avm_value 时走逐笔 valuation_from_avm（CDP 增量 / 测试默认路径，保持旧行为）。
_NO_AVM_BATCH = object()


def _avm_estimate_is_community_level(model, collateral: dict, city_map: dict) -> bool:
    """AVM 估值是否落在「小区级」：实际匹配到训练产物的 (城市,小区) 目标编码。

    只有小区级估值才与按小区合成的 true_market_price 同粒度、可比；城市/全局兜底
    估值粒度更粗，与小区级基准比会系统性偏离，不应触发 R-UNW-03（见 enrich_loan
    的偏差计算）。复用了 valuation 的地址解析，与 AVM 推理时的小区匹配口径一致。
    """
    if not isinstance(model, dict) or not model.get("encoders"):
        return False
    addr = (collateral.get("property_addr") or "").strip()
    if not addr or addr.startswith("合成地址"):
        return False
    code = valuation._city_code_from_addr(addr, city_map)
    if not code:
        return False
    comm = valuation._community_from_addr(addr)
    if not comm:
        return False
    return (code, comm) in model["encoders"].get("comm", {})


# P6 空间惩罚项（A1：城市粒度）纯函数：分类提级语义复用 config.CLASS_ORDER。
# 与下方硬编码高危区提级同源，由可配置策略驱动（ltv_cap/zone/reject 延后到 A2）。
_ORDER = config.CLASS_ORDER  # ["正常","关注","次级","可疑","损失"]


def _bump_one_tier(cls):
    """strict：把分类抬升一档，封顶「损失」。"""
    i = _ORDER.index(cls)
    return _ORDER[min(i + 1, len(_ORDER) - 1)]


def _floor_at(cls, floor):
    """notice：把分类至少抬到 floor（仅上调，不下压）。"""
    return _ORDER[max(_ORDER.index(cls), _ORDER.index(floor))]


def _city_code_from_addr(addr):
    """从抵押物地址解析城市码（复刻 p6_policy._city_from_addr）。"""
    if not addr:
        return None
    for name, code in config.CITY_MAP.items():
        if str(addr).startswith(name):
            return code
    return None


# 网格命中半径：复用项目「局部邻域」口径 tools/spatial/config.NEIGHBOR_RADIUS_KM = 5.0
ZONE_MATCH_RADIUS_KM = 5.0


def _haversine_km(lat1, lng1, lat2, lng2):
    """两点经纬度球面距离（km）。"""
    r = 6371.0
    p = math.pi / 180.0
    a = (
        math.sin((lat2 - lat1) * p / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _apply_policy_rule(rule, risk_class, ltv):
    """规则命中(ltv 超 cap 或无 cap)才生效；返回 (risk_class, rejected, hit)。

    - notice：至少抬到「关注」（_floor_at）
    - strict：抬一档（_bump_one_tier）
    - reject：维持 risk_class，仅置 rejected=True
    hit 为 True 表示该规则真正命中（ltv 超 cap 或无 cap）——供 enrich_loan 收集
    policy_hits（P6 策略推演中心规则快照逐笔展示「命中哪条策略、动作是什么」）。口径只此一处。
    """
    if not rule:
        return risk_class, False, False
    action = rule.get("action")
    cap = rule.get("ltv_cap")
    hit = ltv is not None and (cap is None or ltv > cap)
    if not hit:
        return risk_class, False, False
    if action == "notice":
        return _floor_at(risk_class, "关注"), False, True
    if action == "strict":
        return _bump_one_tier(risk_class), False, True
    if action == "reject":
        return risk_class, True, True
    return risk_class, False, True


def _append_policy_hit(policy_hits: list, rule: dict, scope_type: str, scope_value) -> None:
    """把命中的策略追加进 policy_hits（P6 策略推演中心规则快照逐笔展示用）。

    记录 scope_type/scope_value/action/ltv_cap 四元组；rule 理论上必非 None
    （_apply_policy_rule 已判过），防御性跳过以防脏数据进列表。
    """
    if not rule:
        return
    policy_hits.append(
        {
            "scope_type": scope_type,
            "scope_value": scope_value,
            "action": rule.get("action"),
            "ltv_cap": rule.get("ltv_cap"),
        }
    )


def enrich_loan(
    loan: dict,
    collateral: dict | None,
    customer: dict | None,
    dwd_unit: dict,
    city_map: dict,
    avm_model=None,
    avm_value=_NO_AVM_BATCH,
    policy_map=None,
) -> dict:
    """单笔贷款打宽：估值/LTV/五级/低置信/预警。

    avm_model 为 None 时跳过 AVM 估值，直接走 DWD/true_market_price（无模型环境兼容）。
    avm_value：性能修复用——由 store.compute_rows 整批预计算后传入（非哨兵即采用该值，
    即使为 None 也视为「AVM 已判定不适用→回退」）；为哨兵时退回逐笔
    valuation_from_avm(avm_model, ...)，与旧行为完全一致（CDP 增量 / 测试路径）。
    """
    if collateral is None:
        return {
            **loan,
            "market_valuation": None,
            "ltv": None,
            "risk_class": "损失",  # 无抵押物视为风险敞口缺失，保守归损失
            "low_confidence": True,
            "is_high_risk_zone": None,
            "alert": False,
            "alert_level": None,
            "dwd_hit": False,
            "avm_hit": False,
            # R-UBQ-01：每笔估值结论都带模型版本；无抵押物无估值，但血缘信息仍随行落库
            "model_version": valuation.model_version(avm_model),
            "valuation_deviation_pct": None,
            "abnormal_valuation": False,
            "is_rejected": False,
            "policy_hits": [],
        }

    if avm_value is not _NO_AVM_BATCH:
        # 整批预计算路径（性能修复）：直接采用预估值；None 表示 AVM 不适用 → 回退。
        val = avm_value
    else:
        val = valuation.valuation_from_avm(avm_model, collateral, city_map)
    avm_hit = val is not None
    dwd_hit = False
    if val is None:
        # 只有 AVM 未命中才试 DWD；否则 dwd_hit 会因 val 非 None 被误置 True，
        # 导致 risk_report 的 avm_hits 与 dwd_hits 同时累计、fallback 变负数。
        val = valuation.valuation_from_dwd(dwd_unit, collateral, city_map)
        dwd_hit = val is not None
    if val is None:
        val = float(collateral.get("true_market_price") or 0.0)  # 回退业务库价格
    balance = float(loan.get("balance") or 0.0)
    ltv = round(balance / val, 4) if val and val > 0 else None

    # R-UNW-03 异常估值：只对 AVM 估值算偏差，参考基准是业务库 true_market_price。
    # 口径约束（全链路验证发现）：true_market_price 按「小区(DWD 键)」层级合成
    # （有键城市取其 DWD 中位单价、无键城市取旧版 AVM 隐含单价，均 ×面积×(0.9~1.1)），
    # 与 AVM 训练所用的 crawl_housing_sale 同一价格量级。但 AVM 估值有三级回退粒度：
    # 命中(城市,小区)目标编码=小区级；只命中城市=城市中位兜底；两者皆无=全局中位兜底。
    # 只有当 AVM 给出「小区级」估值时，其粒度才与小区级 true_market_price 可比；
    # 城市/全局兜底估值与小区级基准不同粒度，直接比会系统性偏离（实测城市兜底 58%、
    # 全局兜底 97% 触发 >30%），属口径不可比而非真实误估——故置 None（不可判）而非 0，
    # 与未命中 AVM 时同口径。这样 R-UNW-03 只剩小区级的真实偏差信号，告警风暴消失。
    deviation_pct = None
    if avm_hit and val and val > 0:
        baseline = float(collateral.get("true_market_price") or 0.0)
        if baseline > 0:
            # 口径约束：仅当「有模型且 AVM 命中 (城市,小区) 目标编码（小区级）」时，估值粒度
            # 才与按小区合成的 true_market_price 可比，偏差才有意义。AVM 回退到城市/全局中位
            # 兜底时（粒度更粗）与小区级基准不同粒度，直接比会系统性偏离（实测城市兜底 58%、
            # 全局兜底 97% 触发 >30%），属口径不可比而非真实误估，置 None（不可判）而非 0。
            # 无模型路径（旧路径/单测 mock）按旧口径全程可判，保持向后兼容。
            if avm_model is None or _avm_estimate_is_community_level(
                avm_model, collateral, city_map
            ):
                deviation_pct = round(abs(val - baseline) / baseline, 4)
    # 严格大于阈值才标记（B-04：偏差恰好 = 30% 不标）
    abnormal_valuation = bool(
        deviation_pct is not None and deviation_pct > config.VALUATION_DEVIATION_THRESHOLD
    )

    missing_pct = float(collateral.get("spatial_feat_missing_pct") or 0.0)
    # AC-04：缺失率**严格大于**阈值才低置信；恰好 = 75 不标记（与 B-04 边界口径一致）。
    low_conf = missing_pct > config.LOW_CONF_MISSING_PCT
    risk_class = config.classify(ltv)
    # 高危区域叠加：LTV 处于「正常/关注」但处高危区，至少升到「关注」
    if collateral.get("is_high_risk_zone") and risk_class in ("正常", "关注"):
        risk_class = "关注"

    # P6 空间惩罚项（US-01，A2：city + zone 粒度，统一规则应用）：
    # 命中口径 = 在作用域内 且 ltv 超 cap（或无 cap）；reject 仅置标记、不改分类。
    # policy_hits：P6 策略推演中心规则快照逐笔展示「命中哪条策略、动作是什么」，口径与 _apply_policy_rule 一致。
    rejected = False
    policy_hits: list[dict] = []
    if policy_map:
        city = _city_code_from_addr(collateral.get("property_addr"))
        if city:
            rc, rj, hit = _apply_policy_rule(policy_map.get(("city", city)), risk_class, ltv)
            risk_class, rejected = rc, rejected or rj
            if hit:
                _append_policy_hit(policy_hits, policy_map.get(("city", city)), "city", city)
        # 行业粒度（US-01）：贷款经 customer_id 继承借款人行业；无客户档/行业为空不命中。
        ind = (customer or {}).get("industry")
        if ind:
            rc, rj, hit = _apply_policy_rule(policy_map.get(("industry", ind)), risk_class, ltv)
            risk_class, rejected = rc, rejected or rj
            if hit:
                _append_policy_hit(policy_hits, policy_map.get(("industry", ind)), "industry", ind)
        # 网格粒度（A2）：抵押物坐标落入某 zone 中心 5km 内才命中
        lat = collateral.get("lat")
        lng = collateral.get("lng")
        if lat is not None and lng is not None:
            for (st, sv), rule in policy_map.items():
                if st != "zone" or not rule.get("center"):
                    continue
                clat, clng = rule["center"]
                if (
                    clat is not None
                    and clng is not None
                    and _haversine_km(lat, lng, clat, clng) <= ZONE_MATCH_RADIUS_KM
                ):
                    rc, rj, hit = _apply_policy_rule(rule, risk_class, ltv)
                    risk_class, rejected = rc, rejected or rj
                    if hit:
                        _append_policy_hit(policy_hits, rule, "zone", sv)

    # AC-03 两档预警：LTV 非空且非低置信才判级。LTV 严格大于强预警线 → 'strong'；
    # 严格大于警示线 → 'warn'；恰好 = 阈值不触发。strong 覆盖 warn，alert 布尔兼容下游。
    alert_level = None
    if ltv is not None and not low_conf:
        if ltv > config.LTV_RED_LINE:
            alert_level = "strong"
        elif ltv > config.LTV_WARN_LINE:
            alert_level = "warn"
    alert = alert_level is not None

    return {
        **loan,
        "market_valuation": val,
        "ltv": ltv,
        "risk_class": risk_class,
        "low_confidence": low_conf,
        "is_high_risk_zone": collateral.get("is_high_risk_zone"),
        "alert": alert,
        "alert_level": alert_level,
        "dwd_hit": dwd_hit,
        "avm_hit": avm_hit,
        "customer_id": loan.get("customer_id"),
        "model_version": valuation.model_version(avm_model),
        "valuation_deviation_pct": deviation_pct,
        "abnormal_valuation": abnormal_valuation,
        "is_rejected": rejected,
        "policy_hits": policy_hits,
    }


def build_aggregate(rows: list[dict]) -> dict:
    """DWS 聚合：五级分类贷款数/余额/占比。"""
    agg = {cls: {"count": 0, "balance": 0.0} for cls in config.CLASS_ORDER}
    for r in rows:
        cls = r["risk_class"]
        if cls in agg:
            agg[cls]["count"] += 1
            agg[cls]["balance"] += float(r.get("balance") or 0.0)
    total_bal = sum(v["balance"] for v in agg.values())
    return {
        "by_class": {
            cls: {**v, "balance_pct": round(v["balance"] / total_bal, 4) if total_bal else 0.0}
            for cls, v in agg.items()
        },
        "total_loans": len(rows),
        "total_balance": round(total_bal, 2),
    }
