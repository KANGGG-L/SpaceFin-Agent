# SpaceFin Agent · 房产金融风险决策支持平台

> 面向互联网金融机构的风险对冲与策略推演决策支持平台：将风控从"数据展示"升级为"可推演的策略沙盒"。

## 这是什么

互联网金融风控的底层逻辑正从单一交易数据，向多模态、时空交织的复杂网络演进。抵押物（尤其不动产）的动态估值，以及借款人所处社区的宏观经济微生态，已成为决定信贷资产质量的关键变量。

本项目从这一观察出发，按产品生命周期逐阶段沉淀设计文档——**每一步都记录"为什么做这个决定、排除了什么选项、接下来验证什么"**，形成可追溯、可复盘的产品决策链路。

## 解决的核心问题

| 问题 | 当前困境 | 本方案目标 |
|------|---------|-----------|
| 抵押物估值靠人工 | 评估师主观判断，周期长、成本高，难以高频重估 | 数据驱动的自动化估值与风险预警 |
| 风控结论缺业务共鸣 | 仅输出干瘪的概率得分 | 可交互、可推演的策略沙盒 |
| 用户画像被美化偏见污染 | 通用模型生成粉饰过的画像 | 真实、可审计的合成行为仿真 |

## 如何阅读本仓库

本仓库按产品生命周期分为 **6 个阶段**（阶段 0–5，另含规划中的阶段 6 上线复盘），每个阶段回答一个核心决策问题并产出对应的开发成果。文档统一存放在 `docs/product/0X/`，开发产出（PoC / 原型）在 `docs/poc/`，跨阶段的 MVP 执行计划在 `docs/开发计划.md`。

> **阶段模型说明**：本仓库以 `docs/product/00`–`05` 的实际产物为权威阶段划分（机会识别 → 需求规划 → 技术可行性 → PRD → 设计/研发评审 → 测试验收），阶段 6（上线复盘）规划中。该划分与产品全流程角色框架（需求规划 / PRD / 设计研发评审 / 研发跟进 / 测试验收 / 上线复盘）一一对应，研发跟进体现为 `docs/开发计划.md` 的跨阶段执行计划。

| 阶段 | 核心决策问题 | 交付物 | 开发产出 | 状态 | 文档 |
|------|-------------|-------|---------|------|------|
| 0 | 这个机会值得做吗？ | 机会描述 + 假设清单 + 不做清单 | — | ✅ 已完成 | [docs/product/00/](docs/product/00/README.md) |
| 1 | 用户的真实痛点是什么？ | 用户画像、竞品分析、数据摸底、优先级 | — | ✅ 已完成 | [docs/product/01/](docs/product/01/README.md) |
| 2 | 技术上是否可行？ | 技术评估 + 效果基线 | 数据底座 PoC 代码 | ✅ 已完成 | [docs/product/02/](docs/product/02/README.md) |
| 3 | 具体做成什么样？ | PRD（含验收标准） | 核心功能原型实现 | ✅ 已完成 | [docs/product/03/](docs/product/03/README.md) |
| 4 | 设计和技术方案对齐了吗？ | 评审纪要、待确认清单、设计规范 | 设计/研发评审材料（已组织评审回填） | ✅ 材料+纪要已产出（结论待干系人确认） | [docs/product/04/](docs/product/04/README.md) |
| 5 | 测试验收与开发是否在轨道上？ | 测试用例、验收清单、上线风险、开发计划 | 测试框架 + 开发排期（待真实数据接入执行） | ⏳ 部分（测试框架就绪，系统集成/联调未开始） | [docs/product/05/](docs/product/05/README.md) · [docs/开发计划.md](docs/开发计划.md) |
| 6 | 上线复盘（规划中） | 复盘报告、迭代 backlog | — | ⏳ 未开始 | [docs/product/06/](docs/product/06/README.md)（待建） |

## 方法论：从构想走向落地

本项目的推进遵循三条原则，贯穿全部阶段，覆盖产品设计与开发实现：

1. **先验证问题，再讨论方案**。每个阶段始于一个待证伪的命题，而非一个待实现的功能列表。阶段 0 的 5 条核心假设中，3 条被标为"生死假设"——只要一条被证伪，就推倒重来或调整方向。
2. **技术选型是阶段 2 的产物，不是阶段 0 的预设**。在确认机会和需求之前，不锁定任何具体技术路径。当前文档只讨论"需要什么能力"，不预设"用什么工具实现"。
3. **每个阶段产出都是下一阶段的输入**。不做"一锤子"方案，而是逐步收敛不确定性——从模糊想法 → 可验证假设 → 用户需求 → 方案设计 → 代码实现 → 交付上线。

### 当前状态：

```
├── 阶段 0 ✅ 机会识别（已完成）
├── 阶段 1 ✅ 需求调研（已完成）
├── 阶段 2 ✅ 技术可行性 → 数据底座 PoC（已完成）
├── 阶段 3 ✅ PRD + 核心功能原型（已完成）
├── 阶段 4 ✅ 设计与研发评审（材料 + 评审纪要已产出）
├── 阶段 5 ⏳ 测试验收（框架就绪，待真实数据接入与研发交付后执行）
└── 阶段 6 ⏳ 上线复盘（规划中）
```

## 工程实现（按需接入，循序渐进）

产品决策文档之外，本平台的可运行工程实现按 **Sprint 自底向上、按需接入**推进：每个组件在其业务需要出现时才引入，并同步补充[组件技术说明](docs/tech/components/)与下表登记。当前正按真实容器栈逐层落地（L0→L5）。

### 已接入组件

| 组件 | 层级 | 状态 | 引入 Sprint | 技术说明 |
|------|------|------|------------|---------|
| MySQL 业务源库 | L0 数据源头 | ✅ 已接入 | Sprint 0 | [mysql-source.md](docs/tech/components/mysql-source.md) |
| 安居客采集器 | L0 公开数据获取 | ✅ 已接入 | 数据源探索 | [anjuke-crawler.md](docs/tech/components/anjuke-crawler.md) |
| └ Redis 任务队列 | 采集中间件 | ✅ 代码集成 | 数据源探索 | [redis-task-queue.md](docs/tech/components/redis-task-queue.md) |
| └ jhao104/proxy_pool 代理池 | 采集反爬（IP 轮换） | ✅ 客户端集成 | 数据源探索 | [proxy-pool.md](docs/tech/components/proxy-pool.md) |
| └ 容器编排采集系统 | 分布式调度 + 宿主渲染 | ✅ 已接入（2026-08-03 全量跑通） | 数据源探索 | [crawler-orchestrator.md](docs/tech/components/crawler-orchestrator.md) |
| └ Airflow 外层编排 | 每日定时触发 + 收尾（DAG 00:30） | ✅ 已接入 | 数据源探索 | [airflow/README.md](airflow/README.md) |
| └ ETL 数据管道 | 跨日去重 + DWD 入库 + ODS 湖 | ✅ 已接入 | Sprint 1 | [crawler-etl.md](docs/tech/components/crawler-etl.md) |
| └ Kubernetes 分布式集群 | 采集横向扩展 | ⏸ 清单齐备（待集群） | 数据源探索 | [k8s-crawler-cluster.md](docs/tech/components/k8s-crawler-cluster.md) |
| MySQL binlog CDC（I-01） | L0 变更接入 | ✅ 已接入 | Sprint 1 | [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| └ CDC 下游消费链 | L0 增量同步 | ✅ 已接入（改一笔 loan 20s 内下游同步） | Sprint 1 | [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| Doris + MinIO 湖仓 | L0 分层 | ⏳ 待接入 | Sprint 1 | — |
| Kafka + Flink 实时 | L1 | ⏳ 待接入 | Sprint 2 | — |
| AVM（GBDT+空间特征） | L3 | ✅ 已接入（MAPE 16.6% vs 基线 22.6%，数据质量修复后可达 10% 目标） | Sprint 2 | [tools/avm/README.md](tools/avm/README.md) · [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| L2 空间特征（高危区/POI/通勤） | L2 | ✅ 已接入（单机近似，降级 Sedona） | Sprint 3 | [spatial-feature.md](docs/tech/components/spatial-feature.md) |
| 倒排索引 / Superset 看板 | L5 | ⏳ 待接入 | Sprint 6 | — |

## 当前状况（采集系统）

自研的容器编排采集系统（`tools/orchestrator/`）已完成开发与全量验证，功能层面满足要求：

- **调度架构**：master 主备（Redis 抢锁选主，standby 自动接管）+ 5 个泛化 worker + 宿主渲染服务，无第三方调度框架依赖。
- **代理体系**：双代理池（青果 qg 短效 1000 配额优先 + 免费池兜底），按缺口小批量补拉；全程强制走 IP 池，渲染服务对无代理请求返回 403，杜绝直连宿主 IP。
- **外层编排**：已接入 Airflow（DAG `guangdong_daily_crawl`，每天 00:30 触发），只做「拉起 + 盯完成 + 收尾」，不替换 Redis 实时派单。
- **ETL 管道**：已落地（DWD 入库 + ODS 数据湖 Parquet + geocode 异步最终一致补全，跨日去重与市场留存指标）。
- **全量运行结果**：sale（出售）21 城 186,635 行 / 出数页率 99.2%；fangyuan（出租）链路修复后出数页率提升 11.8 倍（2.82%→33.33%），页均产出 18.27 行。
- **空转根因修复（7 项全落地）**：fangyuan 分页越界、验证码误判空页、Chrome 空壳页识别（66 样本零错判）、探针自伤 pkill、fd 上限、补池重试、渲染槽并发。
- **当前瓶颈**：青果 1000 配额已耗尽，免费池通过率实测 0%（出口 IP 被反爬验证码墙标记）——采集能力就绪，待新代理配额或 Linux 新机迁移后全量重跑。

## 落地与迁移（采集系统）

采集系统主体（调度 / 每城 IP 预算 / 增量断点续爬 / ETL / Airflow 编排）已实现并合入 develop；剩余为 Linux 迁移与验收：

| 项 | 状态 |
|----|------|
| 定时触发（Airflow DAG `guangdong_daily_crawl`，00:30） | ✅ 已实现并合入 develop |
| ETL 跨日去重 / 入库 / 数据湖落盘（DAG 收尾自动执行） | ✅ 已实现 |
| 轮次策略（取消 MAX_ROUNDS=3，读完/预算耗尽即终态） | ✅ 已实现 |
| 每城 IP 预算（sale 600 / fangyuan 400+免费池，广深 15%） | ✅ 已实现 |
| 增量断点续爬 + 回扫头部 2-3 页 | ✅ 已实现 |
| Linux 新机部署（launchd → systemd、Airflow 同机、环境装依赖） | ⏳ 待执行 |

> 🔴 **最大风险**：fangyuan 渲染依赖宿主 Chrome。容器 Chrome（Linux/headless）已被反爬按指纹软拦截，Linux 宿主 Chrome 预期可行但**未实测**——新机部署第一步必须做渲染冒烟测试（`/render` 拿 zu-itemmod），失败则需决策降级方案。

### 本地运行

前置：本机可用 Docker。

```bash
cp .env.example .env   # .env 已被 gitignore，勿提交真实密码
make up                # 启动 MySQL 业务源库（首次自动建表 + 灌入合成 seed）
make sql               # 进入 MySQL 交互终端（spacefin 库）
make down              # 停止
```

更多入口见根目录 `Makefile`（`make help`）。合成 seed 由 `seed/generate_seed.py` 生成（确定性、可复现），说明见 [seed/README.md](seed/README.md)。

## 合规与数据

- 项目涉及金融业务数据，严格执行合规脱敏流程处理，不涉及个人信息出境。
- 房产与地理数据通过合规开放 API / 地理编码服务获取。
- 合成行为仿真仅用于策略推演与产品设计验证，不作为任何个体授信决策依据。

## 许可证与免责

本仓库用于产品设计与技术交流，示例数据均为脱敏 / 模拟样本，不涉及真实个人金融信息。

---

## 开发环境与版本

> 开发环境 Python 统一到 **3.10**。宿主唯一 Python 环境 = **conda `spark`**（`tools/orchestrator/.venv` 是指向它的符号链接别名，脚本默认路径无需改动）；Docker worker / Airflow 目标机亦为 3.10。

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| Python（conda 环境 `spark`） | 3.10.18（`tools/orchestrator/.venv` 是其别名） | **唯一宿主环境**：ETL / 编排 / 测试 / 宿主渲染 / 钩子 / PySpark 产品栈；依赖见 `environment.yml` + `tools/orchestrator/requirements.txt` |
| Python（worker/master 容器） | 3.10（`python:3.10-slim`） | 采集 worker / master 容器 |
| Python（Airflow，Linux 目标机） | 3.10（apache-airflow 2.10.5） | 外层编排 DAG |
| pre-commit | 4.6.1 | 提交前 / 提交信息钩子 |
| Node.js | v24.16.0 | commitlint 运行环境 |
| npm | 11.13.0 | 依赖安装 |
| @commitlint/cli | 19.8.1 | 提交信息校验 |
| @commitlint/config-conventional | 19.8.1 | Conventional Commits 规则 |

> 注：本机有两套 anaconda（PATH 上是 `/opt/anaconda3`，env 实际在 `/Users/ethan/anaconda3/envs/`），用 `conda` 命令操作 `spark` 环境须先 `conda activate spark`，或直接使用 `/Users/ethan/anaconda3/envs/spark/bin/...` 路径。

初始化（在仓库根目录执行）：

```bash
conda activate spark                        # 唯一宿主环境（Python 3.10.18）
pip install -r tools/orchestrator/requirements.txt   # 采集/ETL 运行依赖（首次建环境时）
pip install pre-commit
pre-commit install
pre-commit install --hook-type commit-msg   # 启用提交信息校验
npm install

# tools/orchestrator/.venv 是指向 spark 的符号链接（别名），无需单独创建
```
