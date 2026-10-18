"""P6 组合压力测试（二期 T6 后端半）的单元测试（零 DB：内存快照 + handle_stress 端点层）。

handle_stress 是**不挂规则**的纯情景组合推演：不读策略表、不写业务表、不写审计。
本文件把任务规约的口径逐条钉死：

0. 角色闸门：非 admin（da/risk）→ (403, ...)，PRD「情景推演仅 admin」同一条线；
1. pct=0：before == after 逐桶相等（parse_scenario 要求 0<pct<1，0% 解析不出来，
   故 monkeypatch parse_scenario 手工构造 pct=0 的 shock 直接打 handler）；
2. 单调性：red_after >= red_before（跌幅只会把笔推过红线，不会拉回来）；
3. 直方图守恒：sum(loans_before) + loans_no_ltv == totals.loans（after 同理）；
4. 城市限定：shock.city="gz" 时只统计广州存量，深圳的 LTV 不被冲击；
5. 非 price_drop（利率类 / 无法解析 / 空 hypothesis）与非法 group_by → ValueError；
6. deepcopy 前后比对：snapshot 零污染（共享缓存不变量，照抄 test_p6_sensitivity 写法）；
7. groups：city 按红_delta_balance 降序且 ≤5 条；industry 超 5 组截断；none 为 []。

内存快照（红线 0.85，全国冲击 pct=0.10）：
  gz: 0.60(1M) → 0.667 仍在 50-70% 档
      0.80(2M) → 0.889 翻越红线（red +1 / +2M）
      0.90(1M) → 1.000 恒越线（red 前后都算，delta 0）
      None(0.5M) 缺估值，不参与重算，进 totals.loans_no_ltv
  sz: 0.80(3M) → 0.889 翻越红线（red +1 / +3M）
      0.95(4M) → 1.056 恒越线
"""

import copy

import pages.p6_policy as p6
import pytest


def _loan(lid, ltv, city="gz", balance=1_000_000.0, industry=None):
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
        "addr": f"测试市某路{lid}号",
        "city": city,
        "industry": industry,
    }


def _snapshot():
    """两城组合：广州 4 笔（含 1 笔缺估值）+ 深圳 2 笔。zones 本接口用不到，给空表。"""
    return {
        "loans": [
            _loan(1, 0.60),
            _loan(2, 0.80, balance=2_000_000.0),
            _loan(3, 0.90),
            _loan(4, None, balance=500_000.0),
            _loan(5, 0.80, city="sz", balance=3_000_000.0),
            _loan(6, 0.95, city="sz", balance=4_000_000.0),
        ],
        "zones": {},
    }


def _industry_snapshot():
    """6 个行业 + 1 笔未标注（industry=None），每笔 0.80 → 冲击后全部翻越红线，
    组增量恰等于各笔余额，供「按 red_delta_balance 降序截前 5」用。"""
    loans = []
    lid = 0
    for code, bal in (
        ("tech", 600_000.0),
        ("mfg", 500_000.0),
        ("retail", 400_000.0),
        ("build", 300_000.0),
        ("agri", 200_000.0),
        ("miner", 100_000.0),
    ):
        lid += 1
        loans.append(_loan(lid, 0.80, balance=bal, industry=code))
    lid += 1
    loans.append(_loan(lid, 0.80, balance=50_000.0, industry=None))
    return {"loans": loans, "zones": {}}


def _stress(monkeypatch, body, snapshot=None, role="admin"):
    """用内存快照顶替 DB 快照，直接跑一次 handle_stress（role 可注入非 admin 角色）。"""
    snap = snapshot if snapshot is not None else _snapshot()
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snap)
    ctx = type(
        "_Ctx",
        (),
        {
            "body": body,
            "ip": "127.0.0.1",
            "user": {"user": "tester", "role": role, "label": role},
        },
    )()
    return p6.handle_stress(ctx)


# ---------------- 0. 角色闸门（PRD：情景推演仅 admin） ----------------


def test_non_admin_rejected_with_403(monkeypatch):
    # 组合压力测试是纯情景推演，落在 PRD「情景推演仅 admin」的同一条线上
    # （handle_preview 对 hypothesis 只认 admin、沙盘再闸 _require_admin）。
    # DA 的「可试算」由 preview（纯规则推演）覆盖，不因本接口扩权。
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%"}, role="da")
    assert isinstance(out, tuple) and out[0] == 403
    assert "admin" in out[1]["error"]
    assert out[1]["error_key"] == "无权限：组合压力测试为情景推演，仅管理员（admin）可用"
    # risk 同样被拒：写角色 ≠ 情景推演可见（preview 的 hypothesis 闸门同款边界）
    out2 = _stress(monkeypatch, {"hypothesis": "房价下跌 10%"}, role="risk")
    assert isinstance(out2, tuple) and out2[0] == 403


# ---------------- 1. pct=0：冲击前后逐桶相等 ----------------


def test_pct_zero_before_equals_after(monkeypatch):
    # parse_scenario 要求 0<pct<1，「房价下跌 0%」解析不出来——手工构造 pct=0 的
    # shock 直接打 handler：零冲击下重算公式退化为恒等，before 必须逐桶等于 after。
    monkeypatch.setattr(
        p6,
        "parse_scenario",
        lambda t: {"kind": "price_drop", "pct": 0.0, "city": None, "label": "房价下跌 0%"},
    )
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 0%"})
    for b in out["ltv_hist"]:
        assert b["loans_before"] == b["loans_after"]
        assert b["bal_before"] == b["bal_after"]
    assert out["stats"]["red_before"] == out["stats"]["red_after"]
    assert out["stats"]["median_before"] == out["stats"]["median_after"]
    assert out["stats"]["p90_before"] == out["stats"]["p90_after"]


# ---------------- 2. 单调性 ----------------


def test_red_after_not_less_than_before(monkeypatch):
    # 跌幅只会把笔推过红线、不会拉回来：red_after 的笔数与余额都不减于 red_before。
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%"})
    rb, ra = out["stats"]["red_before"], out["stats"]["red_after"]
    assert ra["loans"] >= rb["loans"]
    assert ra["balance"] >= rb["balance"]
    # 钉住本快照的绝对数字：0.90/0.95 恒越线（before 2 笔 5M），
    # 冲击后 0.80 的两笔（gz 2M + sz 3M）翻越 → after 4 笔 10M。
    assert rb["loans"] == 2
    assert rb["balance"] == pytest.approx(5_000_000.0)
    assert ra["loans"] == 4
    assert ra["balance"] == pytest.approx(10_000_000.0)


# ---------------- 3. 直方图守恒 ----------------


def test_histogram_conservation(monkeypatch):
    # 每笔有估值的贷款恰落一个 before 桶、一个 after 桶；缺估值的单列 no_ltv——
    # 三者对得上，分母才守得住。
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%"})
    t, hist = out["totals"], out["ltv_hist"]
    assert sum(b["loans_before"] for b in hist) + t["loans_no_ltv"] == t["loans"]
    assert sum(b["loans_after"] for b in hist) + t["loans_no_ltv"] == t["loans"]
    # 余额同样守恒（各桶已 round 到分位，留 5 分容忍）
    bal_b = sum(b["bal_before"] for b in hist) + t["balance_no_ltv"]
    bal_a = sum(b["bal_after"] for b in hist) + t["balance_no_ltv"]
    assert bal_b == pytest.approx(t["balance"], abs=0.05)
    assert bal_a == pytest.approx(t["balance"], abs=0.05)


# ---------------- 4. 城市限定 ----------------


def test_city_scope_excludes_other_cities(monkeypatch):
    # 「广州房价下跌 10%」只统计广州存量：深圳 2 笔（0.80/0.95，7M）不进任何统计，
    # 否则深圳的 LTV 会被广州的房价冲击放大（口径错误，不是近似误差）。
    out = _stress(monkeypatch, {"hypothesis": "广州房价下跌 10%"})
    assert out["shock"]["city"] == "gz"
    assert out["scope_note"] == "仅统计广州存量"
    t = out["totals"]
    assert t["loans"] == 4
    assert t["loans_no_ltv"] == 1
    assert t["balance"] == pytest.approx(4_500_000.0)
    assert t["balance_no_ltv"] == pytest.approx(500_000.0)
    # 直方图只含广州 3 笔有估值的贷款（0.60/0.80/0.90），深圳的没进来
    assert sum(b["loans_before"] for b in out["ltv_hist"]) == 3
    # 若深圳被错误重算，red_after 会多出 2 笔（0.80 翻线 + 0.95 恒越线）
    assert out["stats"]["red_before"]["loans"] == 1
    assert out["stats"]["red_before"]["balance"] == pytest.approx(1_000_000.0)
    assert out["stats"]["red_after"]["loans"] == 2
    assert out["stats"]["red_after"]["balance"] == pytest.approx(3_000_000.0)


# ---------------- 5. 入参校验 ----------------


def test_non_price_drop_rejected(monkeypatch):
    # 利率类情景不能诚实逐笔（缺执行利率/剩余期限）→ 明确报错而非给假分布；
    # 收入类已支持（逐笔 DTI 压力），不再被拒。
    with pytest.raises(ValueError, match="利率类情景"):
        _stress(monkeypatch, {"hypothesis": "贷款利率下调 1 个百分点"}, _snapshot())
    # 解析不出（None）与空 hypothesis 同样拒绝
    with pytest.raises(ValueError, match="未能解析为可推演情景"):
        _stress(monkeypatch, {"hypothesis": "随便说说没有情景"}, _snapshot())
    with pytest.raises(ValueError):
        _stress(monkeypatch, {"hypothesis": ""}, _snapshot())
    with pytest.raises(ValueError):
        _stress(monkeypatch, {}, _snapshot())


def test_group_by_invalid_rejected(monkeypatch):
    with pytest.raises(ValueError, match="group_by 非法"):
        _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "region"})


# ---------------- 6. 快照零污染 ----------------


def test_stress_leaves_snapshot_untouched(monkeypatch):
    # 只读纪律：snapshot 是带指纹缓存的共享对象，组合推演绝不能改它
    snap = _snapshot()
    before = copy.deepcopy(snap)
    _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "city"}, snapshot=snap)
    assert snap == before, "组合压力测试污染了共享快照（后续请求会读到脏数据）"


# ---------------- 7. groups ----------------


def test_groups_city_order_and_none_empty(monkeypatch):
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "city"})
    g = out["groups"]
    assert len(g) <= 5
    deltas = [x["red_delta_balance"] for x in g]
    assert deltas == sorted(deltas, reverse=True), "groups 未按 red_delta_balance 降序"
    # 深圳 +3M（0.80 那笔余额更大）> 广州 +2M
    assert [(x["key"], x["red_delta_balance"]) for x in g] == [
        ("sz", 3_000_000.0),
        ("gz", 2_000_000.0),
    ]
    assert g[0]["label"] == "深圳"
    assert g[1]["label"] == "广州"
    # 组规模口径含缺估值笔（gz 4 笔），red_delta 只基于有 LTV 的笔
    assert g[1]["loans"] == 4
    assert g[1]["red_delta_loans"] == 1
    assert g[1]["median_after"] == pytest.approx(0.8889, abs=1e-4)

    # group_by 缺省不再 none：全国情景自动推断为 city（分组维度与冲击范围互补）
    out2 = _stress(monkeypatch, {"hypothesis": "房价下跌 10%"})
    assert out2["group_by"] == "city" and out2["groups"]
    assert out2["ltv_hist"], "none 模式下直方图仍应输出"


def test_groups_industry_full_and_sorted(monkeypatch):
    # 7 个组（6 行业 + 未标注）全量返回不截断（前端滑动面板承接），降序不变；
    # 未标注组不因 None 被丢弃（余额最小，按 red_delta_balance 排在末位）
    out = _stress(
        monkeypatch,
        {"hypothesis": "房价下跌 10%", "group_by": "industry"},
        snapshot=_industry_snapshot(),
    )
    g = out["groups"]
    assert len(g) == 7
    assert [x["key"] for x in g] == [
        "tech",
        "mfg",
        "retail",
        "build",
        "agri",
        "miner",
        "未标注",
    ]
    assert all(x["red_delta_loans"] == 1 for x in g)
    assert g[0]["red_delta_balance"] == pytest.approx(600_000.0)
    assert g[-1]["red_delta_balance"] == pytest.approx(50_000.0)
    deltas = [x["red_delta_balance"] for x in g]
    assert deltas == sorted(deltas, reverse=True), "groups 未按 red_delta_balance 降序"


# ---------------- 响应结构 ----------------


def test_response_shape(monkeypatch):
    # 前端将按此渲染，键名与档位标签是契约，不得漂移
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "city"})
    assert set(out) == {
        "shock",
        "scope_note",
        "scope_note_key",
        "scope_note_params",
        "group_by",
        "group_note",
        "group_note_key",
        "group_note_params",
        "totals",
        "ltv_hist",
        "stats",
        "groups",
        "zones",
        "affected",
        "affected_total",
        "generated_at",
    }
    assert set(out["shock"]) == {"label", "label_key", "label_params", "pct", "city", "district"}
    assert out["shock"]["label"] == "房价下跌 10%"
    assert out["shock"]["pct"] == 0.10
    assert out["shock"]["district"] is None  # 全国情景无区级限定
    assert out["scope_note"] is None  # 全国情景无 scope_note
    # group_by 显式传入 → 原样生效，group_note 说明推断/生效口径
    assert out["group_by"] == "city"
    assert out["group_note"]
    assert [b["bucket"] for b in out["ltv_hist"]] == [
        "0-50%",
        "50-70%",
        "70-85%",
        "85-100%",
        "100%+",
    ]
    assert set(out["totals"]) == {"loans", "balance", "loans_no_ltv", "balance_no_ltv"}
    assert set(out["stats"]) == {
        "median_before",
        "median_after",
        "p90_before",
        "p90_after",
        "red_before",
        "red_after",
    }
    # 5 笔有估值：[0.60, 0.80, 0.80, 0.90, 0.95] → 中位 0.80；等比 ÷0.9 后中位 0.8889
    assert out["stats"]["median_before"] == pytest.approx(0.80, abs=1e-4)
    assert out["stats"]["median_after"] == pytest.approx(0.8889, abs=1e-4)


def test_affected_list_new_crossing_only(monkeypatch):
    # 受冲击新增影响（affected）只收「冲击前 ≤ 红线、冲击后越线」的新越线任务：
    # 0.90/0.95 属恒越线（不含），0.60 未越（不含），0.80 两笔冲击后 0.889 越过 → 2 笔。
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "city"})
    assert out["affected_total"] == 2
    assert len(out["affected"]) == 2
    for a in out["affected"]:
        # 白名单字段：loan_id / ltv_before / ltv_after / balance / city / 坐标(lat/lng) + 地址与行业
        # （随行供导出结论逐笔复核与空间 CSV/散点图；PII 红线仅限借款人标识 customer_id/customer_name）
        assert set(a) == {
            "loan_id",
            "ltv_before",
            "ltv_after",
            "balance",
            "city",
            "lat",
            "lng",
            "addr",
            "industry",
        }
        assert a["ltv_before"] == pytest.approx(0.80, abs=1e-4)
        assert a["ltv_after"] == pytest.approx(0.8889, abs=1e-4)
        assert a["loan_id"] in (2, 5)
    # 按冲击后 LTV 降序
    assert out["affected"][0]["ltv_after"] >= out["affected"][1]["ltv_after"]


def _assert_note_double_track(note, key, params):
    """双轨不变量（仿 test_p6_ltv_basis_static.py:116）：中文原句 == key 用 code→中文名
    映射后的 params 回填。

    锁死「key 模板句与中文原句逐字同源」——任一侧改了文案而另一侧忘了同步，
    这条断言会立刻红，避免 i18n 在 en/zh-HK 下翻出与中文不符的口径。
    params 里 city 是 code（gz），须先经 CITY_NAME 映射回中文名再 format。
    """
    if note is None:
        assert key is None and params is None, "原句为 None 时 key/params 也须为 None（不得出空串）"
        return
    assert key is not None and params is not None, "原句非空却缺 key/params 双轨"
    zh = {}
    for k, v in (params or {}).items():
        if k == "city":
            zh[k] = p6.CITY_NAME.get(v, v)
        elif k == "industry":
            zh[k] = p6.config.INDUSTRY_CATEGORIES.get(v, v)
        else:
            zh[k] = v
    assert note == key.format(**zh), (
        f"双轨漂移：\n 原句={note!r}\n key={key!r}\n params={params!r}\n zh={zh!r}"
    )


def test_stress_bilingual_keys_invariant(monkeypatch):
    # handle_stress（房价冲击路径）三组维度 + 有无地域限定，全走双轨不变量
    # 全国（无 scope）→ scope_note 为 None，key/params 也须 None
    out = _stress(monkeypatch, {"hypothesis": "全国房价下跌 20%"})
    _assert_note_double_track(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    _assert_note_double_track(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["scope_note"] is None
    # 城市限定 → scope_note 带市名（数据值），group_note 走「按行业内分组」
    out = _stress(monkeypatch, {"hypothesis": "广州房价下跌 10%"})
    _assert_note_double_track(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    _assert_note_double_track(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["scope_note"] == "仅统计广州存量"
    # 显式 none → group_note 为「全组合口径，不分组」（纯文案，params 空字典）
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "none"})
    _assert_note_double_track(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["group_note_params"] == {}


# ---------------- 8. 分组维度自动推断（自然语言 → 维度，前端不再提供选择器） ----------------


def test_group_by_inferred_national_scenario(monkeypatch):
    # 无地域限定（全国）→ city：分组维度与冲击范围互补，「哪个城市最危险」。
    out = _stress(monkeypatch, {"hypothesis": "全国房价下跌 20%"})
    assert out["group_by"] == "city"
    assert out["group_note"]
    g = out["groups"]
    assert [(x["key"], x["red_delta_balance"]) for x in g] == [
        ("sz", 3_000_000.0),
        ("gz", 2_000_000.0),
    ]


def test_group_by_inferred_geo_scoped_scenario(monkeypatch):
    # 城市限定 → industry：受冲击面已被地域闸门过滤到该城市，再按城市分组
    # 只会是单行表（零信息量）；地域内按行业拆才是「哪里最危险」的自然问法。
    snap = {
        "loans": [
            _loan(1, 0.80, industry="tech"),
            _loan(2, 0.80, balance=2_000_000.0, industry="mfg"),
            _loan(3, 0.95, city="sz", industry="tech"),
        ],
        "zones": {},
    }
    out = _stress(monkeypatch, {"hypothesis": "广州房价下跌 10%"}, snap)
    assert out["group_by"] == "industry"
    assert "广州" in out["group_note"]
    # 深圳那笔被地域闸门排除，组内只剩广州的行业
    assert [x["key"] for x in out["groups"]] == ["mfg", "tech"]  # 2M 增量在前
    assert out["totals"]["loans"] == 2


def test_group_by_explicit_none_still_honored(monkeypatch):
    # 显式传 none（API 兼容路径）→ 不分组
    out = _stress(monkeypatch, {"hypothesis": "房价下跌 10%", "group_by": "none"})
    assert out["group_by"] == "none"
    assert out["groups"] == []
    assert "不分组" in out["group_note"]


# ---------------- 9. 上行（脱险）累计 --------------------
# Phase2 脱险桶（口径与 _impact 的 exited_red / risk_reduced_balance 一致）：
# 上行情景（涨价，pct<0 / direction=up）只降险——冲击后 LTV 降回红线以下的笔算脱险、
# 有效 LTV 下降笔累计 余额×(原 LTV−有效 LTV)。广州 0.90(1M) 笔冲击后 ÷1.10 → 0.818
# 落回红线(0.85)以下 → 脱险 1 笔；0.60/0.80 与 0.90 都构成敞口下降。


def test_up_shock_exited_red_accumulated(monkeypatch):
    # 广州房价上涨 10%：direction=up，脱险桶非空、敞口下降为正，且上行恒无新增越线
    # （affected 为空，区别于下行「新增命中」语义）。
    out = _stress(monkeypatch, {"hypothesis": "广州房价上涨 10%"})
    assert out["shock"]["direction"] == "up"
    assert out["shock"]["pct"] == pytest.approx(-0.10, abs=1e-4)
    # 广州 0.90(1M) 冲击后 ÷1.10 → 0.818 ≤ 红线 0.85 → 脱险 1 笔 / 1M
    assert out["shock"]["exited_red"]["loans"] == 1
    assert out["shock"]["exited_red"]["balance"] == pytest.approx(1_000_000.0)
    # 敞口下降量 = Σ 有效 LTV 下降笔 余额×(原 LTV−有效 LTV)，广州三笔有估值皆参与
    assert out["shock"]["risk_reduced_balance"] > 0
    # 上行只降险：无新增越线（affected 恒空）
    assert out["affected"] == []
    assert out["affected_total"] == 0
    # 红线口径：上行只降险 → after 越线笔数不增（应 ≤ 冲前）
    assert out["stats"]["red_after"]["loans"] <= out["stats"]["red_before"]["loans"]
    # PII 白名单：exited_red 只含 loans/balance，不返回地址/客户维度
    assert set(out["shock"]["exited_red"]) == {"loans", "balance"}


def test_down_shock_control_affected_not_empty(monkeypatch):
    # 下行对照：受冲击「新增越线」（affected）非空，与上行脱险语义互补。
    out = _stress(monkeypatch, {"hypothesis": "广州房价下跌 10%"})
    assert out["shock"].get("direction") != "up"
    assert out["affected_total"] > 0
    assert len(out["affected"]) > 0
