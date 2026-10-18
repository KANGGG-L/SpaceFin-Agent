#!/usr/bin/env python3
"""证据层快照生成器（stdlib-only，零新依赖）。

用法：
    python3 tools/dev/gen_evidence.py --out docs/evidence     # 生成/刷新全部快照
    python3 tools/dev/gen_evidence.py --check --out docs/evidence  # 重算并比对磁盘快照

诚实规则（见 docs/evidence/README.md）：
- 绝不臆造数值：`verified` 快照的数值来自**当场实测**；`unavailable` 快照的 value 一律 null。
- 产物缺失不删文件：output/ 产物被 gitignore 时仍写出快照，标 `reproducibility: unavailable`。
"""

import argparse
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 遍历时跳过的目录（版本库 / 依赖 / 各类缓存）
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
    "htmlcov",
    ".idea",
    ".vscode",
    ".eggs",
    "*.egg-info",
}

CORE_MODULES = ["tools/frontend", "tools/risk", "tools/alerting", "tools/avm"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _environment() -> dict:
    return {"python": platform.python_version(), "platform": platform.platform()}


def _iter_test_files(root: Path):
    """遍历仓库内 test_*.py，跳过 .git / node_modules / 缓存目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.startswith("test_") and fn.endswith(".py"):
                yield Path(dirpath) / fn


def _count_def_test(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return len(re.findall(r"def test_", text))


def collect_test_metrics() -> dict:
    """当场测量测试规模（与 registry 的 verified 条目同口径）。"""
    total = 0
    files = 0
    in_tests_dirs = 0
    core = {m: 0 for m in CORE_MODULES}
    for f in _iter_test_files(REPO_ROOT):
        n = _count_def_test(f)
        total += n
        files += 1
        if "tests" in f.parts:
            in_tests_dirs += n
        for mod in CORE_MODULES:
            if f.is_relative_to(REPO_ROOT / mod):
                core[mod] += n
                break
    core_total = sum(core.values())
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": "仓库全量 test_*.py（排除 .git/node_modules/缓存）",
        "reproducibility": "verified",
        "metrics": {
            "test_total_def_test": total,
            "test_in_tests_dirs": in_tests_dirs,
            "test_files_total": files,
            "test_core_four_modules": core_total,
            "breakdown": {m: core[m] for m in CORE_MODULES},
        },
    }


def _dig(obj, keys, default=None):
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _missing_snapshot(name: str, source: str, note: str, metric_keys) -> dict:
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": source,
        "reproducibility": "unavailable",
        "metrics": {k: None for k in metric_keys},
        "note": note,
    }


def _load_report(rel_path: str):
    p = REPO_ROOT / rel_path
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_avm_metrics() -> dict:
    rel = "output/avm/avm_report.json"
    rep = _load_report(rel)
    keys = [
        "model_version",
        "mape_full",
        "baseline_mape",
        "ac07_coverage_at_10pct",
        "n_test",
    ]
    if rep is None:
        return _missing_snapshot(
            "avm_metrics.json",
            rel,
            "产物缺失：output/avm/avm_report.json 位于被 gitignore 的 output/ 目录、未随仓库分发；"
            "如需生成请在本机重训 `python3 tools/avm/train.py --loss quantile --quantile 0.45 --smooth-mode eb`。",
            keys,
        )
    metrics = {
        "model_version": rep.get("version"),
        "mape_full": _dig(rep, ["metrics", "model_total_price", "mape"]),
        "baseline_mape": _dig(rep, ["metrics", "baseline_median_x_area", "mape"]),
        "ac07_coverage_at_10pct": rep.get("confidence", {}).get("ac07_coverage_at_10pct"),
        "n_test": rep.get("n_test"),
    }
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": rel,
        "reproducibility": "verified",
        "metrics": metrics,
    }


def collect_crawl_stock() -> dict:
    rel = "output/guangdong/etl_report.json"
    rep = _load_report(rel)
    keys = ["sale_total", "rent_total", "n_raw", "unique_total", "dup_rate"]
    if rep is None:
        return _missing_snapshot(
            "crawl_stock.json",
            rel,
            "产物缺失：output/guangdong/etl_report.json 位于被 gitignore 的 output/ 目录、未随仓库分发；"
            "如需生成请本机跑 `python3 tools/orchestrator/etl.py`。",
            keys,
        )
    metrics = {
        "sale_total": _dig(rep, ["stock", "sale_total"]),
        "rent_total": _dig(rep, ["stock", "rent_total"]),
        "n_raw": _dig(rep, ["input", "raw_count"]),
        "unique_total": _dig(rep, ["input", "unique_total"]),
        "dup_rate": _dig(rep, ["input", "dup_rate"]),
    }
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": rel,
        "reproducibility": "verified",
        "metrics": metrics,
    }


def collect_risk_summary() -> dict:
    rel = "output/risk/risk_report.json"
    rep = _load_report(rel)
    keys = [
        "model_version",
        "total_loans",
        "alerts_count",
        "low_confidence",
        "over_red_line",
        "abnormal_valuation",
    ]
    if rep is None:
        return _missing_snapshot(
            "risk_summary.json",
            rel,
            "产物缺失：output/risk/risk_report.json 位于被 gitignore 的 output/ 目录、未随仓库分发；"
            "如需生成请本机跑 `python3 tools/risk/main.py --date <YYYY-MM-DD>`（需 MySQL + DWS 数据）。",
            keys,
        )
    metrics = {
        "model_version": rep.get("model_version"),
        "total_loans": rep.get("total_loans"),
        "alerts_count": rep.get("alerts_count"),
        "low_confidence": _dig(rep, ["ltv_distribution", "low_confidence"]),
        "over_red_line": _dig(rep, ["ltv_distribution", "over_red_line"]),
        "abnormal_valuation": _dig(rep, ["risk_valuation_alerts", "abnormal_valuation"]),
    }
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": rel,
        "reproducibility": "verified",
        "metrics": metrics,
    }


def collect_llm_ops() -> dict:
    """LLM 运维指标：读取评测 harness 产物 llm_eval_report.json（见 docs/models/LLM_EVAL_CASES.md）。
    产物缺失时保持 unavailable（附埋点方式），不臆造数值。
    等级说明：该数值来自配置 LLM_KEY 的实测运行，完整复跑依赖仓库外资源（LLM key），
    故记为 recorded（产物随仓库分发，但本仓默认无 key 时只测得确定性路径子集）。"""
    rel = "tools/frontend/tests/llm_eval_report.json"
    rep = _load_report(rel)
    keys = [
        "llm_fallback_rate",
        "llm_guard_block_rate",
        "llm_eval_case_pass_rate",
        "llm_p95_latency_s",
    ]
    if rep is None:
        return {
            "generated_at": _utc_now_iso(),
            "environment": _environment(),
            "source": rel,
            "reproducibility": "unavailable",
            "metrics": {k: None for k in keys},
            "metric_definitions": {
                "llm_fallback_rate": "LLM 调用失败/超时→规则或模板回退的请求占比（%，评测集口径）",
                "llm_guard_block_rate": "ai_guard 拦截（注入/超长/超预算）的请求占比（%，评测集口径）",
                "llm_eval_case_pass_rate": "LLM 回归用例集通过率（%）",
                "llm_p95_latency_s": "LLM 端到端调用时延 P95（秒）",
            },
            "missing_instrumentation": [
                "先在 .env 配 LLM_KEY，再跑 python3 tools/frontend/tests/llm_eval_harness.py --json tools/frontend/tests/llm_eval_report.json",
            ],
        }
    m = rep.get("metrics", {})
    return {
        "generated_at": _utc_now_iso(),
        "environment": _environment(),
        "source": rel,
        "reproducibility": "recorded",
        "metrics": {
            "llm_fallback_rate": m.get("llm_fallback_rate_pct"),
            "llm_guard_block_rate": m.get("llm_guard_block_rate_pct"),
            "llm_eval_case_pass_rate": m.get("llm_eval_case_pass_rate_pct"),
            "llm_p95_latency_s": m.get("llm_p95_latency_s"),
        },
        "note": "评测集口径（非生产流量）；单次运行、LLM 非确定性；完整复跑需 .env 配 LLM_KEY，"
        "本仓默认无 key 时只测得确定性路径子集。复跑见 llm_eval_harness.py。",
    }


# 文件名 -> 生成函数（顺序即写入/校验顺序）
SNAPSHOTS = [
    ("test_metrics.json", collect_test_metrics),
    ("avm_metrics.json", collect_avm_metrics),
    ("crawl_stock.json", collect_crawl_stock),
    ("risk_summary.json", collect_risk_summary),
    ("llm_ops.json", collect_llm_ops),
]


def generate(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in SNAPSHOTS:
        snap = fn()
        path = out_dir / name
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[gen] 写出 {path}  (reproducibility={snap['reproducibility']})")
    return 0


def _diff_metrics(fresh: dict, old: dict) -> list:
    """比对两组 metrics；返回差异描述列表。"""
    diffs = []
    fm, om = fresh.get("metrics", {}), old.get("metrics", {})
    for k in sorted(set(fm) | set(om)):
        fv, ov = fm.get(k, "<缺>"), om.get(k, "<缺>")
        if fv != ov:
            diffs.append(f"    metrics.{k}: on-disk={ov!r} fresh={fv!r}")
    return diffs


def check(out_dir: Path) -> int:
    status = 0
    for name, fn in SNAPSHOTS:
        path = out_dir / name
        fresh = fn()
        if not path.exists():
            print(f"[check] {name}: FAIL（磁盘快照缺失）")
            status = 1
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[check] {name}: FAIL（无法解析：{exc}）")
            status = 1
            continue

        if fresh["reproducibility"] != old.get("reproducibility"):
            if old.get("reproducibility") == "unavailable" and fresh["reproducibility"] in (
                "verified",
                "recorded",
            ):
                print(
                    f"[check] {name}: OK（本机存在产物 → fresh={fresh['reproducibility']}；"
                    f"磁盘 unavailable 反映 clean-checkout 口径，不判漂移）"
                )
                continue
            print(
                f"[check] {name}: FAIL（reproducibility 漂移 "
                f"on-disk={old.get('reproducibility')!r} fresh={fresh['reproducibility']!r}）"
            )
            status = 1
            continue

        if fresh["reproducibility"] == "verified":
            diffs = _diff_metrics(fresh, old)
            if diffs:
                print(f"[check] {name}: FAIL（verified 指标漂移）")
                for d in diffs:
                    print(d)
                status = 1
            else:
                print(f"[check] {name}: OK（verified 指标一致）")
        else:
            print(f"[check] {name}: OK（{fresh['reproducibility']}，reproducibility 一致）")

    print("=" * 48)
    print("[check] 结论：PASS" if status == 0 else "[check] 结论：FAIL（存在漂移）")
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="证据层快照生成器（stdlib-only）")
    ap.add_argument("--out", default="docs/evidence", help="输出目录（默认 docs/evidence）")
    ap.add_argument("--check", action="store_true", help="重算并比对磁盘快照；漂移则 exit 1")
    args = ap.parse_args(argv)
    out_dir = (REPO_ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    return check(out_dir) if args.check else generate(out_dir)


if __name__ == "__main__":
    sys.exit(main())
