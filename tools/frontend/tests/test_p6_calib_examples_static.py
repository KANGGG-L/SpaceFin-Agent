"""R2-12 · 校准证据区「具体案例 · 修正前 vs 修正后」前端静态 + 透传不变量。

先红后绿的锚点：改前 p6_policy.js 无 renderCalibExamples、renderEvidence 体内
renderComparison 之后无 renderCalibExamples 调用、get_sandbox 返回里无 example_cases
透传——下列判据全部失败；护栏（既有 test_p10_sandbox.py / test_p6_policy_js_static.py /
test_p6_batch3b_static.py）改前改后均全绿背书。
"""

import json
import re
from pathlib import Path

import pages.p10_sandbox as mod

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    pattern = "function " + re.escape(name) + r"\([^)]*\)[^{}]*\{.*?\n  \}"
    m = re.search(pattern, text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


# ---------------- R2-12 前端静态判据 ----------------


def test_js_has_render_calib_examples():
    """p6_policy.js 必须定义 renderCalibExamples（改前红）。"""
    assert "function renderCalibExamples" in _js_text(), "缺 renderCalibExamples 函数"


def test_evidence_renders_examples_after_comparison():
    """renderEvidence 体内 renderCalibExamples 必须排在 renderComparison（违约率对比）之后。"""
    body = _function_body(_js_text(), "renderEvidence")
    assert body.find("renderComparison(") >= 0, "renderEvidence 缺 renderComparison 调用"
    assert body.find("renderComparison(") < body.find("renderCalibExamples("), (
        "renderCalibExamples 未排在 renderComparison（违约率对比）之后"
    )


def test_examples_show_before_after_and_delta():
    """案例卡片主数字：修正前 X% → 修正后 Y%（±Δpp）。"""
    body = _function_body(_js_text(), "renderCalibExamples")
    assert "修正前" in body and "修正后" in body, "案例主数字缺「修正前/修正后」"
    assert "pp" in body, "案例主数字缺 pp 表述"


def test_examples_carry_demo_badge_and_time():
    """案例卡片不再挂合成数据徽标（数据全合成统一前提），保留生成时间。"""
    body = _function_body(_js_text(), "renderCalibExamples")
    assert "合成数据" not in body and "演示数据" not in body, "案例卡片仍残留合成/演示标注"
    assert "fmtTs(generatedAt)" in body, "案例卡片缺生成时间（fmtTs(generatedAt)）"


def test_examples_graceful_empty():
    """函数体开头判空：cases 为空/缺 → 返回空串、整段不渲染、无空壳。"""
    body = _function_body(_js_text(), "renderCalibExamples")
    assert 'if (!cases || !cases.length) return ""' in body, (
        "函数体开头缺判空（空 cases 应返回空串）"
    )


# ---------------- R2-12 后端透传 ----------------


def _write_report(tmp_path, payload):
    p = tmp_path / "persona_report.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_sandbox_passthrough_example_cases(monkeypatch, ctx, tmp_path):
    """报告含 example_cases → get_sandbox 原样透传（改前红）。"""
    cases = [{"label": "案例 A", "index": 37, "delta": 0.1129}]
    p = _write_report(tmp_path, {"example_cases": cases})
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(p))
    out = mod.get_sandbox(ctx)
    assert out["example_cases"] == cases


def test_sandbox_missing_example_cases_returns_empty_list(monkeypatch, ctx, tmp_path):
    """报告缺 example_cases 字段 → 恒返回 []（向后兼容，改前红）。"""
    p = _write_report(tmp_path, {"comparison_default_prob": {"benchmark": {"mean": 0.3}}})
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(p))
    out = mod.get_sandbox(ctx)
    assert out["example_cases"] == []
