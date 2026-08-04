"""风险引擎：估值 → LTV → 五级分类 → 贷后保全预警（AC-02/03/04）。

- 估值：优先 DWD 行情（valuation.valuation_from_dwd），未命中回退 true_market_price。
- LTV = 贷款余额 / 抵押物估值。
- 五级分类：按 LTV 阈值(config.CLASS_LTV_UPPER)分档。
- 低置信(AC-04)：spatial_feat_missing_pct >= 25% → 标记 low_confidence，不触发自动预警。
- 预警(AC-03)：LTV > 红线(0.85) 且非低置信 → 进入 ads_ltv_alerts。
"""

from __future__ import annotations

import config  # noqa: F401  (运行入口 tools/risk/main.py 已把本目录加入 sys.path)
import valuation


def enrich_loan(
    loan: dict, collateral: dict | None, customer: dict | None, dwd_unit: dict, city_map: dict
) -> dict:
    """单笔贷款打宽：估值/LTV/五级/低置信/预警。"""
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
        }

    val = valuation.valuation_from_dwd(dwd_unit, collateral, city_map)
    dwd_hit = val is not None
    if val is None:
        val = float(collateral.get("true_market_price") or 0.0)  # 回退业务库价格
    balance = float(loan.get("balance") or 0.0)
    ltv = round(balance / val, 4) if val and val > 0 else None

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
        "customer_id": loan.get("customer_id"),
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
