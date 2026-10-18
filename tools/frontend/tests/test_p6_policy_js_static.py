"""G-1 回归防线：runId 指针的静态不变量（pytest 触达不到浏览器侧运行时，故用文本级断言）。

背景：st 字面量漏初始化 `_runSeq`，
`runId: ++st._runSeq` 全部得 NaN；`NaN != null` 过第一关、`NaN === NaN` 恒假过不了第二关，
回放高亮 `.active` / `aria-current` 在任何路径都不出现。用三条静态断言锁死：
1. st 字面量必须显式初始化 _runSeq / _activeRunId（NaN 根因的唯一必要条件）；
2. restoreRun 不得再用数组下标覆盖 #p10-history-note（note 单一来源 = renderHistory 按 runId 生成）；
3. renderHistory 的 note 生成必须带 findIndex === -1 兜底（指针指向的 runId 被 pop 挤出历史时
   置空，绝不输出越界序号）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根（parents[0]=tests/）
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{.*?\n  \}}", text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


def test_runseq_and_pointer_initialized_in_st_literal():
    literal_m = re.search(r"const st = \{[^}]*\};", _js_text(), re.S)
    assert literal_m, "未找到 st 状态字面量"
    literal = literal_m.group(0)
    assert "_runSeq:" in literal, (
        "st 字面量缺 _runSeq 初始化 → ++st._runSeq 得 NaN → runId 全为 NaN（G-1 根因）"
    )
    assert "_activeRunId:" in literal, "st 字面量缺 _activeRunId 初始化"


def test_restore_run_does_not_override_note_by_index():
    body = _function_body(_js_text(), "restoreRun")
    assert "_history.length - idx" not in body, (
        "restoreRun 仍用数组下标覆盖 note——与 renderHistory 的 runId 单一来源矛盾（G-1 一致性问题①）"
    )
    assert "p10-history-note" not in body, "restoreRun 仍直接写 #p10-history-note"


def test_history_note_guards_evicted_runid():
    body = _function_body(_js_text(), "renderHistory")
    assert "findIndex" in body, "note 序号应来自 findIndex(runId)（下标会随 unshift 位移）"
    assert "-1" in body, (
        "findIndex 结果必须做 === -1 兜底：runId 被挤出历史时置空指针，不输出越界序号"
    )
