"""增强三·前端静态验收：命中行展开详情面板 + st 初始化 + 无新端点（spec §3e）。

仿 test_p6_policy_js_static.py 的文本级静态断言，锁：
1. st 字面量显式初始化 `_expandedHitRow`（防 G-1 类「运行时未定义 → 判空失效」）；
2. 详情面板含 collateral_id / addr 且不含任何 customer 字段（PII 红线）；
3. 有 shock 且 shock_added 时用「因市场情景 LTV 放大后新增受限」类文案 + ltv_base→ltv 对照；
4. 未新增 /api/alerts/detail 或 /api/avm/valuations 的新 fetch。
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


def test_expanded_hit_row_initialized_in_st_literal():
    # §3e-1：st 字面量含 _expandedHitRow（防运行时未定义，G-1 类缺陷）
    literal_m = re.search(r"const st = \{[^}]*\};", _js_text(), re.S)
    assert literal_m, "未找到 st 状态字面量"
    assert "_expandedHitRow:" in literal_m.group(0), "st 字面量缺 _expandedHitRow 初始化"


def test_detail_panel_has_collateral_and_addr_no_customer():
    # §3e-2：详情面板渲染含 collateral_id 与 addr，且不含 customer_id/customer_name
    body = _function_body(_js_text(), "renderHitDetail")
    assert "collateral_id" in body, "详情面板缺 collateral_id"
    assert "addr" in body, "详情面板缺 addr"
    assert "customer_id" not in body, "详情面板含 customer_id（PII 红线被破坏）"
    assert "customer_name" not in body, "详情面板含 customer_name（PII 红线被破坏）"


def test_shock_added_copy_and_ltv_base_to_ltv():
    # §3e-3：有 shock_added 分支文案 + shock 时用 ltv_base → ltv 对照
    body = _function_body(_js_text(), "renderHitDetail")
    assert "因市场情景 LTV 放大后新增受限" in body, "缺「因市场情景 LTV 放大后新增受限」文案分支"
    assert "ltv_base" in body and "→" in body, "有 shock 时未用 ltv_base → ltv 对照"
    # change 兜底分支仍在（无 shock_added 的行沿用 change）
    assert ".change" in body or "row.change" in body


def test_no_new_endpoint_fetch():
    # §3e-4：未新增对 /api/alerts/detail 或 /api/avm/valuations 的 fetch
    text = _js_text()
    assert "/api/alerts/detail" not in text, "误引入 /api/alerts/detail 钻取端点"
    assert "/api/avm/valuations" not in text, "误引入 /api/avm/valuations 钻取端点"
    assert '"/api/policy/preview"' in text  # 现有 preview 仍在用，未移除
