"""P6 敏感性阶梯（房价跌幅五档）· 前端静态不变量。

后端 tools/frontend/tests/test_p6_sensitivity.py 锁的是五档数据与三种触发条件；
本文件只锁「前端代码必须长成这样」的那部分——pytest 触达不到浏览器运行时，
渲染结果由浏览器实跑另记。

守护四条（工单 #11 指定）：
1. 判空：sensitivity 键可能整个不存在（情景不是房价下跌 / 情景城市与作用域对不上 /
   没填 LTV 上限），也可能是空数组。两种情况都整块不渲染——拿一条五档全平的假阶梯
   凑块图，比没有这块更误导；
2. 归第二档：五档压力曲线是可核对数字，不是一句话结论。结论层（默认可见）仍只
   三张 KPI（R2-3：实测一次运行铺 900px 高 620 字的教训）；
3. 本次档高亮用 1e-9 容差：阶梯固定五档，跌 8.5% 时没有对应行，
   就近取整把 8.5% 标成 10% 会被读成「这次跑的就是这一档」；字符串比较
   （0.1 !== "0.1"）会静默不高亮，同样不许；
4. 不得把新增受限描述成随跌幅递增：真库实测 2/2/2/0/2（跌到 20% 时那两笔越过
   红线翻成「已超红线」），非单调是物理事实，写成单调递增就是错话。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{.*?\n  \}}", text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


def _dict_block(text: str, lang: str) -> str:
    start = text.index(f'registerI18n("{lang}", {{')
    end = text.index('  }, "policy");', start)
    return text[start:end]


def _dict_keys(text: str, lang: str) -> set:
    """字典块的 key 集合。

    只认「恰好 4 空格缩进 + key + 冒号」的行：多行译文的续行是 6 空格缩进，
    而英文译文里常自带冒号（"Gap: the statistical difference…"、"After: …"），
    不按缩进过滤会把译文片段当成 key（实测多出 18 个假 key）。
    """
    keys = set()
    for line in _dict_block(text, lang).splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        m = re.match(r'^ {4}("(?:[^"\\]|\\.)*"|[^\s:{}][^:]*?)\s*:', line)
        if m:
            keys.add(m.group(1).strip('"'))
    return keys


# ---------------- 1 · 判空 ----------------


def test_absent_or_empty_payload_renders_nothing():
    text = _js_text()
    ladder = _function_body(text, "renderSensitivityLadder")
    fill = _function_body(text, "fillSensitivityChart")
    # 字符串（标题 / 占位 div）由 renderSensitivityLadder 产出，判空在那儿必须同时
    # 覆盖「键缺失」与「空数组」：只写 !rows.length 会在键缺失时抛 TypeError，
    # 整块预览区白屏（键缺失是常态——非房价情景 / 城市不匹配 / 没填上限都不下发）。
    assert re.search(r"if\s*\(!rows\s*\|\|\s*!rows\.length\)\s*return", ladder), (
        "renderSensitivityLadder 没有同时判「键缺失」与「空数组」"
    )
    # 图是二次填充（占位 div 已在 DOM 里），这条路径同样要判空，否则旧图残留。
    assert re.search(r"!p\.sensitivity\s*\|\|\s*!p\.sensitivity\.length", fill), (
        "fillSensitivityChart 没有判空：占位 div 存在但数据缺失时会画出空/旧图"
    )
    assert 'id="p6-preview-sensitivity"' in ladder, "缺柱状图占位容器"


# ---------------- 2 · 归第二档 ----------------


def test_ladder_is_folded_into_the_numbers_tier():
    body = _function_body(_js_text(), "renderPreview")
    assert re.search(r"nums\s*\+=\s*renderSensitivityLadder\(", body), (
        "敏感性阶梯没有挂进第二档 nums：五档曲线是可核对数字，"
        "结论层只允许三张 KPI（R2-3 实测 900px 高 620 字的教训）"
    )
    assert not re.search(r"out\s*\+=\s*renderSensitivityLadder\(", body), (
        "敏感性阶梯直出了默认可见区——结论层会重新变成「结论 + 一张图」"
    )
    # 图必须随每次重绘（翻页 / 展开行）重新填充，否则第二次重绘后图空白。
    assert re.search(r"fillSensitivityChart\(p\)\s*;", body), (
        "renderPreview 没有调用 fillSensitivityChart：占位 div 每次 innerHTML 重建，"
        "不重填的话翻一页图就没了"
    )


def test_fold_summary_mentions_the_ladder():
    body = _function_body(_js_text(), "renderPreview")
    m = re.search(r'foldSection\(\s*"numbers",([\s\S]{0,600}?)\n\s+nums\b', body)
    assert m, "第二档折叠标题缺失"
    assert "跌幅阶梯" in m.group(1), (
        "有阶梯时折叠标题不带提示：收起态用户不知道里面还有一条压力曲线"
    )


# ---------------- 3 · 本次档高亮 ----------------


def test_current_step_matched_with_tolerance_only():
    text = _js_text()
    ladder = _function_body(text, "renderSensitivityLadder")
    fill = _function_body(text, "fillSensitivityChart")
    # 标题（是否带「本次 X%」）与柱色（高亮哪一根）是两条独立路径，两处都要 1e-9。
    for name, body in (("renderSensitivityLadder", ladder), ("fillSensitivityChart", fill)):
        assert re.search(r"Math\.abs\([\s\S]{0,120}?\)\s*<\s*1e-9", body), (
            f"{name} 没有用 1e-9 容差匹配 shock.pct：阶梯固定五档，跌 8.5% 时没有对应行，"
            "就近取整（Math.round / 取最近档）会把 8.5% 标成 10%，"
            "用户会读成「这次跑的就是这一档」"
        )
    for body in (ladder, fill):
        assert "Math.round" not in body, "本次档位不得用取整匹配"
        assert "toFixed" not in body, "本次档位不得用格式化后的字符串比较（0.1 !== '0.1'）"
    assert "SENS_BAR_CUR" in fill, "高亮的那一档没有用独立柱色区分"


# ---------------- 4 · 不得承诺单调 ----------------


def test_newly_restricted_is_not_described_as_monotonic():
    text = _js_text()
    ladder = _function_body(text, "renderSensitivityLadder")
    for bad in ("跌幅越大", "跌幅越深", "跌得越多", "随跌幅递增", "越多"):
        assert bad not in ladder, (
            f"阶梯里出现「{bad}」类措辞：新增受限非单调（真库实测 2/2/2/0/2），"
            "这么写就是把物理事实写成了错话"
        )
    # 非单调这条事实必须常驻在图上（不是只写在注释里）。
    assert "可能回落" in ladder, (
        "缺非单调说明：新增受限＝（本次上限, 红线] 敏感带内的笔数，跌到 20% 时那两笔"
        "越过红线翻成「已超红线」而消失，不给说明会被当成 bug"
    )
    # 未命中整档时的降级说明：不给的话用户会以为自己跑的那一档就在某根柱上。
    assert "高亮无匹配" in ladder, "缺「本次跌幅不在五档内」的降级说明"


# ---------------- i18n 对账 ----------------

# 阶梯可见文案的 key（含柱标签、图标题、两条说明、折叠标题提示）。
_LADDER_KEYS = {
    "敏感性阶梯",
    "敏感性阶梯（本次 {pct}）",
    "跌 {pct}",
    "新增受限 {n} 笔",
    "{n} 笔",
    "高亮柱 = 本次情景跌幅",
    "本次情景跌幅 {pct} 介于固定档位之间，高亮无匹配",
    "跌幅敏感性柱状图",
    " · 跌幅阶梯",
}


def test_ladder_strings_have_en_and_zh_tw_translations():
    text = _js_text()
    en, tw = _dict_keys(text, "en"), _dict_keys(text, "zh-HK")
    assert _LADDER_KEYS <= en, f"en 字典缺阶梯词条：{sorted(_LADDER_KEYS - en)}"
    assert _LADDER_KEYS <= tw, f"zh-HK 字典缺阶梯词条：{sorted(_LADDER_KEYS - tw)}"
    # 非单调说明（含 {red} 占位）是这张图唯一的口径出处，必须双语。
    note = [k for k in en if k.startswith("新增受限是（本次上限, 红线] 敏感带的计数")]
    assert len(note) == 1, f"非单调说明词条缺失或不唯一：{note}"
    assert note[0] in tw, "非单调说明没有 zh-HK 译文"


def test_every_en_entry_has_a_zh_tw_counterpart():
    """en 字典的每个 key 都必须在 zh-HK 有对应条目。

    反向（zh-HK 多出的条目）不判——简繁同形的动作值「拒贷 / 加严审批 / 仅提示」
    在 zh-HK 下需要写回 DOM，而 en 侧由 global 层承接，本就不必在页面字典里重复。
    """
    text = _js_text()
    missing = sorted(_dict_keys(text, "en") - _dict_keys(text, "zh-HK"))
    assert not missing, f"以下 en 词条在 zh-HK 字典里没有译文：{missing}"
