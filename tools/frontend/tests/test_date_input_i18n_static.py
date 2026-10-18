"""静态回归：所有 type="date" 输入控件必须符合 i18n。

设计反馈背景：date input 自身需带本地化的 accessible name 与 tooltip，否则切到
zh-HK/zh-CN 时仍显示西式日期格式且无本地化可访问名（设计反馈判定「没符合i18n」）。
- lang 由 i18n.js 的 applyI18nText 在每次语言切换时按当前语言注入到 date 类输入；
- data-i18n-aria / data-i18n-title 让输入的可访问名与悬浮提示跟随应用语言。

本测试扫描全部源码里的 <input type="date"> 标签，断言每个都带 data-i18n-aria，
防止新增 date 输入时漏掉 i18n 归属。
"""

import os
import re

_STATIC = os.path.join(os.path.dirname(__file__), "..", "static")

_INPUT_DATE_RE = re.compile(r"<input\b[^>]*\btype=\"date\"[^>]*>", re.DOTALL)


def _source_files():
    files = []
    for root, _dirs, names in os.walk(_STATIC):
        if "vendor" in root:
            continue
        for n in names:
            # i18n.js 里的 type="date" 是 querySelectorAll 选择器字符串，不是真实 input 标签
            if n == "i18n.js":
                continue
            if n.endswith(".html") or n.endswith(".js"):
                files.append(os.path.join(root, n))
    return files


def test_every_date_input_has_i18n_aria():
    """每个 <input type="date"> 都必须带 data-i18n-aria（i18n 归属标记）。"""
    missing = []
    for path in _source_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for m in _INPUT_DATE_RE.finditer(text):
            tag = m.group(0)
            if "data-i18n-aria" not in tag:
                missing.append((os.path.relpath(path, _STATIC), tag))
    assert not missing, f"以下 date input 缺少 data-i18n-aria（不符合 i18n）: {missing}"


def test_every_date_input_is_localized_wrapper():
    """每个 <input type="date"> 都必须位于 .date-input-wrap 内，并伴随本地化 .date-display。

    原生 date input 的显示格式在 Chromium 中跟随浏览器 locale 而非应用语言，
    因此需要 text display + 隐藏的 date picker 包装，由 i18n.js 用当前语言格式化显示值。
    """
    bad = []
    for path in _source_files():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for m in _INPUT_DATE_RE.finditer(text):
            start = max(0, m.start() - 400)
            end = min(len(text), m.end() + 400)
            ctx = text[start:end]
            if "date-input-wrap" not in ctx or "date-display" not in ctx:
                bad.append((os.path.relpath(path, _STATIC), text[start:end].replace("\n", " ")))
    assert not bad, (
        f"以下 date input 未使用 .date-input-wrap + .date-display 包装（格式无法本地化）: {bad}"
    )
