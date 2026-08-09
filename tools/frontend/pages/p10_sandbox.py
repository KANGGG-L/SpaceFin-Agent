#!/usr/bin/env python
"""P10 · 策略沙盒推演（设计评审 P10 / R-OPT-01-02，仅出框架）。

设计评审明确「本期仅出交互框架 + 未校准标记规则，不进入本期开发排期」——
本页**不实现**生成→批评→校准的闭环交互，只做三件事：

1. **说明框架**：把「生成（Generator）→ 批评（Critic）→ 校准（Calibration）」
   闭环的三步语义、入口与未来交互形态画在页面上，让评审看得见闭环长什么样；
2. **未校准标记（R-OPT-01）**：读取 `output/persona/persona_report.json`，
   若其中存在 naive 模式（calibration_status="未校准"），页面顶部**醒目标记**
   「未校准输出不得直接用于决策」——这是本页的核心验收点 D-09 / R-OPT-01；
3. **校准证据展示**：naive vs calibrated 的 KS 指标、校准轨迹（trajectory）、
   违约率分布对比，让「未校准 vs 校准」的差距有数据可看。

数据来源 `output/persona/persona_report.json`（tools/persona 产物，output/ 下
.gitignore），报告缺失是正常路径——页面显示占位说明与重建命令，绝不 500。

RBAC：admin / risk / da 可见（策略研究员 / AI PM 视角；贷后不接触策略推演）。
"""

import json
import os
import sys

# pages 以包形式被导入时 frontend 根目录不一定在 sys.path 上（取决于启动方式），
# 这里补一次，保证 `import db` 在 app.py 直跑与 `python -c "import pages"` 两种场景都成立。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

# 仓库根：本文件在 <root>/tools/frontend/pages/ 下，回退四层。
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
# 策略画像推演报告：tools/persona 的产物。
_PERSONA_FILE = os.path.join(_REPO_ROOT, "output", "persona", "persona_report.json")

# 精简版新增：LangChain 假设推演 agent（与 db.py 同目录，已加入 sys.path）。
import sandbox_agent  # noqa: E402


def _load_json(path):
    """读一个 JSON 产物；文件缺失/损坏返回 None（页面降级展示，不 500）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _calib_card(raw):
    """把 naive/calibrated 的 KS 报告压成前端卡片所需的平面结构。

    raw 形如 {"calibration_status": "未校准", "ks_report": {"features": {...},
    "default_prob": {...}}}。报告缺失字段时返回 None，前端优雅降级。
    """
    if not raw:
        return None
    ks = raw.get("ks_report") or {}
    dp = ks.get("default_prob") or {}
    return {
        "bias": raw.get("bias"),
        "calibration_status": raw.get("calibration_status"),
        "ks_features": ks.get("features") or {},
        "ks_default_prob": dp.get("ks"),
        "default_prob_passed": dp.get("passed_ks_le_0_05"),
    }


def get_sandbox(ctx):
    """读 persona_report.json，组装闭环框架 + 校准状态 + 未校准标记。"""
    report = _load_json(_PERSONA_FILE)
    rel_path = os.path.relpath(_PERSONA_FILE, _REPO_ROOT)
    if report is None:
        return {
            "available": False,
            "message": "策略画像推演报告未生成，请先运行 "
            f"<code>python tools/persona/main.py</code>（产出 {rel_path}）。"
            "当前页面仅展示生成→批评→校准的框架与 R-OPT-01 未校准标记规则。",
            "path": rel_path,
        }

    naive = _calib_card(report.get("naive"))
    calibrated = _calib_card(report.get("calibrated"))
    calibration = report.get("calibration") or {}

    return {
        "available": True,
        "path": rel_path,
        "generated_at": report.get("generated_at"),
        "tool": report.get("tool"),
        "h3": report.get("h3"),
        "acceptance": report.get("acceptance"),
        "params": report.get("params") or {},
        "naive": naive,
        "calibrated": calibrated,
        "calibration": {
            "mechanism": calibration.get("mechanism"),
            "target_ks": calibration.get("target_ks"),
            "max_rounds": calibration.get("max_rounds"),
            "rounds": calibration.get("rounds"),
            "converged": calibration.get("converged"),
            "trajectory": calibration.get("trajectory") or [],
        },
        "comparison": report.get("comparison_default_prob") or {},
        "uncalibrated_rule": report.get("uncalibrated_flag") or {},
        "honest_declaration": report.get("honest_declaration") or {},
        # R-OPT-01：只要报告里存在 naive（未校准）输出，就必须醒目标记。
        "uncalibrated_present": bool(naive) and naive.get("calibration_status") == "未校准",
    }


def run_hypothesis(ctx):
    """POST /api/sandbox/hypothesis：假设推演（LangChain）。

    ctx.body 形如 {"hypothesis": "..."}；空假设由 sandbox_agent 抛 ValueError → 400。
    """
    body = ctx.body or {}
    return sandbox_agent.run_hypothesis(body.get("hypothesis"))


PAGE = {
    "id": "sandbox",
    "label": "假设推演",
    # 策略研究员 / AI PM 视角；贷后不接触策略推演（PRD §7.3 语义）。
    "roles": {"admin", "risk", "da"},
    "order": 100,
    "js": "p10_sandbox.js",
    "routes": {
        ("GET", "/api/sandbox"): get_sandbox,
        ("POST", "/api/sandbox/hypothesis"): run_hypothesis,
    },
}
