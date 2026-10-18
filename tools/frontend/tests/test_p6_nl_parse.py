"""P6 自然语言起草规则（一期确定性解析）的单元测试 + 前端静态守门。

产品红线（docs/tech/p6-nl-rule-draft.md）：解析只产草稿、确认权在用户、
解析成功也绝不自动运行——守门测试钉住前端 handler 内无 runSimulation 调用。
"""

import os
import re

import pages.p6_policy as p6
import pytest


def _parse(text):
    ctx = type(
        "_Ctx",
        (),
        {
            "body": {"text": text},
            "ip": "127.0.0.1",
            "user": {"user": "qa", "role": "admin", "label": "admin"},
        },
    )()
    return p6.handle_nl_parse(ctx)


# ---------------- 提取口径 ----------------


def test_parse_full_sentence():
    out = _parse("广州 LTV 超 70% 就拒贷")
    assert (out["scope_type"], out["scope_value"]) == ("city", "gz")
    assert out["action"] == "reject"
    assert out["ltv_cap"] == 0.70
    assert set(out["filled"]) == {"scope", "action", "ltv_cap"}


def test_parse_industry_scope_and_strict():
    out = _parse("制造业 加严审批 抵押率 65%")
    assert (out["scope_type"], out["scope_value"]) == ("industry", "manufacturing")
    assert out["action"] == "strict"
    assert abs(out["ltv_cap"] - 0.65) < 1e-9


def test_parse_ltv_ratio_without_percent():
    # 「抵押率0.75」无百分号且 ≤1 → 按小数
    out = _parse("广州 抵押率0.75 拒贷")
    assert abs(out["ltv_cap"] - 0.75) < 1e-9


def test_parse_ltv_without_percent_but_gt1_treated_as_percent():
    # 「LTV超70就拒贷」→ 0.70（>1 视为百分比口径）
    out = _parse("广州 LTV超70就拒贷")
    assert abs(out["ltv_cap"] - 0.70) < 1e-9


def test_parse_partial_success():
    # 只有作用域：部分回填 + filled 只含 scope
    out = _parse("广州")
    assert out["filled"] == ["scope"]
    assert out["action"] is None and out["ltv_cap"] is None


def test_parse_city_and_industry_ambiguous_prefers_city():
    out = _parse("广州制造业收入下降后 LTV 超 80% 拒贷")
    assert (out["scope_type"], out["scope_value"]) == ("city", "gz")
    assert any("行业" in n for n in out["notes"])


def test_parse_ltv_out_of_range_not_filled():
    out = _parse("广州 LTV 超 250% 拒贷")
    assert out["ltv_cap"] is None
    assert "ltv_cap" not in out["filled"]
    assert any("1%–200%" in n for n in out["notes"])


def test_parse_nothing_recognized():
    out = _parse("随便说点什么")
    assert out["filled"] == []
    assert out["scope_value"] is None and out["action"] is None and out["ltv_cap"] is None


def test_parse_notice_action():
    assert _parse("佛山 LTV 85% 仅提示")["action"] == "notice"


# ---------------- 同句混合输入：冲击短语识别（shock_text） ----------------


def test_parse_mixed_rule_and_shock_extracts_clause():
    # 规则 + 冲击同句：三要素照常提取，冲击子句原文回告（引导词「假设」剥掉）
    out = _parse("广州 LTV 超 70% 就拒贷，假设房价跌 10%")
    assert set(out["filled"]) == {"scope", "action", "ltv_cap"}
    assert out["shock_text"] == "房价跌 10%"


def test_parse_shock_clause_keeps_geo_and_industry_words():
    # 冲击子句里用户自写的地域/行业限定必须原样保留（不做规范句重建）——
    # 重建会把规则部分的地域关键词误挂到冲击上
    out = _parse("佛山 LTV 85% 仅提示，假设全国房价下跌 20%")
    assert out["shock_text"] == "全国房价下跌 20%"
    out2 = _parse("深圳互联网行业平均收入下降 15% 后广州 LTV 超 80% 拒贷")
    assert "互联网行业" in out2["shock_text"]
    assert "深圳" in out2["shock_text"]


def test_parse_space_separated_clause_not_whole_sentence():
    # 口语里两个子句常只用空格断开（无逗号）。分隔符只认标点会让 start 退到 0，
    # 整句被当成冲击原文回告——冲击字段会显示成「广州 LTV 超 70% 就拒贷 全国房价下跌30%」。
    out = _parse("广州 LTV 超 70% 就拒贷 全国房价下跌30%")
    assert out["shock_text"] == "全国房价下跌30%"
    assert "LTV" not in out["shock_text"] and "拒贷" not in out["shock_text"]


def test_parse_unseparated_clause_falls_back_to_shock_phrase():
    # 完全没有分隔符时切不出子句：兜底只回告冲击片段，绝不带规则要素
    out = _parse("广州LTV超70%就拒贷全国房价下跌30%")
    assert out["shock_text"], "未识别出冲击"
    assert "LTV" not in out["shock_text"]
    assert "拒贷" not in out["shock_text"]


def test_parse_rule_only_has_no_shock():
    # 纯规则句（NL 示例胶囊同款）不得误识别出冲击
    for text in ("广州 LTV 超 70% 就拒贷", "制造业 加严审批 抵押率 65%", "佛山 LTV 85% 仅提示"):
        assert _parse(text)["shock_text"] is None


def test_parse_invalid_shock_pct_not_prefilled():
    # 跌 150% 无推演意义：parse_scenario 返回 None → 不给 shock_text（别预填一个跑不动的情景）
    out = _parse("广州 LTV 超 70% 就拒贷，假设房价下跌 150%")
    assert out["shock_text"] is None


def test_parse_empty_text_rejected():
    with pytest.raises(ValueError, match="text 不能为空"):
        _parse("")
    with pytest.raises(ValueError):
        _parse(None)


# ---------------- 前端静态守门 ----------------

_JS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "static", "pages", "p6_policy.js"
)
with open(_JS, encoding="utf-8") as _f:
    JS_TEXT = _f.read()


def test_fe_nl_entry_present_and_wired():
    # NL 是唯一主入口（表单收起为确认面板）。
    # S2 两阶段：不再单设「解析」按钮——「运行推演」首点承接解析+复述（绝不运行），
    # 核对无误后再点一次才真正运行。确认权仍在用户（先看到系统复述才运行）。
    assert 'id="p6-nl-input"' in JS_TEXT, "缺 NL 入口"
    assert '"/api/policy/nl_parse"' in JS_TEXT, "未接解析端点"
    assert 'id="p6-nl-parse"' not in JS_TEXT, "「解析」按钮必须移除（并入「运行推演」两阶段）"
    assert "parseAndFillRule" in JS_TEXT, "解析函数缺失"
    assert "await parseAndFillRule();" in JS_TEXT, "运行首点未承接解析（两阶段第一步）"


def test_fe_rule_form_hidden_by_default():
    # 拟议规则表单默认**完全不展示**（收起摘要行也是认知负担）——NL 是唯一主入口；
    # 但必须保留极轻的「手动配置规则」兜底出口（网格作用域无法用 NL 表达）
    m = re.search(r'<div id="p6-rule-fields" style="display:none', JS_TEXT)
    assert m, "规则面板必须默认 display:none（不是收起摘要）"
    assert 'id="p6-rule-manual"' in JS_TEXT and "手动配置规则" in JS_TEXT, "缺专家兜底出口"
    assert 'manualBtn.addEventListener("click", revealRuleFields)' in JS_TEXT, "手动按钮未接展开"


def test_fe_run_parses_nl_before_form():
    # S2 两阶段：有 NL 输入且无草稿时，运行首点只做「解析+复述」——紧跟的返回把
    # 实际运行短路掉，绝不压进同一次点击；核对后的第二次点击才走草稿（st.nlDraft）
    # 跑真推演。否则又回到旧版「一次点击里解析完直接跑」，用户根本没机会看到系统复述。
    # 不变的契约是：路由判定必须先于规则校验，否则仅冲击输入会先被规则校验拦截。
    m = re.search(r"async function runSimulation\(\)(.*?)\n  \}", JS_TEXT, re.S)
    assert m, "未找到 runSimulation"
    body = m.group(1)
    assert "await parseAndFillRule();" in body, "运行首点未承接解析（两阶段第一步）"
    assert "!st.nlDraft" in body, "阶段一缺「无草稿才解析」判定"
    assert "return;" in body, "阶段一解析后必须短路返回、绝不顺手运行"
    assert "st.nlDraft" in body, "运行未读已核对的解析草稿"
    assert 'route === "stress"' in body, "runSimulation 缺仅冲击路由"
    assert body.index("st.nlDraft") < body.index("readForm()"), "路由必须先于 readForm"
    assert body.index("parseAndFillRule") < body.index("readForm()"), "阶段一解析必须先于 readForm"
    assert "还缺作用域" in JS_TEXT, "缺「缺作用域中止」文案"


def test_fe_reveal_wired_into_state_changes():
    # 展开/收起联动：回放、保存回填、P5 预填都要展开；重置收回
    for anchor in (
        "revealRuleFields(); // 回放",
        "revealRuleFields();",
        'ruleFields.style.display = "none"',
    ):
        assert anchor in JS_TEXT, f"缺联动：{anchor}"
    assert JS_TEXT.count("revealRuleFields()") >= 5, (
        "展开调用点不足（解析/回放/保存/预填/函数定义）"
    )


def test_fe_scope_fill_reuses_candidate_check():
    # 回填必须走现有候选校验（先切粒度 → fillScopeOptions → 值必须真在选项里），
    # 不悄悄换值。旧断言钉的是 `sel.value !== r.scope_value` 这一比对写法，
    # 但那写法在粒度尚未切换时会误判「不在候选中」：默认粒度是 zone，拿城市码 fs
    # 去比对 zone 候选必然落空，城市 / 行业作用域就永远回填不上（实测
    # 「佛山 LTV 85%」显示已填入佛山，跑出来的却是默认广州网格）。
    m = re.search(r"async function parseAndFillRule\(\)(.*?)\n  \}", JS_TEXT, re.S)
    assert m and "fillScopeOptions()" in m.group(1)
    # 粒度必须先切，再刷候选——顺序反了就回到上面那个 bug。
    body = m.group(1)
    assert 'el("p6-scope-type").value = r.scope_type' in body, "回填前未切换粒度"
    assert body.index('el("p6-scope-type").value = r.scope_type') < body.index(
        "fillScopeOptions()"
    ), "必须先切粒度再刷候选"
    # 候选校验本身：真的在选项里才回填
    assert "inOptions" in body and "sel.options" in body, "未做选项级候选校验"


def test_fe_shock_recognition_routes_and_gated():
    # 同句冲击识别（NL 完全替代情景输入）：识别到 shock_text 必须经 setScenarioDisplay
    # 展示（所见即系统理解），且
    # 1) 随 admin 闸门（st.sbAllowed）——非 admin 冲击静默忽略；
    # 2) 仅冲击无规则 → 返回 "stress" 路由组合压力分支，不展开规则确认面板；
    # 3) 解析路径绝不顺手触发运行（确认权在用户的红线不变）。
    m = re.search(r"async function parseAndFillRule\(\)(.*?)\n  \}", JS_TEXT, re.S)
    assert m, "未找到 parseAndFillRule"
    body = m.group(1)
    assert "r.shock_text" in body and "st.sbAllowed" in body, "冲击识别未随 admin 闸门"
    assert "setScenarioDisplay(" in body and '"stress"' in body, "缺仅冲击路由或统一展示写点"
    assert "runSimulation" not in body, "NL 解析路径内不得触发运行"


# ---------------- 7. NL 示例胶囊：每条示例必须可被解析器完整解析 ----------------


def test_nl_suggestion_chips_all_fully_parseable():
    # 胶囊示例的 data-value 与前端 NL_SUGGESTIONS 逐条对账，且每条跑一遍
    # handle_nl_parse 必须三要素齐（作用域/动作/上限）——示例本身说谎比没有示例更糟。
    m = re.search(r"const NL_SUGGESTIONS = \[(.*?)\];", JS_TEXT, re.S)
    assert m, "缺 NL_SUGGESTIONS"
    chips = re.findall(r'"([^"]+)"', m.group(1))
    assert len(chips) >= 3, "NL 示例胶囊至少 3 条（覆盖三档动作）"
    for text in chips:
        out = _parse(text)
        assert set(out["filled"]) == {"scope", "action", "ltv_cap"}, (
            f"胶囊示例解析不全：{text} → {out}"
        )
        assert out["notes"] == [], f"胶囊示例带歧义说明：{text}"


def test_nl_suggestions_include_mixed_rule_and_shock():
    # 示例里必须有「规则 + 冲击同句」的混合用法：用户最常问的正是「这条规则在某冲击下
    # 会怎样」。示例里没有它，用户只能自己猜两种条件能不能一起写。
    m = re.search(r"const NL_SUGGESTIONS = \[(.*?)\];", JS_TEXT, re.S)
    chips = re.findall(r'"([^"]+)"', m.group(1))
    mixed = []
    for text in chips:
        out = _parse(text)
        if out["shock_text"]:
            mixed.append(text)
            # 混合示例本身仍得是完整规则：三要素齐且无歧义
            assert set(out["filled"]) == {"scope", "action", "ltv_cap"}, f"混合示例规则不全：{text}"
    assert len(mixed) >= 2, f"混合示例不足 2 条（房价类 + 收入类），当前：{mixed}"
    assert any("房价" in t for t in mixed), "缺房价类混合示例"
    assert any("收入" in t for t in mixed), "缺收入类混合示例"


def test_nl_parse_always_reports_nl_mode():
    # 前端按 nl_mode 决定是否提示用户（注入被拦时必须明说），缺字段等于静默放行。
    for text in ("广州 LTV 超 70% 就拒贷", "全国房价下跌 20%", "把佛山那边的单子盯紧点"):
        out = _parse(text)
        assert out.get("nl_mode"), f"出参缺 nl_mode：{text}"
        # 默认未配置 LLM：只可能是纯正则或静默回退
        assert out["nl_mode"] in ("regex", "regex-no-key", "regex-fallback"), text


def test_nl_parse_never_calls_llm_without_key(monkeypatch):
    # 公益站不可用是常态（本仓库 .env 默认就没有 key）：不能因此卡住或改变解析结果
    calls = []
    monkeypatch.setattr(p6, "_nl_llm_call", lambda text, env: calls.append(text) or "{}")
    monkeypatch.setattr(p6, "_nl_env", lambda: {})
    for text in ("把佛山那边的单子盯紧点", "广州 LTV 超 70% 就拒贷", "全国房价下跌 20%"):
        _parse(text)
    assert calls == [], "未配置 LLM 时不得发起任何调用"


def test_nl_scenario_chips_are_pure_shock():
    # 情景胶囊（仅冲击 → 组合压力分支）逐条对账两条纪律：
    # 1) 必须能解析出冲击（shock_text 非空）——点开就跑不出压力的示例在说谎；
    # 2) 必须是**纯**冲击：filled 为空——带了城市/行业词会顺带回填出规则要素，
    #    把「仅冲击」的示例搅成规则运行。
    m = re.search(r"const NL_SCENARIO_SUGGESTIONS = \[(.*?)\];", JS_TEXT, re.S)
    assert m, "缺 NL_SCENARIO_SUGGESTIONS"
    chips = re.findall(r'"([^"]+)"', m.group(1))
    assert len(chips) >= 3, "情景胶囊至少 3 条（房价/收入类定量口径）"
    for text in chips:
        out = _parse(text)
        assert out["shock_text"], f"情景胶囊未解析出冲击：{text}"
        assert out["filled"] == [], f"情景胶囊带出了规则要素（含作用域词）：{text} → {out}"
        assert out["notes"] == [], f"情景胶囊带歧义说明：{text}"


def test_fe_nl_scenario_chips_bilingual():
    # 情景胶囊必须三语注册（en / zh-HK 字典各一条，连同 zh-CN 原文 ≥2 次）。
    # 分组小标题「只看市场冲击的影响（不改规则）：」已按设计反馈删除——示例不再按
    # 「规则 / 市场冲击」分两组，用户不必先判断自己要的是哪一类条件。
    assert "只看市场冲击的影响（不改规则）：" not in JS_TEXT, "示例分组标题回潜"
    for key in (
        "全国房价下跌 20%",
        "房价下跌 30%",
        "居民收入下降 15%",
    ):
        assert JS_TEXT.count(f'"{key}"') >= 2, f"情景胶囊文案未双语注册：{key}"


def test_fe_nl_chips_fill_input_without_running():
    # 胶囊点击只填入输入框（与情景芯片同款交互），不得自动解析/运行
    assert ".p6-nl-suggest" in JS_TEXT and "box.value = b.dataset.value" in JS_TEXT
    seg = JS_TEXT[JS_TEXT.index('p6-nl-suggest"') : JS_TEXT.index('p6-nl-suggest"') + 400]
    assert "runSimulation" not in seg and "parseAndFillRule" not in seg, "胶囊点击不得触发解析/运行"


def test_fe_nl_chips_data_value_is_translated():
    # 胶囊的 data-value 必须与按钮文案同为译文 t(s)：曾用中文原文写入 data-value，
    # 于是英文/繁中界面「显示英文、点进输入框变简体」。两处须同一表达式。
    for cls in ("p6-nl-suggest", "p6-nl-scenario-suggest"):
        seg = JS_TEXT[JS_TEXT.index(cls) : JS_TEXT.index(cls) + 300]
        assert 'data-value="${esc(t(s))}"' in seg, f"{cls} 的 data-value 未使用译文：{seg[:120]}"


def test_nl_shock_chips_translated_parseable():
    # 示例胶囊点击后填入的是**当前语言**的译文，故英文与繁中的情景原文也必须能被
    # 确定性冲击解析器吃下（en/zh-HK 用户的示例不能是「点了解析不出」的谎报）。
    # 只钉确定性口径（不依赖 LLM）：繁中靠 价|價 等繁简并列，英文靠英文词表。
    for text in (
        "全國房價下跌 20%",
        "房價下跌 30%",
        "National home prices fall 20%",
        "Home prices fall 30%",
        "Household income falls 15%",
    ):
        out = _parse(text)
        assert out["shock_text"], f"译文情景胶囊未解析出冲击：{text} → {out}"
        assert out["filled"] == [], f"译文情景胶囊带出了规则要素：{text} → {out}"


def test_fe_nl_chips_bilingual():
    for key in (
        "广州 LTV 超 70% 就拒贷",
        "制造业 加严审批 抵押率 65%",
        "佛山 LTV 85% 仅提示",
    ):
        assert JS_TEXT.count(f'"{key}"') >= 2, f"胶囊文案未双语注册：{key}"
