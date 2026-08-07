# 组件技术说明 · Airflow 每日采集总控（外层编排）

> **状态**：✅ 本机 Linux 部署运行中（scheduler + webserver 常驻，linger）
> **能力地图层级**：采集链路的「总控层」——把采集栈的拉起、盯完成、收尾 ETL、地理补全串成每日一次的可观测流水线
> **引入原则**：只做「拉起 + 盯完成 + 收尾」，**不替换** master/worker 的 Redis 实时派单

---

## 1. 为何引入

采集链路本身已有完整闭环：orchestrator（master 派单 + worker 抓取）跑在 Docker 上，
失败自动重试，无需外部介入。缺的只是**定时总控与收尾**：

1. **定时拉起**：每天要有人把采集栈拉起、确认渲染服务活着、为当天 run 注入
   `CRAWL_RUN_ID={{ ds }}`；
2. **盯完成**：42 个城市任务何时全 `finished` 是未知的，需要一个带超时（默认 6h）的等待者；
3. **收尾**：采集完成后顺序执行 ETL 入库、地理补全、CDC 消费、风险重算——当天行情闭环的
   最后一公里。

Airflow 提供 DAG（调度 + 依赖 + 重试 + Sensor）+ Web UI，正好覆盖「每日一次、线性依赖、
需盯状态」的外层编排，比 cron + 自拼状态机更可观测：每次 run 的 task 状态、日志、重试都在 UI 里。

## 2. 生产对应物（诚实标注）

| 本项目（本机实际部署） | 生产对应物 |
|---|---|
| venv `~/airflow-venv` + SQLite + SequentialExecutor | 独立 Airflow 集群 + Postgres 元数据库 + CeleryExecutor |
| systemd 用户单元常驻 | k8s / 托管平台上的 Airflow 托管 |
| 单机（scheduler 与采集栈同宿主） | 多节点（调度 / worker / 元数据库分离） |

> ⚠️ 与 `airflow/README.md` §1 的差异：README 推荐 `/opt/airflow` + Postgres + LocalExecutor
> （理由是 Airflow 2.x 对 SQLite + LocalExecutor 是硬报错，scheduler 起不来）。**本机实际部署是
> SQLite + SequentialExecutor**——SequentialExecutor 与 SQLite 兼容，本 DAG 任务本就线性执行，
> 串行化无损失。本文档按实际部署写，README 未同步此差异。

## 3. 架构与流程

```
  systemd 用户单元（linger 常驻）
  ├─ airflow-scheduler.service ──▶ 00:30 触发 guangdong_daily_crawl
  └─ airflow-webserver.service ──▶ :8080 Web UI
          │
          ▼
  DAG 任务链（8 步线性，guangdong_daily_crawl.py:296-306）：
  render_smoke_test ─▶ start_stack ─▶ wait_crawl_done ─▶ drain_workers
        ─▶ etl_finalize ─▶ geocode_fill ─▶ geocode_backfill_finalize
        ─▶ cdc_consume ─▶ risk_recalc
```

- **render_smoke_test**：探活宿主渲染服务，取到 `zu-itemmod` 才放行；失败即中止整条链
  （Linux 宿主 Chrome 反爬是最大风险点，先于装 Airflow 必须过 `render_smoke_test.sh`，见 README §4）。
- **start_stack**：`start_all.sh && sleep 30`，幂等拉起 Docker 采集栈，注入 `CRAWL_RUN_ID={{ ds }}`；
  sleep 是等 leader 完成新 run 引导，避免 `wait_crawl_done` 首次 poke 落进上一 run 残留状态窗口。
- **wait_crawl_done**：PythonSensor（`mode=reschedule`，运行中不占 slot），每 300s 轮询 master
  `GET /crawl_status`，`run_id=={{ ds }}` 且 `all_done=true` 才放行；超时上限默认 6h
  （`spacefin_crawl_timeout_hours`）；master 不可用按「还没好」处理，不失败。
- **drain_workers**：轮询 `GET /tasks`（而非 /crawl_status——它只有 finished/reason，看不出逐任务
  running），等无 `status=running` 后再等 10s flush 余量，避免 ETL 读到写了一半的 raw JSON 行；
  900s 超时（`spacefin_drain_timeout`）只告警不失败，`retries=0`。
- **etl_finalize**：`etl.py --date {{ ds }}` 跨日去重、入库、数据湖落盘。
- **geocode_fill**：`geocode_fill.py --daily-limit 6000` 填 community_coords 词典，腾讯 geocoder
  每日配额跑满即停，pending 状态隔日续跑。
- **geocode_backfill_finalize**：`geocode_backfill.py` 补 DWD 坐标（无 `--date`，扫全表属正常）。
- **cdc_consume**：`consumer.py --once --date {{ ds }} --batch 5000`，消费 ODS 积压业务变更，
  增量刷新 DWS/ADS——批处理时点对齐保证，不依赖常驻 consumer 健康。
- **risk_recalc**：`main.py --date {{ ds }} --write-db` 全量重算 LTV。必须排在 CDC 之后：CDC 只覆盖
  「有变更的贷款」，而 ETL 刷新的行情影响**全部**贷款估值，只有全量过一遍才跟得上。

DAG 参数：`schedule="30 0 * * *"`（Asia/Shanghai，每日 00:30）、`catchup=False`、
`max_active_runs=1`、`start_date=2026-08-01`、默认 `retries=2 / retry_delay=5min`
（Sensor 与 drain 单独 `retries=0`）。

## 4. 部署形态（本机 Linux，实际运行）

| 项 | 值 |
|---|---|
| venv | `~/airflow-venv`（apache-airflow 2.10.5，Python 3.12） |
| AIRFLOW_HOME | `~/airflow`（airflow.cfg / airflow.db / logs） |
| 元数据库 | SQLite（`sqlite:////home/azureuser/airflow/airflow.db`） |
| 执行器 | SequentialExecutor（SQLite 兼容；LocalExecutor 配 SQLite 是硬报错） |
| dags_folder | `/home/azureuser/SpaceFin-Agent/airflow/dags` |
| systemd | 用户单元 `airflow-scheduler` / `airflow-webserver`（`~/.config/systemd/user/`，linger 常驻） |
| webserver | `:8080` |

单元关键点（`~/.config/systemd/user/airflow-{scheduler,webserver}.service`，仓库外）：
- `Environment=PATH=/home/azureuser/airflow-venv/bin:/usr/local/bin:/usr/bin:/bin`——**必须**含 venv bin（见 §6）；
- 配置以单元内 `AIRFLOW_HOME` + `AIRFLOW__*` 环境变量为准，`~/airflow/airflow.cfg` 同步一致；
- scheduler 有 `After=spacefin-host-render.service`，采集依赖渲染服务先就绪。

### 备选：容器化（未采用）

`airflow/docker-compose.airflow.yml`（apache/airflow:2.10.5 + postgres:16 + LocalExecutor）：
- 必须挂 `/var/run/docker.sock` 才能在容器里调宿主 docker compose（等于给容器宿主 root）；
- 必须挂仓库根到 `/opt/spacefin`，`spacefin_repo_root` 改容器内路径；venv 须是 Linux 宿主建的；
- 访问宿主 master/渲染服务走 `host.docker.internal`（`extra_hosts` 已配）；若改 `network_mode: host`
  则服务名解析、端口、元数据库连接串（`@postgres:5432` → `@127.0.0.1:5432`）都要跟着改；
- **AIRFLOW_UID 坑**：`echo "AIRFLOW_UID=$(id -u)" >> .env` 必须与宿主 UID 一致，否则挂载卷内
  logs/plugins 文件属主错乱、写不进去。

结论：容器版连不到宿主服务，DAG 又要执行宿主命令（start_all.sh / docker compose / venv python），
**选宿主 systemd 直装**。

## 5. Airflow Variables（均有默认值，一般无需设置）

| Variable | 默认值 | 说明 |
|---|---|---|
| `spacefin_repo_root` | DAG 所在仓库根（`__file__` 推导，`guangdong_daily_crawl.py:39-46`） | 所有 Bash 任务 cwd |
| `spacefin_venv` | `<repo_root>/tools/orchestrator/.venv` | Python 环境**目录**（DAG 拼 `{venv}/bin/python`） |
| `spacefin_master_url` | `http://127.0.0.1:5100` | wait_crawl_done 轮询 /crawl_status、drain 轮询 /tasks |
| `spacefin_render_url` | `http://127.0.0.1:8899` | 渲染服务探活；端口解析不出回 8899 |
| `spacefin_crawl_timeout_hours` | `6` | wait_crawl_done 超时上限 |
| `spacefin_drain_timeout` | `900` | drain_workers 等待秒数，超时只告警不失败 |

`Variable.get` 的 `default_var` 只兜「变量不存在」，元数据库连不上时仍抛异常；DAG parse 期抛异常
会让整个 DAG 导入失败、00:30 调度静默错过，故 `_var()`（`guangdong_daily_crawl.py:24-36`）显式
兜住回落默认值。

## 6. 已知坑

1. **`Environment=PATH` 必须含 airflow-venv/bin**：scheduler 以子进程方式拉起 task 时要再调
   `airflow` CLI，PATH 里找不到就 `FileNotFoundError`、DAG 全红。单元里必须写
   `PATH=/home/azureuser/airflow-venv/bin:...`（`airflow-scheduler.service:8`）。
2. **Airflow 容器版连不到宿主服务**：DAG 的 Bash 任务要在宿主执行 `start_all.sh` / `docker compose` /
   venv python，容器化默认做不到——必须挂 docker.sock、挂仓库根、URL 改 host.docker.internal，
   等于把宿主 root 给容器。因此采用宿主 systemd 直装（README §1.2 的结论）。
3. **AIRFLOW_UID 坑**（容器化时）：`AIRFLOW_UID` 不设或与宿主不一致，挂载卷 logs/plugins 属主
   错乱导致写失败；本机直装无此问题。
4. **bash_command 结尾空格是必须的**（`guangdong_daily_crawl.py:210,228`）：否则 Airflow 会把以
   `.sh` 结尾的字符串当 Jinja 模板文件加载。
5. **SQLite + LocalExecutor 是硬报错**（Airflow 2.x），scheduler 起不来；SQLite 只能配
   SequentialExecutor。
6. **wait_crawl_done 必须校验 `run_id == {{ ds }}`**：master 的 HTTP 服务先于 `_bootstrap_run`
   就绪，否则 /crawl_status 会拿昨天残留状态（42 个 finished=1 或 stop 未清）直接把 Sensor 骗过去
   （`poke_crawl_done`，`guangdong_daily_crawl.py:94-134`）。

## 7. 目录

| 路径 | 用途 |
|---|---|
| `airflow/dags/guangdong_daily_crawl.py` | 每日总控 DAG（8 任务线性链） |
| `airflow/README.md` | 部署手册（推荐 Postgres + LocalExecutor 形态） |
| `airflow/docker-compose.airflow.yml` | 备选容器化编排（未采用） |
| `airflow/.env.example` / `.env` | 容器化表单参（Fernet / Postgres 密码，勿提交） |
| `~/.config/systemd/user/airflow-{scheduler,webserver}.service` | 实际运行的用户单元（仓库外，未随 repo 交付） |
| `~/airflow/` | AIRFLOW_HOME：airflow.cfg / airflow.db / logs |

## 8. 合规

Airflow 本身只做调度与串行化，不触碰业务数据；DAG 触发的 ETL/地理补全处理的是公开非 PII 房源
数据，与 [crawler-orchestrator.md](crawler-orchestrator.md) §6 口径一致。fernet_key 明文写在用户
单元属本机作品集可接受范围，投产前应移入 secret 管理。
