"""广东 21 城安居客采集的每日总控 DAG（外层编排，不替换 Redis 实时派单）。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pendulum
from airflow.models import Variable
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.sensors.python import PythonSensor

from airflow import DAG

LOCAL_TZ = pendulum.timezone("Asia/Shanghai")


def _var(name: str, default: str) -> str:
    """读 Airflow Variable，元数据库抖动时回落默认值。

    Variable.get 的 default_var 只兜「变量不存在」，库连不上时仍会抛异常；
    DAG parse 期抛异常会让整个 DAG 导入失败、00:30 的调度静默错过，故这里显式兜住。
    """
    try:
        return Variable.get(name, default_var=default)
    except Exception as exc:  # noqa: BLE001 - parse 期不允许因元数据库抖动而炸掉 DAG
        logging.getLogger(__name__).warning(
            "[dag] Variable.get(%s) failed (%s), fallback to %r", name, exc, default
        )
        return default


_DAG_REPO_ROOT = Path(__file__).resolve().parents[2]

REPO_ROOT = _var("spacefin_repo_root", str(_DAG_REPO_ROOT))
VENV_DIR = _var("spacefin_venv", str(_DAG_REPO_ROOT / "tools" / "orchestrator" / ".venv"))
# 容器化部署下 Airflow 任务运行在 airflow 容器内，127.0.0.1 指向容器自身而非宿主，
# 必须用 host.docker.internal（compose 已映射 host-gateway）才能访问宿主的渲染服务(8899)
# 与采集 master(5100)。非容器化直装 Airflow 时仍应改回 http://127.0.0.1:PORT。
MASTER_URL = _var("spacefin_master_url", "http://host.docker.internal:5100")
RENDER_URL = _var("spacefin_render_url", "http://host.docker.internal:8899")
# S2 观测完备性账本（census.py）要读 Redis 里的波次快照与任务终态才有 finish_reason。
# 容器内 127.0.0.1 指向容器自身，故默认走 host.docker.internal 访问宿主常驻的
# spacefin-redis（docker run 发布 0.0.0.0:6379，不随采集栈 stop 消失）。
REDIS_HOST = _var("spacefin_redis_host", "host.docker.internal")
CRAWL_TIMEOUT_HOURS = float(_var("spacefin_crawl_timeout_hours", "6"))
DRAIN_TIMEOUT = float(_var("spacefin_drain_timeout", "900"))

# I-05 贷后保全真实推送（postloan_http 驱动）：留空则 tools/alerting 不自动启用真实通道，
# 仅靠站内告警表 + 推送日志闭环（不静默失败）。非空时经 Airflow Variable 注入子进程环境，
# 并覆盖 worker 进程的同名环境变量；留空则回落到 worker  ambient 环境（append_env）。
POSTLOAN_WEBHOOK_URL = _var("spacefin_postloan_webhook_url", "")
POSTLOAN_WEBHOOK_TOKEN = _var("spacefin_postloan_webhook_token", "")

# ---------------------------------------------------------------------------
# 故障加固（G1 通知 / G2 闸门 / G3 严格排空 / G4 预警审计）
# ---------------------------------------------------------------------------
# G1：失败通知渠道（Slack incoming-webhook）。留空则仅打 warning，不通知。
ALERT_SLACK_WEBHOOK = _var("spacefin_alert_slack_webhook", "")
# 通知提及：Slack user-group / 成员 handle，如 "<!subteam^XXX> @alice"。留空则只列四角色。
ALERT_MENTIONS = _var("spacefin_alert_mentions", "")
# G2：采集质量闸门的硬/软下限（当日新增爬取行数 first_seen_date={{ ds }}）。
MIN_CRAWL_ROWS = int(_var("spacefin_min_crawl_rows", "50"))
MIN_CRAWL_ROWS_WARN = int(_var("spacefin_min_crawl_rows_warn", "200"))
# G2 严格闸门（默认关）：低于硬下限才拦停 ETL；默认只告警放行。
GATE_STRICT = _var("spacefin_crawl_gate_strict", "").lower() in ("1", "true", "yes")
# G3 严格排空（默认关）：drain 超时即失败；默认 best-effort 放行（可能带截断行）。
DRAIN_STRICT = _var("spacefin_drain_strict", "").lower() in ("1", "true", "yes")
# 严格停栈（默认关）：关不掉采集栈即失败；默认 best-effort（只告警不阻断 ETL）。
STOP_STACK_STRICT = _var("spacefin_stop_stack_strict", "").lower() in ("1", "true", "yes")


def _resolve_python_bin() -> str:
    """解析下游脚本可用的 Python 解释器。

    仓库内的 tools/orchestrator/.venv 在部分部署（如 Airflow 容器挂载）下是符号链接，
    指向仅宿主存在的解释器（例如宿主 conda 环境里的 python），
    在容器内该目标路径不存在，直接执行会报 "No such file or directory"（exit 127）。
    故当 venv 解释器实际不可运行时，回退到 PATH 上的 python3 —— 容器镜像已预置全部
    下游依赖（pymysql/pyarrow/scipy/pymysqlreplication/scikit-learn 等），无需重建 venv。
    宿主侧 venv 可用时仍优先使用，保持兼容。

    注意：仅用 os.path.exists 判断在「挂载时有时无 / 路径存在但解释器损坏」场景下会
    误判，因此这里额外做**运行时试跑**探测，真正能起来才用 venv，否则回退 python3。
    """
    venv_py = os.path.join(VENV_DIR, "bin", "python")
    target = os.path.realpath(venv_py)
    if os.path.exists(target) and os.access(target, os.X_OK):
        try:
            subprocess.run(
                [target, "-c", "import sys; sys.exit(0)"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return venv_py
        except Exception:
            pass
    return shutil.which("python3") or "python3"


PYTHON_BIN = _resolve_python_bin()

# 下游脚本依赖自愈：Airflow 容器内 python3 虽已预置 pymysql/pyarrow 等，但 scipy /
# mysql-replication / scikit-learn 等需按需安装。best-effort，已装则秒过；
# 避免容器重建后缺失第三方包导致链路中断。宿主侧 venv 已含则同样为无操作。
# 版本须钉在 numpy<2 象限（与镜像内 pandas 2.1.4 二进制兼容），否则 scikit-learn
# 把 numpy 拉到 2.x 会让 pandas 报 "numpy.dtype size changed" 连带崩掉 etl/risk。
PY_DEP_HEAL = (
    f'"{PYTHON_BIN}" -m pip install -q --disable-pip-version-check '
    f"numpy'<2' scikit-learn'<1.6' scipy'<1.14' mysql-replication curl_cffi "
    f'|| echo "[PY_DEP_HEAL] WARN: dependency self-heal pip install failed (non-fatal, task will fail later if pkg actually missing)"; '
)

DRAIN_POLL_INTERVAL = 15
DRAIN_FLUSH_GRACE = 10


def _render_port() -> int:
    """从 spacefin_render_url 解析端口，解析不出回 8899。

    这样改一个 Variable 就同时同步 DAG 探活与 start_all.sh 起的渲染服务端口，
    不会出现两边静默错位。
    """
    try:
        port = urlparse(RENDER_URL).port
    except ValueError:
        port = None
    return port if port else 8899


DOC_MD = """
### guangdong_daily_crawl

每天 00:30（Asia/Shanghai）拉起广东 21 城安居客采集，盯到全部完成后跑收尾。

任务链：
1. `render_smoke_test` — 探活宿主渲染服务，失败即中止（Linux 宿主 Chrome 未实测，这是已知最大风险点）。
2. `start_stack` — 幂等启动采集栈（`start_all.sh`），传入 `CRAWL_RUN_ID={{ ds }}`，不阻塞等采集跑完。
3. `wait_crawl_done` — 轮询 master `GET /crawl_status`，等 `all_done=true`（波次状态机 floor->rescue->depth->done 完成，42 个任务全 finished 或 stop 置位）。
4. `drain_workers` — 等在跑的 worker 收尾（`GET /tasks` 无 `status=running`），避免 ETL 读到写了一半的 raw 行。
   默认 best-effort（超时仅告警放行）；Variable `spacefin_drain_strict=1` 时超时即失败（G3）。
4.1 `stop_crawl_stack` — 爬完即关采集栈（`stop_stack.sh` → `docker compose stop` 7 个服务）。
   采集栈是 T2 按需组件，此前爬完从不关闭，7 个容器天天驻留（实测约 2.1G）把机器拖到 OOM。
   排空后关栈是安全的：下游 14 个任务（含 etl_readiness 哨兵；排除哨兵则为 13）只碰 MySQL / 本地文件 / 腾讯 geocoder / Doris，无一依赖采集栈
   （只有 render_smoke_test / wait_crawl_done / drain_workers 需要 master 或渲染服务，都在排空前）。
   用 `stop` 而非 `down`：保留容器与日志（爬取失败的唯一现场），次日 `up -d` 秒起。
   默认 best-effort（失败只告警 + Slack，不阻断 ETL）；Variable `spacefin_stop_stack_strict=1` 时失败即拦停。
   注：`render_smoke_test` 排在 `start_stack` 之前，故次日面对的是「已停的栈」——它只把渲染服务
   `/health` 当硬门禁，master 没起走 WARN + exit 0（`render_smoke_test.sh` 已按此设计），不受影响。
   用 `trigger_rule=all_done`：关栈是收敛动作，上游排空失败/超时也必须执行，否则栈会泄漏到次日。
4.15 `crawl_census` — 观测完备性账本（S2）：跑 `census.py --date {{ ds }}`，把「今天到底扫到了
   哪几页」写成 `crawl_census` 表（每观测日每 (城,型,波,容器) 一行，`complete=1` 才可作缺席证据）。
   排在**关栈之后、ETL 之前**：raw/stats 此时已落盘且不再被写，账本先落库当日 ETL 与闸门才读得到。
   `stop_crawl_stack` 已改 `all_done`，故关栈抖动不会把账本（及下游整条日链路）一起挡住。
   默认只记账、exit 0：旧镜像或停爬日会天然全 0（42 行 `no_observation`），这是正确的
   fail-closed 答案——「今天没有完备观测」恰恰是账本要表达的结论，不是工具故障，
   故不传 `--fail-on-incomplete`（S3 强制阶段再开）。
4.25 `etl_readiness` — ETL 产物哨兵（依赖前置校验）：显式校验 `etl_finalize` 已为 `{{ ds }}`
   写入数据湖分区 `data_lake/housing/dt={{ ds }}`。把「闸门必须排在 etl 之后」这条原本*隐式*的
   顺序依赖变成*可见、失败响亮*的前置校验——一旦上游 etl 未跑/未完成，立即以明确的依赖错误
   失败（点名「闸门须在 etl_finalize 之后」），不再把「etl 没跑」误判成「爬虫 0 行」静默死锁。
   依赖缺失时不重试。
4.3 `market_state` — 房源市场状态（S3）：跑 `market_state.py --date {{ ds }}`，读当日 ETL 写入的
   `last_seen_date/last_seen_page` + `crawl_census` 的 `complete=1` 认证区间，逐 url_key 判定
   `active/absent/unknown/removed` 并写 `dwd_listing_market_state`（每 ds 每 key 一行，权威表）。
   必须排在 `etl_finalize` **之后**：判定的「今日是否出现」证据就是 etl 刚写入的 last_seen_*，
   排在 etl 之前会永久查到 0 行、把所有房源判成缺席（与闸门顺序坑同源，本项目已踩两次）。
   **影子模式**：不传 `--enforce`、`SPACEFIN_MARKET_STATE_ENFORCE=0`，只写派生表 + 日报，
   不拦停下游、不改 `crawl_quality_gate` 的 exit code。
4.5 `crawl_quality_gate` — 采集后数据质量闸门（G2）：统计当日新增爬取行数
   （`first_seen_date={{ ds }}`）及 21 城 0-row 告警。前置 `etl_readiness` 已确保 ETL 分区存在，
   故此处再遇 0 行必为「当日确无新数据」真故障，而非 etl 没跑。默认低于软/硬下限只发 Slack
   告警、放行；`spacefin_crawl_gate_strict=1` 时低于硬下限才拦停 ETL，避免空/失真数据流入全链路。
5. `etl_finalize` — 跑 `etl.py --date {{ ds }}`（跨日去重、入库、数据湖落盘）。
5.5 `geocode_fill` — 跑 `geocode_fill.py --daily-limit 6000` 填 community_coords 词典
    （腾讯 geocoder 每日 6000 配额，跑满即停；pending 状态隔日续跑，断点由 status 驱动）。
6. `geocode_backfill_finalize` — 跑 `geocode_backfill.py` 补 DWD 坐标（该脚本无 `--date` 参数）。
7. `cdc_consume` — 跑 `tools/cdc/consumer.py --once`，消费 ODS 里积压的业务变更
   （loan/collateral/customer），增量刷新 DWS/ADS。常驻服务 `spacefin-cdc-consumer` 已在做
   实时消费，这里再跑一次是**批处理时点的对齐保证**：不依赖常驻进程是否健康，DAG 自证一致。
8. `risk_recalc` — 跑 `tools/risk/main.py --write-db` 全量重算。必须排在 CDC 之后：CDC 只覆盖
   「有变更的贷款」，而 ETL 刷新的行情（DWD）影响**全部**贷款的估值，只有全量过一遍 LTV
   才跟得上新行情。两条路径共用 tools/risk/store 的写库语义，结果可互证。
   · 重算完成紧接 `risk_snapshot`（`tools/frontend/pages/p3_migration.py --snapshot --date {{ ds }}`）：把 `dws_risk_class` 当前态落成贷款级每日快照 `dws_risk_class_snapshot`（主键 snap_date+loan_id，幂等覆盖写），供 P3 迁徙矩阵/Roll Rate 使用。必须排在 risk_recalc 之后——`dws_risk_class` 是覆盖写，只有重算完成那一刻才是当日终态，提前落快照会把上一日残留当成今日分类。
9. `lake_sync` — 跑 `tools/lake/sync.py --date {{ ds }}`，把 MySQL 全量同步进 Doris（ODS/DWD/DWS/ADS
   分层）并把当日数据湖快照 `data_lake/housing/dt={{ ds }}` 上传 MinIO。必须排在 risk_recalc 之后：
   `sync.py` 的报表口径日 stat_date 取自 `ads_risk_class` 的 MAX(stat_date)，而该表由 risk_recalc
   写入。`--date` 必须与 ETL 落盘日一致（同步按 dt 分区选快照，传错会找不到目录直接失败）。
10. `alerting_push` — 跑 `tools/alerting/main.py --date {{ ds }}`（I-05 贷后保全推送）。
   必须排在 risk_recalc 之后：预警清单 `ads_ltv_alerts` 由风险引擎写入，当日预警需先生成。
   配置 `SPACEFIN_POSTLOAN_WEBHOOK_URL` 时自动追加真实贷后系统 HTTP 推送（T+1 送达闭环）；
   未配置时仅落站内告警表 + 推送日志，台账 `ads_alert_dispatch` 仍记录送达状态。
10.5 `alerting_audit` — I-05 预警终态失败审计（G4）：只读核查 `ads_alert_dispatch` 的
   `final_failed`（attempt_count≥max_retries），>0 发 Slack 告警（任务本身 exit 0，
   不标红整日 DAG；交付失败由跨天状态机兜底）。

**故障加固（G1 通知）**：任意任务失败触发 `on_failure_callback`，把失败推到
`spacefin_alert_slack_webhook`（含链路/任务/业务日/负责角色/四角色分发 产品·开发·QA·审核）。
未配置 webhook 仅 warning。各任务 `execution_timeout` 防挂死。

依赖的 Airflow Variable（均有默认值）：
`spacefin_repo_root`、`spacefin_venv`、`spacefin_master_url`、`spacefin_render_url`、
`spacefin_redis_host`（S2 账本读波次终态，默认 `host.docker.internal`）、
`spacefin_crawl_timeout_hours`、`spacefin_drain_timeout`、
`spacefin_postloan_webhook_url`（I-05 真实推送，留空则不启用）、
`spacefin_postloan_webhook_token`（I-05 推送鉴权，可选）、
`spacefin_alert_slack_webhook`（失败通知，留空仅 warning）、
`spacefin_alert_mentions`（Slack 提及，可选）、
`spacefin_min_crawl_rows`（G2 硬下限，默认 50）、
`spacefin_min_crawl_rows_warn`（G2 软下限，默认 200）、
`spacefin_crawl_gate_strict`（G2 严格拦停，默认关）、
`spacefin_drain_strict`（G3 严格排空，默认关）。
"""


def poke_crawl_done(**context) -> bool:
    """轮询 master /crawl_status；master 暂时不可用时返回 False 继续等，不让任务失败。

    必须同时校验 `run_id == {{ ds }}`：master 的 HTTP 服务先于 `_bootstrap_run` 起来，
    若 master 仍带着上一 run 的 CRAWL_RUN_ID，/crawl_status 会拿昨天的残留状态
    （42 个 finished=1 或 spacefin:stop 未清）直接回 all_done=true，把 Sensor 骗过去。
    """
    log = logging.getLogger(__name__)
    expected_run = context.get("ds")
    url = f"{MASTER_URL.rstrip('/')}/crawl_status"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - master 502/超时都只当作「还没好」
        log.warning("[wait_crawl_done] poll %s failed: %s", url, exc)
        return False

    log.info(
        "[wait_crawl_done] run_id=%s all_done=%s done_reason=%s phase=%s stop=%s "
        "finished_tasks=%s/%s finished_by_type=%s qg_consumed=%s rows=%s",
        payload.get("run_id"),
        payload.get("all_done"),
        payload.get("done_reason"),
        payload.get("phase"),
        payload.get("stop"),
        payload.get("finished_tasks"),
        payload.get("total_tasks"),
        payload.get("finished_by_type"),
        payload.get("qg_consumed"),
        payload.get("rows"),
    )

    run_id = payload.get("run_id")
    if expected_run and run_id != expected_run:
        log.warning(
            "[wait_crawl_done] run_id=%s != expected %s（master 尚未按本 run 引导），继续等",
            run_id,
            expected_run,
        )
        return False
    return bool(payload.get("all_done"))


def drain_running_workers(**context) -> None:
    """等所有 worker 把手上的任务写完，再放 ETL 进场。超时只告警，绝不失败。

    `all_done` 有两条来源：42 个任务全 finished，或 `spacefin:stop` 置位。后者是危险路径——
    STOP 一置位 /crawl_status 立刻回 true，但 worker 只在**任务之间**查 STOP，正在跑的任务
    仍在往 output/guangdong/raw/ds={{ ds }}/*.jsonl 追加，ETL 会读到截断的 JSON 行。

    判据取 `GET /tasks`：`/crawl_status` 的 cities[] 只有 finished/reason，没有逐任务 status，
    看不出谁还在 running；`/tasks` 的每项带 status，是唯一能判排空的端点。

    master 不可达时按「还没排空」处理（最终走超时分支放行）；反过来把「拿不到状态」当成
    已排空，等于把上面这个窗口又打开。
    """
    log = logging.getLogger(__name__)
    url = f"{MASTER_URL.rstrip('/')}/tasks"
    deadline = time.monotonic() + DRAIN_TIMEOUT

    while True:
        running = None
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            tasks = payload.get("tasks") or {}
            running = sorted(
                name for name, st in tasks.items() if (st or {}).get("status") == "running"
            )
        except Exception as exc:  # noqa: BLE001 - master 抖动只当作「还没排空」
            log.warning("[drain_workers] poll %s failed: %s", url, exc)

        if running is not None and not running:
            log.info("[drain_workers] 无 running 任务，已排空")
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if DRAIN_STRICT:
                # G3 严格模式：宁可失败也不带截断行进 ETL（失败由 on_failure_callback 通知）。
                raise RuntimeError(
                    f"[drain_workers] 等待 {int(DRAIN_TIMEOUT)}s 超时，仍有 running="
                    f"{running if running is not None else 'unknown(master 不可达)'}；"
                    "严格模式：存在截断行风险，任务失败"
                )
            log.warning(
                "[drain_workers] WARNING 等待 %ss 超时，仍有 running=%s；"
                "不阻断 ETL（STOP 已置位、数据基本齐全），但本 run 的 raw 尾部可能有截断行",
                int(DRAIN_TIMEOUT),
                running if running is not None else "unknown(master 不可达)",
            )
            break

        log.info(
            "[drain_workers] 仍在跑 %s，剩余等待 %ss",
            running if running is not None else "unknown(master 不可达)",
            int(remaining),
        )
        time.sleep(min(DRAIN_POLL_INTERVAL, remaining))

    # worker 写完最后一行到 flush 落盘之间有窗口，多给一点余量再交给 ETL。
    time.sleep(DRAIN_FLUSH_GRACE)


def _etl_readiness(**context) -> None:
    """ETL 产物哨兵：显式校验 crawl_quality_gate 的上游依赖已满足。

    闸门统计 crawl_housing_sale/rent 的 first_seen_date={{ ds }} 行数，而该字段由
    etl_finalize 写入；此前（一次顺序修复）闸门曾排在 etl 之前，永久查到 0 行被
    exit 2 永久拦停——「etl 没跑」与「爬虫真没采到」都表现为 0 行，顺序一颠倒就被误判成
    「爬虫空跑」死锁。本哨兵把这条*隐式*顺序依赖变成*显式、可见*的前置校验：

    etl.py 每次 finalize 都会写 data_lake/housing/dt={{ ds }}/（与新增行数无关），是
    「ETL 已为当日产出」的可靠标记。缺失即说明上游 etl_finalize 未跑/未完成，此处抢先以
    **明确的依赖错误**失败并点名「闸门必须在 etl_finalize 之后」，运维一眼可定位；
    不再静默卡死。etl 真正跑完后闸门再遇到的 0 行，才是「当日确无新数据」真故障。

    retries=0：依赖缺失时重试只会空等，立即响亮失败更利于排障。
    """
    ds = context.get("ds")
    if not ds:
        raise RuntimeError("[etl_readiness] 缺少 ds 上下文，无法校验 ETL 产物")
    # 与 etl_finalize 的写入路径保持一致：etl 以 cwd=REPO_ROOT + --lake-dir data_lake/housing
    # 写入 REPO_ROOT/data_lake/housing/dt={ds}。此前误用 _DAG_REPO_ROOT（容器内指向
    # /opt 这类安装前缀），导致哨兵永远查不到、etl_readiness 必败。见 issue 2026-08-25。
    sentinel = Path(REPO_ROOT) / "data_lake" / "housing" / f"dt={ds}"
    if not sentinel.exists():
        raise RuntimeError(
            f"[etl_readiness] ETL 产物哨兵缺失：{sentinel} 不存在；"
            f"crawl_quality_gate 必须在 etl_finalize 之后运行"
            f"（依赖 ETL 为 {ds} 写入的数据湖分区 dt={ds}）。请检查 etl_finalize 是否成功执行。"
        )
    logging.getLogger(__name__).info("[etl_readiness] ETL 产物哨兵存在，放行闸门：%s", sentinel)


# ---------------------------------------------------------------------------
# 故障通知与角色分发（G1 / G4 共用）
# ---------------------------------------------------------------------------
# 每个任务对应的主要负责角色；告警文案据此点名，并统一分发到 产品/开发/QA/审核。
TASK_OWNERS = {
    "render_smoke_test": "开发",
    "start_stack": "开发",
    "wait_crawl_done": "开发",
    "drain_workers": "开发",
    "stop_crawl_stack": "开发",
    "crawl_census": "开发/QA",
    "market_state": "开发/QA",
    "etl_readiness": "开发",
    "crawl_quality_gate": "开发",
    "etl_finalize": "开发",
    "geocode_fill": "开发",
    "geocode_backfill_finalize": "开发",
    "spatial_features": "开发",
    "cdc_consume": "开发",
    "risk_recalc": "开发/产品",
    "risk_snapshot": "开发",
    "alerting_push": "产品/审核",
    "alerting_audit": "产品/审核",
    "lake_sync": "开发/QA",
}


def _build_alert_message(
    task_id: str, ds: str, owner_roles: str, detail: str, mentions: str = ""
) -> str:
    """组装告警文案（与 tools/orchestrator/slack_notify.build_alert_message 同格式）。"""
    dist = "产品/开发/QA/审核"
    suffix = f" {mentions}" if mentions else ""
    return (
        "[SpaceFin 风控日终告警]\n"
        f"链路: guangdong_daily_crawl\n"
        f"任务: {task_id}\n"
        f"业务日: {ds}\n"
        f"负责: {owner_roles}\n"
        f"通知: {dist}{suffix}\n"
        f"详情: {detail}"
    )


def _post_slack(webhook: str, text: str) -> bool:
    """POST 告警到 Slack；任何异常吞掉返回 False（通知失败不反噬主流程）。"""
    if not webhook:
        return False
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= (resp.getcode() or 0) < 300
    except Exception:  # noqa: BLE001
        return False


# 优先复用 CLI 共享实现（同格式、stdlib only），失败则本文件内联兜底，保证 DAG parse 不崩。
try:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "orchestrator"))
    from slack_notify import build_alert_message as _build_alert_message
    from slack_notify import post_slack as _post_slack
except Exception:  # noqa: BLE001
    pass


def _alert_on_failure(context) -> None:
    """任务失败回调（G1）：把失败推到 Slack，并按任务点名负责角色。

    未配置 webhook 时仅 warning，绝不因通知失败而二次炸 DAG。
    """
    ti = context.get("task_instance")
    task_id = getattr(ti, "task_id", "?") if ti else "?"
    ds = context.get("ds", "?")
    run_id = context.get("run_id", "?")
    exc = context.get("exception")
    detail = str(exc) if exc else "（无异常详情）"
    owner = TASK_OWNERS.get(task_id, "开发/产品/QA/审核")
    webhook = _var("spacefin_alert_slack_webhook", "")
    mentions = _var("spacefin_alert_mentions", "")
    msg = _build_alert_message(task_id, ds, owner, f"[{run_id}] {detail}", mentions)
    if webhook:
        _post_slack(webhook, msg)
    else:
        logging.getLogger(__name__).warning("[alert] %s", msg)


default_args = {
    "owner": "spacefin",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "on_failure_callback": _alert_on_failure,
}

with DAG(
    dag_id="guangdong_daily_crawl",
    description="广东 21 城安居客采集每日总控（拉起 → 盯完成 → ETL → 地理补全）",
    schedule="30 0 * * *",
    start_date=pendulum.datetime(2026, 8, 1, tz=LOCAL_TZ),
    catchup=False,
    max_active_runs=1,
    # 整 run 卡死的兜底：远小于「6h 采集 + etl/risk ~2h + 其余」的合法上限，
    # 不会误杀正常长 run，但能避免某任务无超时挂死后 run 永久挂起、转 failed 后难自愈。
    dagrun_timeout=timedelta(hours=16),
    default_args=default_args,
    tags=["spacefin", "crawler"],
    doc_md=DOC_MD,
) as dag:
    # bash_command 结尾的空格是必须的：否则 Airflow 会把以 .sh 结尾的字符串当 Jinja 模板文件去加载。
    render_smoke_test = BashOperator(
        task_id="render_smoke_test",
        bash_command="bash tools/orchestrator/render_smoke_test.sh ",
        cwd=REPO_ROOT,
        env={
            "RENDER_URL": RENDER_URL,
            "MASTER_URL": MASTER_URL,  # 脚本无 SMOKE_PROXY 时向 master 要一个代理
            "SMOKE_CITY": "gz",
            "SMOKE_PAGE": "1",
        },
        append_env=True,
    )

    # 尾部 sleep：docker compose up -d 返回时 master 进程刚起，HTTP 服务先于 _bootstrap_run
    # 就绪，此刻 /crawl_status 读到的是上一 run 的残留状态。留 30s 让 leader 完成新 run 引导，
    # 避免 wait_crawl_done 的首次 poke 落进这个窗口。
    start_stack = BashOperator(
        task_id="start_stack",
        bash_command="bash tools/orchestrator/start_all.sh && sleep 30 ",
        cwd=REPO_ROOT,
        env={
            "CRAWL_RUN_ID": "{{ ds }}",
            "RENDER_PORT": str(_render_port()),
        },
        append_env=True,
    )

    wait_crawl_done = PythonSensor(
        task_id="wait_crawl_done",
        python_callable=poke_crawl_done,
        poke_interval=300,
        timeout=int(CRAWL_TIMEOUT_HOURS * 3600),
        mode="reschedule",
        soft_fail=False,
        retries=0,
    )

    # retries=0：超时分支已经是「尽力而为」的语义，重试只会再空等一遍 15min。
    drain_workers = PythonOperator(
        task_id="drain_workers",
        python_callable=drain_running_workers,
        retries=0,
    )

    # 爬完即关采集栈：T2 按需组件用完必须归还内存，否则 7 个容器天天驻留（实测约 2.1G）
    # 把 7.7G 机器拖到 OOM（2026-08-28 调度器就是在派发 spatial_features 时被 OOM 杀掉的）。
    # 默认 best-effort：docker 抖动不该连坐整条日链路，失败靠 Slack 补可见性；
    # spacefin_stop_stack_strict=1 时才让关栈失败阻断下游。
    # bash_command 结尾的空格是必须的：否则 Airflow 会把以 .sh 结尾的字符串当 Jinja 模板文件加载。
    stop_crawl_stack = BashOperator(
        task_id="stop_crawl_stack",
        bash_command=(
            f"bash tools/orchestrator/stop_stack.sh{' --strict' if STOP_STACK_STRICT else ''} "
        ),
        cwd=REPO_ROOT,
        env={
            "SPACEFIN_ALERT_SLACK_WEBHOOK": ALERT_SLACK_WEBHOOK,
            "SPACEFIN_ALERT_MENTIONS": ALERT_MENTIONS,
            "SPACEFIN_DS": "{{ ds }}",
        },
        append_env=True,
        retries=1,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=5),
        # all_done：关栈是「收敛」动作，上游 drain_workers 失败/超时也必须执行，
        # 否则采集栈会带着 7 个容器泄漏到下次调度（实测约 2.1G 常驻，是宿主 OOM 主因之一）。
        # 同理下游 crawl_census/etl 不该被一次 docker 抖动连坐挡住。
        trigger_rule="all_done",
    )

    # S2 观测完备性账本 crawl_census（census.py）：把「今天到底扫到了哪几页」落成
    # (ds, city, typ, wave, worker) 一行，是缺席（absent）判定的唯一证据源——
    # 缺了它，「这条房源今天没出现」就等于拿「我没看」当「它不在」。
    # 位置：stop_crawl_stack 之后（raw/stats 已落盘且不再被写）、etl_finalize 之前
    # （账本先落库，当日 ETL 与 crawl_quality_gate 才能读到它）。
    # 默认 exit 0 只记账：旧镜像/停爬日会天然全 0，这是正确的 fail-closed 答案而非故障，
    # 故不带 --fail-on-incomplete（需要硬拦时由 S3 阶段再打开）。
    # redis 是 census 独占依赖（读波次快照/任务终态），Airflow 容器默认没有；装不上也不致命：
    # census 会降级成「查无终态」→ complete=0，仍不向缺席判定提供任何页码证据。
    crawl_census = BashOperator(
        task_id="crawl_census",
        bash_command=(
            f'"{PYTHON_BIN}" -m pip install -q --disable-pip-version-check redis || true && '
            f'"{PYTHON_BIN}" tools/orchestrator/census.py '
            "--date {{ ds }} --out-dir output/guangdong"
        ),
        cwd=REPO_ROOT,
        env={"REDIS_HOST": REDIS_HOST},
        append_env=True,
        execution_timeout=timedelta(minutes=10),
    )

    # S3 房源市场状态（market_state.py）：读当日 ETL 刚写入的 last_seen_date/last_seen_page，
    # 叠加 crawl_census 认证区间，逐 key 判 active/absent/unknown/removed 写派生表。
    # 位置：etl_finalize 之后、crawl_quality_gate 之前。判据来源是 etl 刚写的 last_seen_*，
    # 排在 etl 之前会永久查到 0 行、把「没入库」误判成「全部缺席」——与闸门顺序坑同源
    # （2026-08-24 修过一次顺序倒置）。
    # 影子模式：不传 --enforce 且显式置 SPACEFIN_MARKET_STATE_ENFORCE=0，只写派生表 + 日报，
    # 不拦停下游、不改动 crawl_quality_gate 的 exit code（S3 强制阶段再打开）。
    market_state = BashOperator(
        task_id="market_state",
        bash_command=f'"{PYTHON_BIN}" tools/orchestrator/market_state.py --date {{{{ ds }}}}',
        cwd=REPO_ROOT,
        env={"SPACEFIN_MARKET_STATE_ENFORCE": "0"},
        append_env=True,
        execution_timeout=timedelta(minutes=15),
    )

    # ETL 产物哨兵：在闸门之前显式校验 etl_finalize 已为 {{ ds }} 写入数据湖分区，
    # 把「闸门必须排在 etl 之后」这条隐式依赖变成可见、失败响亮的前置校验
    # （详见 _etl_readiness 注释）。依赖缺失时不重试、立即明确失败。
    etl_readiness = PythonOperator(
        task_id="etl_readiness",
        python_callable=_etl_readiness,
        retries=0,
        execution_timeout=timedelta(minutes=5),
    )

    etl_finalize = BashOperator(
        task_id="etl_finalize",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/orchestrator/etl.py '
            "--date {{ ds }} "
            "--raw-dir output/guangdong/raw "
            "--lake-dir data_lake/housing"
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=60),
    )

    # G2 采集后数据质量闸门：**必须排在 etl_finalize 之后**（2026-08-24 修复顺序倒置）。
    # 闸门查 crawl_housing_sale/rent 的 first_seen_date={{ ds }} 行数，而该字段由 etl 写入；
    # 若排在 etl 之前，全新 ds 会因 etl 尚未入库而永远查到 0 行、被 exit 2 永久拦停（链路口死锁）。
    # 排在 etl 之后，闸门才是在校验"当日 ETL 入库后是否有足够新增数据"，语义正确。
    # 前置 etl_readiness 已显式校验 ETL 分区存在，故闸门在此再遇 0 行必为「当日确无新数据」
    # 真故障（而非 etl 没跑），不会再被误判成爬虫空跑死锁。
    # 默认只告警不挡（GATE_STRICT 关）；开启后低于硬下限才拦停下游。
    crawl_quality_gate = BashOperator(
        task_id="crawl_quality_gate",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/orchestrator/crawl_quality_gate.py '
            f"--date {{{{ ds }}}} --hard-floor {MIN_CRAWL_ROWS} "
            f"--soft-floor {MIN_CRAWL_ROWS_WARN}"
            f"{' --strict' if GATE_STRICT else ''}"
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=15),
        env={
            "SPACEFIN_ALERT_SLACK_WEBHOOK": ALERT_SLACK_WEBHOOK,
            "SPACEFIN_ALERT_MENTIONS": ALERT_MENTIONS,
        },
        append_env=True,
    )

    # 腾讯 geocoder 每日 6000 配额：--daily-limit 跑满即停，pending 状态保留到次日续跑（隔日补全）。
    # 放在 etl_finalize 之后（DWD 有 pending 行）、geocode_backfill_finalize 之前（词典先填好）。
    geocode_fill = BashOperator(
        task_id="geocode_fill",
        bash_command=f'{PY_DEP_HEAL}"{PYTHON_BIN}" tools/orchestrator/geocode_fill.py --daily-limit 6000',
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=30),
    )

    geocode_backfill_finalize = BashOperator(
        task_id="geocode_backfill_finalize",
        bash_command=f'{PY_DEP_HEAL}"{PYTHON_BIN}" tools/orchestrator/geocode_backfill.py',
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=30),
    )

    # P5 空间特征：依赖地理编码坐标（dws_spatial_feature / ads_spatial_zone 落库）。
    # 放 geocode_backfill_finalize 之后（坐标已就绪）、cdc_consume 之前，与相邻 python 步骤一致：
    # cwd=REPO_ROOT，由脚本自行读仓库根 .env 连库（无需显式传 MYSQL_* env）。
    spatial_features = BashOperator(
        task_id="spatial_features",
        bash_command=f'{PY_DEP_HEAL}"{PYTHON_BIN}" tools/spatial/main.py --once',
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=30),
    )

    # --batch 大于常驻消费者：DAG 这一趟要能把整夜积压一次吃完，不留尾巴给下一个调度周期。
    cdc_consume = BashOperator(
        task_id="cdc_consume",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/cdc/consumer.py --once --date {{{{ ds }}}} --batch 5000'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=30),
    )

    risk_recalc = BashOperator(
        task_id="risk_recalc",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/risk/main.py --date {{{{ ds }}}} --out-dir output/risk --write-db'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=60),
    )

    # 风险重算后落贷款级每日快照，供 P3 迁徙矩阵/Roll Rate 使用。
    # 必须在 risk_recalc 之后：dws_risk_class 是覆盖写，仅重算完成那一刻为当日终态。
    # 显式传 --date {{ ds }} 与 risk_recalc 对齐，使 snap_date 与 ads_risk_class.stat_date 一致。
    # 幂等：同一天重跑覆盖而非堆重复行（ON DUPLICATE KEY UPDATE）。
    risk_snapshot = BashOperator(
        task_id="risk_snapshot",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/frontend/pages/p3_migration.py --snapshot --date {{{{ ds }}}}'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=15),
    )

    # 排 risk_recalc 之后：当日 ads_ltv_alerts 已生成。配置 spacefin_postloan_webhook_url
    # （Airflow Variable）即自动启用真实贷后系统推送；留空则仅靠站内告警表 + 推送日志闭环。
    # 仅在 Variable 非空时显式注入，避免覆盖 worker ambient 环境里已设的同名变量。
    alerting_env = {}
    if POSTLOAN_WEBHOOK_URL:
        alerting_env["SPACEFIN_POSTLOAN_WEBHOOK_URL"] = POSTLOAN_WEBHOOK_URL
        if POSTLOAN_WEBHOOK_TOKEN:
            alerting_env["SPACEFIN_POSTLOAN_WEBHOOK_TOKEN"] = POSTLOAN_WEBHOOK_TOKEN
    alerting_push = BashOperator(
        task_id="alerting_push",
        bash_command=(
            f"{PY_DEP_HEAL}"
            f'"{PYTHON_BIN}" tools/alerting/main.py --date {{{{ ds }}}} --out-dir output/alerting'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=30),
        env=alerting_env,
        append_env=True,
    )

    # G4 预警终态失败审计：推送后只读核查 ads_alert_dispatch 的 final_failed，
    # >0 发 Slack 告警（任务本身 exit 0，不标红整日 DAG——交付失败由跨天状态机兜底）。
    alerting_audit = BashOperator(
        task_id="alerting_audit",
        bash_command=(
            f'{PY_DEP_HEAL}"{PYTHON_BIN}" tools/alerting/audit_final_failed.py --date {{{{ ds }}}}'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=15),
        env={
            "SPACEFIN_ALERT_SLACK_WEBHOOK": ALERT_SLACK_WEBHOOK,
            "SPACEFIN_ALERT_MENTIONS": ALERT_MENTIONS,
        },
        append_env=True,
    )

    # --date 必须与 etl_finalize 落盘日一致：sync.py 按 dt= 分区选当日数据湖快照上传 MinIO，
    # 传错会找不到目录直接失败。Doris 全量同步幂等（TRUNCATE 重灌），可安全每日执行。
    #
    # 链路修正（2026-08-23）：Doris/MinIO 在 deploy/doris-minio 里用 network_mode: host，
    # 在 Docker Desktop + WSL2 下只绑在 Docker Desktop VM 的 127.0.0.1，airflow 容器（WSL VM
    # 另一网络）经 host.docker.internal 也连不到。故改为在 spacefin-doris-fe 容器内执行
    # sync.py——该容器与 Doris/MinIO 同 host 网络，127.0.0.1 可达；仓库已挂载到其 /opt/spacefin。
    # MySQL 经 host.docker.internal 走 Windows 宿主发布端口可达（已实测）。doris-fe 重启不影响
    # BE 与数据（元数据在 /srv/spacefin-lake 持久化）。
    lake_sync = BashOperator(
        task_id="lake_sync",
        bash_command=(
            "docker exec spacefin-doris-fe bash -c "
            "'cd /opt/spacefin/tools/lake && "
            # doris-fe 容器重建会丢 pip 包，先自愈依赖（root 免 sudo，apt 源可用）
            "python3 -m pip install -q pymysql requests 2>/dev/null || true && "
            "MYSQL_HOST=host.docker.internal "
            "python3 sync.py --date {{ ds }}'"
        ),
        cwd=REPO_ROOT,
        retries=2,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=30),
    )

    (
        render_smoke_test
        >> start_stack
        >> wait_crawl_done
        >> drain_workers
        >> stop_crawl_stack
        >> crawl_census
        >> etl_finalize
        >> market_state
        >> etl_readiness
        >> crawl_quality_gate
        >> geocode_fill
        >> geocode_backfill_finalize
        >> spatial_features
        >> cdc_consume
        >> risk_recalc
        >> risk_snapshot
        >> alerting_push
        >> alerting_audit
        >> lake_sync
    )
