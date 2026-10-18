"""批次②（R2-3 / R2-4 / R2-5）静态不变量。

pytest 触达不到浏览器运行时，本文件只锁「代码里必须长成这样」的那部分，
浏览器侧的字数 / 高度 / computed color 判据由页面实测另记（见工单 §H）。

R2-3 结果区三档信息架构：
1. renderPreview 默认串只出三张结论 KPI，其余数字与明细折进默认关的 <details>，
   且 <summary> 自带笔数与余额；
2. 折叠展开态存在 st（按 section key）并在重绘时回填，不得靠「重拉数据」保状态；
3. #p10-framework 的无条件自动展开必须删除，注释同步改写而非删掉，
   #p10-trace 单独一个默认关的折叠（不再埋在 1100px 长块里）。

R2-4 术语内联化：A/B 字母代号退出可见文案；LTV 定义与「存量」口径成为常驻文本节点
（不是 title）；动作说明三条在 p6_policy.py 单点重写，「准入门槛」类抽象词清除。

R2-5 模板 vs AI：死分支 langchain-llm 删除、AI 判据收成一个函数（全站仅一处比较
"langchain-agent"）；兜底模式给常驻整行说明，「模板生成」徽标不得再用全站最弱的 off。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"
PY = ROOT / "tools" / "frontend" / "pages" / "p6_policy.py"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _py_text() -> str:
    return PY.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{.*?\n  \}}", text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


_KPI_TARGETS = (
    ("out", re.compile(r"\blet out =|\bout \+=")),
    ("nums", re.compile(r"\blet nums =|\bnums \+=")),
)


def _kpi_assign_target(body: str, pos: int) -> str:
    """pos 处这段 kpi() 拼在哪个局部变量上。

    out = 直接进默认可见区；nums = 第二档字符串，整体交给 foldSection 才落到 DOM。
    只看「在第几个 foldSection( 之后」会误判——实现是先拼 nums 再折，故按最近的赋值目标算。
    """
    best = (None, -1)
    for name, pat in _KPI_TARGETS:
        last = None
        for m in pat.finditer(body[:pos]):
            last = m
        if last is not None and last.start() > best[1]:
            best = (name, last.start())
    assert best[0], "该 kpi() 既没挂在 out 也没挂在 nums 上，判据失效"
    return best[0]


# ---------------- R2-3 三档信息架构 ----------------


def test_preview_keeps_three_conclusion_kpis_and_folds_the_rest():
    text = _js_text()
    body = _function_body(text, "renderPreview")
    assert "foldSection(" in body, (
        "renderPreview 仍在一次运行后全量铺开：shock 对比 / 后三张 KPI / 五级图 / 明细表"
        "必须折进默认关的折叠（foldSection 外壳），默认可见只留三张结论 KPI + 结论行 + dup 预警"
    )
    for label in ("区域内存量", "命中规则", "其中新增受限"):
        pos = body.find(f'kpi("{label}"')
        assert pos >= 0, f"缺结论层 KPI「{label}」"
        assert _kpi_assign_target(body, pos) == "out", (
            f"结论层 KPI「{label}」被折进折叠里了（必须默认可见）"
        )
    for label in ("原已超红线", "命中余额占比", "低置信笔数"):
        pos = body.find(f'kpi("{label}"')
        assert pos >= 0, f"缺数字层 KPI「{label}」"
        assert _kpi_assign_target(body, pos) == "nums", (
            f"数字层 KPI「{label}」仍在默认可见区（应折进折叠，实测改前 6 卡全铺 900px 高）"
        )
    assert not re.search(r"\bout \+=\s*nums\b", body), (
        "nums 只能作为 foldSection 的第三个实参下发，不得再直接拼进可见区"
    )
    assert re.search(r"foldSection\([\s\S]{0,600}?\bnums\b", body), (
        "第二档数字没有交给 foldSection 折叠"
    )
    shell = _function_body(text, "foldSection")
    assert re.search(r'<details data-sec="\$\{key\}"', shell), (
        "折叠外壳必须带 section key（data-sec），重绘时才能按 key 回填展开态"
    )
    assert "st._openSections" in shell, "折叠外壳没从 st 读展开态"
    assert re.search(r"\$\{[^}]*open[^}]*\}", shell), (
        "open 必须是条件插值：<details> 默认关闭，只有 st 记着用户主动展开过才回填"
    )


def test_preview_fold_summary_carries_numbers():
    body = _function_body(_js_text(), "renderPreview")
    # 标题可以是「模板 + 模板」拼接，所以按 key 截到第三个实参起始处，不再假设单个字面量。
    numbers = re.search(r'foldSection\(\s*"numbers",([\s\S]{0,400}?)\n\s+nums\b', body)
    # 第三档内部含「覆盖存量」子区块（路线 B 新增），故第三实参是 evidence 变量而非字面量
    # `<div`；标题仍自带「笔数 / 余额」，截到 foldSection("rows", ...) 调用结束处即可。
    rows = re.search(r'foldSection\(\s*"rows",([\s\S]{0,400}?)\)\s*;', body)
    assert numbers, "第二档折叠（numbers）没有自带数字的标题"
    assert rows, "第三档折叠（rows）没有自带数字的标题"
    for m, key in ((numbers, "numbers"), (rows, "rows")):
        assert "笔" in m.group(1) and "${" in m.group(1), (
            f"{key} 折叠的 <summary> 只剩标题：必须自带命中笔数等关键量"
            "（如「影响明细与逐笔清单（168 笔 / 2.48 亿）」），收起态也要读得到数字"
        )
    assert "fmtMoney" in rows.group(1) and "笔 /" in rows.group(1), (
        "证据层标题必须同时带「笔数 / 余额」两个量，收起态才能判断这份明细值不值得点开"
    )
    # 第一轮 B-3 的圆盘口径句仍在，但归第二档（口径层），不得留在默认可见区。
    assert re.search(r"nums\s*=\s*caliber", body), "命中口径行没有作为第二档首行下发"
    assert not re.search(r"out \+= caliber", body), (
        "命中口径行仍直出默认可见区（结论层只留 3 卡 + 结论 + dup）"
    )


def test_details_open_state_is_restored_from_st_not_by_refetch():
    text = _js_text()
    assert re.search(r"const st = \{[^}]*_openSections", text), (
        "st 字面量缺 _openSections 初始化——#p6-preview 每次 innerHTML = out 会重置所有 "
        "<details>，用户主动展开的折叠必须在重绘时按 section key 回填"
    )
    body = _function_body(text, "renderPreview")
    assert "_openSections" in body or "foldSection(" in body, (
        "renderPreview 产出的折叠没有引用 st._openSections（展开态回填未接上）"
    )
    assert "ensureFullPreview" not in body, (
        "不得用「重拉数据」保展开态（第一轮 §F-3-② 已定：状态变化才重绘）"
    )
    assert re.search(r'addEventListener\(\s*"toggle"', text), (
        "缺 toggle 监听：用户展开/收起折叠这一动作没有任何地方写回 st，回填无从谈起"
    )


def test_framework_auto_expand_removed_and_trace_hidden_when_empty():
    text = _js_text()
    assert "默认折叠" in text, "不得删掉「默认折叠」这条注释语义（资质折叠与证据折叠仍是默认收起）"
    assert "就绪时自动展开" not in text, "注释仍在承诺「报告就绪时自动展开」，与实际行为不符"
    # 结构：叙事块已平铺（无 details 外壳、无 #p10-framework 中间层），#p10-trace
    # 落在 #p10-narr-box div 内；资质折叠 #p10-qual-box 内直接就是校准证据——
    # 三步法四卡与内层 #p10-calib 折叠都已移除（见 test_qual_box_evidence_always_shown）。
    narr = text.find('id="p10-narr-box"')
    assert narr > 0, "未找到 #p10-narr-box"
    assert '<details id="p10-narr-box"' not in text, "#p10-narr-box 应为平铺 div，不再是折叠外壳"
    assert '<details id="p10-framework"' not in text, (
        "「推演是怎么做的」中间层折叠残留（应并入资质折叠直陈）"
    )
    trace = text.find('id="p10-trace"')
    assert trace > narr, "#p10-trace 必须在叙事块内部"
    assert 'id="p10-trace-box"' not in text, "第二层折叠残留（已拍平为同层平铺）"
    qual = text.find('<details id="p10-qual-box"')
    assert qual > trace, "资质折叠必须摆在叙事块之后"
    status = text.find('id="p10-status"')
    assert qual < status, "校准证据必须直接落在资质折叠内部"
    assert '<details id="p10-calib"' not in text, "内层证据折叠已压平，不得回潜（多一次点击）"
    # 内容显隐的单一写点在 renderNarrTrace（沙盘拆分后叙事机制的退场写点）：
    # 无叙事 → #p10-narr-box 整块 display:none。
    body = _function_body(text, "renderNarrTrace")
    assert 'narrCard.style.display = "none"' in body, "renderNarrTrace 缺「无内容整块退场」分支"
    proc_body = _function_body(text, "renderProcChain")
    assert 'fold.style.display = "none"' in proc_body, "renderProcChain 缺「无传导链整块退场」分支"
    assert "无传导链可展示" not in text, "空态占位文案残留（无内容时整块隐藏，不再需要文案）"


# ---------------- R2-4 术语内联化 ----------------


def test_letter_codes_retired_from_visible_captions():
    text = _js_text()
    assert "A · 市场情景" not in text and "B · 拟议规则" not in text, (
        "① 区标题仍带字母代号（实测可见文本「A·市场情景」「B·拟议规则」），"
        "代号与页面别处无任何对照，属内部沟通口径漏到界面上"
    )
    # 「市场情景 / 拟议规则」已按设计反馈合并为一个模块「推演条件」：冲击与规则同属
    # 本次推演的前提，内部字段平铺，不再各立分区小标题。守护的是「合并后的抬头还在、
    # 旧的两个分区小标题没回潜」——注意这里判的是模板调用 ${t(...)}，注释里出现的
    # 旧词不算（本断言曾因注释含「市场情景」而意外放过模板改动）。
    assert "推演条件" in text, "合并后的模块抬头「推演条件」缺失"
    assert '${t("市场情景")}' not in text, "旧分区小标题「市场情景」回潜"
    assert '${t("拟议规则")}' not in text, "旧分区小标题「拟议规则」回潜"
    # NL 主入口形态：标题「自然语言输入（推荐）」单独成立，长句副标题按设计反馈
    # 删除（冗余说明）；「至少填其一」由交互保证（无描述时须有作用域才能运行）
    assert "点「运行推演」后系统按你的话填入规则" not in text, (
        "NL 副标题长句已按设计反馈删除，不得回潜"
    )
    assert not re.search(r"[AB] 路", text), "可见文案里仍残留「A 路 / B 路」内部代号"


def test_sandbox_and_rule_fields_never_fake_state():
    """两处「拿默认值/离线产物冒充本次结果」的误导不得回潮：

    1. 情景沙盘是推演的产出区：未运行任何推演时整卡隐藏（卡头「已校准」徽标讲的
       是离线上一次的报告，不该在用户什么都没做时出现）；
    2. 解析出纯冲击时不展开规则字段——摊开一整套默认网格 + 70% + 拒贷，
       就是在冒充「系统理解的条件」。
    """
    text = _js_text()
    assert 'sandboxZone.style.display = ""' not in text, "loadSandbox 不得无条件显示沙盘卡"
    assert "function syncSandboxZone()" in text, "沙盘显隐必须有唯一收口"
    assert text.count("syncSandboxZone()") >= 4, "收口应被 load/运行/回放/重置各调一次"
    body = _function_body(text, "parseAndFillRule")
    assert 'if (route === "stress") {' in body and "revealRuleFields();" in body
    # 纯冲击分支里不得出现 revealRuleFields（只在 else / 规则分支调用）
    stress_branch = body[body.index('if (route === "stress") {') : body.index("} else {")]
    assert "revealRuleFields" not in stress_branch, "纯冲击分支不得展开规则字段"


def test_qual_box_evidence_always_shown():
    """报告就绪即展示校准证据，不再要求先跑一次推演。

    原闸门（st._simShown）本意是防止「离线上一次产物」被读成当前结果，但离线属性
    已由折叠标题后缀、卡头徽标、横幅上的生成时间讲清；空折叠反而更像故障。
    三步法四卡同时移除：判读口径（要求 ≤ 0.05）由 renderStatus 随证据现算现示。
    """
    text = _js_text()
    assert "_simShown" not in text, "「跑过推演才展示证据」的闸门已移除，不得回潜"
    body = _function_body(text, "syncCalibEvidence")
    assert "data.available" in body, "判据必须落在报告是否就绪上"
    assert "st._simShown" not in body
    for k in ("① 造一批", "② 对照真实客户挑毛病", "③ 修正到位", "⛔ 没达标的结果不能用于决策"):
        assert k not in text, f"三步法说明卡已移除，不得回潜：{k}"
    # 判读口径仍在证据区（随差距数字现算现示），移除说明卡不会让人读不懂数字
    assert "要求 ≤ 0.05" in text, "达标口径必须随证据展示"


def test_ltv_cap_title_present():
    text = _js_text()
    # T4：输入框口径已改为百分比（填 70，不再填 0.7），title 释义随之改成百分比说法。
    # 守护的本体是「这条释义必须还在」，不是某一个字面数字——内联化不等于删掉。
    assert "填 70 表示拦截抵押率超过 70%" in text, (
        "LTV 上限输入框的 title 释义被误删（第一轮验收过的内容，内联化不等于删掉）"
    )


def test_action_descs_rewritten_in_backend_single_source():
    py = _py_text()
    m = re.search(r"ACTION_DESC = \{.*?\n\}", py, re.S)
    assert m, "未找到 ACTION_DESC"
    block = m.group(0)
    assert len(re.findall(r'"(reject|strict|notice)":', block)) == 3, "ACTION_DESC 三条应各改其一"
    assert "准入门槛" not in block, (
        "「加严审批」的说明仍用「准入门槛」这类抽象词，必须改成可核对的行为句"
    )
    assert "人工复核" in block and "可贷成数" in block, (
        "strict 说明未改写为可核对的行为句（例：不直接拒，转人工复核并下调可贷成数）"
    )
    assert "处置等级" in block, "reject / strict 仍须交代「已在预警内的贷款会怎样」，不得改丢语义"


def test_scope_def_and_action_hint_spans_removed():
    """设计反馈：删除作用域口径旁注 #p6-scope-def 与动作说明常驻行 #p6-action-hint。

    两串小字属 declutter，删除后不应再出现在骨架里；动作 desc 仍由后端 ACTION_DESC
    单点下发（只是不再在表单正下方常驻渲染）。
    """
    text = _js_text()
    assert "p6-scope-def" not in text, "#p6-scope-def 应已删除"
    assert "p6-action-hint" not in text, "#p6-action-hint 应已删除"
    assert "存量＝该作用域内抵押贷款笔数" not in text, "作用域口径旁注文案应随 span 一起移除"
    assert "updateActionHint" not in text, "删除 span 后 updateActionHint 成为死代码，应一并移除"


# ---------------- R2-5 模板 vs AI 叙事显著度 ----------------


def test_dead_langchain_llm_branch_removed_and_ai_judgment_single_source():
    text = _js_text()
    assert "langchain-llm" not in text, (
        "后端从不下发 langchain-llm（只有 langchain-agent / deterministic-fallback），死分支未删"
    )
    assert text.count('"langchain-agent"') == 1, (
        "AI 模式判据仍在三处各写一遍（renderNarrativeBlock / renderHypothesisResult / genDesc），"
        "必须收敛为一个函数或常量，全站只剩一处比较"
    )
    assert re.search(r"function isAiMode\(|const isAiMode = ", text), "缺统一的 isAiMode 判据"


def test_template_mode_resident_notice_and_warn_badge():
    """整句必须存在且兜底徽标不得用最弱的 off。

    「常驻可见」这件事不在本用例里判——见 test_template_notice_line_is_outside_the_narrative_fold。
    """
    text = _js_text()
    assert re.search(r'note\.textContent = t\("本段结论由内置规则模板生成', text), (
        "兜底模式只给了全站最弱的徽标，缺一行常驻整句说明"
        "「本段结论由内置规则模板生成，可复现，不代表大模型推理」"
    )
    # i18n 后徽标文案经 t() 渲染；同一 <span> 内须紧含 t("模板生成")（不允许跨标签串到别的徽标）。
    badges = re.findall(r'class="badge (\w+)"[^>]*>[^<]*t\("模板生成"\)', text)
    assert badges, "未找到「模板生成」徽标"
    assert set(badges) == {"warn"}, f"模板徽标仍是 {set(badges)}（off 是全站最弱视觉，须升为 warn）"
    assert "deterministic-fallback" in text, "兜底模式判据缺失（常驻说明没有触发条件）"


def test_template_notice_line_is_outside_the_narrative_block():
    """R5-a（批次② 验收返工项）：兜底整句必须在 #p10-narr-box **之外**。

    批次② 把整句渲染进了叙事块内部，而叙事块无叙事时会整块退场
    （renderNarrTrace 置 display:none），「可复现」「不代表大模型推理」
    两个关键限定将一字不可见（§F R2-5）。叙事块平铺后本约束仍然成立：
    整句的承载容器必须是 #p10-narr-box 之前的兄弟节点。
    """
    text = _js_text()
    narr = text.find('id="p10-narr-box"')
    assert narr > 0, "未找到 #p10-narr-box 叙事块"
    note = text.find("本段结论由内置规则模板生成")
    assert note > 0, "兜底整句说明不存在"
    # 字典条目（en / zh-HK）以中文为 key 是 i18n 框架约定，不计为「渲染来源」；
    # 只数经 t() 实际渲染的出处，必须唯一，否则展开叙事卡会同屏重复两句。
    assert text.count('t("本段结论由内置规则模板生成') == 1, (
        "整句有两处渲染来源，展开叙事卡会同屏重复出现两句"
    )
    # 整句由 JS 写入，源码位置必然在骨架之后，只能判「承载它的容器在块之外」：
    # 骨架里 #p10-narr-box 之前必须已有这个常驻兄弟节点。
    assert text.rfind('id="p10-narr-mode-note"', 0, narr) > 0, (
        "#p10-narr-mode-note 容器不在 #p10-narr-box 之前（叙事块退场时不可见）"
    )
    fn = _function_body(text, "syncNarrFoldLabel")
    assert "p10-narr-mode-note" in fn, (
        "折叠外那条常驻说明没有由 syncNarrFoldLabel 统一驱动显隐"
        "——它是唯一同时覆盖「运行 / 重置 / 历史回放」三个时机的函数"
    )
    assert "本段结论由内置规则模板生成" in fn, (
        "整句唯一来源必须在 syncNarrFoldLabel 内（骨架 HTML 里没有第二份）"
    )
    assert re.search(r"note\.style\.display = isTemplateMode", fn), (
        "整句显隐没有随兜底模式判据走，AI 模式下会常驻一句与事实相反的说明"
    )


def test_method_area_split_by_refresh_cadence():
    """推演依据区按更新时机分成两块。

    用户实测反馈：单入口「这是怎么算的（管理员）」把随运行重画的内容（轨迹 / 叙事）
    与离线固定的校准证据混装在一起，后者被误读成「本次自然语言推演的产物」——
    换情景假设重跑后证据数字不变，被怀疑没真正执行。拆分后：
    叙事块 = 本次推演的产物（随每次运行更新、平铺直陈）；资质折叠 = 沙盘资质（离线报告）。
    """
    text = _js_text()
    narr = text.find('<div id="p10-narr-box" class="card"')
    qual = text.find('<details id="p10-qual-box" class="card"')
    assert narr > 0 and qual > narr, (
        "缺两个区块或顺序不对：先本次运行产物（#p10-narr-box），后离线资质（#p10-qual-box）"
    )
    # 标题自带更新时机标注，收起态即可分辨两类内容，不再依赖展开后读小字。
    assert "本次推演的依据（随每次运行更新）" in text, "叙事折叠标题未标注「随每次运行更新」"
    assert "沙盘方法与校准资质（离线报告）" in text, "资质折叠标题未标注「离线报告」"
    # 旧单入口的标题与死词条必须清干净（en / zh-HK 字典同文件，一并覆盖）。
    assert "这是怎么算的（管理员）" not in text, "旧单入口标题残留（骨架或字典）"
    assert "推演方法与叙事依据" not in text, "旧叙事折叠标题残留（骨架或字典）"
    # 资质折叠的生成时间后缀：loadSandbox 拿到 generated_at 后写入标题，收起态可见。
    assert 'el("p10-qual-summary")' in text, "资质折叠缺时间戳后缀写点（#p10-qual-summary）"
    # 拆分后旧单入口容器 id 不得残留。
    assert "p10-method-all" not in text, "旧单入口容器 id 残留"


def test_sandbox_zone_split_from_result_card():
    """沙盘产出侧独立分区（用户痛点：沙盘混杂在规则推演当中）。

    输入侧统一入口不动（NL 解析路由照旧）；产出侧全拆——沙盘的一切产出
    （校准横幅、叙事卡与生成机制、压力结果、方法与校准资质）归 #p6-sandbox-zone，
    规则结果卡只留定量 KPI 与定量传导链。
    """
    text = _js_text()
    i_input = text.find('id="p6-rule-input-card"')
    i_zone = text.find('id="p6-sandbox-zone"')
    i_result = text.find('id="p6-rule-result-card"')
    assert 0 < i_input < i_zone < i_result, "沙盘分区必须位于 ① 推演输入卡之后、② 规则结果卡之前"
    # 结果卡纯净：从结果卡起到「已配置规则」卡之间不得残留任何沙盘节点。
    result_slice = text[i_result : text.find('id="p6-rule-policies-card"')]
    assert 'id="p10-' not in result_slice, "规则结果卡内仍残留沙盘依据区节点"
    assert 'id="p6-stress-card"' not in result_slice, "压力结果仍挂在规则结果卡内"
    # 压力卡归属：必须落在沙盘分区切片内。
    zone_slice = text[i_zone:i_result]
    assert 'id="p6-stress-card"' in zone_slice, "压力结果未迁入沙盘分区"
    assert 'id="sb-admin-wrap"' in zone_slice, "校准横幅区未迁入沙盘分区"
    assert 'id="p10-method-wrap"' in zone_slice, "推演依据区未迁入沙盘分区"
    # 定量传导链新折叠：留在规则结果卡内。
    assert 'id="p6-proc-box"' in result_slice, "定量传导链折叠不在规则结果卡内"
    # teaser 全链退场（浮层、函数、跳转动作、st 缓存、互斥切换函数）。
    for token in (
        "p6-narr-teaser",
        "renderNarrTeaser",
        "narr-jump",
        "_narrTeaser",
        "_narrProfile",
        "function showResultCard",
        'id="p6-result-mode"',
        'id="p6-result-note"',
    ):
        assert token not in text, f"teaser 全链残留：{token}"
    # renderTrace 聚合入口与两个分渲染函数并存，聚合体内依次调用。
    for fn in (
        "function renderProcChain()",
        "function renderNarrTrace()",
        "function renderTrace()",
    ):
        assert fn in text, f"缺 {fn}"
    agg = _function_body(text, "renderTrace")
    assert "renderProcChain();" in agg and "renderNarrTrace();" in agg, (
        "renderTrace 聚合入口未调用两个分渲染（调用点零改动的契约）"
    )
    # 导出叙事唯一来源是 st.lastHyp。
    assert "buildSummaryText(st.preview, st.lastHyp)" in text
    assert "buildSummaryText(p, st.lastHyp)" in text
    # runStress 成功路径滚到沙盘分区。
    stress_body = _function_body(text, "runStress")
    assert 'el("p6-sandbox-zone")' in stress_body, "runStress 成功后未滚动到沙盘分区"
    # 搬家只能有一份：hyp-admin-badge 已自 ① 输入卡卡头迁入沙盘分区卡头，
    # 搬迁时若留副本会造成 id 重复 + el() 命中错误节点（沙盘徽标永不显示）。
    assert text.count('id="hyp-admin-badge"') == 1, "hyp-admin-badge 重复（搬入新卡头时未删原件）"
    # 分区闸门与校准徽标：loadSandbox 放行分区并单点写徽标。
    load_body = _function_body(text, "loadSandbox")
    assert 'el("p6-sandbox-zone")' in load_body, "loadSandbox 未放行沙盘分区"
    assert "syncCalibBadge(" in text and "function syncCalibBadge(" in text, (
        "缺校准状态徽标写点（#p6-sb-calib-badge 四态）"
    )
