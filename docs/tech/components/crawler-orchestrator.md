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

| 本项目（可跑实现） | 生产对应物 |
|--------------------|-----------|
| 公开免费代理源（TheSpeedX 等）| 商业代理池（可用性稳定、质量可控）|
| Docker Compose 本地编排 | k8s Deployment/StatefulSet + HPA |
| Redis 单实例 | Redis 集群 / 哨兵 |

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
- 抢锁：`SET spacefin:master:leader <id> NX EX 30`；leader 每 10s 续期；
- standby 每 5s 检查 leader 心跳，超时（>60s）则 `try_become_leader` 接管；
- leader 的 maintenance 线程：**双池巡检**——青果池（`spacefin:proxy_pool:qg`，
  存活 1 分钟到期即清；**按需补拉**：`/proxy/qg` 池空时当场提取（`refill_qg(on_demand=True)`），
  维护线程补拉另加**需求闸门**——近 120s 无实际发放则跳过，避免「提取→55s 过期」烧配额）、
  免费池（`spacefin:proxy_pool:free`，仅青果为空时兜底填充；**本轮发放总量上限
  `FREE_BUDGET`（默认 1000）**，到限后本轮不再发免费代理）；为每个 worker 同步 proxy list；
- **青果 IP 分配**：总量 `QG_BUDGET`=1000/天，按城每类型预算由 `try_consume_ip` 单独保障
  （sale 合计 500 / fangyuan 合计 500，与各城预算之和一致），`QG_SALE_BUDGET` 仅为 `/tasks`
  回显的**历史观测字段**，不再作为发放闸门；
  每城预算按 `BUDGET_TOP_CITIES` 分档（广深各 60、其余 19 城各 20），
  见 compose 的 `IP_BUDGET_SALE/FY_TOP`（60）与 `IP_BUDGET_SALE/FY_OTHER`（20）；
- **fangyuan 仅用青果（qg only）**：`/proxy/free` 与 `/proxy/random` 对 fangyuan 均不回落免费池，
  免费池兜底仅限 sale；
- **调度顺序（2026-08-07 起：按城交错 city-interleave + fangyuan 先执行）**：全 42 任务
  （21 城 × sale/fangyuan）自 bootstrap 起同时有效并一次性入队，队列顺序为**先全 21 城 fangyuan、
  再全 21 城 sale**（`[gz_fangyuan, sz_fangyuan, ..., yf_fangyuan, gz_sale, sz_sale, ..., yf_sale]`），
  worker 从队首领取 → fangyuan 波次优先消耗其 500 qg、再进入 sale 波次（qg 500 + 免费池兜底），
  不再有全局 sale→fangyuan 阶段切换与阶段闸门；
- leader 初始化 42 任务到 `spacefin:tasks` 队列，回收心跳超时任务（running 判死先重试、
  超 `MAX_REQUEUE` 再盖 `stale_abandoned` 终态，保证 42 任务有限步内必全 finished）。

**worker（泛化，N 个）**
- 启动注册到 `spacefin:workers`；循环 `LPOP spacefin:tasks` 领任务
  `{city, pages, target}`；
- 取代理：`/proxy/random`（青果优先、青果空则回落免费池；**fangyuan 仅取青果，不回落免费池**）；
  master 另提供 `/proxy/qg`/`/proxy/free` 直取端点与 Redis proxy list 兜底；
  **代理复用**——一个代理连抓多页（青果 1 IP ≈ 7 页/475 条）直到被拦才换，
  最大化配额利用率；
- 持续向 `spacefin:task:{city}` 上报 `count` + `worker_hb`（心跳）；
- 达 target 后标 done，继续领下一任务，直到队列清空。

**故障转移验证**（实测）
- 停掉 primary → 30s 内 standby 接管为 leader，任务/代理调度无中断；
- 重启 primary → 识别 leader 已是 standby，自觉降级待命，**无脑裂**。

## 4. 如何运行

```bash
# 前置：spacefin-redis 已运行于 spacefin-net 网络

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

# 产物
ls output/guangdong/*_proxy.csv            # 每城独立 CSV（17 字段数值 schema）
output/guangdong/guangdong_all_cities.csv  # 21 城合并
```

**参数**：worker `--pages`（默认 10，约 600 条）`--target`（默认 500）；
master 环境变量 `REFRESH_INTERVAL`（代理巡查周期）/ `LEADER_TTL` / `WORKER_TTL`。

## 5. 目录

| 路径 | 用途 |
|------|------|
| `tools/orchestrator/master.py` | master：主备选举 + 代理巡查 + 任务调度 + HTTP API |
| `tools/orchestrator/worker.py` | 泛化 worker：领任务 + 代理抓取 + 进度上报 |
| `tools/orchestrator/Dockerfile` | 镜像（基于 jhao104/proxy_pool 镜像，含 curl_cffi/lxml/redis）|
| `tools/orchestrator/docker-compose.yml` | master 主备 + worker×5 编排 |
| `output/guangdong/` | 每城 CSV + 合并文件（已 gitignore）|

## 6. 合规

与 [anjuke-crawler.md](anjuke-crawler.md) §6 一致：仅采集公开非 PII 房源数据；
代理仅用于轮换出口 IP；控制频次与规模；投产前法律审查。编排本身不触碰任何
个人数据，Redis 仅存任务/代理/进度元数据。
