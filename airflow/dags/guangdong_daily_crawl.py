"""广东 21 城安居客采集的每日总控 DAG（外层编排，不替换 Redis 实时派单）。"""

from __future__ import annotations

import json
import logging
import os
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
MASTER_URL = _var("spacefin_master_url", "http://127.0.0.1:5100")
RENDER_URL = _var("spacefin_render_url", "http://127.0.0.1:8899")
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

PYTHON_BIN = f"{VENV_DIR}/bin/python"

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
4.5 `crawl_quality_gate` — 采集后数据质量闸门（G2）：统计当日新增爬取行数
   （`first_seen_date={{ ds }}`）及 21 城 0-row 告警。默认低于软/硬下限只发 Slack 告警、放行；
   `spacefin_crawl_gate_strict=1` 时低于硬下限才拦停 ETL，避免空/失真数据流入全链路。
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
    仍在往 output/guangdong/raw/*.jsonl 追加，ETL 会读到截断的 JSON 行。

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


# ---------------------------------------------------------------------------
# 故障通知与角色分发（G1 / G4 共用）
# ---------------------------------------------------------------------------
# 每个任务对应的主要负责角色；告警文案据此点名，并统一分发到 产品/开发/QA/审核。
TASK_OWNERS = {
    "render_smoke_test": "开发",
    "start_stack": "开发",
    "wait_crawl_done": "开发",
    "drain_workers": "开发",
    "crawl_quality_gate": "开发",
    "etl_finalize": "开发",
    "geocode_fill": "开发",
    "geocode_backfill_finalize": "开发",
    "cdc_consume": "开发",
    "risk_recalc": "开发/产品",
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

    etl_finalize = BashOperator(
        task_id="etl_finalize",
        bash_command=(
            f'"{PYTHON_BIN}" tools/orchestrator/etl.py '
            "--date {{ ds }} "
            "--raw-dir output/guangdong/raw "
            "--lake-dir data_lake/housing"
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=60),
    )

    # G2 采集后数据质量闸门：drain 完成、raw 稳定后，统计当日新增爬取行数。
    # 默认只告警不挡（GATE_STRICT 关）；开启后低于硬下限才拦停 ETL。
    crawl_quality_gate = BashOperator(
        task_id="crawl_quality_gate",
        bash_command=(
            f'"{PYTHON_BIN}" tools/orchestrator/crawl_quality_gate.py '
            f"--date {{{{ ds }}}} --hard-floor {MIN_CRAWL_ROWS} "
            f"--soft-floor {MIN_CRAWL_ROWS_WARN}"
            f"{' --strict' if GATE_STRICT else ''}"
        ),
        cwd=REPO_ROOT,
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
        bash_command=f'"{PYTHON_BIN}" tools/orchestrator/geocode_fill.py --daily-limit 6000',
        cwd=REPO_ROOT,
    )

    geocode_backfill_finalize = BashOperator(
        task_id="geocode_backfill_finalize",
        bash_command=f'"{PYTHON_BIN}" tools/orchestrator/geocode_backfill.py',
        cwd=REPO_ROOT,
    )

    # --batch 大于常驻消费者：DAG 这一趟要能把整夜积压一次吃完，不留尾巴给下一个调度周期。
    cdc_consume = BashOperator(
        task_id="cdc_consume",
        bash_command=(
            f'"{PYTHON_BIN}" tools/cdc/consumer.py --once --date {{{{ ds }}}} --batch 5000'
        ),
        cwd=REPO_ROOT,
    )

    risk_recalc = BashOperator(
        task_id="risk_recalc",
        bash_command=(
            f'"{PYTHON_BIN}" tools/risk/main.py --date {{{{ ds }}}} --out-dir output/risk --write-db'
        ),
        cwd=REPO_ROOT,
        execution_timeout=timedelta(minutes=60),
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
            f'"{PYTHON_BIN}" tools/alerting/main.py --date {{{{ ds }}}} --out-dir output/alerting'
        ),
        cwd=REPO_ROOT,
        env=alerting_env,
        append_env=True,
    )

    # G4 预警终态失败审计：推送后只读核查 ads_alert_dispatch 的 final_failed，
    # >0 发 Slack 告警（任务本身 exit 0，不标红整日 DAG——交付失败由跨天状态机兜底）。
    alerting_audit = BashOperator(
        task_id="alerting_audit",
        bash_command=(f'"{PYTHON_BIN}" tools/alerting/audit_final_failed.py --date {{{{ ds }}}}'),
        cwd=REPO_ROOT,
        env={
            "SPACEFIN_ALERT_SLACK_WEBHOOK": ALERT_SLACK_WEBHOOK,
            "SPACEFIN_ALERT_MENTIONS": ALERT_MENTIONS,
        },
        append_env=True,
    )

    # --date 必须与 etl_finalize 落盘日一致：sync.py 按 dt= 分区选当日数据湖快照上传 MinIO，
    # 传错会找不到目录直接失败。Doris 全量同步幂等（TRUNCATE 重灌），可安全每日执行。
    lake_sync = BashOperator(
        task_id="lake_sync",
        bash_command=(f'"{PYTHON_BIN}" tools/lake/sync.py --date {{{{ ds }}}}'),
        cwd=REPO_ROOT,
        retries=1,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=30),
    )

    (
        render_smoke_test
        >> start_stack
        >> wait_crawl_done
        >> drain_workers
        >> crawl_quality_gate
        >> etl_finalize
        >> geocode_fill
        >> geocode_backfill_finalize
        >> cdc_consume
        >> risk_recalc
        >> alerting_push
        >> alerting_audit
        >> lake_sync
    )
