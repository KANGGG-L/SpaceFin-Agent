"""增强一·前端静态验收：CSV 导出列白名单 + 明细分页关键字（spec §1e-7/8）。

仿 test_p6_policy_js_static.py 的文本级静态断言：读文件 + re 断言，pytest 触达不到
浏览器侧运行时，故锁关键函数与字面量，防「改到一半丢了」的回归。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"

WHITELIST = [
    "loan_id",
    "collateral_id",
    "ltv",
    "balance",
    "risk_class",
    "low_confidence",
    "addr",
    "change",
]


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def test_csv_export_function_exists_and_columns_no_customer():
    # §1e-7：含 CSV 导出函数，且其列定义串不含 customer_id/customer_name
    text = _js_text()
    assert "function downloadCSV" in text, "未找到 CSV 导出函数 downloadCSV"
    assert "function downloadFullCSV" in text, "未找到全量下载函数 downloadFullCSV"
    m = re.search(r"const cols = \[([^\]]*)\]", text)
    assert m, "未找到 CSV 列定义 const cols = [...]"
    cols = m.group(1)
    assert "customer_id" not in cols, "CSV 列定义含 customer_id（PII 红线被破坏）"
    assert "customer_name" not in cols, "CSV 列定义含 customer_name（PII 红线被破坏）"
    for col in WHITELIST:
        assert col in cols, f"CSV 列定义缺白名单列 {col!r}"
    # 导出文件名约定 policy_preview_<scope_value>_<ts>.csv
    assert "policy_preview_" in text
    assert "Blob" in text and "createObjectURL" in text  # 确实自实现 Blob 下载


def test_detail_rendering_keywords():
    # §1e-8：明细区含「共 」「笔命中」汇总行与「上一页」分页控件
    text = _js_text()
    assert "共 " in text and "笔命中" in text, "明细缺「共 N 笔命中」汇总行"
    assert "上一页" in text, "分页条缺「上一页」"


def test_load_all_reuses_preview_no_new_endpoint():
    # §1e-8：加载全部只复用 POST /api/policy/preview（page_size=10000），不新增端点
    text = _js_text()
    assert "page_size: 10000" in text, "加载全部未用 page_size=10000 取全量"
    assert '"/api/policy/preview"' in text, "加载全部未复用 /api/policy/preview"
    assert "/api/alerts/detail" not in text, "误引入 /api/alerts/detail 新端点"
    assert "/api/avm/valuations" not in text, "误引入 /api/avm/valuations 新端点"


# ---------------- 结果区「一页纸」导出（复制结论 / 下载 .md） ----------------
# 与 CSV 导出同属「结果区产物」，故并入本文件：都是读 p6_policy.js 的文本级断言，
# 守护目标是「改到一半丢了」的回归，而不是运行时行为。


def test_onepager_export_buttons_and_md_download():
    text = _js_text()
    assert "function buildSummaryText" in text, "缺一页纸正文构造函数 buildSummaryText"
    assert 'data-act="copy-summary"' in text, "结果区缺「复制结论」按钮"
    assert 'data-act="download-summary"' in text, "结果区缺「下载 .md」按钮"
    # 两个 act 必须落在同一个分支里，否则新增的按钮没人接线（点了没反应）
    assert re.search(r'act === "copy-summary" \|\| act === "download-summary"', text), (
        "复制 / 下载未在同一事件分支中处理"
    )
    assert "policy_summary_" in text, "md 文件名缺 policy_summary_ 前缀"
    assert ".md" in text
    # 复制走 Clipboard API 且保留 execCommand 降级：http 内网下 navigator.clipboard 不存在
    assert "navigator.clipboard" in text, "复制未用 Clipboard API"
    assert "execCommand" in text, "复制缺 execCommand 降级（http 内网会完全不可用）"


def test_onepager_export_disabled_in_history_readonly():
    # 历史回放是快照，导出按今天存量重算会与历史口径冲突，两个按钮必须一起禁用。
    text = _js_text()
    assert re.search(r'const roAttr = st\._historyReadOnly \? " disabled" : "";', text), (
        "只读态未按 st._historyReadOnly 生成 disabled 属性"
    )
    for act in ("copy-summary", "download-summary"):
        assert f'data-act="{act}"${{roAttr}}' in text, f"{act} 按钮未挂 disabled 属性"
    # 按钮层的 disabled 只挡鼠标，事件处理里必须再拦一道
    assert re.search(
        r'act === "copy-summary" \|\| act === "download-summary"\)\s*\{\s*[^}]*?st\._historyReadOnly\)\s*return;',
        text,
        re.S,
    ), "导出事件分支缺 _historyReadOnly 二次拦截"


def test_conclusion_label_and_body_are_separate_nodes():
    # 标签与正文写进同一个插值会粘成「推演结论抵押物估值下行…」，必须拆成两个节点。
    # 沙盘拆分后结果区浮层已删除，本断言只锁完整叙事卡（#hyp-narr-panel 内）的拆分。
    text = _js_text()
    # 完整叙事卡：标签进标题行，正文进 hyp-inference 块
    assert re.search(
        r't\("推演结论"\)\s*\+\s*"</div>"\s*\+\s*`<div class="hyp-inference">', text
    ), "叙事卡「推演结论」标签与正文未分开渲染"
