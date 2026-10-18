"""增强二·后端验收：shock 时「因情景新增受限」行级标记（spec §2e）。

复用 test_p6_policy_scenario._snapshot 数据集（cap=0.70 / 红线 0.85 / 冲击 10%）：
- L1 0.68→0.756：基线不命中、冲击命中 → shock_added=True
- L6 0.64→0.711：基线不命中、冲击命中 → shock_added=True
- L2 0.80→0.889：基线命中 → shock_added=False
- L3 0.90→1.000：基线命中 → shock_added=False
- L4 None：恒不命中，不在 rows
覆盖 §2e-1/2/3/5；§2e-4（既有测试不回归）由全量套件保证。
"""

import json

import pages.p6_policy as p6
from test_p6_policy_scenario import ZONE_ID, _snapshot


def _run(shock_pct=None):
    shock = {"kind": "price_drop", "pct": shock_pct, "label": "房价下跌 X%"} if shock_pct else None
    return p6._impact(_snapshot(), "zone", ZONE_ID, "reject", 0.70, shock=shock)


def test_shock_added_rows_exactly_l1_and_l6():
    # §2e-1：shock_pct=0.10 时 shock_added==True 的行恰为 {loan_id==1, loan_id==6}
    out = _run(shock_pct=0.10)
    flagged = {r["loan_id"] for r in out["rows"] if r["shock_added"]}
    assert flagged == {1, 6}
    # 命中行只含 L1/L6/L2/L3（L4 缺 LTV 不命中），且 shock_added 全都有定义
    hit_ids = {r["loan_id"] for r in out["rows"]}
    assert hit_ids == {1, 6, 2, 3}


def test_shock_added_sum_consistent_with_extra_hit_loans():
    # §2e-2：sum(shock_added) == 2 == out["shock"]["extra_hit_loans"]（口径自洽）
    out = _run(shock_pct=0.10)
    flagged_sum = sum(1 for r in out["rows"] if r["shock_added"])
    assert flagged_sum == 2
    assert flagged_sum == out["shock"]["extra_hit_loans"] == 2


def test_row_ltv_relationship_with_shock_and_without():
    # §2e-3：shock 行每行 ltv == round(ltv_base/(1-pct),4)；无 shock 时 ltv_base==ltv 且全 False
    pct = 0.10
    out = _run(shock_pct=pct)
    for r in out["rows"]:
        assert r["ltv_base"] is not None
        # 冲击后 LTV 与「冲击前原始 LTV ÷(1−pct)」自洽
        assert abs(r["ltv"] - round(r["ltv_base"] / (1.0 - pct), 4)) < 1e-6
        # 基线命中（shock_added=False）的行，原始 LTV 必 > cap 0.70
        if not r["shock_added"]:
            assert r["ltv_base"] > 0.70

    # 无 shock：ltv_base == ltv 且 shock_added 全 False（新增字段不改变现有语义）
    plain = _run()
    for r in plain["rows"]:
        assert r["ltv_base"] == r["ltv"]
        assert r["shock_added"] is False


def test_pii_no_customer_fields():
    # §2e-5 红线：任一命中行字段不含 customer_id/customer_name；ltv_base 仅放 LTV
    for out in (_run(shock_pct=0.10), _run()):
        ser = json.dumps(out, ensure_ascii=False)
        assert "customer_id" not in ser
        assert "customer_name" not in ser
        for r in out["rows"]:
            for k in r:
                assert not k.startswith("customer_"), f"字段 {k!r} 违反 PII 白名单"
