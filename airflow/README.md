# SpaceFin · Airflow 部署手册（Linux 新机）

Airflow 只做**外层总控**：每天 00:30 拉起采集栈 → 盯完成 → 收尾跑 ETL + 地理补全。
**不替换** master/worker 的 Redis 实时派单。DAG：`dags/guangdong_daily_crawl.py`。

---

## 1. 两种部署形态

DAG 的 Bash 任务要在**宿主**上执行 `start_all.sh`、`docker compose`、`tools/orchestrator/.venv/bin/python`。
本仓库**交付形态 = 容器化**（`docker-compose.airflow.yml`，apache/airflow:2.10.5 + postgres:16 +
LocalExecutor）；DAG 为 19 任务链，收尾到 `lake_sync`。
**注意**：本仓库**不随环境安装 apache-airflow**，`make up` 不会启动它；标准 Linux 新机的端到端
调度**未验证**——既有跑通记录来自开发机的容器化环境（§6），且 Linux 宿主 Chrome 反爬从未实测（§4）。

| 形态                                       | 能否直接跑宿主命令                                            | 额外要求                                               | 结论                                                        |
| ------------------------------------------ | ------------------------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------- |
| **容器化（`docker-compose.airflow.yml`）** | 能（挂 docker.sock + 仓库根，经 `host.docker.internal` 互通） | 挂 `/var/run/docker.sock`、挂仓库根、AIRFLOW_UID=50000 | ✅ **交付形态（开发机容器化验证；标准新机端到端未验证）**   |
| 宿主 systemd 直装                          | 能，天然同一台机器                                            | 宿主装 Python 3.10 + Airflow venv                      | ⚠️ 备选（单元文件未随仓库交付，不可直接复现，仅作原理参考） |

### 1.1 备选：宿主 systemd 直装（单元文件未交付）

> 本形态仅供原理参考：对应的 `.service` 单元文件**未随仓库交付**，无法直接复现，不可据此"采用"。
> 交付形态见 §1.2 容器化。

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

| 配置项                        | 值                                                            |
| ----------------------------- | ------------------------------------------------------------- |
| `[database] sql_alchemy_conn` | `postgresql+psycopg2://airflow:<改我>@127.0.0.1:5432/airflow` |
| `[core] executor`             | `LocalExecutor`                                               |
| `[core] load_examples`        | `False`                                                       |
| `[core] default_timezone`     | `Asia/Shanghai`                                               |
| `[core] dags_folder`          | `<REPO_ROOT>/airflow/dags`                                    |

```bash
/opt/airflow/venv/bin/airflow db migrate
/opt/airflow/venv/bin/airflow users create --role Admin --username airflow \
  --firstname SpaceFin --lastname Admin --email admin@example.com --password '<改我>'
```

两个 systemd unit（本目录不交付 `.service` 文件，按此自建于 `/etc/systemd/system/`）：

| unit                        | ExecStart                                         | 备注                                                                      |
| --------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------- |
| `airflow-scheduler.service` | `/opt/airflow/venv/bin/airflow scheduler`         | `User=airflow`、`Restart=always`、`Environment=AIRFLOW_HOME=/opt/airflow` |
| `airflow-webserver.service` | `/opt/airflow/venv/bin/airflow webserver -p 8080` | 同上                                                                      |

> airflow 用户须加入 `docker` 组（`usermod -aG docker airflow`），否则 `start_all.sh` 调 `docker compose` 会 permission denied。
> 直装无 `AIRFLOW_UID` 容器属主坑，但 scheduler 子进程拉 task 时 `PATH` 须含 airflow venv bin，否则 `FileNotFoundError`。

### 1.2 交付形态：容器化

`docker-compose.airflow.yml` = `apache/airflow:2.10.5` + `postgres:16` + LocalExecutor
（postgres / airflow-init / airflow-webserver / airflow-scheduler），**该 compose 曾在开发机端到端跑通；
标准 Linux 新机端到端未验证**。技术要点：

- 必须挂 `/var/run/docker.sock` 才能在容器里调宿主 docker compose（如 `start_stack` 拉采集栈）；这等于把宿主 root 权限给了容器。
- 必须挂仓库根到 `/opt/spacefin`。路径类 Variable 默认经 `Path(__file__).resolve().parents[2]`
  自动推导（`guangdong_daily_crawl.py:39-46`），容器里挂到 `/opt/spacefin` 后自动得到
  `spacefin_repo_root=/opt/spacefin`、`spacefin_venv=/opt/spacefin/tools/orchestrator/.venv`；
  **生产由 Airflow Variable 显式设 `spacefin_repo_root=/opt/spacefin`**（两者一致）。
  （**目录，不带 `/bin/python`**：DAG 内部自己拼 `{venv}/bin/python`）。
- 容器访问宿主 master/渲染服务要走 `host.docker.internal`（已配 `extra_hosts`），
  Variable 的两个 URL 默认值已为此改写（见 §2）；若想直接用 `127.0.0.1`，得给两个 airflow 服务加 `network_mode: host`
  并删掉 `ports:`（此时 webserver 端口由 `airflow.cfg` 决定）。注意 host 网络下服务名 `postgres`
  不再解析，还须把 `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` 里的 `@postgres:5432` 改成 `@127.0.0.1:5432`，
  并给 postgres 服务同样加 `network_mode: host`（或给它 `ports: ["5432:5432"]`），否则连不上元数据库会 crashloop。
- **AIRFLOW_UID 必须 50000**：镜像 `/etc/passwd` 无宿主 uid 条目，`echo "AIRFLOW_UID=$(id -u)"`（=1000）
  会让 `airflow-init` 的 `getuser()` 抛 `AirflowConfigException` 失败。`.env` 已写 `AIRFLOW_UID=50000`，**不要改成宿主 uid**。
- `start_all.sh` 已按平台分支：macOS 走 launchd，Linux 走 systemd（`start_all.sh:71-111`：`uname` 分支第 71 行、Linux 分支第 80 行、systemd 检测 99-104），在 WSL2 + Docker Desktop 下两者都不可用，代码里已加兜底 `nohup .../host_render_service.py 8899 &` 拉起（该兜底路径未实测）。

```bash
cd airflow && cp .env.example .env && vi .env
mkdir -p logs plugins && echo "AIRFLOW_UID=50000" >> .env
docker compose -f docker-compose.airflow.yml config --quiet   # 只校验
docker compose -f docker-compose.airflow.yml up -d
```

---

## 2. 需要创建的 Airflow Variables

UI → Admin → Variables，或 `airflow variables set <k> <v>`。DAG 全部带 `default_var`，其中路径类默认值由
`Path(__file__).resolve().parents[2]` 自动推导（见 `guangdong_daily_crawl.py:39-46`），**仓库挂到任何目录都自动正确，
一般无需覆盖**；仅当 DAG 文件被挪到仓库外、或要用与默认不同的值时才显式设置。

| Variable                       | 默认值                                 | 说明                                                                                                                                                                                                                                                                                   |
| ------------------------------ | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `spacefin_repo_root`           | DAG 所在仓库根（`__file__` 推导）      | 仓库根，所有 Bash 任务的 cwd；自动推导，无需设置                                                                                                                                                                                                                                       |
| `spacefin_venv`                | `<repo_root>/tools/orchestrator/.venv` | Python 环境**目录**（DAG 拼 `{venv}/bin/python`）。⚠️ 容器内该 .venv 是悬空软链（指向宿主 conda `spark` 路径，容器内不存在，直接执行 exit 127）；DAG 已自动回退容器 `python3`（`/home/airflow/.local/bin/python3`）。**不要假设 venv 可用，也不要手动在容器里 pip install 来"修复"**。 |
| `spacefin_master_url`          | `http://host.docker.internal:5100`     | Sensor 轮询 `/crawl_status`（容器内需走 host-gateway 才到宿主）                                                                                                                                                                                                                        |
| `spacefin_render_url`          | `http://host.docker.internal:8899`     | 宿主渲染服务探活                                                                                                                                                                                                                                                                       |
| `spacefin_crawl_timeout_hours` | `6`                                    | `wait_crawl_done` 超时上限（默认 6；建议设 8 留足窗口；**不要设 1.5**，否则爬取超 90 分钟会被 Sensor 误杀）                                                                                                                                                                              |
| `spacefin_drain_timeout`       | `900`                                  | `drain_workers` 等待秒数，超时只告警不失败                                                                                                                                                                                                                                             |

> **Variables（均有默认值兜底）**：`spacefin_repo_root` / `spacefin_venv` / `spacefin_master_url` /
> `spacefin_render_url` / `spacefin_crawl_timeout_hours`(=8) 一般无需设置；按上表 `airflow variables set`
> 重设值一致、幂等无副作用，可重跑。`spacefin_crawl_timeout_hours` 建议设为 `8`（给爬取留足窗口），
> **不要设成 1.5**，否则爬取超 90 分钟会被 Sensor 超时误杀。
> **完整 Variable 清单（16 个，均有默认值兜底）**见 `docs/tech/components/airflow.md` §5：除上表 6 个外，
> 还含 `spacefin_redis_host`、`spacefin_postloan_webhook_url/token`、`spacefin_alert_slack_webhook`、
> `spacefin_alert_mentions`、`spacefin_min_crawl_rows`(50)、`spacefin_min_crawl_rows_warn`(200)、
> `spacefin_crawl_gate_strict`、`spacefin_drain_strict`、`spacefin_stop_stack_strict`（默认不设置即可，
> 留空走默认行为）。

**`PY_DEP_HEAL` 依赖自愈**（已在 `guangdong_daily_crawl.py:120` 落实）：所有下游 python 任务的
bash_command 开头拼了
`"/home/airflow/.local/bin/python3" -m pip install -q --disable-pip-version-check numpy'<2' scikit-learn'<1.6' scipy'<1.14' mysql-replication curl_cffi || echo "[PY_DEP_HEAL] WARN: ..."`。
`numpy` 钉 `<2`（与镜像 pandas 2.1.4 ABI 兼容），`curl_cffi` 是 `etl.py` 经 `anjuke_crawler` 间接
import 的依赖（漏装让 `etl_finalize` 报 `ModuleNotFoundError: No module named 'curl_cffi'`）；自愈失败
打印 WARN 且不阻断下游（已改为可见 WARN 而非静默）。**不要手动 pip install 修复 venv。**

---

## 3. 依赖前置（缺一不可）

| 依赖                    | 位置                                         | 校验                                                                                                                                                                                    |
| ----------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 宿主渲染服务（systemd） | `deploy/systemd/`（仓库内交付）              | `curl -s localhost:8899/` 有响应                                                                                                                                                        |
| 采集栈 compose          | `tools/orchestrator/docker-compose.yml`      | `docker compose ... ps` 见 master/worker                                                                                                                                                |
| 采集 venv               | `tools/orchestrator/.venv`                   | ⚠️ 容器内该 venv 为悬空软链（指向宿主 conda 路径），实际用容器 `python3`（`/home/airflow/.local/bin/python3`）；DAG 通过 `_resolve_python_bin()` 自动回退。**不要假设容器内 venv 可用** |
| 仓库根 `.env`           | `<repo_root>/.env`                           | 含 `QG_*`、`MYSQL_*`；ETL 缺它无法入库                                                                                                                                                  |
| Redis / MySQL           | `spacefin-redis` 容器、`spacefin_crawler` 库 | Redis AOF 持久化开启                                                                                                                                                                    |

---

## 4. 🔴 未验证风险：Linux 宿主 Chrome 反爬

容器内 Chrome 151（Linux/headless）**已被 58 反爬按指纹软拦截**（返回空心壳页，无 `zu-itemmod`）。
macOS 宿主 Chrome 150 正常，**Linux 宿主 Chrome 从未实测（未验证）**。

**上线第一步必须**（先于装 Airflow）：

```bash
bash tools/orchestrator/render_smoke_test.sh     # 需拿到 zu-itemmod
```

拿不到 `zu-itemmod` 就**停止**接入 Airflow：fangyuan（出租）会全量空转，
DAG 只会把空转自动化。此时须先决策降级方案（换 UA/指纹、或接受 fangyuan 降级），再继续。

---

## 5. 首次上线验收清单

| #   | 任务                        | 怎么看通过                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --- | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0   | 渲染冒烟                    | `render_smoke_test.sh` 返回含 `zu-itemmod`（见 §4）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| 1   | `render_smoke_test`         | 任务 green；停掉渲染服务重跑应 **fail**（不能静默过）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 2   | `start_stack`               | `docker ps` 见 master + 5 worker；`redis-cli get spacefin:crawl_run:current` == 当日 `ds`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| 3   | `wait_crawl_done`           | `curl master:5100/crawl_status`：`total_tasks=42`、`finished_tasks` 单调上升，最终 `all_done=true`；Sensor 用 reschedule 模式，运行中不占 slot                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 4   | `etl_finalize`              | MySQL `spacefin_crawler` 当日行数增长；`data_lake/housing` 出现 `dt=<ds>` 分区                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 4.5 | `crawl_census`              | S2 观测完备性账本（2026-08-31 新增），排在 `stop_crawl_stack` 之后、`etl_finalize` 之前。固定产出 42 行（21 城 × sale/fangyuan）；**当前采集栈仍是旧镜像，必然 42 行 `no_observation`、完备 0/42，这是正确的 fail-closed 答案而非故障**（该任务 exit 0 只记账）。验收看两件事：① `crawl_quality_gate` 是否把无 `complete=1` 的 (城,型) 全部点名；② 新镜像（`docker compose build` 之后）是否让「完备任务 N/42」出现 N>0                                                                                                                                                                                                                                                                                                                                                                   |
| 4.6 | `market_state`              | S3 状态机（2026-08-31 新增），排在 `etl_finalize` 之后、`etl_readiness` 之前——必须读当日 ETL 写入的 `last_seen_date` / `last_seen_page`，排在之前会永久查到 0。**影子模式**（`SPACEFIN_MARKET_STATE_ENFORCE=0`，DAG 显式注入 env，不传 `--enforce`）只写 `dwd_listing_market_state` 并打日报，不拦停任何东西。验收：① 影子期 `crawl_quality_gate` 的 exit code 与开启前**逐次相同**；② 当日有 `complete=1` 的 (城,型) 里 `active` 数应等于当日 ETL 入库行数（2026-08-31 实测 2543 = 2543）；③ 无完备观测的日子 `absent_streak` 必须全为 0（零假缺席）                                                                                                                                                                                                                                     |
| 5   | `crawl_quality_gate`        | **须在 `etl_finalize` 之后**（已修复过"跑在 etl 前→永远 0 行死锁"，见 §6）。双口径：`new_rows` = `first_seen_date={{ds}}`；`refreshed` = `last_seen_date={{ds}}` **且 `last_seen_page` 非空**。三档：`new>0` 按软/硬下限判（低于硬下限仅在 `SPACEFIN_CRAWL_GATE_STRICT=1` 时拦停）；`new==0 且 refreshed>0` 告警放行（爬穿稳态）；`new==0 且 refreshed==0` `exit 2` 拦停（不论 strict）。**关键验收**：造一轮零页面爬取（停代理 / `HEAD_REWIND=0`）后闸门必须 **exit 2**——不必整链重跑（见 #7），直接 `python3 tools/orchestrator/crawl_quality_gate.py --date <ds>` 复核即可。闸门若放行，说明 `last_seen_page IS NOT NULL` 谓词被去掉，历史存量会冒充今日刷新（2026-08-30 实测假绿：零爬取仍报 `refreshed=25,241`）。详见 `docs/tech/components/airflow.md` §crawl_quality_gate |
| 6   | `geocode_backfill_finalize` | DWD `geocode_status='hit'` 行数增长；无 `--date`，扫全表属正常                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| 7   | 幂等边界                    | `CRAWL_RUN_ID` 幂等**只在 master 容器不被重启时成立**：直接对 master 之外的场景重跑已完成日不重派发。⚠️ 但通过 DAG 重跑同一 ds 会经 `start_all.sh`（默认 `DO_BUILD=1`）重建 crawler master 容器、触发**真实重爬**，并非"不重置进度/跳过"。本流程不支持"零爬取验证后段"。                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 8   | 预算生效                    | `crawl_status.cities` 里 gz/sz 的 `budget=60/60`，其余城 `20/20`，耗尽者 `reason=budget_exhausted` 且 `finished=true`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 9   | 调度                        | UI 见 `guangdong_daily_crawl`，schedule `30 0 * * *`，`max_active_runs=1`，次日 00:30 自动跑                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 10  | `lake_sync`                 | 在 `spacefin-doris-fe` 容器内 `docker exec` 执行 `tools/lake/sync.py`；确认 FE healthy 后 9030 OPEN、MinIO 9000 可达。**注意**：MySQL（`spacefin_crawler` 库）经 `host.docker.internal` 在 Airflow 容器内解析到的是 `spacefin-mysql` 容器本身（非 Windows 宿主）；Doris/MinIO 才是经 `host.docker.internal` 走 Windows 宿主发布端口可达（故 lake_sync 改在 doris-fe 容器内跑）。etl/gate 连的是同一个 `spacefin-mysql` 容器，插入与读取保持一致即可                                                                                                                                                                                                                                                                                                                                       |

---

## 6. 已知修复：crawl_quality_gate 须在 etl_finalize 之后（2026-08-24）

- **症状（修复前）**：gate 原本位于 `etl_finalize` 之前，而它统计的是 ETL 写入的 `first_seen_date={{ds}}` 行；ETL 尚未运行时这些行恒为 0 → gate 永远 `new_rows==0` → exit 2，整条日终链路卡死/误拦。
- **修复**：调整 DAG 依赖为 `drain_workers >> etl_finalize >> crawl_quality_gate >> geocode_fill >> …`。gate 现在评估的是 ETL 之后的真实数据，不再死锁。
- **行为校正（同一脚本内，早于本次）**：`new_rows==0` 永远 exit 2（0 新增＝爬虫当天未产出任何数据，属故障必须拦停），避免旧版把 0 行当"硬下限未达"只告警而假成功。
- **e2e 验证结论**（`output/e2e_run_log.md` 第七、八章，来自开发机的验证记录，非标准环境复现）：
  - happy-path：ds=2026-08-23（预存 21469 行）→ gate `etl_done_before_gate=True` → success，整链贯通到 `lake_sync=success`（14/14 任务 success）；
  - 无数据路径：ds=2026-08-21（全新，房源级 `first_seen` 幂等导致 0 新增）→ gate 正确 exit 2 拦停，非死锁缺陷。
- **7.7G 内存注意**：`spatial_features` 与 Doris 争用会 OOM（SIGTERM）；该次运行编排为「spatial 阶段停 Doris 释放 ~2.2G，其成功后再起 Doris 供 lake_sync」。failed dag_run 的下游 `upstream_failed` 任务不会因上游成功自动续跑，须复位 dag_run + 清空前序 TI（详见 `output/e2e_run_log.md` 第八章）。
