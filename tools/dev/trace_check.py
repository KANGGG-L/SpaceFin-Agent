#!/usr/bin/env python3
"""可追溯性自动检查（stdlib-only，零新依赖）。

用法：
    python3 tools/dev/trace_check.py                 # 默认跑全部 5 项检查
    python3 tools/dev/trace_check.py --links         # 仅文档链接可达性
    python3 tools/dev/trace_check.py --adr           # 仅 ADR 结构与取证来源
    python3 tools/dev/trace_check.py --registry      # 仅 evidence registry 结构校验
    python3 tools/dev/trace_check.py --consistency   # 仅文档一致性断言
    python3 tools/dev/trace_check.py --terms         # 仅黑名单词扫描

退出码：全部通过 = 0；任一项失败 = 1。每项检查打印 PASS/FAIL 摘要。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    # 被 .gitignore 排除的产物 / 缓存目录（不随仓库分发）：非红旗扫描范围
    "output",
    "data_lake",
}

# 黑名单扫描需跳过的「定义自身 / 数据自身」文件：
#  - trace_check.py 内含黑名单正则字面量；
#  - registry.json 的 asserts 以黑名单词作为 must_not_contain 数据。
# 二者是检查机制本身，不是对外泄漏，必须排除，否则检查恒自命中。
TERMS_SELF_EXCLUDE = {
    Path("tools/dev/trace_check.py"),
    Path("docs/evidence/registry.json"),
}

BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".joblib",
    ".pkl",
    ".pyc",
    ".so",
    ".dylib",
    ".dll",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".bin",
    ".mp4",
    ".parquet",
    ".bundle",
    ".exe",
}

# 仅内联 markdown 链接 [text](target "title")，捕获 target。
LINK_RE = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")

ADR_REQUIRED_HEADINGS = [
    "## 状态",
    "## 迭代",
    "## 背景（Context）",
    "## 决策（Decision）",
    "## 备选方案（Alternatives）",
    "## 影响（Consequences）",
    "## 证据（Evidence）",
    "## 取证来源（Provenance）",
    "## 回退（Rollback）",
    "## 关联（Related）",
]
ADR_PROVENANCE_VALUES = (
    "documented",
    "self-reported",
    "unknown",
    "code-inspection",
    "file-evidence",
)

REQUIRED_ENTRY_KEYS = [
    "metric_id",
    "value",
    "unit",
    "data_version",
    "model_version",
    "run_date",
    "source_artifact",
    "reproducibility",
    "rerun_command",
    "note",
]
VALID_REPRODUCIBILITY = {"verified", "recorded", "unavailable", "unverified"}
REGISTRY_REQUIRED_TOP_KEYS = [
    "schema_version",
    "updated_at",
    "provenance_levels",
    "entries",
    "asserts",
]

BLACKLIST_RE = re.compile(
    r"全球领先|业界第一|首屈一指|独一无二|革命性|颠覆性|遥遥领先", re.IGNORECASE
)


def _walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield Path(dirpath) / fn


# ---------------------------------------------------------------- 1) links
def check_links() -> bool:
    docs = REPO_ROOT / "docs"
    files = sorted(docs.rglob("*.md")) if docs.is_dir() else []
    for extra in ("README.md", "CONTRIBUTING.md"):
        p = REPO_ROOT / extra
        if p.exists():
            files.append(p)

    broken = []
    checked = 0
    for f in files:
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
                continue  # 跳过围栏代码块内的示例链接（模板占位符等）
            for m in LINK_RE.finditer(line):
                url = m.group(1).strip()
                if url.startswith(("http://", "https://", "mailto:", "#", "/", "ftp:")):
                    continue
                target = url.split("#", 1)[0].split("?", 1)[0]
                target = unquote(target)
                if not target:
                    continue
                checked += 1
                resolved = (f.parent / target).resolve()
                if not resolved.exists():
                    broken.append((f.relative_to(REPO_ROOT), i, url))

    print(f"[links] 扫描 {len(files)} 个 .md，检查 {checked} 条相对链接")
    if broken:
        print(f"[links] BROKEN（{len(broken)} 条）:")
        for rel, i, url in broken:
            print(f"    {rel}:{i} -> {url}")
        return False
    print("[links] PASS：无失效相对链接")
    return True


# ----------------------------------------------------------------- 2) adr
def check_adr() -> bool:
    adr_dir = REPO_ROOT / "docs" / "adr"
    if not adr_dir.is_dir():
        print("[adr] SKIP：docs/adr/ 目录不存在（可能由其他 agent 并行创建中）")
        return True
    adr_files = sorted(adr_dir.glob("ADR-*.md"))
    if not adr_files:
        print("[adr] SKIP：docs/adr/ 下无 ADR-*.md")
        return True

    problems = []
    for f in adr_files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        missing = [h for h in ADR_REQUIRED_HEADINGS if h not in text]
        if missing:
            problems.append((f.relative_to(REPO_ROOT), "缺少标题: " + ", ".join(missing)))
        has_prov = any(v in text for v in ADR_PROVENANCE_VALUES)
        if not has_prov:
            problems.append(
                (
                    f.relative_to(REPO_ROOT),
                    "取证来源取值缺失（需 documented/self-reported/unknown 之一）",
                )
            )

    print(f"[adr] 检查 {len(adr_files)} 个 ADR 文件")
    if problems:
        print(f"[adr] FAIL（{len(problems)} 处）:")
        for rel, msg in problems:
            print(f"    {rel}: {msg}")
        return False
    print("[adr] PASS：标题与取证来源齐备")
    return True


# ------------------------------------------------------------ 3) registry
def _resolve_registry(path_arg: str) -> Path:
    p = Path(path_arg)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _load_registry(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[registry] FAIL：{path} 无法解析：{exc}")
        return "ERROR"


def check_registry(reg_path: Path) -> bool:
    reg = _load_registry(reg_path)
    if reg is None:
        print(f"[registry] SKIP：{reg_path.name} 不存在")
        return True
    if reg == "ERROR":
        return False

    problems = []
    for k in REGISTRY_REQUIRED_TOP_KEYS:
        if k not in reg:
            problems.append(f"顶层缺少键: {k}")
    entries = reg.get("entries", [])
    if not isinstance(entries, list):
        problems.append("entries 不是数组")
        entries = []
    seen = set()
    for idx, e in enumerate(entries):
        mid = e.get("metric_id", f"<#{idx}>")
        if mid in seen:
            problems.append(f"metric_id 重复: {mid}")
        seen.add(mid)
        for k in REQUIRED_ENTRY_KEYS:
            if k not in e:
                problems.append(f"{mid}: 缺少键 {k}")
        r = e.get("reproducibility")
        if r not in VALID_REPRODUCIBILITY:
            problems.append(f"{mid}: 非法 reproducibility={r!r}")
        if r == "verified" and not str(e.get("rerun_command", "")).strip():
            problems.append(f"{mid}: verified 但 rerun_command 为空")
    if not isinstance(reg.get("asserts"), list):
        problems.append("asserts 不是数组")

    print(f"[registry] 校验 {len(entries)} 个 entry（schema_version={reg.get('schema_version')}）")
    if problems:
        print(f"[registry] FAIL（{len(problems)} 处）:")
        for p in problems:
            print(f"    {p}")
        return False
    print("[registry] PASS：结构与条目校验通过")
    return True


# --------------------------------------------------------- 4) consistency
def check_consistency(reg_path: Path) -> bool:
    reg = _load_registry(reg_path)
    if reg is None:
        print(f"[consistency] SKIP：{reg_path.name} 不存在")
        return True
    if reg == "ERROR":
        return False

    asserts = reg.get("asserts", [])
    problems = []
    for a in asserts:
        aid = a.get("id", "<无 id>")
        rel = a.get("file", "")
        target = REPO_ROOT / rel
        if not rel or not target.exists():
            problems.append(f"{aid}: 目标文件不存在 -> {rel}")
            continue
        text = target.read_text(encoding="utf-8", errors="ignore")
        for s in a.get("must_contain", []):
            if s not in text:
                problems.append(f"{aid}: 缺少 must_contain {s!r}（{rel}）")
        for s in a.get("must_not_contain", []):
            if s in text:
                problems.append(f"{aid}: 命中 must_not_contain {s!r}（{rel}）")

    print(f"[consistency] 评估 {len(asserts)} 条断言")
    if problems:
        print(f"[consistency] FAIL（{len(problems)} 处）:")
        for p in problems:
            print(f"    {p}")
        return False
    print("[consistency] PASS：全部断言成立")
    return True


# ---------------------------------------------------------------- 5) terms
def check_terms() -> bool:
    hits = []
    scanned = 0
    for f in _walk_files(REPO_ROOT):
        if f.suffix.lower() in BINARY_EXTS:
            continue
        if f.relative_to(REPO_ROOT) in TERMS_SELF_EXCLUDE:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 二进制/非 UTF-8，跳过
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            if BLACKLIST_RE.search(line):
                hits.append((f.relative_to(REPO_ROOT), i, line.strip()))

    print(f"[terms] 扫描 {scanned} 个文本文件（黑名单：夸大/非中立宣传措辞）")
    if hits:
        print(f"[terms] FAIL：命中 {len(hits)} 处")
        for rel, i, snippet in hits:
            shown = snippet if len(snippet) <= 100 else snippet[:100] + "…"
            print(f"    {rel}:{i}  {shown}")
        return False
    print("[terms] PASS：无黑名单词命中")
    return True


CHECK_NAMES = ["links", "adr", "registry", "consistency", "terms"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可追溯性自动检查（stdlib-only）")
    for name in CHECK_NAMES:
        ap.add_argument(f"--{name}", action="store_true", help=f"仅运行 {name} 检查")
    ap.add_argument(
        "--registry-file",
        default="docs/evidence/registry.json",
        help="registry 路径（默认 docs/evidence/registry.json；用于指向临时副本做负向测试）",
    )
    args = ap.parse_args(argv)

    reg_path = _resolve_registry(args.registry_file)
    checks = [
        ("links", check_links),
        ("adr", check_adr),
        ("registry", lambda: check_registry(reg_path)),
        ("consistency", lambda: check_consistency(reg_path)),
        ("terms", check_terms),
    ]

    selected = [name for name, _ in checks if getattr(args, name.replace("-", "_"))]
    if not selected:
        selected = [name for name, _ in checks]

    results = {}
    print("=" * 56)
    for name, fn in checks:
        if name not in selected:
            continue
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 —— 单项崩溃不应掩盖其余检查
            print(f"[{name}] FAIL：检查抛出异常 {type(exc).__name__}: {exc}")
            ok = False
        results[name] = ok
        print("-" * 56)

    print("汇总：")
    for name in selected:
        print(f"  {name:<12} {'PASS' if results.get(name) else 'FAIL'}")
    failed = [n for n in selected if not results.get(n)]
    all_ok = not failed
    print("=" * 56)
    print("总结果：PASS" if all_ok else f"总结果：FAIL（未通过：{', '.join(failed)}）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
