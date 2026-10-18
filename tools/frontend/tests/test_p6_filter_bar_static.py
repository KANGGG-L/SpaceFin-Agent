"""P6 拟议规则栏（filter-bar）与作用域组合框的静态不变量。

浏览器侧几何与交互判据需实测，pytest 触达不到运行时，故用文本级断言锁死五件事：
1. 本页 filter-bar 顶部对齐（全局 .filter-bar 仍是 flex-end，不得被顺手改掉——
   那条 ``test_global_filter_bar_still_bottom_aligned`` 是**护栏**，改前改后都绿，不是修复证据）；
2. 「惩罚动作 / LTV 上限」的说明行不在包裹控件的 <label> 内——在 label 内就等于
   把控件热区从 select 的 33px 撑到 72px，点说明行也会开下拉；
3. 作用域列表框的 click 必须先判「值真的变了」才 commitScope，且**基线只能取 mousedown
   时记下的值**（R2-11 ②：拿 st.scopeValue 当基线会因 change 先于 click 而恒等、面板永远点不关）；
4. 弹出面板有 max-height（否则长列表溢出视口），且 setScopeOpen 的
   preventScroll 焦点策略不被回退（第一轮为防页面跳动加的）；
5. live region 语义落在收起态真读得到的 #p6-scope-note 上（role="status"），
   不是藏在 display:none 面板里的 #p6-scope-count（R2-11 ①）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
STATIC = ROOT / "tools" / "frontend" / "static"
CSS = STATIC / "style.css"
JS = STATIC / "pages" / "p6_policy.js"


def _css_text() -> str:
    return CSS.read_text(encoding="utf-8")


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{.*?\n  \}}", text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


def _label_block(text: str, caption: str) -> str:
    """取「<label>caption …</label>」整块（本页 label 不嵌套，非贪婪即闭合）。"""
    m = re.search(rf"<label[^>]*>\s*{caption}(?:.|\n)*?</label>", text)
    assert m, f"未找到 <label>{caption} 块"
    return m.group(0)


def _attr_snippet(text: str, anchor: str, span: int = 900) -> str:
    i = text.find(anchor)
    assert i >= 0, f"未找到锚点 {anchor}"
    return text[i : i + span]


def test_page_policy_filter_bar_aligns_top():
    assert re.search(
        r"#page-policy\s+\.filter-bar\s*\{[^}]*align-items:\s*flex-start", _css_text()
    ), (
        "缺 `#page-policy .filter-bar { align-items: flex-start }`：底部对齐下带说明行的"
        "控件（label 高 72）与不带的（53）下拉框落在不同 y，看着一行、点不到同一行"
    )


def test_global_filter_bar_still_bottom_aligned():
    """护栏（改前改后都绿，**不是修复证据**）：只防「顺手把全站 .filter-bar 改成 flex-start」。

    本用例在 R2-1 动手之前就是通过——批次①验收方 §F R2-1 ⑧ 已点名它「唯一改前即绿」，
    因此它不得被用作任何修复的判据；真正的判据是 test_page_policy_filter_bar_aligns_top。
    函数名保留是因为工单 §F 按名引用它。
    """
    m = re.search(r"(?<!#page-policy )\.filter-bar\s*\{[^}]*align-items:\s*flex-end", _css_text())
    assert m, "全局 .filter-bar 的 align-items:flex-end 被改动——驾驶舱 / 预警 / 报送共用它"


def test_action_hint_removed():
    """设计反馈：删除动作说明常驻行 #p6-action-hint（declutter）。

    原 test_action_hint_outside_wrapping_label 校验「说明行在 label 外」已无意义——
    说明行本身已删。这里改为回归守卫：该 span 不应再出现。
    """
    assert "p6-action-hint" not in _js_text(), "#p6-action-hint 应已删除"


def test_scope_listbox_click_gated_on_value_change():
    """判据（R2-11 ② 收紧）：基线**只能**是 mousedown 记下的 scopeValueAtPointer。

    曾经写成「mousedown 基线 或 st.scopeValue」的或分支——而修复方自己踩过的那次车
    正是用 st.scopeValue 做基线：真鼠标点选时 change 先于 click 派发，那边已把
    st.scopeValue 同步成新值，再拿它当基线比较恒等，面板永远点不关。
    所以这里额外禁止守卫里出现 `!== st.scopeValue`，让该用例对那种写法必红。
    """
    text = _js_text()
    assert not re.search(r'addEventListener\(\s*"click"\s*,\s*commitScope\s*\)', text), (
        "#p6-scope-value 仍绑定裸 click → commitScope（拖滚动条 / 点空白 / 点已选行都会收起面板）"
    )
    handler = _attr_snippet(text, 'el("p6-scope-value").addEventListener("click"', 400)
    assert re.search(r"!==\s*scopeValueAtPointer", handler), (
        "列表框 click 收起的基线必须是 mousedown 时记下的值（只有点出新值才 commit）"
    )
    assert not re.search(r"!==\s*st\.scopeValue", handler), (
        "守卫拿 st.scopeValue 当基线：change 早于 click 已同步新值，比较恒等、面板再也点不关"
    )
    mousedown = _attr_snippet(text, 'el("p6-scope-value").addEventListener("mousedown"', 200)
    assert 'el("p6-scope-value").addEventListener("mousedown"' in text, (
        "缺 mousedown 基线：只比 st.scopeValue 会恒等，面板永远点不关"
    )
    assert "scopeValueAtPointer" in mousedown, "mousedown 监听没有记录基线值，click 守卫无从比较"


def test_scope_panel_max_height_and_focus_strategy_kept():
    panel = _attr_snippet(_js_text(), 'id="p6-scope-panel"', 700).split(">")[0]
    assert "max-height" in panel, "#p6-scope-panel 无 max-height：长列表底边溢出视口"
    body = _function_body(_js_text(), "setScopeOpen")
    assert "scrollIntoView" in body, "展开面板后需 scrollIntoView 修正视口位置"
    assert "preventScroll: true" in body, (
        "setScopeOpen 的 focus({preventScroll:true}) 被回退——上一轮为防展开瞬间页面跳动而加"
    )


def test_scope_refresh_announced_outside_panel():
    text = _js_text()
    panel_open = text.find('id="p6-scope-panel"')
    sel_open = text.find('id="p6-scope-value"', panel_open)
    assert panel_open >= 0 and sel_open > panel_open, "未找到作用域面板 / 列表框锚点"
    assert "p6-scope-note" not in text[panel_open:sel_open], (
        "常驻行被放进面板内：面板 display:none 时仍读不到"
    )
    sel_close = text.find("</select>", sel_open)
    note_i = text.find('id="p6-scope-note"')
    assert note_i > sel_close, (
        "#p6-scope-field 下缺面板外的常驻行：粒度切换会悄悄换掉作用域值，"
        "而唯一输出 #p6-scope-count 在 display:none 的面板里，收起态读不到"
    )
    assert text[sel_close:note_i].count("</div>") >= 1, "常驻行未落在面板闭合之后"


def test_live_region_is_the_resident_note_not_the_hidden_count():
    """R2-11 ①：live 语义归 #p6-scope-note（面板外、收起态可读），不是面板里的计数。"""
    text = _js_text()
    note = _attr_snippet(text, 'id="p6-scope-note"', 300).split(">")[0]
    assert 'role="status"' in note, (
        '#p6-scope-note 没标 role="status"——它才是承载「粒度已切为…作用域已刷新…」的那一行，'
        "实测收起态 offsetParent !== null"
    )
    count = _attr_snippet(text, 'id="p6-scope-count"', 300).split(">")[0]
    assert "aria-live" not in count, (
        "#p6-scope-count 上的 aria-live 实测根本不生效：它在 display:none 的 #p6-scope-panel 内，"
        "收起态 offsetParent === null、getBoundingClientRect() = 0×0，"
        "「共 501 条 → 共 21 条」的变化辅助技术收不到；留着只会误导后人"
    )
