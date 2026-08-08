# SpaceFin · Airflow 部署手册（Linux 新机）

Airflow 只做**外层总控**：每天 00:30 拉起采集栈 → 盯完成 → 收尾跑 ETL + 地理补全。
**不替换** master/worker 的 Redis 实时派单。DAG：`dags/guangdong_daily_crawl.py`。

---

## 1. 两种部署形态

DAG 的 Bash 任务要在**宿主**上执行 `start_all.sh`、`docker compose`、`tools/orchestrator/.venv/bin/python`。
容器化的 Airflow 默认做不到这三件事，这是选型的核心矛盾。

| 形态 | 能否直接跑宿主命令 | 额外要求 | 结论 |
|---|---|---|---|
| **宿主 systemd 直装**（推荐） | 能，天然同一台机器 | 宿主装 Python 3.10 + Airflow venv | ✅ 采用 |
| 容器化（`docker-compose.airflow.yml`） | 不能，需打洞 | 挂 `/var/run/docker.sock`、挂仓库根、宿主网络可达 | ⚠️ 备选 |

### 1.1 推荐：宿主 systemd 直装

```bash
sudo useradd -r -m -d /opt/airflow airflow && sudo -iu airflow
python3 -m venv /opt/airflow/venv && /opt/airflow/venv/bin/pip install \
  "apache-airflow[postgres]==2.10.5" \
  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.5/constraints-3.10.txt"
export AIRFLOW_HOME=/opt/airflow
```

**元数据库必须是 Postgres，不能用默认 SQLite**：Airflow 2.x 对 SQLite + LocalExecutor 是硬报错
（`cannot use SQLite with the LocalExecutor`），scheduler 起不来。所以先建库、先改配置，最后才 `db migrate`：

```bash
sudo -u postgres psql -c "CREATE USER airflow WITH PASSWORD '<改我>';" \
                      -c "CREATE DATABASE airflow OWNER airflow;"
```

`$AIRFLOW_HOME/airflow.cfg` 需改（等价的 `AIRFLOW__<SECTION>__<KEY>` 环境变量亦可）：

| 配置项 | 值 |
|---|---|
| `[database] sql_alchemy_conn` | `postgresql+psycopg2://airflow:<改我>@127.0.0.1:5432/airflow` |
| `[core] executor` | `LocalExecutor` |
| `[core] load_examples` | `False` |
| `[core] default_timezone` | `Asia/Shanghai` |
| `[core] dags_folder` | `<REPO_ROOT>/airflow/dags` |

```bash
/opt/airflow/venv/bin/airflow db migrate
/opt/airflow/venv/bin/airflow users create --role Admin --username airflow \
  --firstname SpaceFin --lastname Admin --email admin@example.com --password '<改我>'
```

两个 systemd unit（本目录不交付 `.service` 文件，按此自建于 `/etc/systemd/system/`）：

| unit | ExecStart | 备注 |
|---|---|---|
| `airflow-scheduler.service` | `/opt/airflow/venv/bin/airflow scheduler` | `User=airflow`、`Restart=always`、`Environment=AIRFLOW_HOME=/opt/airflow` |
| `airflow-webserver.service` | `/opt/airflow/venv/bin/airflow webserver -p 8080` | 同上 |

> airflow 用户须加入 `docker` 组（`usermod -aG docker airflow`），否则 `start_all.sh` 调 `docker compose` 会 permission denied。

### 1.2 备选：容器化

`docker-compose.airflow.yml` = `apache/airflow:2.10.5` + `postgres:16` + LocalExecutor
（postgres / airflow-init / airflow-webserver / airflow-scheduler）。如实说明局限：

- 必须挂 `/var/run/docker.sock` 才能在容器里调宿主 docker compose；这等于把宿主 root 权限给了容器。
- 必须挂仓库根到 `/opt/spacefin`。路径类 Variable **无需改动**：DAG 用 `Path(__file__).resolve().parents[2]`
  自动推导（`guangdong_daily_crawl.py:39-46`），容器里挂到 `/opt/spacefin` 后自动得到
  `spacefin_repo_root=/opt/spacefin`、`spacefin_venv=/opt/spacefin/tools/orchestrator/.venv`
  （**目录，不带 `/bin/python`**：DAG 内部自己拼 `{venv}/bin/python`）；venv 是 Linux 宿主建的才可用。
- 容器访问宿主 master/渲染服务要走 `host.docker.internal`（已配 `extra_hosts`），
  Variable 的两个 URL 需相应改写；若想直接用 `127.0.0.1`，得给两个 airflow 服务加 `network_mode: host`
  并删掉 `ports:`（此时 webserver 端口由 `airflow.cfg` 决定）。注意 host 网络下服务名 `postgres`
  不再解析，还须把 `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` 里的 `@postgres:5432` 改成 `@127.0.0.1:5432`，
  并给 postgres 服务同样加 `network_mode: host`（或给它 `ports: ["5432:5432"]`），否则连不上元数据库会 crashloop。
- `start_all.sh` 已按平台分支：macOS 走 launchd，Linux 走 systemd（`start_all.sh:64-87`），
  渲染服务托管单元见 `deploy/systemd/spacefin-host-render.service`（用户级安装，须 `loginctl enable-linger` 常驻）。

```bash
cd airflow && cp .env.example .env && vi .env
mkdir -p logs plugins && echo "AIRFLOW_UID=$(id -u)" >> .env
docker compose -f docker-compose.airflow.yml config --quiet   # 只校验
docker compose -f docker-compose.airflow.yml up -d
```

---

## 2. 需要创建的 Airflow Variables

UI → Admin → Variables，或 `airflow variables set <k> <v>`。DAG 全部带 `default_var`，其中路径类默认值由
`Path(__file__).resolve().parents[2]` 自动推导（见 `guangdong_daily_crawl.py:39-46`），**仓库挂到任何目录都自动正确，
一般无需覆盖**；仅当 DAG 文件被挪到仓库外、或要用与默认不同的值时才显式设置。

| Variable | 默认值 | 说明 |
|---|---|---|
| `spacefin_repo_root` | DAG 所在仓库根（`__file__` 推导） | 仓库根，所有 Bash 任务的 cwd；自动推导，无需设置 |
| `spacefin_venv` | `<repo_root>/tools/orchestrator/.venv` | Python 环境**目录**（DAG 拼 `{venv}/bin/python`）；默认指向 `tools/orchestrator/.venv`（本机为 conda `spark` 的符号链接别名），一般无需设置 |
| `spacefin_master_url` | `http://127.0.0.1:5100` | Sensor 轮询 `/crawl_status` |
| `spacefin_render_url` | `http://127.0.0.1:8899` | 宿主渲染服务探活 |
| `spacefin_crawl_timeout_hours` | `6` | `wait_crawl_done` 超时上限 |
| `spacefin_drain_timeout` | `900` | `drain_workers` 等待秒数，超时只告警不失败 |

---

## 3. 依赖前置（缺一不可）

| 依赖 | 位置 | 校验 |
|---|---|---|
| 宿主渲染服务（systemd） | `deploy/systemd/`（scripts agent 交付） | `curl -s localhost:8899/` 有响应 |
| 采集栈 compose | `tools/orchestrator/docker-compose.yml` | `docker compose ... ps` 见 master/worker |
| 采集 venv | `tools/orchestrator/.venv` | `.venv/bin/python -c "import DrissionPage"` |
| 仓库根 `.env` | `<repo_root>/.env` | 含 `QG_*`、`MYSQL_*`；ETL 缺它无法入库 |
| Redis / MySQL | `spacefin-redis` 容器、`spacefin_crawler` 库 | Redis AOF 持久化开启 |

---

## 4. 🔴 未验证风险：Linux 宿主 Chrome 反爬

容器内 Chrome 151（Linux/headless）**已被 58 反爬按指纹软拦截**（返回空心壳页，无 `zu-itemmod`）。
macOS 宿主 Chrome 150 正常，**Linux 宿主 Chrome 从未实测**。

**上线第一步必须**（先于装 Airflow）：

```bash
bash tools/orchestrator/render_smoke_test.sh     # 需拿到 zu-itemmod
```

拿不到 `zu-itemmod` 就**停止**接入 Airflow：fangyuan（出租）会全量空转，
DAG 只会把空转自动化。此时须先决策降级方案（换 UA/指纹、或接受 fangyuan 降级），再继续。

---

## 5. 首次上线验收清单

| # | 任务 | 怎么看通过 |
|---|---|---|
| 0 | 渲染冒烟 | `render_smoke_test.sh` 返回含 `zu-itemmod`（见 §4） |
| 1 | `render_smoke_test` | 任务 green；停掉渲染服务重跑应 **fail**（不能静默过） |
| 2 | `start_stack` | `docker ps` 见 master + 5 worker；`redis-cli get spacefin:crawl_run:current` == 当日 `ds` |
| 3 | `wait_crawl_done` | `curl master:5100/crawl_status`：`total_tasks=42`、`finished_tasks` 单调上升，最终 `all_done=true`；Sensor 用 reschedule 模式，运行中不占 slot |
| 4 | `etl_finalize` | MySQL `spacefin_crawler` 当日行数增长；`data_lake/housing` 出现 `dt=<ds>` 分区 |
| 5 | `geocode_backfill_finalize` | DWD `geocode_status='hit'` 行数增长；无 `--date`，扫全表属正常 |
| 6 | 幂等 | 同一 `ds` 再 trigger：不重置进度（`crawl_run:current` 未变）、DWD 不重复 |
| 7 | 预算生效 | `crawl_status.cities` 里 gz/sz 的 `budget=60/60`，其余城 `20/20`，耗尽者 `reason=budget_exhausted` 且 `finished=true` |
| 8 | 调度 | UI 见 `guangdong_daily_crawl`，schedule `30 0 * * *`，`max_active_runs=1`，次日 00:30 自动跑 |
