"""风险引擎：估值 → LTV → 五级分类 → 贷后保全预警（AC-02/03/04）。

- 估值：三级回退，见 valuation.py——AVM（S2 模型）→ DWD 行情 → true_market_price。
- LTV = 贷款余额 / 抵押物估值。
- 五级分类：按 LTV 阈值(config.CLASS_LTV_UPPER)分档。
- 低置信(AC-04)：spatial_feat_missing_pct >= 25% → 标记 low_confidence，不触发自动预警。
- 预警(AC-03)：LTV > 红线(0.85) 且非低置信 → 进入 ads_ltv_alerts。
- 异常估值(R-UNW-03)：AVM 命中时按 |AVM 估值 - true_market_price| / true_market_price 算偏差，
  严格大于 30%（config.VALUATION_DEVIATION_THRESHOLD）→ abnormal_valuation，走人工核查。
- 血缘(R-UBQ-01)：每行带 model_version，模型缺失/无版本号 → 'unknown' 并触发「不可溯源」告警。
"""

from __future__ import annotations

import config  # noqa: F401  (运行入口 tools/risk/main.py 已把本目录加入 sys.path)
import valuation


def enrich_loan(
    loan: dict,
    collateral: dict | None,
    customer: dict | None,
    dwd_unit: dict,
    city_map: dict,
    avm_model=None,
) -> dict:
    """单笔贷款打宽：估值/LTV/五级/低置信/预警。

    avm_model 为 None 时跳过 AVM 估值，直接走 DWD/true_market_price（无模型环境兼容）。
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
            "dwd_hit": False,
            "avm_hit": False,
            # R-UBQ-01：每笔估值结论都带模型版本；无抵押物无估值，但血缘信息仍随行落库
            "model_version": valuation.model_version(avm_model),
            "valuation_deviation_pct": None,
            "abnormal_valuation": False,
        }

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
    # 回退链（DWD/true_market_price）本身不产生「模型判断」，其值与基准同源，偏差恒为 0，
    # 算了只会把异常噪声化，故 avm 未命中时偏差置 None（无法判别）而非 0。
    deviation_pct = None
    if avm_hit and val and val > 0:
        baseline = float(collateral.get("true_market_price") or 0.0)
        if baseline > 0:
            deviation_pct = round(abs(val - baseline) / baseline, 4)
    # 严格大于阈值才标记（B-04：偏差恰好 = 30% 不标）
    abnormal_valuation = bool(
        deviation_pct is not None and deviation_pct > config.VALUATION_DEVIATION_THRESHOLD
    )

    missing_pct = float(collateral.get("spatial_feat_missing_pct") or 0.0)
    low_conf = missing_pct >= config.LOW_CONF_MISSING_PCT
    risk_class = config.classify(ltv)
    # 高危区域叠加：LTV 处于「正常/关注」但处高危区，至少升到「关注」
    if collateral.get("is_high_risk_zone") and risk_class in ("正常", "关注"):
        risk_class = "关注"

    alert = bool(ltv is not None and ltv > config.LTV_RED_LINE and not low_conf)

    return {
        **loan,
        "market_valuation": val,
        "ltv": ltv,
        "risk_class": risk_class,
        "low_confidence": low_conf,
        "is_high_risk_zone": collateral.get("is_high_risk_zone"),
        "alert": alert,
        "dwd_hit": dwd_hit,
        "avm_hit": avm_hit,
        "customer_id": loan.get("customer_id"),
        "model_version": valuation.model_version(avm_model),
        "valuation_deviation_pct": deviation_pct,
        "abnormal_valuation": abnormal_valuation,
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
