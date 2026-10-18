# 组件技术说明 · Airflow 每日采集总控（外层编排）

> **部署形态**：容器化（`airflow/docker-compose.airflow.yml`，apache/airflow:2.10.5 + postgres:16 + LocalExecutor）；DAG 为 19 任务链，收尾到 `lake_sync`。
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

| 本项目（容器化部署）                                                                              | 生产对应物                                             |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 容器化（`airflow/docker-compose.airflow.yml`，apache/airflow:2.10.5 + Postgres 16 + LocalExecutor） | 独立 Airflow 集群 + Postgres 元数据库 + CeleryExecutor |
| 容器常驻（docker compose up -d）                                                                    | k8s / 托管平台上的 Airflow 托管                        |
| 单机（scheduler 与采集栈同宿主网络，经 `host.docker.internal` 互通）                                | 多节点（调度 / worker / 元数据库分离）                 |

> 采纳形态：容器化（Postgres + LocalExecutor）为本仓库交付形态；宿主 systemd 直装形态的单元文件（`.service`）**未随仓库交付**，无法直接复现，仅作原理参考。

## 3. 架构与流程

```
  docker compose 常驻（airflow/docker-compose.airflow.yml）
  ├─ spacefin-airflow-scheduler ──▶ 00:30 触发 guangdong_daily_crawl
  └─ spacefin-airflow-webserver ──▶ :8080 Web UI（admin/admin）
          │
          ▼
  DAG 任务链（19 个任务，按依赖顺序，guangdong_daily_crawl.py）：
  render_smoke_test ─▶ start_stack ─▶ wait_crawl_done ─▶ drain_workers
        ─▶ stop_crawl_stack ─▶ crawl_census ─▶ etl_finalize ─▶ market_state
        ─▶ etl_readiness ─▶ crawl_quality_gate ─▶ geocode_fill
        ─▶ geocode_backfill_finalize ─▶ spatial_features ─▶ cdc_consume
        ─▶ risk_recalc ─▶ risk_snapshot ─▶ alerting_push ─▶ alerting_audit ─▶ lake_sync
```

- **render_smoke_test**：探活宿主渲染服务（经 `host.docker.internal:8899` 的 `/health`），取不到
  即中止整条链。注意门禁只查 `/health` 不校验真实渲染产出（见 §6：宿主 Chrome 不可用时可能"绿跑零 fangyuan 数据"）。
- **start_stack**：`start_all.sh && sleep 30`，注入 `CRAWL_RUN_ID={{ ds }}`，拉起 Docker 采集栈；
  sleep 是等 leader 完成新 run 引导，避免 `wait_crawl_done` 首次 poke 落进上一 run 残留状态窗口。
  ⚠️ **注意 `start_all.sh` 默认 `DO_BUILD=1`**（`tools/orchestrator/start_all.sh:35` 定义默认值，build 分支在 `:135`），
  会先 `docker compose build master-primary master-standby worker-1..5` 再 `up -d`，即**重建 crawler
  master 容器**。因此通过 DAG 重跑同一 ds 会重启 master、丢失其内存 `all_done` 态，触发**真实重爬**——
  `CRAWL_RUN_ID` 幂等**只在 master 容器不被重启时成立**（master 不重建时已完成日不重派发）。
  **本流程不支持"零爬取验证后段"**：想验证 ETL→湖仓后段，变通是选库里已有 `first_seen_date=ds`
  数据的 ds（如 `ds=2026-08-20` 有 36652 行）跑完整 run，接受重爬，但 `crawl_quality_gate` 因历史
  数据仍过闸、后段可完整验证。不要承诺"秒过/跳过"。
- **wait_crawl_done**：PythonSensor（`mode=reschedule`，运行中不占 slot），每 300s 轮询 master
  `GET /crawl_status`，`run_id=={{ ds }}` 且 `all_done=true` 才放行；超时上限默认 6h
  （`spacefin_crawl_timeout_hours`，当前实测设为 8 留足爬取窗口）；master 不可用按「还没好」处理，不失败。
- **drain_workers**：轮询 `GET /tasks`（而非 /crawl_status——它只有 finished/reason，看不出逐任务
  running），等无 `status=running` 后再等 10s flush 余量，避免 ETL 读到写了一半的 raw JSON 行；
  900s 超时（`spacefin_drain_timeout`）只告警不失败，`retries=0`。
- **stop_crawl_stack**：`stop_stack.sh` → `docker compose stop` 采集栈 7 个服务（master 主备 + worker-1..5）。
  采集栈是 [T2 按需组件](../components/ops.md#4-managesh资源管家)，此前爬完从不关闭、天天驻留（实测约 2.1G），
  在小内存宿主上曾把机器拖到 OOM 重启（调度器在派发 `spatial_features` 时被 OOM kill）。
  排空后关栈安全：**下游 14 个任务（含 etl_readiness 哨兵；排除哨兵则为 13）只碰 MySQL / 本地文件 / Redis / 腾讯 geocoder / Doris，无一依赖采集栈**
  （只有 render_smoke_test / wait_crawl_done / drain_workers 需要 master 或渲染服务，全在排空前）。
  用 `stop` 不用 `down`：保留容器与 `docker logs`（爬取失败时唯一的现场），次日 `up -d` 是启动而非重建。
  默认 best-effort（失败只告警 + Slack，不阻断 ETL）；`spacefin_stop_stack_strict=1` 时失败即拦停。
  `trigger_rule=all_done`：关栈是**收敛**动作，上游排空失败/超时也必须执行，否则采集栈会带着 7 个容器
  泄漏到次日调度（此前若 `retries=1` 无 trigger_rule，关栈一旦失败就把下游整条日链路一起挡住）。
  宿主渲染服务不在此列（宿主机进程，容器内管不到），仍由 systemd / nohup 管。
- **crawl_census**（S2）：跑 `census.py --date {{ ds }}`，把「今天到底扫到了哪几页」
  写成 `crawl_census` 表：每观测日每 (城,型,波,容器) 一行，只有 `complete=1` 的行才是缺席（absent）
  判定的证据。位置**在关栈之后、ETL 之前**：raw/stats 此时已落盘且不再被写，账本先落库当日 ETL 与
  `crawl_quality_gate` 才读得到。`complete` 是**白名单**判定——只认 4 个正常终态（`pages_exhausted` /
  `target_reached` / `not_found` / `empty_pages`）加「无页洞的页区间」，任何不确定的输入一律 0，
  且不向下游提供任何页码证据（缺省即未知）。默认 exit 0 **只记账**：旧镜像或停爬日会天然全 0
  （42 行 `no_observation`），这是正确的 fail-closed 答案——「今天没有完备观测」恰恰是账本要表达的
  结论，不是工具故障，故不传 `--fail-on-incomplete`（S3 强制阶段再开）。
  ⚠️ 终态取自 Redis 波次快照：容器内 `127.0.0.1` 指容器自己，故经 Variable `spacefin_redis_host`
  （默认 `host.docker.internal`）访问宿主常驻的 `spacefin-redis`（发布 `0.0.0.0:6379`，不随采集栈 stop 消失）。
  连不上只降级成「查无终态」→ `complete=0`，绝不会反过来把未知读成完备。
- **crawl_quality_gate**：查 `crawl_housing_sale` + `crawl_housing_rent` 的 `first_seen_date={{ ds }}`
  （新增）与 `last_seen_date={{ ds }} 且 last_seen_page 非空`（刷新）两个口径，判定分三档：
  - `new_rows > 0`：按软/硬下限与 `spacefin_crawl_gate_strict` 正常判定；
  - `new_rows == 0` 但 `refreshed > 0`：**告警放行**。全量页已爬穿（21 城 sale 断点=100=PAGES_SALE 上限）
    后每日只回扫前 `HEAD_REWIND` 页，新房源为 0 是必然结果；此时必须放行，否则快照点会断档。
    「爬虫活着」的证据只可能是**带页码的刷新行**——`last_seen_page` 由 worker 随行落盘，
    它说明这条房源今天确实在第 N 页列表上被见到了。**`count_refreshed_rows` 必须带
    `last_seen_page IS NOT NULL`**：否则 `etl.py` 每晚重读同一批冻结 raw 文件、把历史行的
    `last_seen_date` 刷成当天，零页面爬取也会报出同样的刷新数（合成信号）。
  - `new_rows == 0` 且 `refreshed == 0`：**`exit 2` 拦停**（不论 strict）——爬虫当天一行都没扫到，属故障。
  **切日预期**：S1 之前入库的行没有页码证据，永远不会被算进 `refreshed`；因此「真零采集日」会
  `exit 2` 而非被历史存量放行（关闭假绿路径）。单跑复核（只读）：`python3 tools/orchestrator/crawl_quality_gate.py --date <ds>`。
  告警外发只看 `os.getenv("SPACEFIN_ALERT_SLACK_WEBHOOK")`；该变量默认留空时 `post_slack` 直接返回
  False——`exit 2` 仍拦得住 DAG，但**无人被通知**，这是独立的待办；取消注释后手工复核想静音可加前缀
  `SPACEFIN_ALERT_SLACK_WEBHOOK= python3 …`。
  ⚠️ **观测列缺失预检**：`missing_observe_columns()` 在三个口径之前先查 `information_schema`，
  缺 `last_seen_page` 的表非空则直接 `exit 2` 并点名根因（gate 跑在 `etl_finalize` 之前，或存量库
  未迁移），而不是抛 1054 栈。列由 `etl.py::_ensure_columns` 每次运行幂等补齐（`CREATE TABLE IF NOT
EXISTS` 在活库上永远不会长出新列），闸门自身保持只读、不 ALTER。
  ⚠️ **census 点名（S2）**：闸门额外查 `crawl_census`，把当日**没有任何 `complete=1` 行**的 (城,型)
  逐一点进告警文案（`incomplete_census_tasks()` + `census_note()`）。这是 S2 的验收点：某城
  `complete=0` → 当日不得对它产出任何 absent 判定，且必须有人看见。账本缺失（表不存在 / 当日无行）
  比「有行但 complete=0」更严重，点名为「全部 21 城无完备观测证据」——**没账本 ≠ 都完备**。
  点名只追加信息、**不改变** `decide()` 的放行/拦停判定。
- **etl_finalize**：`etl.py --date {{ ds }}` 跨日去重、入库、数据湖落盘。
- **market_state**（S3）：跑 `market_state.py --date {{ ds }}`，把观测完备性账本
  （`crawl_census`）与 DWD 的在架记录合成派生表 `dwd_listing_market_state`（逐 ds 逐 key 一行、可重放），
  判定在架 / 缺席 / 未知 / 下架。**必须排在 `etl_finalize` 之后**——它要读当日 ETL 写入的
  `last_seen_date` / `last_seen_page`，排在之前会永久查到 0（与闸门顺序坑同源，本项目已踩两次）。
  **影子模式**：`SPACEFIN_MARKET_STATE_ENFORCE=0`（DAG 里显式注入 env，不传 `--enforce`）时只写派生表 +
  打印日报，不改闸门、不拦停任何东西。缺席资格三条件缺一即 `eligible=0` 并落 `unknown`：该 (城,型)
  当日有 `complete=1` 的认证页、房源页码落在认证区间内、首见日不早于 cutover——「没看就等于不知道，
  不等于没了」的落点。缺证据的日子一律不推进缺席计数，也不清零。
- **etl_readiness**（PythonOperator，`retries=0`）：ETL 产物哨兵——校验 `etl_finalize` 已为 `{{ ds }}`
  写入数据湖分区 `data_lake/housing/dt={{ ds }}`（与新增行数无关，是「ETL 已产出当日数据」的可靠标记）。
  把「闸门必须排在 etl 之后」这条隐式顺序依赖变成**显式可见**的前置校验：哨兵缺失即立即以明确依赖错误
  失败并点名「crawl_quality_gate 必须在 etl_finalize 之后」（避免顺序倒置造成死锁），
  不重试；`etl_readiness` 放行后 `crawl_quality_gate` 再遇 0 行才是「当日确无新数据」真故障。
  注意哨兵路径用 `REPO_ROOT`，不误用容器内安装前缀。
- **geocode_fill**：`geocode_fill.py --daily-limit 6000` 填 community_coords 词典，腾讯 geocoder
  每日配额跑满即停，pending 状态隔日续跑。
- **geocode_backfill_finalize**：`geocode_backfill.py` 补 DWD 坐标（无 `--date`，扫全表属正常）。
- **spatial_features**：空间特征计算（重计算任务，注意 §6 内存争用 OOM 风险）。
- **cdc_consume**：`consumer.py --once --date {{ ds }} --batch 5000`，消费 ODS 积压业务变更，
  增量刷新 DWS/ADS——批处理时点对齐保证，不依赖常驻 consumer 健康。
- **risk_recalc**：`main.py --date {{ ds }} --write-db` 全量重算 LTV。必须排在 CDC 之后：CDC 只覆盖
  「有变更的贷款」，而 ETL 刷新的行情影响**全部**贷款估值，只有全量过一遍才跟得上。
- **alerting_push**：贷后保全预警推送。
- **alerting_audit**：预警终态审计。
- **lake_sync**：MySQL 全量同步进 Doris + 数据湖快照上传 MinIO。注意执行位置修正——见 §6
  （因 WSL2 网络隔离，改为在 `spacefin-doris-fe` 容器内 `docker exec` 执行 `tools/lake/sync.py`）。

DAG 参数：`schedule="30 0 * * *"`（Asia/Shanghai，每日 00:30）、`catchup=False`、
`max_active_runs=1`、`start_date=2026-08-01`、默认 `retries=2 / retry_delay=5min`
（Sensor 与 drain 单独 `retries=0`）。

## 4. 部署形态（容器化）

| 项           | 值                                                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 编排         | `airflow/docker-compose.airflow.yml`（apache/airflow:2.10.5 + postgres:16 + LocalExecutor）                             |
| 容器         | `spacefin-airflow-scheduler` / `spacefin-airflow-webserver`（:8080，admin/admin）/ `spacefin-airflow-postgres`（:5432） |
| AIRFLOW_HOME | 容器内 `/opt/airflow`（logs/plugins 挂载卷）                                                                            |
| 元数据库     | Postgres（`AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`，Postgres + LocalExecutor 是 Airflow 2.x 要求）                         |
| 执行器       | LocalExecutor                                                                                                           |
| dags_folder  | `/opt/airflow/dags`（compose 把 `./dags` 挂到该路径）；仓库根另挂 `/opt/spacefin` 供 Bash 任务 cwd 使用                                                   |
| webserver    | `:8080`                                                                                                                 |
| AIRFLOW_UID  | **50000**（镜像内置，见下方坑）                                                                                         |

关键点：

- 容器访问宿主 master/渲染服务走 `host.docker.internal`（`extra_hosts` 已配）；URL 默认值已改为此（见 §5）。
- 必须挂 `/var/run/docker.sock` 才能在容器里调宿主 docker compose（如 `start_stack` 拉采集栈）；
- `spacefin_repo_root` 在生产由 Airflow Variable 显式设为 `/opt/spacefin`（默认经
  `Path(__file__).resolve().parents[2]` 推导，两者在容器内一致），`spacefin_venv`
  指向 `/opt/spacefin/tools/orchestrator/.venv`（**注意该 .venv 是悬空软链，见 §6**）。

### 备选：宿主 systemd 直装（单元文件未交付，不可直接复现）

`~/.config/systemd/user/airflow-{scheduler,webserver}.service` 这类单元文件的 `.service` 内容**未随
仓库交付**，无法据此直接复现，仅供原理参考；实际运行形态是上面的容器化。

- 若采用直装，DAG 的 Bash 任务能在宿主直接执行 `start_all.sh` / `docker compose` / venv python，
  URL 默认需改回 `127.0.0.1`；
- 直装无 `AIRFLOW_UID` 容器属主坑，但仍须 `PATH` 含 airflow venv bin（否则 scheduler 子进程拉 task 时
  `FileNotFoundError`）。

### AIRFLOW_UID 坑（已解决，容器化）

apache/airflow 镜像的 `/etc/passwd` 没有宿主 uid（如 1000）的条目。照 `airflow/README.md` 旧注释用
`echo "AIRFLOW_UID=$(id -u)"`（=1000）会导致 `airflow-init` 的 `getuser()` 抛 `AirflowConfigException`
失败。**已用镜像内置 `AIRFLOW_UID=50000`（`airflow/.env`）解决**。文档明确：AIRFLOW_UID **不要取宿主 uid，用 50000**。

## 5. Airflow Variables（均有默认值，一般无需设置）

| Variable                          | 默认值                                                              | 说明                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `spacefin_repo_root`              | DAG 所在仓库根（`__file__` 推导，`guangdong_daily_crawl.py:39-46`） | 所有 Bash 任务 cwd                                                                                                                                                                                                                                                                                                                                                |
| `spacefin_venv`                   | `<repo_root>/tools/orchestrator/.venv`                              | Python 环境**目录**（DAG 拼 `{venv}/bin/python`）。⚠️ 容器内该 .venv 是悬空软链（指向宿主 conda `spark` 路径，容器内不存在），直接执行会 **exit 127**；DAG 已用 `_resolve_python_bin()` 自动回退容器 `python3`（`/home/airflow/.local/bin/python3`）。**不要假设 venv 可用，也不要手动在容器里 pip install 来"修复"**。 |
| `spacefin_master_url`             | `http://host.docker.internal:5100`                                  | wait_crawl_done 轮询 /crawl_status、drain 轮询 /tasks（容器内需走 host-gateway 才到宿主）                                                                                                                                                                                                                                                                         |
| `spacefin_render_url`             | `http://host.docker.internal:8899`                                  | 渲染服务探活；端口解析不出回 8899                                                                                                                                                                                                                                                                                                                                 |
| `spacefin_crawl_timeout_hours`    | `6`                                                                 | wait_crawl_done 超时上限（当前实测设为 8 给爬取留足窗口，默认 6 也够；**不要设成 1.5**，否则爬取超 90 分钟会被 Sensor 超时误杀）                                                                                                                                                                                                                                  |
| `spacefin_drain_timeout`          | `900`                                                               | drain_workers 等待秒数，超时只告警不失败                                                                                                                                                                                                                                                                                                                          |
| `spacefin_redis_host`             | `host.docker.internal`                                              | S2 账本读取波次终态（census 等）；容器内需走 host-gateway 才到宿主                                                                                                                                                                                                                                                                                                |
| `spacefin_postloan_webhook_url`   | `""`（空）                                                          | I-05（贷后告警）真实推送地址；留空=不启用，仅任务内写日志                                                                                                                                                                                                                                                                                                         |
| `spacefin_postloan_webhook_token` | `""`（空）                                                          | I-05 推送鉴权 Bearer Token，可选                                                                                                                                                                                                                                                                                                                                  |
| `spacefin_alert_slack_webhook`    | `""`（空）                                                          | Slack 失败通知 webhook；留空=不实际推送，仅 warning                                                                                                                                                                                                                                                                                                               |
| `spacefin_alert_mentions`         | `""`（空）                                                          | Slack 提及对象（@名单），可选                                                                                                                                                                                                                                                                                                                                     |
| `spacefin_min_crawl_rows`         | `50`                                                                | G2 闸门硬下限：低于该行数视为数据失真（严格模式下拦停 ETL）                                                                                                                                                                                                                                                                                                       |
| `spacefin_min_crawl_rows_warn`    | `200`                                                               | G2 闸门软下限：低于该行数仅告警放行（正常稳态 < 50 时按 new_rows==0 规则处理）                                                                                                                                                                                                                                                                                    |
| `spacefin_crawl_gate_strict`      | `""`（关）                                                          | G2 严格拦停开关（`1`/`true`/`yes` 生效）；开时低于硬下限才 exit 2，否则告警放行                                                                                                                                                                                                                                                                                   |
| `spacefin_drain_strict`           | `""`（关）                                                          | drain_workers 排空超时是否失败；留空=超时仅告警放行                                                                                                                                                                                                                                                                                                               |
| `spacefin_stop_stack_strict`      | `""`（关）                                                          | stop_crawl_stack 关不掉采集栈时是否失败；留空=只告警放行                                                                                                                                                                                                                                                                                                          |

**`PY_DEP_HEAL` 依赖自愈**（已在 `guangdong_daily_crawl.py:120` 落实，所有下游 python 任务的
bash_command 开头拼上）：

```
"/home/airflow/.local/bin/python3" -m pip install -q --disable-pip-version-check \
  numpy'<2' scikit-learn'<1.6' scipy'<1.14' mysql-replication curl_cffi \
  || echo "[PY_DEP_HEAL] WARN: dependency self-heal pip install failed (non-fatal, task will fail later if pkg actually missing)";
```

要点：

- `numpy` 必须钉 `<2`（与镜像 pandas 2.1.4 ABI 兼容，否则 scikit-learn 把 numpy 拉到 2.x 报
  "numpy.dtype size changed"）；
- `curl_cffi` 是 `etl.py` 经 `anjuke_crawler` 间接 import 的依赖，漏装会让 `etl_finalize` 报
  `ModuleNotFoundError: No module named 'curl_cffi'`；
- 自愈失败**打印 WARN 且不阻断下游**（设计取舍：依赖可能已在镜像里，不必因自愈失败而失败任务）。

`Variable.get` 的 `default_var` 只兜「变量不存在」，元数据库连不上时仍抛异常；DAG parse 期抛异常
会让整个 DAG 导入失败、00:30 调度静默错过，故 `_var()`（`guangdong_daily_crawl.py:24-36`）显式
兜住回落默认值。

## 6. 部署注意事项

以下为从零部署与排障时的关键结论（面向部署者，细节因环境而异，以实际探测为准）：

1. **容器内 venv 不可用（已自动回退，非缺陷）**：`tools/orchestrator/.venv/bin/python` 是指向宿主 conda `spark` 的悬空软链，在 airflow 容器内不存在，直接执行会 **exit 127**。DAG 已用 `_resolve_python_bin()`（运行时试跑探测）回退到容器 `python3`。**不要宣称 venv 可用，也不要手动在容器里 pip install 来"修复"——缺失依赖由 `PY_DEP_HEAL` 处理**。
2. **AIRFLOW_UID 必须 50000**：apache/airflow 镜像 `/etc/passwd` 无宿主 uid 条目，用 `$(id -u)`（如 1000）会让 `airflow-init` 的 `getuser()` 抛 `AirflowConfigException`。已用镜像内置 `AIRFLOW_UID=50000`，不要改成宿主 uid。
3. **`max_active_runs=1` 占槽阻塞**：DAG 写死单 active-run；若有一个 run（含 failed/running 残留）占槽，新 run 会停在 `queued` 不被调度。释放占槽 run：改元数据库 `dag_run` 状态置 `failed`，或 `airflow tasks clear -s/-e <exec_date> -y` 重跑该 run 重置（Airflow 2.10.5 无 `airflow dags fail` 子命令）。
4. **`lake_sync` 须在 Doris FE 容器内执行（WSL2 网络隔离）**：`deploy/doris-minio` 的 Doris/MinIO 用 `network_mode: host`，在 Docker Desktop + WSL2 下只绑在 VM 的 `127.0.0.1`，airflow 容器经 `host.docker.internal` 连不到 9030/9000。故改为在 `spacefin-doris-fe` 容器内 `docker exec` 执行 `tools/lake/sync.py`（与 Doris/MinIO 同 host 网络，127.0.0.1 可达）。**前置：先确认 `spacefin-doris-fe` 为 healthy（FE 9030 OPEN）再跑**，失败用 `airflow tasks clear -t lake_sync` 重跑。该容器重建会丢 pip 包，命令内已加 `pip install pymysql requests` 自愈。
5. **重计算任务与 Doris 的内存争用**：`spatial_features` 等重计算任务与常驻 Doris 争内存时可能被 OOM kill（exit -9）。编排上约定「spatial 阶段先停 Doris 释放内存，其成功后再起，供 `lake_sync`」。宿主内存吃紧时应把 Doris+MinIO 的置备列为独立运维项，而不是当作常态绕过。
6. **`ds` 与 `-e` 的 -1 偏移（Airflow 2.x）**：`ds` = data_interval 起点 = logical_date − 1（每日调度下）。要得到 `ds=<日期>`，须 `airflow dags trigger -e <次日>`。`tasks clear` 的 `-e` 是结束日，要覆盖某 run 用 `-e <run次日>`。**补跑已存在的 ds 会撞 `DagRunAlreadyExists`**：此时不要再用 `trigger`，改用 `airflow tasks clear -s/-e <exec_date> -y guangdong_daily_crawl` 重跑既有 run。
7. **任务顺序坑：`crawl_quality_gate` 必须排在 `etl_finalize` 之后**：闸门统计 ETL 写入的 `first_seen_date={{ ds }}` 行，排在 etl 之前 → 全新 ds 恒为 0 行 → `exit 2` 永久死锁、到不了 etl/lake_sync。DAG 依赖已定为 `drain_workers >> etl_finalize >> crawl_quality_gate`，并加 `etl_readiness` 哨兵把该顺序变成显式前置校验。`market_state` 同样必须排在 `etl_finalize` 之后（要读当日 ETL 写入的 `last_seen_date` / `last_seen_page`）。
8. **`PY_DEP_HEAL` 依赖自愈须可见**：所有下游 python 任务的 bash_command 开头拼依赖自愈（`numpy'<2'` 与镜像 pandas 2.1.4 ABI 兼容、`scikit-learn'<1.6'`、`scipy'<1.14'`、`mysql-replication`、`curl_cffi`）。自愈失败打印可见 WARN 且不阻断下游；**旧版 `2>/dev/null || true` 会静默吞掉失败**（已改为可见 WARN）。
9. **`render_smoke_test` 门禁只查 `/health`，不校验真实渲染产出**：门禁能过，但若宿主 Chrome 不可用，fangyuan（出租）城市会 `render_unreachable`、0 行，形成"绿跑零 fangyuan 数据"——这是已知 gap，非代码缺陷。接入前应先用 `render_smoke_test.sh` 确认能拿到 `zu-itemmod`。
10. **`bash_command` 结尾空格是必须的**：否则 Airflow 会把以 `.sh` 结尾的字符串当 Jinja 模板文件加载。
11. **`wait_crawl_done` 必须校验 `run_id == {{ ds }}`**：master 的 HTTP 服务先于 run 引导就绪，否则 `/crawl_status` 会拿上一 run 残留状态直接骗过 Sensor。
12. **`etl_finalize` 首次需 `--init-db`**：从零环境（MySQL 无 `crawl_housing_sale/rent` 表）直接跑会失败，须先 `etl.py --init-db`（用 root 凭据建表），之后日常 run 走 app 账号。
13. **Redis 启动**：`make infra` 里含 `spacefin-redis`；独立从零时等价执行 `docker network create spacefin-net`（若不在）后 `docker run -d --name spacefin-redis redis:7`（或 `docker start spacefin-redis`）。
14. **采集产出的 bind 挂载路径**：compose 相对 bind 源由 **dockerd**（而非 compose 客户端）解析，容器内执行 `start_all.sh` 时相对路径会被解析成 daemon 命名空间内的绝对路径并自动创建 orphan 目录，导致 worker 写入的 raw jsonl 落进 ETL 永不读取的树。两处 bind 已改用 `${SPACEFIN_REPO_ROOT:?…}` 绝对路径变量，由 `lib_host_repo_root.sh` 在 `start_all.sh` / `stop_stack.sh` 内导出，推不出即 exit 1、绝不静默沿用相对路径。运维约束：**绕开脚本手工跑 compose 前必须先 `export SPACEFIN_REPO_ROOT="$(pwd)"`**。验收须看**仓库** `output/guangdong/raw/*.jsonl` 的 mtime 是当日（`data_lake/housing/dt=<ds>` 分区存在不能证明修好了——`etl.py collect_files` 重读冻结语料也会造出来）。
15. **Doris `url` 列宽限制**：`tools/lake/schema.py` 建表 `url VARCHAR(1024)`，真实长 URL 会触发 Stream Load `DATA_QUALITY_ERROR`，需 `ALTER TABLE ... MODIFY COLUMN url STRING`（MySQL 侧同理 `TEXT`）放宽，否则 `lake_sync` 失败。
16. **MySQL 连接目标易误判**：etl/gate 连 `MYSQL_HOST=host.docker.internal`、库 `spacefin_crawler`（不是 `spacefin`；`spacefin` 库只存 collateral/customer/loan）。在 Airflow 容器与 doris-fe 容器内，`host.docker.internal` **均解析到 `spacefin-mysql` 容器本身，不是 Windows 宿主**——etl 写入与 gate/sync 读取命中同一容器，插数据可直接 `docker exec spacefin-mysql mysql ...`。只有 **Doris/MinIO** 才真正经 `host.docker.internal` 走宿主发布端口（故 lake_sync 在 doris-fe 容器内跑）。

## 7. 目录

| 路径                                                           | 用途                                                                       |
| -------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `airflow/dags/guangdong_daily_crawl.py`                        | 每日总控 DAG（19 任务链，含 S2 的 `crawl_census` 与 S3 的 `market_state`） |
| `airflow/README.md`                                            | 部署手册（容器化 Postgres + LocalExecutor 形态已采用）                     |
| `airflow/docker-compose.airflow.yml`                           | **交付采用的**容器化编排                                                   |
| `airflow/.env.example` / `.env`                                | 容器化表单参（Fernet / Postgres 密码，AIRFLOW_UID=50000，勿提交）          |
| `~/.config/systemd/user/airflow-{scheduler,webserver}.service` | 备选 systemd 单元（未随 repo 交付，不可直接复现）                          |
| 容器内 `/opt/airflow/`                                         | AIRFLOW_HOME：airflow.cfg / logs（挂载卷）                                 |

## 8. 合规

Airflow 本身只做调度与串行化，不触碰业务数据；DAG 触发的 ETL/地理补全处理的是公开非 PII 房源
数据，与 [crawler-orchestrator.md](crawler-orchestrator.md) §6 口径一致。fernet_key 明文写在
`airflow/.env` 属当前项目阶段可接受范围，投产前应移入 secret 管理。
