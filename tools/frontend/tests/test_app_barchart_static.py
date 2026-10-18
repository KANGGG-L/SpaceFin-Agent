"""app.js `barChart` 的 per-bar hover（<title>）能力 · 静态不变量。

barChart 是全站共用组件（P2 驾驶舱的「五级分类分布」「LTV 分布直方图」、
P6 的「五级分类分布」「跌幅敏感性阶梯」、P7 的「估值偏差分布」共 5 处调用），
为 P6 敏感性阶梯加 hover 时动过它。本文件锁住这次改动的边界：

1. hover 是**可选**能力：不传 opts.title 时柱体形状一个字符都不变，
   存量 5 处调用（均不传）不受影响；
2. 柱体三个属性（class / data-i / tabindex）必须留在共用那份属性串里——
   全站点击钻取依赖 `.bar-rect[data-i]`（app.js 的 bar-rect 委托 handler），
   任一个丢了钻取就静默失效；
3. hover 文本必须过 esc()：柱体 title 里会拼后端下发的情景名/格式化数字，
   不过 esc 就是一个 XSS 面。

渲染结果（属性逐字一致、不传时无 <title> 子元素）由浏览器实跑另记，
pytest 触达不到运行时，这里只锁代码形状。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "app.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _bar_chart_body(text: str) -> str:
    m = re.search(r"function barChart\(container, data, opts\) \{.*?\n\}", text, re.S)
    assert m, "未找到 function barChart"
    return m.group(0)


def test_bar_hover_is_opt_in_and_keeps_the_plain_rect_shape():
    body = _bar_chart_body(_js_text())
    # 不传时 _title 为 null → 走自闭合分支。写成「总是画 <title>」会给 5 处存量
    # 图表都塞一个空 title（hover 冒出空白气泡），这正是要避免的。
    m = re.search(r"const _title = (.*)", body)
    assert m, "barChart 缺 _title 定义（per-bar hover 能力不存在）"
    assert "opts.title" in m.group(1), "_title 必须来自 opts.title（可选能力）"
    assert re.search(r"!== \"function\"", m.group(1)) or "typeof opts.title" in m.group(1), (
        "_title 应兼容「回调」与「数据键」两种传法（与 sub 同套路）"
    )
    # 两条分支共用同一份属性串：各写一遍的话改一处漏一处，
    # 表现是「传了 title 的图能钻取、没传的不能」这类幽灵 bug。
    attrs = re.search(r"const rectAttrs = (`[^`]*`)", body)
    assert attrs, "柱体属性没有抽成共用变量"
    assert body.count("${rectAttrs}") == 2, (
        "title 分支与普通分支没有共用 rectAttrs（各拼一份，改动必漏一处）"
    )
    assert "</rect>" in body and "/>`" in body, (
        "柱体形状只在传了 title 时才带子元素：必须保留自闭合分支"
    )


def test_bar_rect_keeps_the_attributes_drilldown_depends_on():
    body = _bar_chart_body(_js_text())
    attrs = re.search(r"const rectAttrs = (`[^`]*`)", body).group(1)
    # 全站钻取：app.js 的委托 handler 用 e.target.closest(".bar-rect, .hbar-row")
    # 取 bar.dataset.i 决定下钻筛选，柱体还带 tabindex 供键盘 Enter 触发。
    for attr in ('class="bar-rect"', 'data-i="${i}"', 'tabindex="0"'):
        assert attr in attrs, f"柱体属性串缺 {attr}——点击/键盘钻取会静默失效"


def test_bar_hover_text_is_escaped():
    body = _bar_chart_body(_js_text())
    assert re.search(r"esc\(_title\(d\)\)", body), (
        "柱体 hover 文本没有过 esc()：title 里会拼后端下发的情景名等文本，"
        "不过 esc 就是一个 XSS 注入面"
    )
