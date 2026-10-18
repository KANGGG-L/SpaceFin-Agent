"""P6 收入类情景逐笔路由 + DTI 红线设置的单元测试（零 DB / 内存快照）。

产品口径（docs/tech/p6-income-dti-routing.md）：
1. parse_scenario：行业限定词白名单解析（制造业/互联网别名/无限定）；
2. _income_stress：行业外 record 不参与、stressed_dti = dti/(1−pct)、
   越线判定 dti > red（恰等于不算）、缺偿债比单列、分组维度互补推断；
3. DTI 红线设置：默认 0.70、POST 持久化 + 审计留痕、越界 400、非写角色 403；
4. pick_profile_loan：行业收窄叙事模拟对象。
"""

import json

import pages.p6_policy as p6
import pytest

# ---------------- 1. parse_scenario：行业限定词 ----------------


def test_parse_income_with_industry():
    s = p6.parse_scenario("制造业收入下降 15%")
    assert s["kind"] == "income_drop"
    assert s["industry"] == "manufacturing"
    assert abs(s["pct"] - 0.15) < 1e-9


def test_parse_income_with_industry_alias():
    # 口语别名命中：「互联网行业」→ 信息传输、软件和信息技术服务业
    assert p6.parse_scenario("互联网行业收入下降 10%")["industry"] == "it_software"
    assert p6.parse_scenario("餐饮业收入下降 20%")["industry"] == "accommodation_food"


def test_parse_income_without_industry_is_all():
    # 无行业限定 → industry=None，作用于全部行业（与「全国」地域语义对称）
    assert p6.parse_scenario("全国收入下降 10%")["industry"] is None


def test_parse_industry_full_name_beats_alias():
    # 完整门类名在前：「金融业」必须命中 finance 而不是被别的词截胡
    assert p6.parse_scenario("金融业收入下降 5%")["industry"] == "finance"


# ---------------- 2. _income_stress：逐笔路由 ----------------


def _loan(lid, dti, industry="tech", city="gz", balance=1_000_000.0):
    return {
        "loan_id": lid,
        "collateral_id": lid,
        "balance": balance,
        "ltv": 0.6,
        "risk_class": "关注",
        "low_confidence": 0,
        "is_high_risk_zone": 0,
        "lat": 23.14,
        "lng": 113.30,
        "addr": f"广州市天河区x路{lid}号",
        "city": city,
        "industry": industry,
        "debt_ratio": dti,
    }


_INCOME_SHOCK = {
    "kind": "income_drop",
    "pct": 0.20,
    "city": None,
    "district": None,
    "industry": None,
    "label": "收入下降 20%",
}


def test_income_stress_routes_to_matching_industry_only():
    # 行业限定 → 行业外 record 完全不参与：it 0.60 → 0.75（越过 0.70），
    # agriculture 0.60 的偿债比绝不能被信息行业的收入冲击放大。
    snap = {
        "loans": [
            _loan(1, 0.60, industry="it_software"),
            _loan(2, 0.60, industry="agriculture"),
        ],
        "zones": {},
    }
    sc = {**_INCOME_SHOCK, "industry": "it_software"}
    out = p6._income_stress(snap, sc, 0.70, "none")
    assert out["totals"]["loans"] == 1
    assert out["totals"]["loans_no_dti"] == 0
    # 越线：0.60 → 0.75 > 0.70，before 0 → after 1
    assert out["stats"]["red_before"]["loans"] == 0
    assert out["stats"]["red_after"]["loans"] == 1
    assert abs(out["stats"]["median_after"] - 0.75) < 1e-6
    assert out["shock"]["industry"] == "it_software"
    # scope_note 用门类中文名，不是裸 slug
    assert "信息传输、软件和信息技术服务业" in out["scope_note"]


def test_income_stress_boundary_exact_red_line_not_breached():
    # dti_after 恰等于红线不算越线（与 LTV 红线同款口径）。
    # 取 pct=0.5（factor=0.5 是二的幂，缩放无浮点误差）：0.35 → 0.70 恰等，0.36 → 0.72 越线
    sc = {**_INCOME_SHOCK, "pct": 0.5, "label": "收入下降 50%"}
    snap = {"loans": [_loan(1, 0.35, industry="tech")], "zones": {}}
    out = p6._income_stress(snap, sc, 0.70, "none")
    assert out["stats"]["red_after"]["loans"] == 0
    out2 = p6._income_stress({**snap, "loans": [_loan(1, 0.36, industry="tech")]}, sc, 0.70, "none")
    assert out2["stats"]["red_after"]["loans"] == 1


def test_income_stress_missing_dti_counted_separately():
    snap = {
        "loans": [
            _loan(1, 0.60, industry="tech"),
            _loan(2, None, industry="tech", balance=500_000.0),
        ],
        "zones": {},
    }
    out = p6._income_stress(snap, dict(_INCOME_SHOCK), 0.70, "none")
    assert out["totals"]["loans"] == 2
    assert out["totals"]["loans_no_dti"] == 1
    assert out["totals"]["balance_no_dti"] == 500_000.0
    # 直方图分母守恒：before 桶合计 + 缺失 = totals.loans
    assert sum(b["loans_before"] for b in out["dti_hist"]) + 1 == 2


def test_income_stress_group_dimension_complementary():
    # 无行业限定 → 按行业分组（哪个行业最危险）
    snap = {
        "loans": [
            _loan(1, 0.60, industry="tech"),
            _loan(2, 0.60, industry="manufacturing", balance=2_000_000.0),
        ],
        "zones": {},
    }
    out = p6._income_stress(snap, dict(_INCOME_SHOCK), 0.70, "industry")
    assert out["group_by"] == "industry"
    assert [g["key"] for g in out["groups"]] == ["manufacturing", "tech"]  # 增量余额降序

    # 行业限定 → 按城市分组（该行业在哪个城市最危险）
    snap2 = {
        "loans": [
            _loan(1, 0.60, industry="tech", city="gz"),
            _loan(2, 0.60, industry="tech", city="sz", balance=3_000_000.0),
        ],
        "zones": {},
    }
    out2 = p6._income_stress(snap2, {**_INCOME_SHOCK, "industry": "tech"}, 0.70, "city")
    assert out2["group_by"] == "city"
    assert [g["key"] for g in out2["groups"]] == ["sz", "gz"]


def test_income_stress_geo_gate_still_applies():
    # 「广州制造业收入下降」同时吃行业与城市两个限定：深圳 mfg 不参与
    snap = {
        "loans": [
            _loan(1, 0.60, industry="manufacturing", city="gz"),
            _loan(2, 0.60, industry="manufacturing", city="sz", balance=3_000_000.0),
        ],
        "zones": {},
    }
    sc = {**_INCOME_SHOCK, "industry": "manufacturing", "city": "gz"}
    out = p6._income_stress(snap, sc, 0.70, "none")
    assert out["totals"]["loans"] == 1
    assert "广州" in out["scope_note"] and "制造业" in out["scope_note"]


def test_income_stress_bilingual_keys_invariant():
    # _income_stress 双轨不变量（仿 test_p6_ltv_basis_static.py:116）：
    # 中文原句 == key 用 code→中文名映射后的 params 回填；原句为 None 时 key/params 也须为 None。
    # params 里 city/industry 是 code（gz / it_software），须先映射回中文名再 format。
    def chk(note, key, params):
        if note is None:
            assert key is None and params is None, "原句为 None 却生成了 key/params"
            return
        assert key is not None and params is not None, "原句非空却缺双轨"
        zh = {}
        for k, v in (params or {}).items():
            if k == "city":
                zh[k] = p6.CITY_NAME.get(v, v)
            elif k == "industry":
                zh[k] = p6.config.INDUSTRY_CATEGORIES.get(v, v)
            else:
                zh[k] = v
        assert note == key.format(**zh), f"双轨漂移：{note!r} != {key!r}.format({zh!r})"

    base = {"loans": [_loan(1, 0.60, industry="tech")], "zones": {}}
    sc = {
        "kind": "income_drop",
        "pct": 0.20,
        "city": None,
        "district": None,
        "industry": None,
        "label": "收入下降 20%",
    }

    # 无行业/无地域 + none → scope_note 为 None，group_note 纯文案 params 空字典
    out = p6._income_stress(base, dict(sc), 0.70, "none")
    chk(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    chk(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["scope_note"] is None
    assert out["group_note_params"] == {}

    # 无行业限定 + industry 分组 → group_note 纯文案（全行业），params 空字典
    out = p6._income_stress(base, dict(sc), 0.70, "industry")
    chk(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["group_note_params"] == {}

    # 行业限定 + city 分组 → scope_note 带行业名、group_note 带 scope=行业名（数据值）
    out = p6._income_stress(base, {**sc, "industry": "it_software"}, 0.70, "city")
    chk(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    chk(out["group_note"], out["group_note_key"], out["group_note_params"])
    assert out["scope_note"] == "仅统计信息传输、软件和信息技术服务业借款人"
    assert out["group_note_params"] == {"industry": "it_software"}

    # 城市限定 → scope_note 带市名（数据值），key 模板与中文逐字同源
    out = p6._income_stress(base, {**sc, "city": "gz"}, 0.70, "none")
    chk(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    assert out["scope_note"] == "仅统计广州存量"

    # 行业 + 城市并存 → scope_note 用「；」拼成组合 key（BUG-2：须注册组合 key 否则整句回落中文）
    out = p6._income_stress(base, {**sc, "industry": "it_software", "city": "gz"}, 0.70, "none")
    chk(out["scope_note"], out["scope_note_key"], out["scope_note_params"])
    assert out["scope_note"] == "仅统计信息传输、软件和信息技术服务业借款人；仅统计广州存量"
    assert out["scope_note_key"] == "仅统计{industry}借款人；仅统计{city}存量"


# ---------------- 3. handle_stress：收入类路由 + 利率类拒绝 ----------------


# 「边界可用但逐笔未命中」的缺省 stub 值（区别于 None=边界数据不可用）。
# 可变对象作默认参在此安全：stub 只读它、从不改写。
_EMPTY_DISTRICT_MAP = {}


def _stress(
    monkeypatch,
    body,
    snapshot,
    role="admin",
    district_map=_EMPTY_DISTRICT_MAP,
    district_level="district",
):
    monkeypatch.setattr(p6, "_load_snapshot", lambda: snapshot)
    monkeypatch.setattr(p6, "get_dti_red_line", lambda: 0.70)
    # 区名映射默认给空表：单测不走 _load_snapshot，_district_loan_map 的缓存键
    # （快照指纹）恒为 None，不 stub 它就会打真实网络。需要特定映射的用例传
    # dict（district_level 随之传 "district"/"town"）、模拟边界数据不可用的用例
    # 传 None（见回落测试）。_district_loan_map 返回二元组（映射, 层级）。
    monkeypatch.setattr(p6, "_district_loan_map", lambda snap, city: (district_map, district_level))
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


def test_stress_income_drop_full_flow(monkeypatch):
    snap = {"loans": [_loan(1, 0.60, industry="tech")], "zones": {}}
    out = _stress(monkeypatch, {"hypothesis": "科技行业收入下降 20%"}, snap)
    assert out["shock"]["kind"] == "income_drop"
    assert out["totals"]["loans"] == 1
    assert out["dti_red_line"] == 0.70
    # 无行业限定（科技行业不在 15 门类词表 → industry=None）→ 全国性冲击，
    # 缺省按城市看落点（分组维度与冲击范围互补）
    assert out["group_by"] == "city"


def test_stress_income_group_by_inference_matrix(monkeypatch):
    # 缺省推断四象限：行业限定 → city；仅区名限定 → industry；仅城市限定 →
    # district；全国无限定 → city。显式传仍原样生效（API 兼容），此处只锁缺省路径。
    snap = {
        "loans": [
            _loan(1, 0.60, industry="tech", city="gz"),
            _loan(2, 0.60, industry="manufacturing", city="sz", balance=2_000_000.0),
        ],
        "zones": {},
    }
    # 行业限定 → 按城市：该行业在哪个城市最危险
    out = _stress(monkeypatch, {"hypothesis": "制造业收入下降 20%"}, snap)
    assert out["group_by"] == "city"

    # 仅区名限定（sc.district 是「越秀区」这类区名字符串，与分组维度 district
    # 同名不同义）→ 按行业：范围已小于一个区，再按区分组只会是单行表（零信息量）。
    out_d = _stress(monkeypatch, {"hypothesis": "广州越秀区收入下降 20%"}, snap)
    assert out_d["group_by"] == "industry"

    # 仅城市限定（有 city、无区名/行业）→ 按区：单城内看哪个区越线增量最大。
    # 映射由 _district_loan_map 供（stub 已在 _stress 里顶住网络）。
    out2 = _stress(monkeypatch, {"hypothesis": "广州收入下降 20%"}, snap)
    assert out2["group_by"] == "district"

    # 全国无限定 → 按城市看落点：哪个城市越线增量最大
    out3 = _stress(monkeypatch, {"hypothesis": "全国收入下降 20%"}, snap)
    assert out3["group_by"] == "city"
    # 城市分组回告全国口径文案（复用房价分支已有词条，双字典已注册）
    assert out3["group_note"] == "全国冲击 → 按城市分组：看哪个城市越线增量最大"
    assert out3["group_note_key"] == "全国冲击 → 按城市分组：看哪个城市越线增量最大"


def test_stress_district_grouping_aggregates_by_boundary(monkeypatch):
    # district 分组实际聚合：2 个区各 1 笔 + 1 笔坐标未命中任何区（「未识别」）+
    # 1 笔缺偿债比（未识别组，计 loans/balance 但不进 red_delta / median_after）+
    # 1 笔非情景城市（geo 闸门剔除，绝不入组）。组按 red_delta_balance 降序。
    district_map = {1: "天河区", 2: "越秀区", 3: None, 4: None}
    snap = {
        "loans": [
            _loan(1, 0.60, balance=1_000_000.0),  # 天河区 0.60→0.75 越线
            _loan(2, 0.60, balance=2_000_000.0),  # 越秀区 0.60→0.75 越线
            _loan(3, 0.95, balance=3_000_000.0, city="sz"),  # 非情景城市 → 不参与
            _loan(4, None, balance=500_000.0),  # 缺 DTI → 未识别组规模口径
        ],
        "zones": {},
    }
    out = _stress(monkeypatch, {"hypothesis": "广州收入下降 20%"}, snap, district_map=district_map)
    assert out["group_by"] == "district"
    assert out["group_level"] == "district"  # 区级层级随响应下发
    assert [g["key"] for g in out["groups"]] == ["越秀区", "天河区", "未识别"]
    by_key = {g["key"]: g for g in out["groups"]}
    assert by_key["越秀区"]["red_delta_loans"] == 1
    assert by_key["越秀区"]["red_delta_balance"] == 2_000_000.0
    assert by_key["天河区"]["red_delta_balance"] == 1_000_000.0
    # 未识别不丢弃：缺 DTI 的笔计入组规模（loans/balance），但不进越线/中位数
    assert by_key["未识别"]["loans"] == 1
    assert by_key["未识别"]["balance"] == 500_000.0
    assert by_key["未识别"]["red_delta_loans"] == 0
    assert by_key["未识别"]["median_after"] is None
    # group_note 双轨：city 发 code（gz），区名只在分组行原文直出
    assert out["group_note"] == "冲击范围广州 → 按区分组：看该范围内哪个区越线增量最大"
    assert out["group_note_key"] == "冲击范围{city} → 按区分组：看该范围内哪个区越线增量最大"
    assert out["group_note_params"] == {"city": "gz"}


def test_stress_district_grouping_boundary_unavailable_falls_back(monkeypatch):
    # 边界数据不可用（下载失败且无缓存）→ 优雅回落按行业分组，group_note 如实
    # 回告回落原因；组数据走行业口径，scope_note 不受影响。
    snap = {
        "loans": [
            _loan(1, 0.60, industry="tech", city="gz"),
            _loan(2, 0.60, industry="manufacturing", city="gz", balance=2_000_000.0),
        ],
        "zones": {},
    }
    out = _stress(
        monkeypatch,
        {"hypothesis": "广州收入下降 20%"},
        snap,
        district_map=None,  # None = 边界数据不可用（区别于空表「可用但未命中」）
    )
    assert out["group_by"] == "industry"
    assert out["group_level"] is None  # 回落后不冒充分组层级
    assert out["group_note"] == "边界数据不可用，已回落按行业分组"
    assert out["group_note_key"] == "边界数据不可用，已回落按行业分组"
    assert out["group_note_params"] == {}
    # 组数据确实是行业口径（回落不是空转）
    assert [g["key"] for g in out["groups"]] == ["manufacturing", "tech"]
    assert out["scope_note"] == "仅统计广州存量"


def test_stress_explicit_district_without_city_rejected(monkeypatch):
    # 显式 group_by=district 但情景无城市落点（「全国收入下降」解析不出 city）：
    # 全省/全国口径下区名会跨城歧义，明确 400（app.py 把 ValueError 转 400）。
    snap = {"loans": [_loan(1, 0.60, city="gz")], "zones": {}}
    with pytest.raises(ValueError, match="按区分组需要情景限定城市"):
        _stress(monkeypatch, {"hypothesis": "全国收入下降 20%", "group_by": "district"}, snap)


def test_stress_rate_drop_still_rejected_with_new_reason(monkeypatch):
    snap = {"loans": [_loan(1, 0.60)], "zones": {}}
    with pytest.raises(ValueError, match="利率类情景"):
        _stress(monkeypatch, {"hypothesis": "贷款利率下调 1 个百分点"}, snap)


# ---------------- 3b. 直筒子市镇级分组 + 区名限定的坐标闸门 ----------------


def test_parse_income_direct_controlled_city():
    # 直筒子市在 CITY_MAP 词表内：「中山 / 东莞收入下降」必须解析出城市码
    # （镇级分组与坐标闸门的入口），且不带区名（两市不设区）
    s = p6.parse_scenario("中山收入下降 20%")
    assert s["kind"] == "income_drop" and s["city"] == "zs" and s["district"] is None
    d = p6.parse_scenario("东莞收入下降 15%")
    assert d["kind"] == "income_drop" and d["city"] == "dg" and d["district"] is None


def test_stress_district_grouping_town_level(monkeypatch):
    # 直筒子市（中山）镇级分组：group_level=town + 镇街版 group_note（双轨，
    # en / zh-HK 双字典注册），组名是镇街业务原文（小榄镇 / 石岐街道）。
    district_map = {1: "小榄镇", 2: "石岐街道"}
    snap = {
        "loans": [
            _loan(1, 0.60, city="zs"),  # 小榄镇 0.60→0.75
            _loan(2, 0.60, city="zs", balance=2_000_000.0),  # 石岐街道，增量最大排前
        ],
        "zones": {},
    }
    out = _stress(
        monkeypatch,
        {"hypothesis": "中山收入下降 20%"},
        snap,
        district_map=district_map,
        district_level="town",
    )
    assert out["group_by"] == "district"
    assert out["group_level"] == "town"
    assert out["group_note"] == "冲击范围中山 → 按镇街分组：看该范围内哪个镇街越线增量最大"
    assert out["group_note_key"] == "冲击范围{city} → 按镇街分组：看该范围内哪个镇街越线增量最大"
    assert out["group_note_params"] == {"city": "zs"}
    assert [g["key"] for g in out["groups"]] == ["石岐街道", "小榄镇"]


def test_income_district_scope_uses_coordinate_map(monkeypatch):
    # 「广州越秀区收入下降」：合成地址不含真实区名（旧地址闸门筛后 0 笔），
    # 坐标优先口径下按注入的坐标映射圈冲击面：
    # L1 坐标落越秀区（地址反写天河也不影响——坐标优先）→ 参与；
    # L2 坐标落天河区 → 不参与；
    # L3 无坐标 + 地址含越秀区 → 地址兜底命中；
    # L4 无坐标 + 地址不含 → 兜底也救不了，排除。
    snap = {
        "loans": [
            {**_loan(1, 0.60), "addr": "广州市天河区x路1号"},  # 坐标 → 越秀区
            {**_loan(2, 0.60), "addr": "广州x小区2号"},  # 坐标 → 天河区
            {
                **_loan(3, 0.60, balance=500_000.0),
                "lat": None,
                "lng": None,
                "addr": "广州市越秀区x路3号",
            },  # 无坐标 → 地址兜底命中
            {
                **_loan(4, 0.60),
                "lat": None,
                "lng": None,
                "addr": "广州市天河区x路4号",
            },  # 无坐标 → 兜底不中
        ],
        "zones": {},
    }
    out = _stress(
        monkeypatch,
        {"hypothesis": "广州越秀区收入下降 20%"},
        snap,
        district_map={1: "越秀区", 2: "天河区", 3: None, 4: None},
    )
    # 区名限定 → 缺省按行业分组（象限推断不变），但组数不再为 0（坐标闸门生效）
    assert out["group_by"] == "industry"
    assert out["totals"]["loans"] == 2
    assert out["totals"]["balance"] == 1_500_000.0
    assert [g["key"] for g in out["groups"]] == ["tech"]
    assert out["groups"][0]["loans"] == 2


def test_income_district_scope_falls_back_to_address_without_boundary(monkeypatch):
    # 边界数据不可用（映射 None）→ 闸门回落地址子串匹配（旧行为）：
    # 坐标在但映射不可用同样回落——兜底口径是「边界不可用」而非「坐标不可用」。
    snap = {
        "loans": [
            {**_loan(1, 0.60), "addr": "广州市越秀区x路1号"},
            {**_loan(2, 0.60), "addr": "广州市天河区x路2号"},
        ],
        "zones": {},
    }
    out = _stress(
        monkeypatch,
        {"hypothesis": "广州越秀区收入下降 20%"},
        snap,
        district_map=None,
        district_level=None,
    )
    assert out["group_by"] == "industry"
    assert out["totals"]["loans"] == 1
    assert out["totals"]["balance"] == 1_000_000.0


def test_geo_gate_district_suffix_normalization():
    # 后缀归一化：输入「越秀」（无后缀）命中数据侧「越秀区」，「小榄镇」同理；
    # 区名分支有映射时坐标说了算（映射 None 即坐标在任何区外 → 不在情景地域，
    # 不回落地址）；无坐标回落地址子串。
    dm = {1: "越秀区", 2: "小榄镇"}
    sc_yuexiu = {"kind": "income_drop", "pct": 0.2, "city": "gz", "district": "越秀", "label": "x"}
    assert p6._loan_in_shock_geo({"loan_id": 1, "lat": 23.1, "lng": 113.3}, sc_yuexiu, dm)
    assert not p6._loan_in_shock_geo({"loan_id": 2, "lat": 22.6, "lng": 113.2}, sc_yuexiu, dm)
    sc_xiaolan = {
        "kind": "income_drop",
        "pct": 0.2,
        "city": "zs",
        "district": "小榄镇",
        "label": "x",
    }
    assert p6._loan_in_shock_geo({"loan_id": 2, "lat": 22.6, "lng": 113.2}, sc_xiaolan, dm)
    assert not p6._loan_in_shock_geo({"loan_id": 1, "lat": 23.1, "lng": 113.3}, sc_xiaolan, dm)
    # 有坐标但映射 None（任何区外）→ 判不在情景地域，绝不拿地址翻案
    assert not p6._loan_in_shock_geo(
        {"loan_id": 3, "lat": 23.0, "lng": 113.0, "addr": "广州市越秀区x路"}, sc_yuexiu, dm
    )
    # 无坐标 → 地址兜底
    assert p6._loan_in_shock_geo({"loan_id": 4, "addr": "广州市越秀区x路"}, sc_yuexiu, dm)
    assert not p6._loan_in_shock_geo({"loan_id": 5, "addr": "广州市天河区x路"}, sc_yuexiu, dm)
    # 边界不可用（映射 None）→ 全量走地址兜底（旧行为）
    sc_full = {"kind": "income_drop", "pct": 0.2, "city": "gz", "district": "越秀区", "label": "x"}
    assert p6._loan_in_shock_geo(
        {"loan_id": 6, "lat": 23.1, "lng": 113.3, "addr": "广州市越秀区x路"}, sc_full, None
    )
    assert not p6._loan_in_shock_geo(
        {"loan_id": 7, "lat": 23.1, "lng": 113.3, "addr": "广州市天河区x路"}, sc_full, None
    )


# ---------------- 4. DTI 红线设置 ----------------


def test_setting_persists_and_audits(monkeypatch, conn):
    # FakeConn 顶替 crawl_conn/ddl_conn：验证 upsert SQL、审计动作与返回值
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(p6.db, "ddl_conn", lambda: conn)
    writes = []
    monkeypatch.setattr(p6.db, "write_audit", lambda *a: writes.append(a))
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {"dti_red_line": 0.6},
            "ip": "127.0.0.1",
            "user": {"user": "qa", "role": "admin", "label": "admin"},
        },
    )()
    out = p6.handle_setting(ctx)
    assert out == {"ok": True, "dti_red_line": 0.6}
    assert writes and writes[0][0] == "policy_setting"
    detail = json.loads(writes[0][3])
    assert detail["old"] == 0.7 and detail["value"] == 0.6


def test_setting_out_of_range_rejected(monkeypatch, conn):
    monkeypatch.setattr(p6.db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(p6.db, "ddl_conn", lambda: conn)
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {"dti_red_line": 1.5},
            "ip": "127.0.0.1",
            "user": {"user": "qa", "role": "admin", "label": "admin"},
        },
    )()
    with pytest.raises(ValueError, match="dti_red_line 需在"):
        p6.handle_setting(ctx)


def test_setting_non_write_role_denied(monkeypatch, conn):
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {"dti_red_line": 0.6},
            "ip": "127.0.0.1",
            "user": {"user": "da", "role": "da", "label": "da"},
        },
    )()
    out = p6.handle_setting(ctx)
    assert isinstance(out, tuple) and out[0] == 403
    assert out[1]["error_key"] == "无权限：仅管理员（admin）可修改空间惩罚项"


def test_get_dti_red_line_falls_back_to_default(monkeypatch, conn):
    class _BoomConn:
        def cursor(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(p6.db, "crawl_conn", lambda: _BoomConn())
    assert p6.get_dti_red_line() == p6.DTI_RED_DEFAULT == 0.70


# ---------------- 5. pick_profile_loan：行业收窄叙事对象 ----------------


def test_pick_profile_narrows_to_scenario_industry():
    snap = {
        "loans": [
            _loan(1, 0.90, industry="tech"),
            _loan(2, 0.85, industry="mfg"),
        ],
        "zones": {},
    }
    p = p6.pick_profile_loan(snap, "city", "gz", None, None, industry="mfg")
    assert p["loan_id"] == 2
    # 行业内无候选 → None（不硬凑行业外贷款）
    assert p6.pick_profile_loan(snap, "city", "gz", None, None, industry="education") is None


def test_pick_profile_income_shock_does_not_inflate_ltv():
    # 收入类 shock 不改 LTV：eff 必须按原 LTV 排序（只有 price_drop 才放大）
    snap = {"loans": [_loan(1, 0.90, industry="tech")], "zones": {}}
    income_shock = {**_INCOME_SHOCK, "industry": "tech"}
    p = p6.pick_profile_loan(snap, "city", "gz", 0.95, income_shock, industry="tech")
    assert p["is_hit"] is False  # 0.90 ≤ 0.95，收入冲击不得虚增 LTV 命中


# ---------------- 6. 前端静态守门（设置控件 + 第二结果形态） ----------------

import os  # noqa: E402

_JS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "static", "pages", "p6_policy.js"
)
with open(_JS, encoding="utf-8") as _f:
    JS_TEXT = _f.read()


def test_fe_dti_setting_control_present_and_wired():
    assert 'id="p6-dti-red"' in JS_TEXT, "缺 DTI 红线设置输入框"
    assert '"/api/policy/setting"' in JS_TEXT, "设置控件未接 POST /api/policy/setting"
    # 初始值来自 GET /api/policy 下发的 thresholds.dti_red_line（而非写死）
    assert "thresholds.dti_red_line" in JS_TEXT
    # DTI 行随「仅冲击」分支显隐（默认隐藏）——它唯一生效的分支就是
    # 组合压力推演的收入类情景；常驻展示会让人误以为规则试算也读它。
    # 显隐写点已收敛到 setScenarioDisplay（原 bindStress 随「不挂规则」开关退场）。
    assert 'id="p6-dti-red-wrap" style="display:none' in JS_TEXT, "DTI 行默认应为隐藏"
    assert 'dti.style.display = st.shockOnly ? "flex" : "none"' in JS_TEXT, (
        "DTI 行未随仅冲击分支显隐"
    )


def test_fe_income_second_form_rendering():
    # renderStress 必须按 shock.kind 分流：DTI 直方图/结论文案/缺失口径
    assert "income_drop" in JS_TEXT and "dti_hist" in JS_TEXT, "renderStress 缺收入第二形态分支"
    for key in (
        "收入压力红线（DTI）",
        "收入下降 {pct} 使 {delta} 笔（{bal}）越过 DTI 红线 {red}",
        "冲击前后 DTI 分布直方图",
        "缺失偿债比 ",
        "DTI 中位数 ",
    ):
        assert key in JS_TEXT, f"缺词条/分支：{key}"


def test_fe_dict_parity_for_new_keys():
    # en 与 zh-HK 字典必须同时收录新词条（对账 test_p6_sensitivity_ladder 的同款纪律）
    for key in (
        "收入压力红线（DTI）",
        "收入压力红线已更新为 {v}%（已留痕）",
        "收入压力红线保存失败：",
        "该情景下无新增越线",
        # 批次①：组合压力作用域提示 / 分组维度说明双轨 key（scope_note / group_note）
        "仅统计{industry}借款人",
        "仅统计{city}存量",
        "仅统计{city}{district}存量",
        # 行业 + 地域并存时后端「；」拼接出的组合 key（BUG-2 修复：须一并注册）
        "仅统计{industry}借款人；仅统计{city}存量",
        "仅统计{industry}借款人；仅统计{city}{district}存量",
        "收入冲击（全行业） → 按行业分组：看哪个行业越线增量最大",
        "冲击范围{industry} → 按城市分组：看该范围内哪个城市越线增量最大",
        "冲击范围{city} → 按城市分组：看该范围内哪个城市越线增量最大",
        "冲击范围{city}{district} → 按城市分组：看该范围内哪个城市越线增量最大",
        "全组合口径，不分组",
        "全国冲击 → 按城市分组：看哪个城市越线增量最大",
        "冲击范围{city} → 按区分组：看该范围内哪个区越线增量最大",
        "冲击范围{city} → 按镇街分组：看该范围内哪个镇街越线增量最大",
        "边界数据不可用，已回落按行业分组",
        "冲击范围{city} → 按行业内分组：看该地域内哪个行业越线增量最大",
        "冲击范围{city}{district} → 按行业内分组：看该地域内哪个行业越线增量最大",
        # 批次②：传导链 #p10-trace 双轨 key（step / detail / basis，覆盖 4 个互斥出口）
        "情景核对",
        "情景解析",
        "传导测算（逐笔）",
        "规则评估",
        "传导测算（示意）",
        "房价下跌 {label_pct}%（限{district}）：作用域内没有位于{district}的存量——该情景对本次推演的定量结果无影响",
        "地域闸门：仅位于{district}的 记录 参与冲击；作用域内命中 0 笔 → LTV 分布与命中判定均按原口径",
        "房价下跌 {label_pct}；（限{district}）仅作用于作用域内位于{district}的{geo_loans} 笔存量（余额 {geo_balance} 元），其余 {outside} 笔不在情景地域、按原 LTV 判定，LTV 按 ÷(1−{pct}) 逐笔重算",
        "房价下跌 {label_pct}；（限{district}）仅作用于作用域内位于{district}的{geo_loans} 笔存量（余额 {geo_balance} 元），LTV 按 ÷(1−{pct}) 逐笔重算",
        "房价下跌 {label_pct}；作用于规则作用域内的 {in_loans} 笔存量（余额 {in_balance} 元），LTV 按 ÷(1−{pct}) 逐笔重算",
        "LTV = 贷款余额 ÷ 抵押物价值，价值 ×(1−{pct}) → 每笔 LTV ×{lift_mult}（等效 +{lift_pct}%），与贷款余额无关，故全作用域等比右移",
        "{district}内 LTV 中位数 {median_before} → {median_after}，P90 {p90_before} → {p90_after}；越过拟议上限 {cap}：{bh_loans} → {h_loans} 笔；越过红线 {red_line}：{ba_loans} → {aa_loans} 笔",
        "作用域内 LTV 中位数 {median_before} → {median_after}，P90 {p90_before} → {p90_after}；越过拟议上限 {cap}：{bh_loans} → {h_loans} 笔；越过红线 {red_line}：{ba_loans} → {aa_loans} 笔",
        "分布整体等比右移，中位数/P90 同比例上移；越线增量取决于原分布在该阈值附近的密度，不是固定的百分比——看分布才知冲击落在哪些贷款上",
        "规则生效后新增受限 {nh_loans} 笔，较无冲击口径 +{extra} 笔（余额 +{extra_balance} 元）；保存落库的始终是无冲击的原规则",
        "可复核：新增受限 {extra} 笔 = 原 LTV 落在冲击敏感带 ({fb0}, {fb1}] 的贷款（敏感带内恰 {flip_count} 笔，两处应相等）；带外贷款冲击前后判定不变",
        "可复核：新增受限 {extra} 笔 = 整区命中口径下（敏感带内恰 {flip_count} 笔，两处应相等）；带外贷款冲击前后判定不变",
        "未识别出改变存量 LTV 的冲击（利率/收入类情景不经过抵押物价值），定量按现行口径",
        "利率变动影响月供（现金流端）、收入变动影响负担率（偿付端），均不触碰 LTV = 余额 ÷ 价值的分子分母",
        # 批次② · scenario_math 双轨（利率/收入类情景的确定性传导测算，示意口径）
        "月供参考（100 万 / 30 年等额本息）：¥{p0} → ¥{p1}（{delta} / 月）",
        "示意口径：基准利率 {base}%，贷款利率下调 {mag} 个百分点；未考虑重定价节奏与个体差异",
        "示意口径：基准利率 {base}%，贷款利率上调 {mag} 个百分点；未考虑重定价节奏与个体差异",
        "等额本息公式 M = P·r·(1+r)^n ÷ ((1+r)^n − 1)，P = 100 万、n = 360 期、r = 年利率 ÷ 12（{base}% → {r1}%）",
        "同等月供下，收支比（负担率）放大约 {factor}%",
        "示意口径：等比传导，未考虑储蓄缓冲与收入结构差异",
        "负担率 = 月供 ÷ 月收入；收入 ×(1−{pct}) → 负担率 ÷(1−{pct})，即放大 1/(1−{pct}) − 1 = +{factor}%",
        # 批次② · 预览级 mismatch（C1）
        "作用域内没有位于{district}的存量——该情景对本次推演的定量结果无影响",
        # 批次② · shock.label 双轨（系统规范化标签）
        "房价下跌 {pct}%",
        "收入下降 {pct}%",
        "贷款利率下调 {mag} 个百分点",
        "贷款利率上调 {mag} 个百分点",
        # 批次② · A6 示意步 detail_key（lines_key 拼接 note_key，须整体注册）
        "同等月供下，收支比（负担率）放大约 {factor}%；示意口径：等比传导，未考虑储蓄缓冲与收入结构差异",
        "月供参考（100 万 / 30 年等额本息）：¥{p0} → ¥{p1}（{delta} / 月）；示意口径：基准利率 {base}%，贷款利率下调 {mag} 个百分点；未考虑重定价节奏与个体差异",
        "月供参考（100 万 / 30 年等额本息）：¥{p0} → ¥{p1}（{delta} / 月）；示意口径：基准利率 {base}%，贷款利率上调 {mag} 个百分点；未考虑重定价节奏与个体差异",
        # 批次② · mismatch 双轨（城市核对不符步：detail_key 模板 + 作用域拆分占位）
        "情景限定{geo}，与作用域（{stype}{sval}）不在同一城市——该情景对本次推演的定量结果无影响",
        "城市 · ",
        "网格 ",
        "行业 · ",
        # Phase2 · 上行情景（脱险）双轨 key（process-chain 叙事 + scenario_math 收入腿翻转）
        "脱险测算",
        "房价上涨 {label_pct}；作用于规则作用域内的 {in_loans} 笔存量（余额 {in_balance} 元），LTV 按 ÷(1{pct}) 逐笔重算",
        "LTV = 贷款余额 ÷ 抵押物价值，价值 ×(1−{pct}) → 每笔 LTV ÷(1+涨幅)，有效 LTV 下降",
        "脱险 {n} 笔（余额 {bal} 元）；风险敞口下降 {rrb} 元",
        "脱险 = 冲击后有效 LTV 降回红线 {red_line} 以下（原越红线者）；风险敞口下降 = Σ 余额×(原 LTV−有效 LTV)",
        "上行只降险，不新增命中",
        "房价上涨 {pct}%",
        "收入提升 {pct}%",
        "同等月供下，收支比（负担率）下降约 {factor}%",
        "负担率 = 月供 ÷ 月收入；收入 ×(1−{pct}) → 负担率 ÷(1−{pct})，即下降 1/(1−{pct}) − 1 = {factor}%",
        "同等月供下，收支比（负担率）下降约 {factor}%；示意口径：等比传导，未考虑储蓄缓冲与收入结构差异",
    ):
        assert JS_TEXT.count(f'"{key}"') >= 2, f"词条未双语齐备：{key}"


def test_fe_district_dim_label_registered():
    # district 分组的维度表头 t("区") / t("镇街")（直筒子市镇级）：en / zh-HK 两
    # 字典各注册一条（裸 key 风格与城市 / 行业 词条一致，故不用上面的 '"key"'
    # 计数法，逐字典点名字面量）。
    assert '区: "District"' in JS_TEXT, "en 字典缺「区」词条"
    assert '区: "區"' in JS_TEXT, "zh-HK 字典缺「区」词条"
    assert '镇街: "Town / Subdistrict"' in JS_TEXT, "en 字典缺「镇街」词条"
    assert '镇街: "鎮街"' in JS_TEXT, "zh-HK 字典缺「镇街」词条"
    # 收入类 dimTh 必须有 district 分支且按 group_level 切区 / 镇街（房价类表不动，
    # 仍只有行业/城市两分支）
    assert 'groupBy === "district" ? (r.group_level === "town" ? t("镇街") : t("区"))' in JS_TEXT, (
        "收入类分组表头缺 district 分支或未按 group_level 切换"
    )


def test_fe_backend_error_sentences_translated():
    # 后端错误句必须整句注册进双语字典，且前端对错误消息 t() 整句查表——
    # 否则 en/zh-HK 下状态行会拼出「压力测试失败：+ 中文原文」（i18n 违例）。
    for key in (
        "hypothesis 不能为空：请输入房价下跌 / 收入下降类情景（如「房价下跌 10%」）",
        "未能解析为可推演情景。定量压力支持：房价下跌（如「广州房价下跌 10%」「全国房价下跌 20%」，需带百分比）、收入下降（如「制造业收入下降 15%」，可带城市 / 区 / 行业限定，如「广州制造业收入下降 20%」）；失业率、利率类暂无逐笔定量口径（利率情景可作规则推演的叙事参考）",
        "组合压力测试暂不支持利率类情景：逐笔月供需要执行利率与剩余期限，数据不具备，利率情景请在规则试算中作叙事参考",
        "dti_red_line 必须是数字",
        "dti_red_line 需在 0.30–0.95 之间",
    ):
        assert JS_TEXT.count(f'"{key}"') >= 2, f"错误句未双语注册：{key[:20]}…"
    assert 't("压力测试失败：") + t(String((e && e.message) || e))' in JS_TEXT, (
        "runStress 错误消息未整句查表"
    )


def test_backend_error_sentences_are_static():
    # 错误句不得回显动态值（动态值进不了字典键）：DTI 两条已静态化
    import inspect

    src = inspect.getsource(p6.handle_setting)
    assert "收到" not in src, "handle_setting 错误句回显动态值，无法整句翻译"
