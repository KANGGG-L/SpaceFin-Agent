"""P6 行业作用域的单元测试（零 DB：纯函数 + 内存快照）。

覆盖策略推演中心新增的第三个作用域维度 `scope_type='industry'`（US-01：
贷款经 customer_id 继承借款人行业，推演可按行业定向而非全量存量）：
1. _in_scope：行业命中 / 跨行业 / 行业为 None（未回填）不命中；
2. _impact：行业作用域命中 = 该行业内超上限笔数，严格小于全量口径；
3. _valid_scope / scope_label：15 个枚举码全合法，未知行业码拒绝；
4. pick_profile_loan / _resolve_shock：情景链路对行业作用域自然生效，
   且城市限定情景不误杀跨城市的行业作用域；
5. seed 行业推导：同 ID 同结果、全量取值落在枚举内、分布钉死防漂移。
"""

import os
import sys
from collections import Counter

import pages.p6_policy as p6
import pytest

# seed/generate_seed.py 不在 tools/frontend 下，按仓库根定位后注入。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_SEED_DIR = os.path.join(_REPO_ROOT, "seed")
if _SEED_DIR not in sys.path:
    sys.path.insert(0, _SEED_DIR)

import generate_seed  # noqa: E402

# ---------------- 内存快照 ----------------


def _loan(lid, ltv, industry, city="gz", balance=1_000_000.0):
    return {
        "loan_id": lid,
        "collateral_id": lid,
        "balance": balance,
        "ltv": ltv,
        "risk_class": "关注",
        "low_confidence": 0,
        "is_high_risk_zone": 0,
        "lat": 23.14,
        "lng": 113.30,
        "addr": "广州市黄埔区某路1号",
        "city": city,
        "industry": industry,
    }


def _snapshot():
    # 阈值 0.70、红线 0.85：
    #   L1 制造业 0.80 → 命中（新增受限，未过红线）
    #   L2 制造业 0.60 → 不命中
    #   L3 制造业 0.90 → 命中（已超红线 → 提级处置）
    #   L4 建筑业 0.95 → 行业不符，任何口径都不应命中
    #   L5 行业未回填（None）0.99 → 同上
    #   L6 批发零售 0.75（sz）→ 行业不符
    loans = [
        _loan(1, 0.80, "manufacturing"),
        _loan(2, 0.60, "manufacturing"),
        _loan(3, 0.90, "manufacturing"),
        _loan(4, 0.95, "construction"),
        _loan(5, 0.99, None),
        _loan(6, 0.75, "wholesale_retail", city="sz"),
    ]
    return {"loans": loans, "zones": {}}


def _run(scope_value="manufacturing", cap=0.70, shock=None):
    return p6._impact(_snapshot(), "industry", scope_value, "reject", cap, shock=shock)


# ---------------- _in_scope ----------------


def test_in_scope_industry_match_and_mismatch():
    loans = _snapshot()["loans"]
    assert p6._in_scope(loans[0], "industry", "manufacturing", {}) is True
    assert p6._in_scope(loans[3], "industry", "manufacturing", {}) is False  # 建筑业
    assert p6._in_scope(loans[4], "industry", "manufacturing", {}) is False  # None 不命中
    # 行业为 None 的贷款不命中任何行业作用域（含未枚举的码——_valid_scope 会先拦）
    assert p6._in_scope(loans[4], "industry", "construction", {}) is False


def test_in_scope_industry_does_not_disturb_city():
    loans = _snapshot()["loans"]
    assert p6._in_scope(loans[0], "city", "gz", {}) is True
    assert p6._in_scope(loans[5], "city", "gz", {}) is False


# ---------------- _impact ----------------


def test_impact_industry_scope_hits_only_in_industry():
    out = _run()
    # 制造业超 0.70 上限的只有 L1 / L3，严格小于全量口径（全量超上限 5 笔）
    assert out["hit"]["loans"] == 2
    assert out["newly_hit"]["loans"] == 1  # L1（0.80 ≤ 红线 0.85）
    assert out["already_alert"]["loans"] == 1  # L3（0.90 > 0.85）
    assert {r["loan_id"] for r in out["rows"]} == {1, 3}
    # 作用域覆盖面：制造业存量 3 笔
    assert out["in_scope"]["loans"] == 3


def test_impact_industry_scope_with_shock():
    # 冲击 −10%（LTV ÷0.9）：L2 0.60→0.667 仍不命中；L1 0.80→0.889 越过红线
    out = _run(shock={"kind": "price_drop", "pct": 0.10, "label": "x"})
    assert out["hit"]["loans"] == 2  # L1 + L3
    assert out["newly_hit"]["loans"] == 0  # L1 翻为提级处置
    assert out["already_alert"]["loans"] == 2
    s = out["shock"]
    assert s["baseline_hit"]["loans"] == 2
    assert s["extra_hit_loans"] == 0
    # 冲击只波及作用域内：建筑业/未回填行业的超上限笔不计入
    assert out["in_scope"]["loans"] == 3


def test_impact_industry_unknown_to_scope_zero_hit():
    out = _run(scope_value="construction")
    # 建筑业只有一笔 0.95 → 命中 1；制造业的笔不串进来
    assert out["hit"]["loans"] == 1
    assert out["in_scope"]["loans"] == 1


# ---------------- _valid_scope / scope_label ----------------


def test_valid_scope_accepts_all_enum_codes():
    for code in p6.config.INDUSTRY_CATEGORIES:
        p6._valid_scope("industry", code, {})  # 不抛即通过


def test_valid_scope_rejects_unknown_industry_code():
    with pytest.raises(ValueError, match="行业码不存在"):
        p6._valid_scope("industry", "mining", {})
    with pytest.raises(ValueError, match="行业码不存在"):
        p6._valid_scope("industry", "制造业", {})  # 中文名不是合法 scope_value


def test_scope_label_industry_uses_chinese_name():
    assert p6.scope_label("industry", "manufacturing", {}) == "行业 · 制造业"
    assert p6.scope_label("industry", "it_software", {}) == "行业 · 信息传输、软件和信息技术服务业"


# ---------------- pick_profile_loan / _resolve_shock ----------------


def test_pick_profile_loan_industry_lands_in_industry():
    p = p6.pick_profile_loan(_snapshot(), "industry", "manufacturing", 0.70, None)
    # 命中笔里 LTV 最高的是 L3（0.90）
    assert p["loan_id"] == 3
    assert p["is_hit"] is True


def test_pick_profile_loan_industry_no_hit_falls_back_in_industry():
    # 阈值 1.5 → 无命中，退化仍必须落在制造业内（L3 0.90 最高）
    p = p6.pick_profile_loan(_snapshot(), "industry", "manufacturing", 1.5, None)
    assert p["loan_id"] == 3 and p["is_hit"] is False


def test_pick_profile_loan_industry_empty_scope():
    # 快照里没有金融业贷款 → 无可模拟对象
    assert p6.pick_profile_loan(_snapshot(), "industry", "finance", 0.70, None) is None


def test_resolve_shock_city_scoped_applies_to_industry_scope():
    # 行业作用域跨城市、无单一归属：城市限定情景视为匹配，不得误杀成「无影响」
    sc = {"kind": "price_drop", "pct": 0.10, "city": "gz", "label": "广州房价下跌 10%"}
    shock, note = p6._resolve_shock(sc, "industry", "manufacturing", {})
    assert shock is sc and note is None


def test_resolve_shock_global_applies_to_industry_scope():
    sc = {"kind": "price_drop", "pct": 0.20, "city": None, "label": "全国房价下跌 20%"}
    shock, note = p6._resolve_shock(sc, "industry", "wholesale_retail", {})
    assert shock is sc and note is None


# ---------------- seed 行业推导：确定性与枚举闭合 ----------------


def test_industry_of_customer_is_deterministic_per_id():
    for cid in (10000, 10001, 12345, 14999):
        assert generate_seed.industry_of_customer(cid) == generate_seed.industry_of_customer(cid)


def test_industry_of_customer_values_within_enum():
    enum = set(p6.config.INDUSTRY_CATEGORIES)
    for i in range(5000):
        assert generate_seed.industry_of_customer(10000 + i) in enum


def test_industry_distribution_pinned_for_seed_stability():
    # 与 02_seed.sql 的生成口径一致：权重/随机种子一动这里就会失败，
    # 逼着改动同步重生成种子并回填，而不是默默漂移。
    dist = Counter(generate_seed.industry_of_customer(10000 + i) for i in range(5000))
    assert sum(dist.values()) == 5000
    assert len(dist) == len(p6.config.INDUSTRY_CATEGORIES)  # 15 类全覆盖
    assert dist.most_common(3) == [
        ("manufacturing", 1113),
        ("wholesale_retail", 759),
        ("construction", 497),
    ]
