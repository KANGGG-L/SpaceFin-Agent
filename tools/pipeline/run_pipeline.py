#!/usr/bin/env python
"""SpaceFin 统一数据链入口：ETL → CDC 消费 → 风险重算，一条命令跑完。

为什么要这层收口：S1 的三段能力（tools/orchestrator/etl.py 采集侧 ETL、tools/cdc CDC、
tools/risk 风险引擎）此前各有各的入口，调度方要自己记住顺序、参数与失败语义。顺序一旦
写错（例如 CDC 消费跑在 ETL 之前），风险重算读到的还是旧行情，结果对不上但不报错——
这类静默错误比崩溃更难查。本模块把顺序与依赖固化下来，只暴露一个入口。

阶段顺序（不可随意调换，理由见各 step 注释）：
    1. etl      采集 raw → DWD（行情侧刷新）
    2. geocode  DWD 坐标补全（可选，--skip-geocode 关闭）
    3. cdc      消费 ODS 变更 → DWS/ADS 增量同步
    4. risk     风险全量重算（对账兜底；--skip-risk 可只跑增量）
    5. snapshot 五级分类每日快照（迁徙矩阵的历史来源；--skip-snapshot 关闭）
    6. g11      1104 G11 资产质量报送（三出口校验 + 模板落库 + CSV/JSON 导出）
    7. alert    LTV 预警推送（T+1 去重 + 失败重试状态机；驱动由 SPACEFIN_ALERT_DRIVER 指定）

用法：
    # 每日完整链（Airflow / 手动）
    python tools/pipeline/run_pipeline.py --date 2026-08-05

    # 只跑「变更消费 + 风险」，不动采集侧（业务库改动后快速刷新下游）
    python tools/pipeline/run_pipeline.py --only cdc,risk

    # 演练：只打印将要执行的命令
    python tools/pipeline/run_pipeline.py --dry-run

退出码：任一阶段失败即非 0（--keep-going 时改为「跑完全部再汇总失败」）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 唯一 Python 环境：tools/orchestrator/.venv（conda spark 的别名）。
# 用 sys.executable 而非硬编码路径——被 Airflow/systemd 以绝对路径调起时自然继承同一解释器。
PYTHON_BIN = sys.executable

sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "risk"))
import config  # noqa: E402  (tools/risk 模块，须在 sys.path 注入之后导入)

STEP_ORDER = ["etl", "geocode", "cdc", "risk", "snapshot", "g11", "alert"]


def _steps(args) -> list[str]:
    """解析要跑哪些阶段：--only 优先，其次按 --skip-* 过滤。"""
    if args.only:
        want = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [s for s in want if s not in STEP_ORDER]
        if bad:
            raise SystemExit(f"[pipeline] 未知阶段 {bad}，可选：{STEP_ORDER}")
        # 按固定顺序执行，忽略用户给的书写顺序——顺序是数据依赖，不该由调用方决定。
        return [s for s in STEP_ORDER if s in want]
    skip = set()
    if args.skip_etl:
        skip |= {"etl"}
    if args.skip_geocode:
        skip |= {"geocode"}
    if args.skip_cdc:
        skip |= {"cdc"}
    if args.skip_risk:
        skip |= {"risk"}
    if args.skip_snapshot:
        skip |= {"snapshot"}
    if args.skip_g11:
        skip |= {"g11"}
    if args.skip_alert:
        skip |= {"alert"}
    return [s for s in STEP_ORDER if s not in skip]


def _build_cmd(step: str, args) -> list[str]:
    if step == "etl":
        return [
            PYTHON_BIN,
            "tools/orchestrator/etl.py",
            "--date",
            args.date,
            "--raw-dir",
            args.raw_dir,
            "--lake-dir",
            args.lake_dir,
        ]
    if step == "geocode":
        # geocode_backfill 无 --date 参数（按 DWD 现状补全），与 DAG 里的用法保持一致。
        return [PYTHON_BIN, "tools/orchestrator/geocode_backfill.py"]
    if step == "cdc":
        # --once：跑完当前积压即退出。常驻实时消费由 systemd spacefin-cdc-consumer 负责，
        # 这里再跑一次是为了保证「批处理时点上下游一定对齐」，不依赖常驻进程是否健康。
        return [
            PYTHON_BIN,
            "tools/cdc/consumer.py",
            "--once",
            "--date",
            args.date,
            "--batch",
            str(args.cdc_batch),
        ]
    if step == "risk":
        # 全量重算放在 CDC 之后：CDC 只覆盖「有变更的贷款」，行情（DWD）刷新影响的是全部
        # 贷款的估值，必须全量过一遍才能让 LTV 跟上新行情。两者写库语义共用 tools/risk/store。
        cmd = [PYTHON_BIN, "tools/risk/main.py", "--date", args.date, "--out-dir", args.risk_out]
        if not args.risk_dry_run:
            cmd.append("--write-db")
        return cmd
    if step == "snapshot":
        # 必须排在 risk 之后：dws_risk_class 是主键覆盖写的「当前态」，risk 重算完成的那一刻
        # 才是当日终态。早于 risk 拍快照会把昨天的分类记成今天的，迁徙矩阵直接失真。
        return [
            PYTHON_BIN,
            "tools/frontend/pages/p3_migration.py",
            "--snapshot",
            "--date",
            args.date,
        ]
    if step == "g11":
        # 报送必须排在 risk 之后：ads_risk_class 五级汇总是 risk 写好的产物，顺序错了
        # 会拿到昨天或空的汇总；三出口校验不过时 g11 自带 exit 非 0，pipeline 自然中断。
        return [PYTHON_BIN, "tools/reporting/g11_report.py", "--date", args.date]
    if step == "alert":
        # 推送排在 risk 之后：ads_ltv_alerts 预警表由 risk 写当日行，T+1 报送昨日预警；
        # 驱动（station 站内表 / file 联调文件）由 SPACEFIN_ALERT_DRIVER 或 --alert-driver 指定。
        cmd = [PYTHON_BIN, "tools/alerting/dispatch.py", "--date", args.date]
        if args.alert_driver:
            cmd.extend(["--driver", args.alert_driver])
        return cmd
    raise SystemExit(f"[pipeline] 未知阶段 {step}")


def run_step(step: str, args) -> dict:
    cmd = _build_cmd(step, args)
    printable = " ".join(cmd)
    print(f"\n[pipeline] ==== {step} ====\n[pipeline] $ {printable}", flush=True)
    if args.dry_run:
        return {"step": step, "cmd": printable, "skipped": "dry-run"}
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)  # noqa: S603 - 命令由本文件构造，无外部注入
    dur = round(time.time() - t0, 1)
    ok = proc.returncode == 0
    print(f"[pipeline] {step} {'OK' if ok else 'FAILED'} rc={proc.returncode} {dur}s", flush=True)
    return {"step": step, "rc": proc.returncode, "ok": ok, "seconds": dur}


def main() -> None:
    ap = argparse.ArgumentParser(description="SpaceFin 统一数据链：ETL → CDC 消费 → 风险重算")
    # 业务日期按 Asia/Shanghai（config.business_date），与 Airflow {{ ds }} 的时区口径一致
    ap.add_argument("--date", default=config.business_date(), help="业务日期 YYYY-MM-DD")
    ap.add_argument("--raw-dir", default="output/guangdong/raw")
    ap.add_argument("--lake-dir", default="data_lake/housing")
    ap.add_argument("--risk-out", default="output/risk")
    ap.add_argument("--cdc-batch", type=int, default=2000, help="单次 CDC 消费的最大事件数")
    ap.add_argument("--only", default=None, help=f"只跑指定阶段（逗号分隔），可选 {STEP_ORDER}")
    ap.add_argument("--skip-etl", action="store_true")
    ap.add_argument("--skip-geocode", action="store_true")
    ap.add_argument("--skip-cdc", action="store_true")
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--skip-g11", action="store_true")
    ap.add_argument("--skip-alert", action="store_true")
    ap.add_argument(
        "--alert-driver",
        default=None,
        help="预警推送驱动（station/db/file），缺省由 SPACEFIN_ALERT_DRIVER 决定",
    )
    ap.add_argument("--risk-dry-run", action="store_true", help="风险阶段只落 CSV 不写库")
    ap.add_argument("--keep-going", action="store_true", help="某阶段失败仍继续后续阶段")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    args = ap.parse_args()

    steps = _steps(args)
    print(f"[pipeline] date={args.date} steps={steps}", flush=True)
    t0 = time.time()
    results = []
    for step in steps:
        res = run_step(step, args)
        results.append(res)
        if not args.dry_run and not res.get("ok") and not args.keep_going:
            print(f"[pipeline] 在 {step} 中止（用 --keep-going 可继续后续阶段）", flush=True)
            break

    failed = [r["step"] for r in results if r.get("ok") is False]
    summary = {
        "date": args.date,
        "steps": results,
        "failed": failed,
        "total_seconds": round(time.time() - t0, 1),
    }
    print(f"\n[pipeline] {json.dumps(summary, ensure_ascii=False)}", flush=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
