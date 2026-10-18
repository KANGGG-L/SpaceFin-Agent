"""P6 · S2 确认环节（解析 → 复述 → 核对 → 运行）的守门测试。

改造前「运行推演」把 NL 解析内联在同一次点击里：点一下就解析完并直接跑完，
而状态行却写着「请核对后点「运行推演」——不会自动开始」，是一句假话——用户从来
没有核对的机会。S2 把确认环节做成两阶段：有 NL 输入时首点「运行推演」只做
解析+复述（returns 短路、绝不运行），核对无误后再点一次才真正运行。本文件
钉死「用户必须先看过复述才会运行」这个分离不再回潮。

守护五条：
1. 解析函数只解析、不运行；
2. 运行首点承接解析+复述但短路返回，二次点击走草稿真运行；
3. 闸门由 syncRunGate 统一裁决，改动后必须回到未核对状态；
4. 非 NL 路径（回放 / P5 预填 / 载入规则）直接可运行，不被确认环节误伤；
5. 复述职责由「推断说明卡」（decomposeInferenceCard）承担：系统理解到的东西摆全
   （市场冲击/粒度/作用域/惩罚动作/LTV 上限），没读出来的要显性降级
   （冲击 →「不可算」，其余 → missing 标黄），不许拿默认值冒充解析结果。
   「可能影响」已按用户口径移除（传导链与配置理由高度重叠），不得回潮。
"""

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "..", "static", "pages", "p6_policy.js"), encoding="utf-8") as _f:
    JS = _f.read()


def _body(sig):
    """按 `  }`（两空格缩进的收尾大括号）切出函数体，函数内部块是四空格，不会误截。"""
    m = re.search(sig + r"(.*?)\n  \}", JS, re.S)
    assert m, f"未找到 {sig}"
    return m.group(1)


# ---------------- 1. 解析只解析，绝不运行 ----------------


def test_parse_never_runs():
    # 本次改造的核心红线：解析函数体内不得出现任何运行入口。
    body = _body(r"async function parseAndFillRule\(\)")
    for token in ("runSimulation", "runStress"):
        assert token not in body, f"解析函数内出现运行入口：{token}（解析与运行必须分离）"


def test_parse_produces_draft_and_renders_card():
    body = _body(r"async function parseAndFillRule\(\)")
    assert "st.nlDraft = {" in body, "解析未固化草稿（运行就没有合法输入源）"
    assert "renderConfirmCard(st.nlDraft)" in body, "解析未渲染复述卡"
    assert 'st.nlStage = "parsed"' in body, "解析未翻转闸门"
    assert "syncRunGate()" in body, "解析后未重裁闸门"


# ---------------- 2. 运行=两阶段：首点解析+复述短路，二次点走草稿真运行 ----------------


def test_run_is_two_stage_parse_first():
    # 两阶段契约：有 NL 输入且无草稿 → 首点「解析+复述」后必短路返回、绝不运行；
    # 核对后的第二次点击才走草稿（st.nlDraft）跑真推演。解析与运行纵在同一个按钮里，
    # 也必然跨两次点击、绝不压进同一次（否则用户又没机会看到复述）。
    body = _body(r"async function runSimulation\(\)")
    assert "await parseAndFillRule();" in body, "首点未承接解析（两阶段第一步）"
    assert "!st.nlDraft" in body, "阶段一缺「无草稿」判定"
    assert "return;" in body, "阶段一解析后必须短路返回、绝不顺手运行"
    assert "st.nlDraft" in body, "运行未读已核对的草稿"
    # 路由判定仍必须先于规则校验——否则仅冲击输入会先被规则校验拦下。
    assert 'route === "stress"' in body, "runSimulation 缺仅冲击路由"
    assert body.index("st.nlDraft") < body.index("readForm()"), "路由必须先于 readForm"


# ---------------- 3. 闸门：首点=复述、二次点=运行，改动后回未核对 ----------------


def test_gate_locks_run_button():
    body = _body(r"function syncRunGate\(\)")
    assert "btn.disabled = !ready" in body, "闸门未接管 sim-run 的可用态"
    # 两阶段契约裁决式：draftOK（已复述，再点=真运行）/ stage1（写了话未解析，再点=复述）/
    # hasScope（手动路径，单点即运行）任一成立即可点；唯一禁的是既没写话也没选作用域。
    assert "const ready = draftOK || stage1 || hasScope" in body, "闸门裁决式变了"
    assert 'draftOK = !!st.nlDraft && st.nlStage !== "empty"' in body, "缺已复述态判定"
    assert "stage1 = !st.nlDraft && !!nlText" in body, "缺阶段一（未解析）判定"


def test_unparsed_nl_becomes_stage1():
    # 写了话但还没解析：不再锁死（旧版会把用户刚写的那句静默忽略），而是放行为阶段一——
    # 首点做「解析+复述」，按钮 tooltip 说明这一趟不会运行，核对后再点一次才运行。
    body = _body(r"function syncRunGate\(\)")
    assert "nlText" in body, "闸门未考虑「写了话但没解析」这一态"
    assert "先复述系统理解到的内容（这趟不会运行），核对后再次点击即运行" in JS, (
        "缺阶段一 tooltip 说明"
    )


def test_edits_invalidate_draft():
    # 改了输入或表单，上一份复述立刻作废——否则等于用旧理解跑新参数。
    body = _body(r"function invalidateDraft\(\)")
    assert "st.nlDraft = null" in body and 'st.nlStage = "empty"' in body
    assert "renderConfirmCard(null)" in body, "草稿作废后复述卡必须收起"
    for anchor in (
        'nlInput.addEventListener("input", invalidateDraft)',
        "invalidateDraft(); // S2：改了表单，上一份复述即作废，闸门重新裁决",
        "invalidateDraft(); // S2：手改作用域即作废上一份复述，并按新作用域重裁闸门",
    ):
        assert anchor in JS, f"改动入口未接作废：{anchor}"


# ---------------- 4. 非 NL 路径不被确认环节误伤 ----------------


def test_replay_and_prefill_skip_confirmation():
    # 回放与 P5 预填填的都是已经确认过的值，不该再要一次确认——
    # 否则用户对着回放出来的结果没法重跑。
    for anchor in (
        "revealRuleFields(); // 回放",
        "revealRuleFields(); // P5 预填了作用域 → 展开确认面板",
    ):
        i = JS.index(anchor)
        assert "invalidateDraft()" in JS[i : i + 400], f"该路径未清草稿：{anchor}"


def test_reset_clears_draft():
    # 重置回到纯输入态：草稿、复述卡、闸门一并复位。
    # （窗口 4000：reset 分支叠加了历史只读锁退出等新增步骤，3600 会截断 syncRunGate。）
    i = JS.index('el("p6-reset").addEventListener("click"')
    seg = JS[i : i + 4000]
    assert "st.nlDraft = null" in seg and "renderConfirmCard(null)" in seg, "重置未清草稿"
    assert "syncRunGate();" in seg, "重置未重裁闸门"
    # 输入框是最主要的输入源，必须一起清：留着话却没有对应复述 → 运行按钮一直锁着，
    # 用户点了重置反而更跑不动，与「重置 = 回到干净状态」的直觉相悖。
    assert 'nlBox.value = ""' in seg, "重置未清自然语言输入框"


# ---------------- 5. 复述卡（推断说明卡）的组成 ----------------


def test_confirm_card_shows_essential_fields():
    # S2 复述卡已被「选中推演条件的推断说明卡」彻底取代（renderConfirmCard 只剩
    # 清空/收起容器的兼容桩，见其头部注释：用户口径——用基于假设/建议规则/
    # 参数说明的完整推断取代那张短复述卡）。复述职责的载体是
    # decomposeInferenceCard，要素清单钉在这里防漂移。
    body = _body(r"function decomposeInferenceCard\(opt\)")
    for label in ("系统理解为", "市场冲击", "粒度", "作用域", "惩罚动作", "LTV 上限"):
        assert f't("{label}")' in body, f"推断说明卡缺字段：{label}"
    # 「可能影响」不得回潮：传导链与「配置理由」高度重叠（后者 = 传导链 + 建议句），
    # 纯规则建议的「未提及冲击」披露由配置理由行同句承担，双行是冗余。
    assert 't("可能影响")' not in body, "「可能影响」行已按用户口径移除，不得回潮"
    # 没读出来的东西必须看得见：冲击缺失 → 「不可算」，其余缺失走 conf 的
    # missing 参数显性标黄——不许静默吞掉或拿默认值冒充解析结果。
    assert "不可算" in body, "推断说明卡缺「不可算」降级态"
    assert "missing" in body, "推断说明卡缺 missing 显性标记（缺失项必须看得见）"
    # 旧复述卡不得回潮：renderConfirmCard 只许清空容器，不许再渲染复述内容
    # （否则系统理解又两处铺开，与推断说明卡分叉）。
    stub = _body(r"function renderConfirmCard\(_draft\)")
    for label in ("市场冲击", "作用域", "惩罚动作"):
        assert f't("{label}")' not in stub, (
            "renderConfirmCard 又渲染了旧复述卡（职责已移交推断说明卡）"
        )


def test_inferred_suggestion_why_rendered_as_pill():
    # 「推断建议：」前缀的 why 须剥前缀、以胶囊内联在配置值后方（区分正文）；
    # 前缀表必须覆盖三语变体，否则对应语言用户会看到裸前缀（模型按请求语言回前缀）。
    body = _body(r"function decomposeInferenceCard\(opt\)")
    for pre in ("推断建议：", "推斷建議：", "Inferred suggestion: "):
        assert f'"{pre}"' in body, f"前缀表缺语言变体：{pre}"
    assert "whyParts" in body and "parts.sug" in body, "建议型 why 须走胶囊分支"
    assert "border-radius:999px" in body, "建议型解释必须是胶囊形态"
    assert "parts.plain" in body, "直判型 why 仍走缩进原句，不得被胶囊分支吞掉"


# ---------------- 6. 复述卡不得与真实表单值分叉 ----------------


def test_confirm_card_reports_actual_form_values():
    # 复述卡必须说「表单里现在是什么」，而不是「后端解析出了什么」。
    # 回填失败时两者会分叉：卡上写「城市 · 佛山」、实际跑默认广州网格，
    # 等于系统当着用户的面说谎——比不复述更糟。
    # 因此回填失败（scopeFailed）时两个都不能显示：既不是后端的解析值（那是谎报），
    # 也不是表单里的默认网格（那是拿默认值冒充解析结果，会让人以为「广州」被
    # 解析成了某个网格）。统一记为「未提取到」并标黄，作用域进 missing、运行闸门
    # 上锁——既然没定就不让用户跑，好过告诉他「会跑这个网格」。
    body = _body(r"async function parseAndFillRule\(\)")
    assert 'const sVal = el("p6-scope-value").value' in body, "复述卡未取表单现值"
    assert 'applied.indexOf("scope") >= 0' in body, "未按「是否真写进表单」判定回填成败"
    assert "scopeFailed" in body, "回填失败分支缺失（会拿默认网格冒充解析结果）"
    assert "scope_value: scopeFailed ? null : sVal || null" in body, (
        "回填失败时不得把默认网格写进草稿"
    )
    # 一个都没真正写进表单时，不许宣称「已按描述填入」。
    assert "if (parts.length) {" in body, "未回填成功也会宣称「已填入」"


# ---------------- 7. 非管理员的纯冲击不再答非所问 ----------------不再答非所问 ----------------


def test_non_admin_pure_shock_gets_permission_hint():
    # 非 admin 输入纯冲击走不到组合压力分支（admin 闸门），落到规则分支会报
    # 「还缺作用域」——与用户写的「房价下跌 20%」文不对题。
    assert "情景推演需要管理员权限，当前账号只能做规则试算" in JS, "缺非 admin 权限提示"
    body = _body(r"async function runSimulation\(\)")
    assert "draft.noPerm" in body, "运行中止提示未区分「缺作用域」与「缺权限」"
