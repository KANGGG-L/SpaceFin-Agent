"""P6 敏感性阶梯的单元测试（零 DB：内存快照 + handle_preview 端点层）。

一次推演只回答「跌 10% 会怎样」，管理层真正要的是压力测试曲线——
handle_preview 在审计之后追加 SENSITIVITY_PCTS 五档重算（见 _sensitivity）。

覆盖：
1. 命中笔数随跌幅单调不减（跌幅越大、越过 LTV 上限的只会更多）；
2. 新增受限是「敏感带计数」而非单调量（大了会翻成已超红线）——把真实语义钉死；
3. 一致性：pct 等于本次情景的那一行必须与 out["hit"] / out["newly_hit"] 逐字相等；
4. 三种不给敏感性的情形：城市不匹配 / 非房价下跌情景 / 未设 ltv_cap；
5. 五档不进审计 detail（阶梯是展示数据，审计只记用户实际跑的那一档）；
6. 只读纪律：循环重算不改动带指纹缓存的共享快照。

快照（cap=0.70、红线 0.85，作用域 = 广州网格，8 笔 z 在作用域内）：
  L(.50) M(.55) A(.60) H(.62) B(.68) C(.75) D(.90) + N(None 缺 LTV)
命中门槛 ltv > 0.70×(1−q)，敏感带 0.70×(1−q) < ltv ≤ 0.85×(1−q)：
  q=0.05 → hit 3 / newly 2
  q=0.10 → hit 3 / newly 2
  q=0.15 → hit 5 / newly 3
  q=0.20 → hit 5 / newly 3
  q=0.30 → hit 7 / newly 2（A/H/B 已翻成「已超红线」， newly 由 3 回落为 2）
"""

import copy

import pages.p6_policy as p6
import pytest

ZONE_GZ = "price-gz-113.30-23.14"

# 情景原文固定用「广州房价下跌 10%」：城市限定 + 档位与 ladder 的 0.10 档重合，
# 一致性用例才能拿同一档对两套数字。
HYP_GZ10 = "广州核心区房价下跌 10%"


def _loan(lid, ltv, city="gz", lat=23.14, lng=113.30, balance=1_000_000.0):
    return {
        "loan_id": lid,
        "collateral_id": lid,
        "balance": balance,
        "ltv": ltv,
        "risk_class": "关注",
        "low_confidence": 0,
        "is_high_risk_zone": 0,
        "lat": lat,
        "lng": lng,
        "addr": f"广州{'市' if city == 'gz' else '外地'}某路{lid}号",
        "city": city,
    }


def _snapshot():
    zones = {
        ZONE_GZ: {
            "zone_id": ZONE_GZ,
            "zone_type": "price",
            "city": "gz",
            "center_lat": 23.14,
            "center_lng": 113.30,
            "sample_count": 8,
            "median_unit_price": 30000.0,
            "median_ltv": 0.7,
            "price_dev_vs_city": -0.1,
            "is_high_risk_zone": 1,
            "high_risk_rule": "price_low",
        }
    }
    loans = [
        _loan(1, 0.60),  # A
        _loan(2, 0.68),  # B
        _loan(3, 0.75),  # C
        _loan(4, 0.90),  # D：恒命中且恒超红线
        _loan(5, None),  # N：缺 LTV，任何口径都不命中
        _loan(6, 0.55),  # M
        _loan(7, 0.50),  # L
        _loan(8, 0.62),  # H
        # 域外：深圳（距广州网格远超 5km），任何档都不该进作用域
        _loan(9, 0.99, city="sz", lat=22.50, lng=114.00),
    ]
    return {"loans": loans, "zones": zones}


@pytest.fixture(autouse=True)
def _no_preview_audit(monkeypatch):
    """默认把审计空转掉（审计行为由 test_p6_preview_history.py 专测）。

    个别用例要检查「阶梯没混进 detail」，用 monkeypatch 换成录制版即可。
    """
    monkeypatch.setattr(p6, "_audit_preview", lambda ctx, detail: None)


def _preview(monkeypatch, body, snapshot=None):
    """用内存快照顶替 DB 快照，按 admin 身份跑一次 handle_preview。"""
    snap = snapshot if snapshot is not None else _snapshot()
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snap)
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {"action": "reject", "ltv_cap": 0.70, **body},
            "ip": "127.0.0.1",
            "user": {"user": "admin", "role": "admin", "label": "系统管理员"},
        },
    )()
    return p6.handle_preview(ctx)


def _zone_body(**kw):
    return {"scope_type": "zone", "scope_value": ZONE_GZ, **kw}


# ---------------- 1. 单调性 ----------------


def test_hit_loans_and_balance_monotonic_in_pct(monkeypatch):
    # 跌幅越大 → 越过 LTV 上限的只会更多：hit 集合单调扩张（笔数与余额都不减）
    out = _preview(monkeypatch, _zone_body(hypothesis=HYP_GZ10))
    rows = out["sensitivity"]
    assert [r["pct"] for r in rows] == list(p6.SENSITIVITY_PCTS)
    loans = [r["hit_loans"] for r in rows]
    bal = [r["hit_balance"] for r in rows]
    assert loans == sorted(loans), f"命中笔数非单调：{loans}"
    assert bal == sorted(bal), f"命中余额非单调：{bal}"
    # 钉住本快照的绝对数字（改了命中口径这里就会红）
    assert loans == [3, 3, 5, 5, 7]
    # 冲击后 LTV 中位数随跌幅上移：0.62（中位数原值）÷(1−q)
    assert abs(rows[1]["ltv_median_after"] - 0.62 / 0.9) < 1e-3
    assert rows[4]["ltv_median_after"] > rows[0]["ltv_median_after"]


def test_newly_hit_is_band_count_not_monotonic(monkeypatch):
    # 新增受限 = 有效 LTV 落在 (cap, 红线] 敏感带的笔数。跌幅加大时这个带整体下移，
    # 笔会「进带」也会「出带」（越过红线翻成已超红线）→ 天然非单调。
    # 这里把真实语义钉死：2,2,3,3,2 —— 若哪天被「改成单调」了，说明口径被改坏。
    rows = _preview(monkeypatch, _zone_body(hypothesis=HYP_GZ10))["sensitivity"]
    newly = [r["newly_hit_loans"] for r in rows]
    assert newly == [2, 2, 3, 3, 2]
    assert newly[-1] < newly[2], "末档回落丢失（新增受限被当成单调量处理了）"
    # 口径自洽：newly + already_alert == hit（同一档内两者互补，不重不漏）
    for r in rows:
        imp = p6._impact(
            _snapshot(),
            "zone",
            ZONE_GZ,
            "reject",
            0.70,
            shock={"kind": "price_drop", "pct": r["pct"], "label": "x"},
        )
        assert r["newly_hit_loans"] + imp["already_alert"]["loans"] == r["hit_loans"]


# ---------------- 2. 一致性（最重要的一条） ----------------


def test_row_equal_to_this_run_impact(monkeypatch):
    # 阶梯里 pct 恰等于本次情景的那一行，必须与本次推演的 hit / newly_hit 逐字相等——
    # 两者同一套 _impact，这条用例防的是「阶梯另起一套算法」的漂移。
    out = _preview(monkeypatch, _zone_body(hypothesis=HYP_GZ10))
    row = next(r for r in out["sensitivity"] if r["pct"] == 0.10)
    assert row["hit_loans"] == out["hit"]["loans"] == 3
    assert row["newly_hit_loans"] == out["newly_hit"]["loans"] == 2
    assert row["hit_balance"] == out["hit"]["balance"]
    assert row["newly_hit_balance"] == out["newly_hit"]["balance"]
    # 与冲击对比块同源：本次增量 = 该档命中 − 基线命中
    assert (
        row["hit_loans"] - out["shock"]["baseline_hit"]["loans"] == out["shock"]["extra_hit_loans"]
    )


# ---------------- 3. 三种不给敏感性的情形 ----------------


def test_no_sensitivity_on_city_mismatch(monkeypatch):
    # 广州房价下跌 × 深圳作用域 → 情景零影响，给一条敏感性曲线是误导
    out = _preview(
        monkeypatch,
        {
            "scope_type": "city",
            "scope_value": "sz",
            "hypothesis": HYP_GZ10,
        },
    )
    assert "sensitivity" not in out
    assert out.get("scenario_note"), "城市不匹配须同时给出核对说明"
    assert "shock" not in out


def test_no_sensitivity_for_non_price_drop_scenario(monkeypatch):
    # 利率类情景不改存量 LTV，重算多少档都是同一条平线 → 不给敏感性
    out = _preview(monkeypatch, _zone_body(hypothesis="贷款利率下调 1 个百分点"))
    assert "sensitivity" not in out
    assert "scenario_math" in out  # 走的是传导测算腿，不是逐笔重算


def test_no_sensitivity_without_ltv_cap(monkeypatch):
    # 未设 LTV 上限 → 整区命中，对跌幅不敏感（条条一样），算出来没意义
    out = _preview(monkeypatch, _zone_body(action="notice", ltv_cap=None, hypothesis=HYP_GZ10))
    assert out["ltv_cap"] is None
    assert "sensitivity" not in out


def test_no_sensitivity_without_hypothesis(monkeypatch):
    # 纯规则推演（无情景）→ 同样不带敏感性
    assert "sensitivity" not in _preview(monkeypatch, _zone_body())


# ---------------- 4. 不进审计 / 不改快照 ----------------


def test_sensitivity_absent_from_audit_detail(monkeypatch):
    # 阶梯放在审计之后：审计只记用户实际跑的那一档，五档是展示数据
    seen = {}
    monkeypatch.setattr(p6, "_audit_preview", lambda ctx, detail: seen.update(detail=detail))
    out = _preview(monkeypatch, _zone_body(hypothesis=HYP_GZ10))
    assert "sensitivity" in out, "响应里应有五档（展示数据）"
    assert "sensitivity" not in seen["detail"], "五档混进了审计 detail"
    assert "sensitivity" not in seen["detail"]["impact"], "五档混进了 impact 快照"


def test_sensitivity_loop_leaves_snapshot_untouched(monkeypatch):
    # 只读纪律：snapshot 是带指纹缓存的共享对象，五档循环绝不能改它
    snap = _snapshot()
    before = copy.deepcopy(snap)
    _preview(monkeypatch, _zone_body(hypothesis=HYP_GZ10), snapshot=snap)
    assert snap == before, "五档重算污染了共享快照（后续请求会读到脏数据）"
