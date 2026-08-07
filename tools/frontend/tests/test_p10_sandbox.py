"""P10 策略沙盒推演页的最小测试集（零 DB：只读 persona_report.json，用临时文件代替）。

覆盖两个验收要点：
1. 报告含 naive（calibration_status="未校准"）时 uncalibrated_present=True（R-OPT-01 标记）；
2. 报告缺失时 available=False 且带重建命令，绝不 500；
3. 校准证据（naive/calibrated KS、轨迹）被正确平面化供前端渲染。
"""

import json

import pages.p10_sandbox as mod
import pytest


def _write_report(tmp_path, payload):
    p = tmp_path / "persona_report.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def report(tmp_path):
    """一个含 naive（未校准）+ calibrated（校准）的完整报告。"""
    return _write_report(
        tmp_path,
        {
            "generated_at": "2026-08-05T17:20:44",
            "naive": {
                "bias": "naive",
                "calibration_status": "未校准",
                "ks_report": {
                    "features": {"income_monthly": {"ks": 0.257, "passed_ks_le_0_05": False}},
                    "default_prob": {"ks": 0.257, "passed_ks_le_0_05": False},
                },
            },
            "calibrated": {
                "bias": "naive→calibrated",
                "calibration_status": "校准",
                "ks_report": {
                    "features": {"income_monthly": {"ks": 0.018, "passed_ks_le_0_05": True}},
                    "default_prob": {"ks": 0.028, "passed_ks_le_0_05": True},
                },
            },
            "calibration": {
                "mechanism": "秩分位数映射",
                "target_ks": 0.05,
                "max_rounds": 20,
                "rounds": 3,
                "converged": True,
                "trajectory": [
                    {"round": 0, "p_ks": 0.257, "alpha": None},
                    {"round": 3, "p_ks": 0.028, "alpha": 0.875},
                ],
            },
            "comparison_default_prob": {
                "benchmark": {"mean": 0.35, "p50": 0.35},
                "naive": {"mean": 0.29, "p50": 0.29},
                "calibrated": {"mean": 0.35, "p50": 0.35},
            },
            "uncalibrated_flag": {"semantics": "PRD R-OPT-01"},
            "honest_declaration": {"not_for_credit_decision": "合成行为仿真不用于个体授信决策"},
        },
    )


# ---------------------------------------------------------------- PAGE 契约
def test_page_contract_roles_and_route():
    assert mod.PAGE["id"] == "sandbox"
    assert mod.PAGE["roles"] == {"admin", "risk", "da"}
    assert ("GET", "/api/sandbox") in mod.PAGE["routes"]
    assert mod.PAGE["js"] == "p10_sandbox.js"


# ---------------------------------------------------------------- 未校准标记（R-OPT-01）
def test_uncalibrated_flag_when_naive_present(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    out = mod.get_sandbox(ctx)

    assert out["available"] is True
    assert out["uncalibrated_present"] is True
    assert out["naive"]["calibration_status"] == "未校准"
    assert out["naive"]["ks_default_prob"] == 0.257
    assert out["calibrated"]["calibration_status"] == "校准"
    assert out["calibrated"]["ks_default_prob"] == 0.028


def test_no_uncalibrated_flag_when_report_has_no_naive(monkeypatch, ctx, tmp_path):
    """报告里只有校准产物（已收敛）时不应误标「未校准」。"""
    p = _write_report(
        tmp_path,
        {
            "calibrated": {
                "bias": "naive→calibrated",
                "calibration_status": "校准",
                "ks_report": {"default_prob": {"ks": 0.028, "passed_ks_le_0_05": True}},
            },
            "calibration": {"converged": True, "trajectory": []},
        },
    )
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(p))
    out = mod.get_sandbox(ctx)

    assert out["uncalibrated_present"] is False
    assert out["naive"] is None


# ---------------------------------------------------------------- 缺失降级
def test_missing_report_degrades_not_500(monkeypatch, ctx, tmp_path):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(tmp_path / "no_persona.json"))
    out = mod.get_sandbox(ctx)

    assert out["available"] is False
    # 降级提示必须给出重建命令（output/ 是 .gitignore 目录）。
    assert "persona" in out["message"]


# ---------------------------------------------------------------- 校准证据平面化
def test_calibration_trajectory_flattened(monkeypatch, ctx, report):
    monkeypatch.setattr(mod, "_PERSONA_FILE", str(report))
    out = mod.get_sandbox(ctx)

    cal = out["calibration"]
    assert cal["converged"] is True
    assert cal["rounds"] == 3
    assert cal["target_ks"] == 0.05
    assert len(cal["trajectory"]) == 2
    assert cal["trajectory"][-1]["p_ks"] == 0.028
    assert out["comparison"]["calibrated"]["mean"] == 0.35
    assert out["honest_declaration"]["not_for_credit_decision"]
