# 组件技术说明 · 安居客采集容器编排（master 主备 + 泛化 worker）

> **状态**：✅ 代码集成 + 本地验证（21 城全部抓取完成）
> **能力地图层级**：anjuke_crawler 阶段四/六的容器化落地（代理轮换 + 分布式调度）
> **引入原则**：按需接入。多城市批量抓取需要"每 IP 1 页"闸门下的可持续采集，
> 由本编排把「代理巡查 + 任务调度 + 横向 worker」容器化。

---

## 1. 为何引入

安居客列表页按 **IP 频次**拦截（每个新 IP 约只放行 1 页），多页/多城抓取必须
**每请求轮换 IP** 且 **多 worker 并发**。前序组件（curl_cffi 指纹伪装 + proxy_pool
对接 + Redis 队列）已分别验证，本编排将它们组装为可横向扩展的容器化流水线，
并解决三个运维问题：

1. **worker 泛化**：worker 不绑定城市，由 master 派单，避免"每城一台专用容器"的
   静态绑定，worker 数量可自由扩缩；
2. **任务状态机**：每城任务有 pending/running/done + worker 心跳，worker 崩溃后
   任务自动重新入队；
3. **master 高可用**：主备 master 通过 Redis 抢锁选主，standby 心跳超时自动接管，
   避免调度中心单点。

## 2. 生产对应物（诚实标注）

| 本项目（可跑实现）             | 生产对应物                         |
| ------------------------------ | ---------------------------------- |
| 公开免费代理源（TheSpeedX 等） | 商业代理池（可用性稳定、质量可控） |
| Docker Compose 本地编排        | k8s Deployment/StatefulSet + HPA   |
| Redis 单实例                   | Redis 集群 / 哨兵                  |

## 3. 架构与流程

```
                         ┌──────────────┐
   master-primary ─────▶ │   Redis      │ ◀──── master-standby (抢锁,心跳超时接管)
   (leader) 巡检双池       │  leader key  │
        │                 │  pool:qg     │ ← 青果短效代理池（优先）
        │                 │  pool:free   │ ← 免费代理池（兜底）
        │                 │  task:{city} │
        │                 │  tasks queue │
        │                 └──────────────┘
        │                      ▲  │
        │  /proxy/qg→free      │  LPOP 任务 / 上报进度
        ▼                      │  ▼
   worker-1..N ── curl_cffi ──▶ 安居客（代理复用，1 IP 连抓多页）
```

**master（主备）**

- 抢锁：`SET spacefin:master:leader <id> NX EX LEADER_TTL`（`LEADER_TTL` 默认 60s）；leader 心跳线程每 `max(1, LEADER_TTL//3)`=20s 续期；
- standby 每 5s 轮询检查：leader 心跳 key 消失（TTL 过期为 None）立即 `try_become_leader` 接管；心跳存在但超 `LEADER_TTL*2`（默认 120s）未续也判失效接管；
- leader 的 maintenance 线程：**双池巡检**——青果池（`spacefin:proxy_pool:qg`，
  存活 1 分钟到期即清；**按需补拉**：`/proxy/qg` 池空时当场提取（`refill_qg(on_demand=True)`），
  维护线程补拉另加**需求闸门**——近 120s 无实际发放则跳过，避免「提取→55s 过期」烧配额）、
  免费池（`spacefin:proxy_pool:free`，仅青果为空时兜底填充；**本轮发放总量上限
  `FREE_BUDGET`（默认 1000）**，到限后本轮不再发免费代理）；为每个 worker 同步 proxy list；
- **青果 IP 分配**：总量 `QG_BUDGET`=1000/天，按城每类型预算由 `try_consume_ip` 单独保障
  （sale 合计 500 / fangyuan 合计 500，合计 1000），失败时通过 `/proxy/report` 执行 DECRGT0 失败退款；
  每城预算按 `BUDGET_TOP_CITIES` 分档（广深各 60、其余 19 城各 20），
  见 compose 的 `IP_BUDGET_SALE/FY_TOP`（60）与 `IP_BUDGET_SALE/FY_OTHER`（20）；
- **波次状态机（Wave State Machine）**：
  1. `floor`：42 任务全量入队，每任务抓取 5 页保底（`WAVE_FLOOR_PAGES=5`），按城序交错优先出数
     （`city-interleave`：每城先 fangyuan 后 sale 交替，即 gz_fy→gz_sale→sz_fy→sz_sale→…，
     非「全 21 城 fangyuan 再全 21 城 sale」）；
  2. `rescue`：筛选上一波中 `finish_reason ∉ {pages_exhausted, empty_pages, not_found, target_reached}` 的失败任务重跑（5 页），`FANGYUAN_FREE_RESCUE=1` 允许租房波次在此回落免费池；
  3. `depth`：针对 `ip_used < budget` 且未遭遇 404 的任务深度翻页，优先广深头部城市（租房 53 页 / 二手 100 页；其中 `PAGES_RENT=53` 为 compose 对 master 的**覆盖值**，master.py 内置默认 `PAGES_RENT=300`，实部署以 compose 为准）；
  4. `done`：波次完成，进入终态。
- **调度顺序**：master 采用 `lpush` 批量入队，worker 采用 `rpoplpush` 领取（FIFO）；波次模式下
  `SCHED_MODE=city-interleave`（2026-08-07 起恒置），floor 波次按每城先 fangyuan 后 sale 交替入队；
  仅 `WAVE_ENABLED=0` 的 legacy 直派模式才按「全 21 城 fangyuan、再全 21 城 sale」的 `DEFAULT_TASKS` 顺序；
- leader 初始化任务到 `spacefin:tasks` 队列，回收心跳超时任务（保留任务 Hash 中的波次 pages，running 判死先重试、超 `MAX_REQUEUE` 再盖 `stale_abandoned` 终态，保证 42 任务有限步内必全 finished）。

**worker（泛化，N 个）**

- 启动注册到 `spacefin:workers`；循环从 `spacefin:tasks` 可靠领取任务
  `{city, type, pages, target}`；
- 取代理：`/proxy/random`（青果优先、青果空则回落免费池；**fangyuan 仅在 rescue 波次允许回落免费池**）；
  抓取失败且使用青果代理时，向 master `/proxy/report` 发起退款，释放 IP 计数；
  **代理复用**——一个代理连抓多页直到被拦才换，最大化配额利用率；
- 持续向 `spacefin:task:{city}:{type}` 上报 `count` + `worker_hb`（心跳）；
- 达 target 或页上限后标 done，继续领下一任务，直到队列清空。

**故障转移验证**（实测）

- 停掉 primary → standby 在 leader 心跳 key 消失后接管：leader key 的 `LEADER_TTL`（默认 60s）到期
  即被清，standby 下一 5s 检查窗口内发现 `hb==None` 立即接管（约 60~65s）；若心跳 key 未消失但
  `elapsed > LEADER_TTL*2`（默认 120s）也判失效接管。任务/代理调度无中断；
- 重启 primary → 识别 leader 已是 standby，自觉降级待命，**无脑裂**。

## 4. 如何运行

```bash
# 前置：spacefin-redis 已运行于 spacefin-net 网络
#   （若尚未创建，先执行 `make infra` 幂等创建二者；详见根目录 Makefile 的 infra target）

# 构建 + 启动全部（master 主备 + 5 个泛化 worker）
docker compose -f tools/orchestrator/docker-compose.yml build
docker compose -f tools/orchestrator/docker-compose.yml up -d

# 只起 master 主备 / 只起 worker（按需扩缩）
docker compose -f tools/orchestrator/docker-compose.yml up -d master-primary master-standby
docker compose -f tools/orchestrator/docker-compose.yml up -d worker-1 worker-2 worker-3 worker-4 worker-5

# 观察
curl -s localhost:5100/role        # master-primary 角色
curl -s localhost:5100/tasks       # 21 城任务状态
curl -s localhost:5100/pool_count  # 可用代理池规模
curl -s localhost:5101/role        # master-standby 角色（MASTER_PORT=5101，主备接管后观察）

# 产物（worker 落盘 JSONL，非 CSV；CSV 输出已于 ETL 改造时移除）
ls output/guangdong/raw/ds=<观测日>/                # 每 (城,型,worker) 一个 raw JSONL（含重复，去重在 ETL）
ls output/guangdong/stats/ds=<观测日>/              # per-request 统计 JSONL（PROXY_REPORT_ENABLED=1 时）
```

**参数**：worker 无 `--pages`/`--target` CLI 参数（仅 `--out-dir`，默认 `/output`；`pages`/`target`
由 master 派单 payload 决定——floor 波次 5 页、depth 波次按城市 `PAGES_SALE`/`PAGES_RENT`）；
master 环境变量 `REFRESH_INTERVAL`（代理巡查周期）/ `LEADER_TTL` / `WORKER_TTL` / `WAVE_ENABLED` /
`WAVE_FLOOR_PAGES` / `QG_BUDGET` / `FREE_BUDGET` / `IP_BUDGET_SALE/FY_TOP|OTHER` / `MAX_REQUEUE` 等
（默认值见 [master.py](../../../tools/orchestrator/master.py) 顶部与 [docker-compose.yml](../../../tools/orchestrator/docker-compose.yml)）。

## 5. 目录

| 路径                                                         | 用途                                                                                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `tools/orchestrator/master.py`                               | master：主备选举（`LEADER_TTL` TTL + 心跳线程）+ 双池巡检 + 波次调度（floor/rescue/depth）+ HTTP API                           |
| `tools/orchestrator/worker.py`                               | 泛化 worker：领任务 + 代理抓取 + raw JSONL 落盘 + 心跳上报                                                                     |
| `tools/orchestrator/Dockerfile`                              | 镜像：基于 `python:3.10-slim`（daocloud），pip 装 redis/curl_cffi/lxml/DrissionPage，apt 装 Chrome，ENTRYPOINT 为 start.sh     |
| `tools/orchestrator/docker-compose.yml`                      | master 主备 + worker×5 编排（含 `PAGES_*`/`WAVE_*`/`IP_BUDGET_*` 等环境变量）                                                  |
| `tools/orchestrator/start_all.sh`                            | 采集栈一键拉起（Airflow `start_stack` 调用；`DO_BUILD=1` 时先 build 再 up）                                                    |
| `tools/orchestrator/stop_stack.sh`                           | 采集栈一键停止（`docker compose stop`，保留容器与日志）                                                                        |
| `tools/orchestrator/lib_host_repo_root.sh`                   | 导出 `SPACEFIN_REPO_ROOT`（bind 源路径修正，见 airflow.md §6 坑 14）                                                           |
| `tools/orchestrator/host_render_service.py`                  | 宿主 fangyuan 渲染服务（DrissionPage+Chrome，:8899，位置参数为端口）                                                           |
| `tools/orchestrator/render_smoke_test.sh`                    | 渲染服务冒烟（门禁只查 `/health`）                                                                                             |
| `tools/orchestrator/start.sh`                                | 容器内启动脚本（镜像 ENTRYPOINT，拉起 master/worker）                                                                          |
| `tools/orchestrator/url_key.py` / `url_city.py`              | URL 规范化键 / 城市归属判定（URL 子域 → 广东 21 城短码/省外子域原文/unknown；爬取与 ETL 共用）                                 |
| `tools/orchestrator/test_url_key.py`                         | url_key 单测（9 用例：sale/58/rent 子域、query 剥离、gfangyuan→zu 归一化、unknown/空→md5、58 与 anjuke 不合并、key≤varchar64） |
| `tools/orchestrator/census.py`                               | 观测完备性账本（`crawl_census` 表，S2；见 airflow.md §3）                                                                      |
| `tools/orchestrator/market_state.py`                         | 房源市场状态派生（S3，影子模式；见 airflow.md §3）                                                                             |
| `tools/orchestrator/crawl_quality_gate.py`                   | 采集质量闸门（G2 三档判定；见 airflow.md §3）                                                                                  |
| `tools/orchestrator/recheck.py`                              | S5 定向复核纯函数（absent_streak∈{1,N-1} 白名单 + 配额硬截断）                                                                 |
| `tools/orchestrator/slack_notify.py`                         | 告警消息构造 + Slack 推送（`post_slack`，webhook 为空即静默）                                                                  |
| `tools/orchestrator/crawl_reconcile.py`                      | raw↔DWD 对账（S1 冻结语料核对）                                                                                                |
| `tools/orchestrator/turnover.py`                             | 房源流转（turnover）统计（S4 纯函数：毛/净换手率，分母 0 返回 None）                                                           |
| `tools/orchestrator/etl.py`                                  | ETL：跨日去重入库 + 数据湖分区（详见 crawler-etl.md）                                                                          |
| `tools/orchestrator/geocode_fill.py` / `geocode_backfill.py` | geocode 词典补全 / DWD 坐标回填（S1）                                                                                          |
| `tools/orchestrator/sql/crawl_schema.sql`                    | DWD 两表 + `community_coords`/`crawl_census`/`dwd_listing_market_state` 完整 schema DDL（含 S1-S3 各列）                       |
| `tools/orchestrator/tests/`                                  | orchestrator 单测（census/质量闸门/波次/重查/slack 等）                                                                        |
| `output/guangdong/`                                          | raw JSONL + stats JSONL（已 gitignore；CSV 已移除）                                                                            |

## 6. 合规

与 [anjuke-crawler.md](anjuke-crawler.md) §6 一致：仅采集公开非 PII 房源数据；
代理仅用于轮换出口 IP；控制频次与规模；投产前法律审查。编排本身不触碰任何
个人数据，Redis 仅存任务/代理/进度元数据。
