#!/usr/bin/env python3
"""台账常量一致性检查（stdlib-only）：LLM_REGISTRY 的护栏常量表必须与代码一致。

权威来源是代码（`tools/frontend/ai_guard.py`、`tools/frontend/sandbox_agent.py`）；
本脚本从 `docs/models/LLM_REGISTRY.md` 的「## 护栏常量」表解析出 (常量名, 文档值)，
与代码中该常量的赋值表达式做**数值字面量集合**比对；不一致即 FAIL（防"文档改代码没改"式漂移）。

用法：
    python3 tools/dev/gen_ledger.py --check     # 校验（默认）
退出码：一致 = 0；漂移 = 1。
"""

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "docs" / "models" / "LLM_REGISTRY.md"
CODE_FILES = [
    REPO_ROOT / "tools" / "frontend" / "ai_guard.py",
    REPO_ROOT / "tools" / "frontend" / "sandbox_agent.py",
]
SECTION_HEAD = "## 护栏常量"
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _code_constants():
    """name -> 赋值表达式源码片段。"""
    consts = {}
    for f in CODE_FILES:
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        seg = ast.get_source_segment(src, node.value)
                        if seg:
                            consts[t.id] = seg
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    seg = ast.get_source_segment(src, node.value)
                    if seg:
                        consts[node.target.id] = seg
    return consts


def _ledger_rows():
    """从「## 护栏常量」小节解析 markdown 表行 -> (name, doc_value)。"""
    if not LEDGER.exists():
        return []
    rows = []
    in_sec = False
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_sec = line.startswith(SECTION_HEAD)
            continue
        if not in_sec or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip("`").strip()
        if not name or name in ("常量",) or set(name) <= set("-"):
            continue
        rows.append((name, cells[1]))
    return rows


def _nums(text: str):
    return set(NUM_RE.findall(text or ""))


def check() -> int:
    consts = _code_constants()
    rows = _ledger_rows()
    if not rows:
        print(
            f"[ledger] FAIL：未在 {LEDGER.relative_to(REPO_ROOT)} 解析到「{SECTION_HEAD}」常量表（约定可能被移除）"
        )
        return 1
    problems = []
    for name, doc_val in rows:
        if name not in consts:
            problems.append(f"{name}: 代码中未找到该常量（docs 有、代码无）")
            continue
        code_nums = _nums(consts[name])
        doc_nums = _nums(doc_val)
        if code_nums != doc_nums:
            problems.append(
                f"{name}: 数值字面量不一致 —— docs={sorted(doc_nums)} code={sorted(code_nums)}"
                f"（code: {consts[name][:60]}）"
            )
    print(f"[ledger] 校验 {len(rows)} 个 LLM 护栏常量（源 {LEDGER.name} ↔ 代码）")
    if problems:
        print(f"[ledger] FAIL（{len(problems)} 条）:")
        for p in problems:
            print(f"    {p}")
        return 1
    print("[ledger] PASS：台账常量与代码数值字面量一致")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台账常量一致性检查（stdlib-only）")
    ap.add_argument("--check", action="store_true", help="校验（默认行为，保留以便 CI 显式调用）")
    ap.parse_args(argv)
    return check()


if __name__ == "__main__":
    sys.exit(main())
