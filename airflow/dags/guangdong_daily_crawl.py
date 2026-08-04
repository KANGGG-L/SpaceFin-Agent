"""广东 21 城安居客采集的每日总控 DAG（外层编排，不替换 Redis 实时派单）。"""

from __future__ import annotations

import json
import logging
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
3. `wait_crawl_done` — 轮询 master `GET /crawl_status`，等 `all_done=true`（42 个任务全 finished 或 stop 置位）。
4. `drain_workers` — 等在跑的 worker 收尾（`GET /tasks` 无 `status=running`），避免 ETL 读到写了一半的 raw 行。
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

依赖的 Airflow Variable（均有默认值）：
`spacefin_repo_root`、`spacefin_venv`、`spacefin_master_url`、`spacefin_render_url`、
`spacefin_crawl_timeout_hours`、`spacefin_drain_timeout`。
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


default_args = {
    "owner": "spacefin",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
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
    )

    (
        render_smoke_test
        >> start_stack
        >> wait_crawl_done
        >> drain_workers
        >> etl_finalize
        >> geocode_fill
        >> geocode_backfill_finalize
        >> cdc_consume
        >> risk_recalc
    )
