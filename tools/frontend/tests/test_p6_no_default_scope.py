"""P6 · 首载/重置「不复选默认网格」守门测试。

用户反馈：没做任何输入就点「推演」，②卡却直接渲染出「广州 · 价格网格 · 拒贷 · LTV70」
的规则试算结果，还提示会覆盖已有规则 #28——这是系统拿表单里自动预选的默认网格去冒充
用户意图。本次改动：首载与重置后作用域留空，空作用域时运行按钮禁用并给引导文案，
只有用户真正选定作用域或解析回填后才能出结果。

守护四条：
1. fillScopeOptions 支持跳过「空作用域兜底选中首项」；
2. 首载 reload 必须传该跳过开关（否则首次进入又自动预选）；
3. 重置后作用域留空，不再强行选回第一项；
4. 非首载的兜底自动选中仍保留（切换粒度/清空搜索等合法场景不受影响）。
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "..", "static", "pages", "p6_policy.js"), encoding="utf-8") as _f:
    JS = _f.read()


def test_fill_scope_options_has_skip_flag():
    # 兜底选中首项必须受跳过开关控制，否则首载仍会拿默认网格冒充用户意图。
    assert "function fillScopeOptions(skipEmptyAutoPick)" in JS, "fillScopeOptions 缺跳过参数"
    assert "!skipEmptyAutoPick && sel.selectedIndex === -1" in JS, (
        "空作用域兜底选中首项未受跳过开关控制"
    )


def test_initial_load_passes_skip_flag():
    # 首载不许自动预选：reload 里对作用域填充必须传 true。
    assert JS.count("fillScopeOptions(true)") >= 2, "首载 / 重置未传跳过开关"


def test_reset_leaves_scope_empty():
    # 重置回到干净起点：作用域置空，且不再把列表第一项写回表单。
    assert "st.scopeValue = null;" in JS, "重置未清空作用域"
    assert "scopeSel.options[0].value" not in JS, "重置仍强行选回第一个作用域项"


def test_non_initial_autopick_preserved():
    # 非首载的兜底选中仍保留（切换粒度 / 清空搜索后的合法回退），反对一刀切删掉。
    assert "sel.selectedIndex = 0;" in JS, "兜底选中首项逻辑被整体移除"
