#!/usr/bin/env python3
"""可追溯性增强检查（stdlib-only，零新依赖）。

与 `tools/dev/trace_check.py` 互补，且**刻意不依赖其内部函数**（可独立运行，
也便于在 trace_check.py 被外部编辑器占用时单独跑）：

    --anchors      证据锚点 `path` :: `片段` 必须逐字命中目标文件（替代易漂移的 `path:行号`）
    --rerun        对 registry 中 `verified` 条目**实际执行** `rerun_command` 并比对登记值
    --metrics      asserts 的 `metric_ids` 必须能回链到 registry 条目，且其值出现在目标文件
    --manifests    docs/poc/**/MANIFEST.json 的产物 sha256 指纹不得漂移
    --adr-rollback 每篇 ADR 必须含 `## 回退（Rollback）` 段

退出码：全部通过 = 0；任一项失败 = 1。
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 证据锚点：`path` :: `snippet`（snippet 逐字命中目标文件；path 以仓库根为基准，回退到文档同级目录）
ANCHOR_RE = re.compile(r"`([^`\n]+?)`\s*::\s*`([^`\n]+?)`")
REGISTRY = REPO_ROOT / "docs" / "evidence" / "registry.json"
MANIFEST_GLOB = "docs/**/MANIFEST.json"


def _md_files():
    files = sorted((REPO_ROOT / "docs").rglob("*.md")) if (REPO_ROOT / "docs").is_dir() else []
    p = REPO_ROOT / "README.md"
    if p.exists():
        files.append(p)
    return files


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _val_hit(text, value):
    """数值型期望值按「数值相等」判定（避开 15.0/15.00 与 2/12 的字符串陷阱）；非数值用子串。"""
    s = str(value)
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        t = float(s)
        return any(abs(float(n) - t) < 1e-9 for n in _NUM_RE.findall(text))
    return s in text


# ------------------------------------------------------------------- anchors
def check_anchors() -> bool:
    checked = 0
    bad = []
    for f in _md_files():
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        in_fence = False
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for m in ANCHOR_RE.finditer(line):
                raw = m.group(1).strip()
                snip = m.group(2).strip()
                if raw.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                cand = [REPO_ROOT / raw, f.parent / raw]
                tgt = next((c.resolve() for c in cand if c.exists()), None)
                checked += 1
                if tgt is None:
                    bad.append((f.relative_to(REPO_ROOT), i, f"路径不存在: {raw}"))
                    continue
                try:
                    content = tgt.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    bad.append((f.relative_to(REPO_ROOT), i, f"无法读取: {raw}"))
                    continue
                if snip not in content:
                    bad.append((f.relative_to(REPO_ROOT), i, f"片段未命中: {raw} :: {snip}"))
    if checked == 0:
        print("[anchors] FAIL：未发现任何证据锚点（`path` :: `片段`）——约定可能被移除")
        return False
    print(f"[anchors] 检查 {checked} 条证据锚点（`path` :: `片段`）")
    if bad:
        print(f"[anchors] FAIL（{len(bad)} 条）:")
        for rel, i, msg in bad:
            print(f"    {rel}:{i}  {msg}")
        return False
    print("[anchors] PASS：锚点片段均逐字命中目标文件")
    return True


# --------------------------------------------------------------------- rerun
def _load_registry():
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[registry] FAIL：无法解析 {REGISTRY}: {exc}")
        return None


def _run(cmd: str):
    return subprocess.run(
        ["bash", "-lc", cmd],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


def check_rerun() -> bool:
    reg = _load_registry()
    if reg is None:
        return False
    checked = 0
    bad = []
    for e in reg.get("entries", []):
        if e.get("reproducibility") != "verified":
            continue
        cmd = str(e.get("rerun_command", "")).strip()
        if not cmd:
            continue
        mid = e.get("metric_id")
        expected = e.get("rerun_expected", e.get("value"))
        checked += 1
        try:
            p = _run(cmd)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{mid}: 执行异常 {type(exc).__name__}: {exc}")
            continue
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            bad.append(f"{mid}: 退出码 {p.returncode}（{cmd}）")
            continue
        if expected is not None and not _val_hit(out, expected):
            bad.append(f"{mid}: 复跑输出未含登记值 {expected!r}（{cmd}）")
    print(f"[rerun] 实际复跑 {checked} 条 verified 条目的 rerun_command")
    if bad:
        print(f"[rerun] FAIL（{len(bad)} 条）:")
        for b in bad:
            print(f"    {b}")
        return False
    print("[rerun] PASS：verified 条目复跑输出与登记值一致")
    return True


# ------------------------------------------------------------------- metrics
def check_metrics() -> bool:
    reg = _load_registry()
    if reg is None:
        return False
    by = {e.get("metric_id"): e for e in reg.get("entries", [])}
    checked = 0
    bad = []
    for a in reg.get("asserts", []):
        ids = a.get("metric_ids", [])
        if not ids:
            continue
        rel = a.get("file", "")
        tgt = REPO_ROOT / rel
        if not rel or not tgt.exists():
            bad.append(f"{a.get('id')}: 目标文件缺失 {rel}")
            continue
        text = tgt.read_text(encoding="utf-8", errors="ignore")
        for mid in ids:
            checked += 1
            e = by.get(mid)
            if e is None:
                bad.append(f"{a.get('id')}: metric_id 不存在 {mid}")
                continue
            val = e.get("value")
            if val is None:
                continue
            if not _val_hit(text, val):
                bad.append(f"{a.get('id')}: {mid} 的值 {val!r} 未出现在 {rel}")
    print(f"[metrics] 校验 {checked} 条 metric_id 回链")
    if bad:
        print(f"[metrics] FAIL（{len(bad)} 条）:")
        for b in bad:
            print(f"    {b}")
        return False
    print("[metrics] PASS：asserts 的 metric_id 均可回链且值出现在目标文件")
    return True


# ----------------------------------------------------------------- manifests
def check_manifests() -> bool:
    files = sorted(REPO_ROOT.glob(MANIFEST_GLOB))
    if not files:
        print("[manifests] FAIL：未找到任何 MANIFEST.json（docs/**）——产物指纹约定可能被移除")
        return False
    checked = 0
    bad = []
    for mf in files:
        rel = mf.relative_to(REPO_ROOT)
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{rel}: 解析失败 {exc}")
            continue
        for it in data.get("files", []):
            rp = it.get("path")
            want = it.get("sha256")
            if not rp or not want:
                bad.append(f"{rel}: 条目缺 path/sha256")
                continue
            t = mf.parent / rp
            if not t.exists():
                t = REPO_ROOT / rp
            if not t.exists():
                bad.append(f"{rel}: 文件缺失 {rp}")
                continue
            checked += 1
            got = hashlib.sha256(t.read_bytes()).hexdigest()
            if got != want:
                bad.append(f"{rel}: {rp} sha256 漂移（disk={got[:12]}… expect={want[:12]}…）")
    print(f"[manifests] 校验 {checked} 个文件指纹（{len(files)} 份 MANIFEST）")
    if bad:
        print(f"[manifests] FAIL（{len(bad)} 条）:")
        for b in bad:
            print(f"    {b}")
        return False
    print("[manifests] PASS：PoC 产物指纹与清单一致")
    return True


# -------------------------------------------------------------- adr-rollback
def check_adr_rollback() -> bool:
    adr_dir = REPO_ROOT / "docs" / "adr"
    files = sorted(adr_dir.glob("ADR-*.md")) if adr_dir.is_dir() else []
    if not files:
        print("[adr-rollback] SKIP：无 ADR-*.md")
        return True
    bad = []
    for f in files:
        t = f.read_text(encoding="utf-8", errors="ignore")
        if "## 回退（Rollback）" not in t:
            bad.append(f"{f.relative_to(REPO_ROOT)}: 缺少「## 回退（Rollback）」段")
            continue
        seg = t.split("## 回退（Rollback）", 1)[1].split("\n## ", 1)[0]
        if len(re.sub(r"\s+", "", seg)) < 10:
            bad.append(f"{f.relative_to(REPO_ROOT)}: 回退段正文过短/为空")
    print(f"[adr-rollback] 检查 {len(files)} 篇 ADR")
    if bad:
        print(f"[adr-rollback] FAIL（{len(bad)} 条）:")
        for b in bad:
            print(f"    {b}")
        return False
    print("[adr-rollback] PASS：ADR 均含回退段")
    return True


CHECKS = [
    ("anchors", check_anchors),
    ("rerun", check_rerun),
    ("metrics", check_metrics),
    ("manifests", check_manifests),
    ("adr-rollback", check_adr_rollback),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可追溯性增强检查（stdlib-only）")
    for n, _ in CHECKS:
        ap.add_argument(f"--{n}", action="store_true", help=f"仅运行 {n} 检查")
    args = ap.parse_args(argv)
    selected = [n for n, _ in CHECKS if getattr(args, n.replace("-", "_"))]
    if not selected:
        selected = [n for n, _ in CHECKS]
    results = {}
    print("=" * 56)
    for n, fn in CHECKS:
        if n not in selected:
            continue
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[{n}] FAIL：检查抛出异常 {type(exc).__name__}: {exc}")
            ok = False
        results[n] = ok
        print("-" * 56)
    print("汇总：")
    for n in selected:
        print(f"  {n:<13} {'PASS' if results.get(n) else 'FAIL'}")
    all_ok = all(results.get(n) for n in selected)
    print("=" * 56)
    print("总结果：PASS" if all_ok else "总结果：FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
