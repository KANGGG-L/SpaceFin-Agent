"""批次③-A · R2-7 前端静态不变量（pytest 触不到浏览器，只锁「代码必须长成这样」）。

运行时判据（刷新后历史仍在、点服务端条后 ① 区被回填）由页面实测另记（工单 §H-10）。

三条硬边界：
1. 历史卡分两段——本会话（≤5 次，内存）+ 服务端最近 10 次；卡片顶部那句
   不加定语的「仅当前会话保留，刷新后清空」必须改掉（服务端段刷新后仍在，留着就是假话）；
2. 服务端条目**只读回放**：填输入 + 明示需重新运行，**不得**伪装成可点的 restoreRun
   ——审计 detail 里没有前端 impact/narr 全量对象，硬当回放会把上一轮的结果留在结果区冒充本次；
3. 服务端段选中态用**新 class**（p10-srv-selected），不复用 .active；
   第一轮 runId 语义（st._runSeq / st._activeRunId / findIndex === -1 / note 单一来源）不得回退。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    pattern = "function " + re.escape(name) + r"\([^)]*\)[^{}]*\{.*?\n  \}"
    m = re.search(pattern, text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


def test_history_card_has_two_sections_with_honest_retention():
    text = _js_text()
    assert 'id="p10-history-session-cap"' in text, "缺本会话段小标题（两段式没有分段标识）"
    assert 'id="p10-server-history-list"' in text, "缺服务端历史列表容器"
    assert 'id="p10-history-server-cap"' in text, "缺服务端段小标题"
    cap_s = text[
        text.find('id="p10-history-session-cap"') : text.find('id="p10-history-session-cap"') + 260
    ]
    cap_v = text[
        text.find('id="p10-history-server-cap"') : text.find('id="p10-history-server-cap"') + 260
    ]
    assert "本会话" in cap_s and "刷新后清空" in cap_s, "本会话段必须自述「内存 + 刷新后清空」"
    assert "服务端" in cap_v and "10" in cap_v, "服务端段必须自述「最近 10 次」"
    # 卡片顶部那句不加定语的旧说明必须删除：它现在只对第一段成立。
    assert ">· 仅当前会话保留，刷新后清空<" not in text, (
        "卡片顶部仍写着「仅当前会话保留，刷新后清空」——服务端段刷新后仍在，这句已成为假话"
    )


def test_server_history_fetched_read_only_and_refreshed_in_the_right_places():
    text = _js_text()
    body = _function_body(text, "loadServerHistory")
    assert 'api("/api/policy/history")' in body, "服务端历史未走 GET /api/policy/history"
    assert "method" not in body, "历史端点只读，不得带 method（那是 POST）"
    assert "catch" in body, "拉取失败必须本地兜住——历史是附加信息，不能掀翻页面"
    assert re.search(r'registerPage\("policy",[\s\S]*?loadServerHistory\(\)', text), (
        "进入页面没有拉服务端历史（刷新后历史卡仍是空的）"
    )
    run = _function_body(text, "runSimulation")
    assert "loadServerHistory()" in run, "跑完一次推演后服务端段不刷新，两段会长期不一致"


def test_server_entry_replay_is_read_only_not_a_fake_restore():
    text = _js_text()
    fn = _function_body(text, "replayServerRun")
    assert "restoreRun" not in fn, "服务端条目复用了会话回放函数（它没有 impact/narr 全量对象）"
    assert "writeForm(" in fn, "只读回放必须回填 ① 区输入"
    assert "setScenarioDisplay(" in fn, "情景原文也要回填（NL 完全替代后走统一展示行）"
    assert "st._historyReadOnly = true" in fn, "回放有 impact 的快照必须置历史只读态"
    assert "st._historyReadOnly" in fn, "历史只读态须有清除/置位逻辑（运行新推演与改表单两处清除）"
    # 路线 B：有 impact 时直接回放快照（renderPreview(run.impact)），无 impact 走老只读路径
    # （renderPreview(null)）。两者都只渲染审计/快照对象，绝不拿旧会话结果冒充。
    assert "renderPreview(run.impact)" in fn, "新格式须回放整份 impact 快照"
    assert "renderPreview(null)" in fn, "老格式（无 impact）须降级为仅回填输入"
    assert "renderNarrativeBlock" not in fn, "不得为服务端条目编叙事"
    assert "st._activeRunId" not in fn, "服务端段不得接管会话回放的 runId 指针"
    assert "p10-server-history-item" in text, "服务端条目缺少可定位的 class"


def test_server_selected_state_uses_its_own_class_not_active():
    text = _js_text()
    assert "p10-srv-selected" in text, "服务端段选中态没有独立 class"
    css = re.search(r"<style>([\s\S]*?)</style>", text)
    assert css, "未找到本页 <style> 块"
    assert re.search(r"\.p10-srv-selected\s*\{", css.group(1)), (
        "p10-srv-selected 只在 JS 里挂名、样式里没有定义"
    )
    body = _function_body(text, "renderServerHistory")
    assert "p10-srv-selected" in body, "服务端条目渲染没有应用新选中态"
    assert '" active"' not in body and "active" not in body.replace("p10-srv-selected", ""), (
        "服务端条目复用了 .active（会与第一轮 runId 选中态混淆）"
    )


# ---------------- 护栏（改前改后都绿，不是修复证据） ----------------


def test_session_history_runid_semantics_still_intact():
    """护栏：只防「加服务端段时顺手把会话段的 runId 语义改坏」，不作 R2-7 证据。"""
    text = _js_text()
    assert re.search(r"const st = \{[^}]*_runSeq:[^}]*_activeRunId:", text)
    assert "_history.length > 5" in text, "本会话段必须仍然只留最近 5 次"
    assert "findIndex" in _function_body(text, "renderHistory")


# ---------------- T8（二期）：勾选对比 + 按条导出 ----------------


def test_t8_compare_checkbox_disabled_without_impact():
    """无 impact 的条目不可参与对比：复选框 disabled 并说明「仅可回填输入」。"""
    text = _js_text()
    assert re.search(
        r'<input type="checkbox" class="p10-cmp-ck" disabled title="\$\{t\("该留痕无完整快照，仅可回填输入"\)\}">',
        text,
    ), "无 impact 条目的复选框必须 disabled，且 title 说明该留痕仅可回填输入"
    # 会话段与服务端段两条列表都要渲染复选框
    for fn in ("renderHistory", "renderServerHistory"):
        assert "p10-cmp-ck" in _function_body(text, fn), f"{fn} 未渲染对比复选框"


def test_t8_compare_button_only_enabled_with_exactly_two():
    """「对比所选」在非 2 条选中时必须禁用；勾选数封顶 2 条。"""
    text = _js_text()
    bar = _function_body(text, "syncCmpBar")
    assert re.search(r"btn\.disabled = n !== 2", bar), "「对比所选」必须在恰好 2 条选中时才可用"
    assert re.search(r"while \(st\._cmpSel\.length > 2\)", _function_body(text, "toggleCmpSel")), (
        "勾选数必须封顶 2 条（第 3 条挤掉最早选中的）"
    )
    # 「对比所选」按钮必须在历史区标题行模板里
    assert 'id="p10-cmp-btn"' in text, "缺「对比所选」按钮"


def test_t8_build_summary_text_two_param_signature_single_template():
    """buildSummaryText 双参签名（+srcId），主按钮与历史导出传同一函数，防模板分叉。"""
    text = _js_text()
    assert re.search(r"function buildSummaryText\(p, narr, srcId\)", text), (
        "buildSummaryText 必须是 (p, narr, srcId) 签名——narr 走参数，防两份模板漂移"
    )
    body = _function_body(text, "buildSummaryText")
    assert "st._narrTeaser" not in text, (
        "st._narrTeaser 已随叙事浮层删除（导出叙事唯一来源是 st.lastHyp）"
    )
    assert 't("来源：")' in body and 't("推演历史快照")' in body, "缺「来源：推演历史快照」标识行"
    # 主按钮两个调用点：复制传 (st.preview, st.lastHyp)，下载传 (p, st.lastHyp)
    assert "buildSummaryText(st.preview, st.lastHyp)" in text, "复制按钮调用点未传当前叙事"
    assert "buildSummaryText(p, st.lastHyp)" in text, "下载按钮调用点未传当前叙事"
    # 历史按条导出调用点：传该条快照的 impact 与 narr
    assert re.search(r"buildSummaryText\(r\.impact, r\.narr \|\| null, srcId\)", text), (
        "历史按条导出必须传该条快照 (r.impact, r.narr || null, srcId)"
    )


def test_t8_history_export_on_item_click():
    """导出已从「导出结论」按钮剥离：点击历史组件（会话段与服务端段）即打开该次快照结论
    弹窗；弹窗在受影响明细上方展示该次规则（作用域/动作/LTV 上限），不再有「回填到表单」
    按钮；按条导出不受历史只读态拦截。"""
    text = _js_text()
    assert 'data-act="history-export"' not in text, "「导出结论」按钮应已从历史条目剥离"
    assert "推演快照" in text, "导出弹窗标题必须说明导的是快照结论"
    assert "点击导出该次推演的快照结论" in text, "历史组件点击提示（title）缺失"
    assert 'openHistoryExportDialog("session"' in text, "会话段条目点击未接导出弹窗"
    assert 'openHistoryExportDialog("server"' in text, "服务端段条目点击未接导出弹窗"
    assert 'data-act="p6-export-restore"' not in text, "「回填到表单」按钮应已删除"
    assert "回填到表单" not in text, "「回填到表单」文案应已移除"
    # 受影响明细上方展示该次规则：作用域 · 动作 · LTV 上限
    assert "actionText(r.rule.action)" in text, "导出弹窗未展示规则动作"
    assert 't(" · LTV 上限 ")' in text, "导出弹窗未展示 LTV 上限"
    export_body = _function_body(text, "exportHistorySnapshot")
    assert "st._historyReadOnly" not in export_body, "按条导出不得被历史只读态拦截"
    # 两个主按钮的只读禁用保持原样（不因 T8 被放松）
    assert re.search(r'const roAttr = st\._historyReadOnly \? " disabled" : "";', text), (
        "主按钮的只读禁用 roAttr 必须保持原样"
    )
