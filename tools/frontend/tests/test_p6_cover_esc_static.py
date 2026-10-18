"""P6 前端静态验收：覆盖存量明细表 loan_id 必须过 esc()（A5 低危清理钉死）。

renderHitDetailTable / 详情面板 / 压力明细的 loan_id 早已转义，唯独
renderCoverTable（作用域内「未命中」逐笔表）的 `<td>${r.loan_id}</td>` 是
裸插值——loan_id 源头虽是库内整数，但静态断言把「明细表数字列一律 esc」
钉成纪律，防止后续把字符串型标识（或被污染字段）接进同一槽位时裸奔。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _function_body(text: str, name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{.*?\n  \}}", text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


def test_cover_table_loan_id_is_escaped():
    body = _function_body(JS.read_text(encoding="utf-8"), "renderCoverTable")
    assert "<td>${esc(r.loan_id)}</td>" in body, "覆盖存量明细表的 loan_id 必须过 esc()"
    assert "<td>${r.loan_id}</td>" not in body, "存在未转义的 loan_id 裸插值"


def test_hit_detail_table_loan_id_is_escaped():
    body = _function_body(JS.read_text(encoding="utf-8"), "renderHitDetailTable")
    assert "<td>${esc(r.loan_id)}</td>" in body
    assert "<td>${r.loan_id}</td>" not in body
