#!/usr/bin/env python
"""幂等生成 P7 归因产物体（rows.json + attribution.json），并接入 make ready。

背景：归因区块（前端 /api/avm/attribution）依赖 output/avm/bias_diag/ 的两个
离线产物。但 output/ 整目录被 .gitignore 排除，换机 clone 后不会自带；这两个
产物体又随 AVM 模型重训（avm_report.json 的 version/trained_at 变化）而需要重建，
否则页面与实时库（dws_risk_class 已在用新模型估值）口径不一致。

本脚本解决两件事（幂等 + 时序）：
  1. 时序：归因分解依赖 dws_risk_class（风险引擎 T+1 批处理产出的估值），
     而 dws 又依赖 seed/爬虫——所以不能在 seed 初始化期跑，须在风险引擎之后。
  2. 幂等：把「AVM 模型版本指纹」与产物绑定。指纹未变且产物存在 → 直接跳过；
     指纹变了或产物缺失 → 依次重建 rows.json（regenerate_bias_diag.py）与
     attribution.json（bias_attribution.py）。

通过 make ready 调用（见 Makefile），保证「机制化」：不必记得手动跑两步，
换环境/重训后只需 make ready 一次即可自愈。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AVM_REPORT = os.path.join(_REPO, "output", "avm", "avm_report.json")
_DIAG = os.path.join(_REPO, "output", "avm", "bias_diag")
_FINGERPRINT = os.path.join(_DIAG, ".fingerprint.json")

# rows.json 必须包含的四分量（前端 _diag_rows 的判定字段）
_REQUIRED_KEYS = ("A1", "A2", "B", "logdev")


def _load_report():
    """读 AVM 训练报告；缺失/损坏返回 None（首次部署尚未训练也属正常路径）。"""
    try:
        with open(_AVM_REPORT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _current_fingerprint():
    """当前模型指纹 = version + trained_at（报告里两个字段不变即视为同模型）。"""
    rep = _load_report()
    if not rep:
        return None
    return {"model_version": rep.get("version"), "trained_at": rep.get("trained_at")}


def _load_fingerprint():
    try:
        with open(_FINGERPRINT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _artifacts_valid():
    rows = os.path.join(_DIAG, "rows.json")
    att = os.path.join(_DIAG, "attribution.json")
    if not (os.path.exists(rows) and os.path.exists(att)):
        return False
    # rows.json 必须是 list 且字段齐全，否则即便存在也不能用（版本对不上）
    try:
        with open(rows, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, list) and any(
        isinstance(r, dict) and all(r.get(k) is not None for k in _REQUIRED_KEYS) for r in data
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="幂等生成 P7 归因产物体（根据 AVM 模型指纹跳过/重建）")
    ap.add_argument("--force", action="store_true", help="忽略指纹，强制重建两个产物")
    ap.add_argument("--python", default=sys.executable, help="运行子脚本的 Python 解释器")
    args = ap.parse_args()

    fp = _current_fingerprint()
    if fp is None:
        print(
            "[gen_attribution] 未找到 avm_report.json（模型尚未训练？），归因产物保持缺失，跳过。"
        )
        return 0

    prev = _load_fingerprint()
    up_to_date = (
        not args.force
        and prev is not None
        and prev.get("model_version") == fp["model_version"]
        and prev.get("trained_at") == fp["trained_at"]
        and _artifacts_valid()
    )
    if up_to_date:
        print(
            f"[gen_attribution] 归因产物已对应模型 {fp['model_version']} "
            f"（{fp['trained_at']}），无需重建。"
        )
        return 0

    print(f"[gen_attribution] 重建归因产物（模型 {fp['model_version']} @ {fp['trained_at']}）…")
    scripts = [
        os.path.join(_REPO, "tools", "avm", "regenerate_bias_diag.py"),
        os.path.join(_REPO, "tools", "avm", "bias_attribution.py"),
    ]
    for script in scripts:
        if not os.path.exists(script):
            print(f"[gen_attribution] 缺少脚本 {script}，中止。", file=sys.stderr)
            return 1
        print(f"  执行 {os.path.basename(script)}")
        subprocess.run([args.python, script], cwd=_REPO, check=True)

    os.makedirs(_DIAG, exist_ok=True)
    tmp = _FINGERPRINT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {**fp, "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")},
            f,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(tmp, _FINGERPRINT)
    print(f"[gen_attribution] 完成，已记录指纹 {fp['model_version']} -> {_FINGERPRINT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
