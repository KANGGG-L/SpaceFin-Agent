"""T6 组合压力测试（前端）+ T7 作用域标签人话化 的守护测试。

统一推演入口改造后的守护口径（NL 完全替代情景输入后的现行形态）：
1. 模式 chip 与「不挂规则」开关均已删：入口职责由 NL 解析结果承担——仅冲击无规则
   → 自动路由组合压力测试；有规则（可叠加冲击）→ 规则试算。情景识别展示块
   （hyp-wrap）仍在规则表单之前，随 NL 解析经 setScenarioDisplay 展开（admin 闸门）；
2. 压力区与规则模式状态隔离：p6_policy.js 的「压力测试区」切片内不得出现
   _historyReadOnly，且 st.preview 只许清空、不许赋非空值——压力结果不落规则态的
   结果区、不可保存生效。但压力**进推演历史**（kind:"stress"，带 affected 受冲击
   新越线逐笔明细，供「导出结论」处查看），与规则试算同列最近 5 次；
3. 压力结果直方图的 barChart 调用必须带 ariaLabel 与 title 回调（一期无障碍
   与 hover 口径）。

附带 scope_label（p6_policy.py，被前端与审计明细共用）的 zh-CN 输出单测，
以及「前端 zoneCoreLabel 与后端 scope_label 数值算法同式」的静态对账——
两端漂移是 scope_label 注释里点名的红线。
"""

import os
import re

import pages.p6_policy as p6

_HERE = os.path.dirname(os.path.abspath(__file__))
_JS_PATH = os.path.join(_HERE, "..", "static", "pages", "p6_policy.js")
with open(_JS_PATH, encoding="utf-8") as _f:
    JS = _f.read()

_P5_JS_PATH = os.path.join(_HERE, "..", "static", "pages", "p5_spatial.js")
with open(_P5_JS_PATH, encoding="utf-8") as _f:
    JS_P5 = _f.read()

# 压力区切片：两个注释锚点之间的全部代码（renderStress / runStress /
# showResultCard 都必须待在锚点内，挪出去即失去隔离守护）。
_m_stress = re.search(
    r"/\* ===== 压力测试区（T6）.*?\*/(.*?)/\* ===== 压力测试区结束 ===== \*/",
    JS,
    re.S,
)
assert _m_stress, "压力测试区锚点丢失：守护切片失效"
STRESS_BLOCK = _m_stress.group(1)


# ---------------- 1. 统一入口：模式 chip 与「不挂规则」开关均已删 ----------------


def test_mode_chips_retired():
    # 「规则试算 / 组合压力测试」两条主线合并为一张表单：chip 与 applyStressMode
    # 必须彻底退场，避免出现第二套入口与 showResultCard 的显隐口径打架。
    for token in (
        'id="p6-mode-bar"',
        'id="p6-mode-rule"',
        'id="p6-mode-stress"',
        "function applyStressMode()",
        "_p6Mode",
    ):
        assert token not in JS, f"旧模式入口残留：{token}"


def test_no_rule_switch_retired_route_by_parse_result():
    # NL 完全替代情景输入后，「不挂规则」开关彻底退场：仅冲击无规则的分支由
    # NL 解析结果结构性决定（parseAndFillRule → "stress"）。开关残留 = 第二套入口。
    assert 'id="p6-no-rule"' not in JS and "p6-no-rule" not in JS, "「不挂规则」开关残留"
    # 情景识别展示块（hyp-wrap）必须仍在规则表单之前：它继承 admin 闸门的展示位——
    # 非 admin 的 NL 冲击识别在闸门处静默忽略，进不了仅冲击分支。
    i_wrap = JS.index('id="hyp-wrap"')
    i_form = JS.index('id="p6-form"')
    assert i_wrap < i_form, "模板结构变化：hyp-wrap 应在 p6-form 之前"
    # 情景回显行（p6-nl-shock）随「AI 多步推理」两段式确认流退场：系统理解改由
    # decompose 确认卡（#p6-confirm-card）复述，不再有内联回显行——模板残留 = 第二套复述。
    assert 'id="p6-nl-shock"' not in JS, "p6-nl-shock 回显行残留：复述职责已移交 decompose 确认卡"
    # DTI 红线行仍挂在 hyp-wrap 内（仅「仅情景 + 收入类情景」分支显隐）。
    assert 'id="p6-dti-red-wrap"' in JS[i_wrap:i_form], "hyp-wrap 缺 DTI 红线行"


def test_unified_run_routes_to_stress():
    # runSimulation 的路由必须发生在规则校验（readForm）之前：
    # NL 解析返回 "stress"（仅冲击无规则）→ runStress；否则规则分支。
    m = re.search(r"async function runSimulation\(\)(.*?)\n  \}", JS, re.S)
    assert m, "未找到 runSimulation"
    body = m.group(1)
    assert 'route === "stress"' in body and "return runStress();" in body, (
        "runSimulation 缺仅冲击路由"
    )
    assert body.index('route === "stress"') < body.index("readForm()"), (
        "路由必须先于 readForm()——否则仅冲击输入会先被规则校验拦截"
    )


def test_only_scenario_branch_requires_scenario():
    # 仅冲击分支的必填校验必须在发请求之前拦下（校验文案 → return，不触达 api）。
    m = re.search(r"async function runStress\(\)(.*?)\n  \}", JS, re.S)
    assert m, "未找到 runStress"
    body = m.group(1)
    i_check = body.index("请先用自然语言描述冲击（例「全国房价下跌 20%」）：仅情景推演不挂规则")
    i_api = body.index('api("/api/policy/stress"')
    assert i_check < i_api, "仅冲击分支的空情景校验必须先于 stress 请求"
    # 冲击文本来自 NL 解析状态（st.nlShock），不得再读任何输入框
    assert "st.nlShock" in body and 'el("p10-hyp-input")' not in JS, (
        "runStress 冲击来源不在 NL 状态"
    )


def test_save_gate_after_sandbox_split():
    # 沙盘拆分后两区各自独立呈现，压力结果不再遮挡规则结果卡——
    # 保存判据收敛为「规则侧有可落库的推演结果」（syncSaveState 不再读压力卡显隐，
    # 压力运行不碰 st.preview，天然无可保存对象）。
    m = re.search(r"function syncSaveState\(\)(.*?)\n  \}", JS, re.S)
    assert m, "未找到 syncSaveState"
    body = m.group(1)
    assert "p6-stress-card" not in body, "syncSaveState 仍读压力卡显隐（拆分后已无此耦合）"
    assert "st.preview != null" in body, "保存判据必须锚定 st.preview"
    # 压力卡显隐写点收敛：runStress 成功路径显示；重置路径与「冲击→规则试算」切换
    # 路径隐藏并清空结果（双向不串味——切走时上一轮压力产出必须退场）。
    assert 'el("p6-stress-card")' in JS, "缺压力卡显示/隐藏写点"
    assert JS.count('stressCard.style.display = "none"') >= 1, "必须存在隐藏压力卡的写点"


def test_group_by_not_a_user_input():
    # 分组维度由后端按情景文本自动推断：前端不得再渲染选择器、不得在请求里传该参数。
    assert "p6-stress-group" not in JS, "分组维度选择器残留（应由后端自然语言推断）"
    m = re.search(r"async function runStress\(\)(.*?)\n  \}", JS, re.S)
    assert m and "group_by: " not in m.group(1), "runStress 仍在传 group_by（应交给后端推断）"


def test_single_deduction_result_heading():
    # 规则结果卡只有一个「② 规则试算结果」标题。沙盘拆分后压力结果挂在 #p6-sandbox-zone
    # 分区内、自带子标题「组合压力测试（不挂规则）」，规则结果卡不再承载任何沙盘产出。
    assert JS.count('t("② 规则试算结果")') == 1, (
        "「② 规则试算结果」标题出现多次（结果区被拆成多张卡）"
    )
    assert 'id="p6-result-mode"' not in JS, "分支徽标残留（两区独立后不再需要互斥徽标）"
    # 压力卡仍是 display:none 的容器（由 runStress / 重置驱动显隐），并带自己的子标题。
    assert 'id="p6-stress-card" style="display:none"' in JS, "压力结果容器形态变了"
    assert 't("组合压力测试（不挂规则）")' in JS, "压力卡缺子标题（原靠结果卡头徽标区分，已删）"


def test_rerun_stress_keeps_shock_text():
    # 重复运行组合压力必须仍然可用。runStress 成功后会调 setScenarioDisplay("stress")
    # 只为展开情景块（不传第二参数），若该函数无条件写 `st.nlShock = shockText || ""`，
    # 就会顺手把冲击原文清空——第二次点运行时必填校验读到空值，直接报
    # 「请先用自然语言描述冲击」。S2 之前每次运行都重新解析、会重设 nlShock，
    # 这个清空一直被掩盖，解析前移之后才暴露。
    m = re.search(r"function setScenarioDisplay\(kind, shockText\)(.*?)\n  \}", JS, re.S)
    assert m, "未找到 setScenarioDisplay"
    assert "shockText !== undefined" in m.group(1), "展开情景块时不得清空 st.nlShock"


def test_group_by_is_defined_before_use():
    # 分组维度变量必须有定义。它一度是**悬空引用**（JS 里从未声明），一读到就抛
    # ReferenceError「groupBy is not defined」，压力测试结果渲染到分组表时整块崩掉。
    # 本文件的守门都是源码字符串比对、并不执行 JS，这类问题天然抓不到，故显式钉死。
    m = re.search(r"function renderStress\(r\)(.*?)\n  \}", JS, re.S)
    assert m, "未找到 renderStress"
    body = m.group(1)
    assert "const groupBy" in body, "groupBy 未定义（悬空变量，读取即 ReferenceError）"
    assert body.index("const groupBy") < body.index("if (groupBy"), "groupBy 定义必须早于使用"


# ---------------- 2. 压力区与规则模式状态隔离 ----------------


def test_stress_block_never_touches_rule_state():
    # 压力运行不得产出「可落库的规则结果」。原先禁止 st.preview 出现，但那留下一个
    # 漏洞：先跑规则试算再跑纯冲击时，st.preview 还留着旧结果——② 卡继续显示上一次
    # 的命中（看着像冲击没生效），保存按钮也还开着。所以现在的口径是：
    # 允许**清空**（清残留），禁止赋任何非空值（那等于把压力结果当规则存库）。
    #
    # _history 例外（T6 新契约）：市场冲击**进推演历史**（kind:"stress"），
    # 与规则试算同列最近 5 次，且带 affected 逐笔明细供「导出结论」处查看。
    # 但压力仍不写规则态的 st.preview / _historyReadOnly —— 两套模式读写面互不重叠。
    for ident in ("_historyReadOnly",):
        assert ident not in STRESS_BLOCK, f"压力测试区内出现规则模式状态引用：{ident}"
    for m in re.finditer(r"st\.preview\s*=\s*([^\s;]+)", STRESS_BLOCK):
        assert m.group(1).startswith("null"), f"压力区只能清空 st.preview，不得赋值：{m.group(0)}"
    # 压力进历史必须带 kind:"stress" 标记（供历史渲染 / 回放 / 导出正确分流），
    # 且受影响逐笔明细取自后端响应的 affected（不是规则 cover）。
    assert 'kind: "stress"' in STRESS_BLOCK, '压力条目缺 kind:"stress" 标记（无法与规则历史分流）'
    assert "r.affected" in STRESS_BLOCK, "压力条目未携带受影响逐笔明细（受冲击新越线 loan）"


def test_stress_block_contains_core_handlers():
    # 统一入口后压力区只保留两件套：渲染、运行（由 runSimulation 按 NL 解析路由）；
    # 「不挂规则」开关的绑定器 bindStress 已随开关退场，残留即死代码；
    # showResultCard 也随沙盘分区拆分退场（压力卡显隐由 runStress / 重置直接驱动）。
    for name in (
        "function renderStress(r)",
        "async function runStress()",
    ):
        assert name in STRESS_BLOCK, f"压力区缺少 {name}（守护切片外的压力代码不受隔离约束）"
    assert "function bindStress" not in JS, "bindStress 残留（开关已删，绑定器是死代码）"
    assert "function showResultCard" not in JS, "showResultCard 残留（沙盘拆分后互斥切换已废弃）"


# ---------------- 3. barChart 调用带 ariaLabel 与 title 回调 ----------------


def test_stress_barchart_has_aria_label_and_title_callback():
    m = re.search(r"barChart\(histBox, histData, \{(.*?)\}\);", STRESS_BLOCK, re.S)
    assert m, "压力区未找到直方图 barChart 调用"
    opts = m.group(1)
    assert "ariaLabel:" in opts, "直方图缺 ariaLabel（无障碍必填）"
    assert re.search(r"title:\s*\(d\)", opts), "直方图缺 title 回调（柱体 hover 口径）"
    assert "yAxisTitle:" in opts, "直方图缺 yAxisTitle（纵轴量纲）"


# ---------------- 4. 两端数值算法同式（T7 标签一致性红线）----------------


def test_zone_label_rounding_formula_synced_two_ends():
    # 后端 scope_label：f"{math.floor(price / 1000 + 0.5) / 10:.1f}"
    assert (
        "math.floor(price / 1000 + 0.5) / 10"
        in open(os.path.join(_HERE, "..", "pages", "p6_policy.py"), encoding="utf-8").read()
    )
    # 前端 zoneCoreLabel：Math.floor(price / 1000 + 0.5) / 10
    assert "Math.floor(price / 1000 + 0.5) / 10" in JS


def test_zone_label_feature_order_synced():
    # 特征取舍顺序两端一致：中位单价 → 相对全市偏离 → 中位 LTV（取前两个）。
    py_src = open(os.path.join(_HERE, "..", "pages", "p6_policy.py"), encoding="utf-8").read()
    feats_py = re.search(r"def _zone_label_feats\(zone\):(.*?)return feats\[:2\]", py_src, re.S)
    assert feats_py, "后端 _zone_label_feats 结构变化：请同步本守护断言"
    body = feats_py.group(1)
    i_price = body.index("median_unit_price")
    i_dev = body.index("price_dev_vs_city")
    i_ltv = body.index("median_ltv")
    assert i_price < i_dev < i_ltv

    core_js = re.search(r"function zoneCoreLabel\(z\) \{(.*?)\n  \}", JS, re.S)
    assert core_js, "前端 zoneCoreLabel 结构变化：请同步本守护断言"
    jbody = core_js.group(1)
    assert (
        jbody.index("median_unit_price")
        < jbody.index("price_dev_vs_city")
        < jbody.index("median_ltv")
    )


# ---------------- 5. scope_label（后端权威输出）zh-CN 单测 ----------------


def _zone(**kw):
    z = {
        "zone_id": "price-gz-113.30-23.14",
        "zone_type": "price",
        "city": "gz",
        "median_unit_price": None,
        "median_ltv": None,
        "price_dev_vs_city": None,
    }
    z.update(kw)
    return {z["zone_id"]: z}


def test_scope_label_zone_full_features():
    zones = _zone(median_unit_price=32456.0, price_dev_vs_city=0.1234)
    assert (
        p6.scope_label("zone", "price-gz-113.30-23.14", zones)
        == "广州 · 价格网格（中位单价 3.2 万/㎡ · 相对全市 +12%）113.30, 23.14"
    )


def test_scope_label_zone_falls_back_to_ltv_feature():
    zones = _zone(median_unit_price=None, price_dev_vs_city=-0.08, median_ltv=0.9123)
    assert (
        p6.scope_label("zone", "price-gz-113.30-23.14", zones)
        == "广州 · 价格网格（相对全市 -8% · 中位 LTV 91.23%）113.30, 23.14"
    )


def test_scope_label_zone_no_features_keeps_coord_tail():
    zones = _zone(median_unit_price=None)
    assert p6.scope_label("zone", "price-gz-113.30-23.14", zones) == "广州 · 价格网格 113.30, 23.14"


def test_scope_label_ltv_grid_without_city():
    zones = {
        "ltv-113.18-23.14": {
            "zone_id": "ltv-113.18-23.14",
            "zone_type": "ltv",
            "city": None,
            "median_unit_price": 21000.0,
            "median_ltv": None,
            "price_dev_vs_city": None,
        }
    }
    assert (
        p6.scope_label("zone", "ltv-113.18-23.14", zones)
        == "LTV 网格（中位单价 2.1 万/㎡）113.18, 23.14"
    )


def test_scope_label_zone_missing_from_zones_dict_degrades():
    assert p6.scope_label("zone", "price-gz-113.30-23.14", {}) == "价格网格 113.30, 23.14"


def test_scope_label_wan_price_floor_rounding():
    # floor(v*10+0.5) 口径：x.x5 边界（如 32500 元/㎡ → 3.25 万）按 +0.5 进到 3.3，
    # 与前端 Math.floor 同式——银行家舍入会给出 3.2，这正是禁用 round 的原因。
    assert p6._wan_price_text(32500.0) == "3.3"
    assert p6._wan_price_text(32499.0) == "3.2"


# ---------------- 6. P5 → P6 交接键两端同键 ----------------


def test_scope_prefill_key_shared_between_p5_and_p6():
    assert "spf_policy_scope_prefill" in JS_P5, "P5 侧未写交接键"
    assert "spf_policy_scope_prefill" in JS, "P6 侧未消费交接键"


# ---------------- 8. 情景示例芯片与「不可定量」黑名单均已退场 ----------------


def test_scenario_suggestion_chips_retired():
    # 情景输入框与示例芯片已随 NL 完全替代退场：不可定量情景（失业率/利率）在
    # 仅冲击分支由后端 400 明示拒绝，前端不再需要「勾选时禁用芯片」的拦截层。
    assert "STRESS_UNSAFE_SUGGESTIONS" not in JS, "不可定量芯片黑名单残留"
    assert ".p10-suggest" not in JS and "p10-hyp-input" not in JS, "情景输入框 / 芯片绑定残留"


# ---------------- 9. 情景块默认不展示（与规则面板同款「隐藏而非收起」口径） ----------------


def test_scenario_block_hidden_behind_admin_toggle():
    # 情景块默认 display:none；admin 的 NL 解析出冲击时由 setScenarioDisplay 展开——
    # 不再有任何按钮/开关可以展开它，唯一入口是 NL 解析与回放回填。
    assert 'id="hyp-wrap" style="display:none' in JS, "情景块必须默认隐藏"
    assert "p6-scenario-btn" not in JS, "「叠加市场情景」小按钮残留"
    # setScenarioDisplay 是情景块显隐的唯一写点（含 DTI 行随分支显隐）
    assert JS.count("setScenarioDisplay") >= 5, "情景展示应经 setScenarioDisplay 统一接管"
    assert 'dti.style.display = st.shockOnly ? "flex" : "none"' in JS, "DTI 行未随仅冲击分支显隐"
    # 重置经 setScenarioDisplay("") 收回情景块
    assert 'setScenarioDisplay("");' in JS, "重置未收回情景块"
