# SpaceFin Agent · 房产金融风险决策支持平台

> 面向互联网金融机构的风险对冲与策略推演决策支持平台：将风控从"数据展示"升级为"可推演的策略沙盒"。

## 这是什么

互联网金融风控的底层逻辑正从单一交易数据，向多模态、时空交织的复杂网络演进。抵押物（尤其不动产）的动态估值，以及借款人所处社区的宏观经济微生态，已成为决定信贷资产质量的关键变量。

本项目从这一观察出发，按产品生命周期逐阶段沉淀设计文档——**每一步都记录"为什么做这个决定、排除了什么选项、接下来验证什么"**，形成可追溯、可复盘的产品决策链路。

## 解决的核心问题

| 问题                   | 当前困境                                     | 本方案目标                     |
| ---------------------- | -------------------------------------------- | ------------------------------ |
| 抵押物估值靠人工       | 评估师主观判断，周期长、成本高，难以高频重估 | 数据驱动的自动化估值与风险预警 |
| 风控结论缺业务共鸣     | 仅输出干瘪的概率得分                         | 可交互、可推演的策略沙盒       |
| 用户画像被美化偏见污染 | 通用模型生成粉饰过的画像                     | 真实、可审计的合成行为仿真     |

## 如何阅读本仓库

本仓库按产品生命周期分为 **6 个阶段**（阶段 0–5 + 阶段 6 上线复盘），每个阶段回答一个核心决策问题并产出对应的开发成果。文档统一存放在 `docs/product/0X/`，开发产出（PoC / 原型）在 `docs/poc/`，跨阶段的 MVP 执行计划在 `docs/开发计划.md`。

> **阶段模型说明**：本仓库以 `docs/product/00`–`05` 的实际产物为权威阶段划分（机会识别 → 需求规划 → 技术可行性 → PRD → 设计/研发评审 → 测试验收），阶段 6（上线复盘）规划中。该划分与产品全流程角色框架（需求规划 / PRD / 设计研发评审 / 研发跟进 / 测试验收 / 上线复盘）一一对应，研发跟进体现为 `docs/开发计划.md` 的跨阶段执行计划。

| 阶段 | 核心决策问题                 | 交付物                                 | 开发产出                                  | 状态                                         | 文档                                                                                 |
| ---- | ---------------------------- | -------------------------------------- | ----------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------ |
| 0    | 这个机会值得做吗？           | 机会描述 + 假设清单 + 不做清单         | —                                         | ✅ 已完成                                    | [docs/product/00/](docs/product/00/README.md)                                        |
| 1    | 用户的真实痛点是什么？       | 用户画像、竞品分析、数据摸底、优先级   | —                                         | ✅ 已完成                                    | [docs/product/01/](docs/product/01/README.md)                                        |
| 2    | 技术上是否可行？             | 技术评估 + 效果基线                    | 数据底座 PoC 代码                         | ✅ 已完成                                    | [docs/product/02/](docs/product/02/README.md)                                        |
| 3    | 具体做成什么样？             | PRD（含验收标准）                      | 核心功能原型实现                          | ✅ 已完成                                    | [docs/product/03/](docs/product/03/README.md)                                        |
| 4    | 设计和技术方案对齐了吗？     | 评审纪要、待确认清单、设计规范         | 设计/研发评审材料（已组织评审回填）       | ✅ 材料+纪要已产出（结论待干系人确认）       | [docs/product/04/](docs/product/04/README.md)                                        |
| 5    | 测试验收与开发是否在轨道上？ | 测试用例、验收清单、上线风险、开发计划 | 测试框架 + 全量执行（8/8 AC 绿、560 测试通过） | ✅ 已完成                                    | [docs/product/05/](docs/product/05/README.md) · [docs/开发计划.md](docs/开发计划.md) |
| 6    | 上线复盘                     | 复盘报告、交付缺口清单               | 阶段 6 复盘 + 最终版补齐（G2/G4/G5/G8 已交付） | ✅ 已完成                                    | [docs/product/06/](docs/product/06/README.md)                                        |

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
├── 阶段 5 ✅ 测试验收（8/8 AC 全绿、560 自动化测试通过）
└── 阶段 6 ✅ 上线复盘（交付缺口已清点，可行项已补齐）
```

## 工程实现（按需接入，循序渐进）

产品决策文档之外，本平台的可运行工程实现按 **Sprint 自底向上、按需接入**推进：每个组件在其业务需要出现时才引入，并同步补充[组件技术说明](docs/tech/components/)与下表登记。当前正按真实容器栈逐层落地（L0→L5）。

### 组件清单（按层级）

| 组件                                         | 层级                             | 状态                                                                                                       | 引入 Sprint | 技术说明                                                                                                                                         |
| -------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| MySQL 业务源库                               | L0 数据源头                      | ✅ 已接入                                                                                                  | Sprint 0    | [mysql-source.md](docs/tech/components/mysql-source.md)                                                                                          |
| 安居客采集器                                 | L0 公开数据获取                  | ✅ 已接入                                                                                                  | 数据源探索  | [anjuke-crawler.md](docs/tech/components/anjuke-crawler.md)                                                                                      |
| Redis 任务队列                              | 采集中间件                       | ✅ 代码集成                                                                                                | 数据源探索  | [redis-task-queue.md](docs/tech/components/redis-task-queue.md)                                                                                  |
| jhao104/proxy_pool 代理池                   | 采集反爬（IP 轮换）              | ✅ 客户端集成                                                                                              | 数据源探索  | [proxy-pool.md](docs/tech/components/proxy-pool.md)                                                                                              |
| 容器编排采集系统                             | 分布式调度 + 宿主渲染            | ✅ 已接入                                                                                                  | 数据源探索  | [crawler-orchestrator.md](docs/tech/components/crawler-orchestrator.md)                                                                          |
| Airflow 外层编排                             | 每日定时触发 + 收尾（DAG 00:30） | ✅ 已接入                                                                                                  | 数据源探索  | [airflow/README.md](airflow/README.md) · [airflow.md](docs/tech/components/airflow.md)                                                           |
| ETL 数据管道                                 | 跨日去重 + DWD 入库 + ODS 湖     | ✅ 已接入                                                                                                  | Sprint 1    | [crawler-etl.md](docs/tech/components/crawler-etl.md)                                                                                            |
| Kubernetes 横向扩展（可选）                  | 采集横向扩展（K8s 替代路径）     | ⏸ 清单已备（hostPath 待改），待 K8s 集群                                                  | 数据源探索  | [k8s-crawler-cluster.md](docs/tech/components/k8s-crawler-cluster.md)                                                                            |
| MySQL binlog CDC（I-01）                     | L0 变更接入                      | ✅ 已接入                                                                                                  | Sprint 1    | [cdc-downstream.md](docs/tech/components/cdc-downstream.md)                                                                                      |
| CDC 下游消费链                               | L0 增量同步                      | ✅ 已接入（改一笔 loan 秒级同步：≤10s 验收线，实测 3s）                                                    | Sprint 1    | [cdc-downstream.md](docs/tech/components/cdc-downstream.md)                                                                                      |
| Doris + MinIO 湖仓                           | L0 分层                          | ✅ 已接入（ODS/DWD/DWS/ADS 四层 24 表，与 MySQL 对账一致）                                                 | Sprint 1    | [doris-lake.md](docs/tech/components/doris-lake.md)                                                                                              |
| Kafka + Flink 实时                           | L1                               | ✅ 已接入（CDC→Kafka→Flink 实时预警，3s 端到端；含 rebuild.sh 一键重建）                                   | Sprint 2    | [kafka-flink-realtime.md](docs/tech/components/kafka-flink-realtime.md)                                                                          |
| AVM（GBDT+空间特征）                         | L3                               | ✅ 已接入（精度@覆盖率：45% 覆盖 MAPE 9.88% ≤10% 达标；全量 14.59%；基线 20.6%）      | Sprint 2    | [avm.md](docs/tech/components/avm.md) · [tools/avm/README.md](tools/avm/README.md) · [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| 风险引擎（LTV 两档预警 / 五级分类 / 低置信） | L3                               | ✅ 已接入（LTV 警示线 0.75 / 强预警线 0.85，均可配置，等号边界严格大于不触发；低置信：空间特征缺失率 >75） | Sprint 2    | [risk-engine.md](docs/tech/components/risk-engine.md)                                                                                            |
| L2 空间特征（高危区/POI/通勤）               | L2                               | ✅ 已接入（单机近似，降级 Sedona）                                                                         | Sprint 3    | [spatial-feature.md](docs/tech/components/spatial-feature.md)                                                                                    |
| 预警推送（I-05 贷后保全）                    | L4 应用接口                      | ✅ 已接入（T+1 推送）                                                                                      | Sprint 4    | [alerting-iv05.md](docs/tech/components/alerting-iv05.md)                                                                                        |
| 1104 报送（G11 三出口校验）                  | L5 合规                          | ✅ 已接入                                                                                                  | Sprint 4    | [reporting-1104.md](docs/tech/components/reporting-1104.md)                                                                                      |
| 前端驾驶舱（S5）                             | L5 展示                          | ✅ 已接入（端口 8500，零依赖插件架构）                                                                     | Sprint 5    | [frontend.md](docs/tech/components/frontend.md) · [frontend README](tools/frontend/README.md)                                                    |
| 运维（容器恢复 + 资源管家）                  | 运维                             | ✅ 已接入                                                                                                  | —           | [ops.md](docs/tech/components/ops.md)                                                                                                            |
| 倒排索引（审计检索，I-04）                   | L5                               | ⏳ 待接入（增强项，审计查询由 MySQL/ADS 直查满足）                                              | Sprint 6    | —                                                                                                                                                |
| Superset 看板（BI 层）                      | L5                               | 📋 规划中（考虑接入，连 Doris/MySQL 作为 BI 看板层）                                            | Sprint 6    | [superset.md](docs/tech/components/superset.md)                                                                                                  |

> 注：采集横向扩展当前由「容器编排采集系统」（master 主备 + 多 worker + Redis 任务队列）提供，已 ✅ 接入；上表「Kubernetes 横向扩展」为可选的更大规模 K8s 路径，本环境无 K8s 集群，且 worker 清单 `hostPath` 须改为节点真实路径后方可 `kubectl apply`。

## 当前状况（采集系统）

自研的容器编排采集系统（`tools/orchestrator/`）已完成开发与功能验证，能力层面满足要求：

- **调度架构**：master 主备（Redis 抢锁选主，standby 自动接管）+ 5 个泛化 worker + 宿主渲染服务，无第三方调度框架依赖。
- **代理体系**：双代理池（青果 qg 短效 1000 配额优先 + 免费池兜底），按缺口小批量补拉；全程强制走 IP 池，渲染服务对无代理请求返回 403，杜绝直连宿主 IP。
- **外层编排**：已接入 Airflow（DAG `guangdong_daily_crawl`，每天 00:30 触发），只做「拉起 + 盯完成 + 收尾」，不替换 Redis 实时派单。
- **ETL 管道**：已落地（DWD 入库 + ODS 数据湖 Parquet + geocode 异步最终一致补全，跨日去重与市场留存指标）。
- **全量运行结果（青果配额充足时实测）**：sale（出售）21 城 186,635 行 / 出数页率 99.2%；fangyuan（出租）链路修复后出数页率提升 11.8 倍（2.82%→33.33%），页均产出 18.27 行。
- **空转根因修复（7 项全落地）**：fangyuan 分页越界、验证码误判空页、Chrome 空壳页识别（66 样本零错判）、探针自伤 pkill、fd 上限、补池重试、渲染槽并发。
- **当前瓶颈（外部资源依赖）**：采集能力已就绪并通过验证，但持续全量爬取依赖代理配额——青果 qg 短效 1000 配额已耗尽，免费池出口 IP 被反爬验证码墙标记（通过率实测 0%）。待补充代理配额后全量重跑（Linux 新机迁移部署已实现，可作为获取未被标记出口 IP 的备选路径）。

## 落地与迁移（采集系统）

采集系统主体（调度 / 每城 IP 预算 / 增量断点续爬 / ETL / Airflow 编排 / Linux 新机部署）已实现并合入 dev；剩余为补充代理配额后的全量重跑验收：

| 项                                                            | 状态                    |
| ------------------------------------------------------------- | ----------------------- |
| 定时触发（Airflow DAG `guangdong_daily_crawl`，00:30）        | ✅ 已实现并合入 dev |
| ETL 跨日去重 / 入库 / 数据湖落盘（DAG 收尾自动执行）          | ✅ 已实现               |
| 轮次策略（取消 MAX_ROUNDS=3，读完/预算耗尽即终态）            | ✅ 已实现               |
| 每城 IP 预算（sale 600 / fangyuan 400+免费池，广深 15%）      | ✅ 已实现               |
| 增量断点续爬 + 回扫头部 2-3 页                                | ✅ 已实现               |
| Linux 新机部署（launchd → systemd、Airflow 同机、环境装依赖） | ✅ 已实现               |

> ✅ **渲染链路已验证**：fangyuan 渲染依赖宿主 Chrome。容器 Chrome（Linux/headless）曾被反爬按指纹软拦截（返回空心壳页，无 `zu-itemmod`），但空壳页识别已落地（66 样本零错判），且 `tools/orchestrator/render_smoke_test.sh`（`/render` 拿 zu-itemmod）已通过（sale 21 城 186,635 行 / fangyuan 出数页率 2.82%→33.33%）。新机部署仍建议先跑一次渲染冒烟测试，但此项已非「未实测」风险。

### 本地运行

前置：本机可用 Docker。

```bash
cp .env.example .env   # .env 已被 gitignore，勿提交真实密码
make up                # 启动 MySQL 业务源库（首次自动建表 + 灌入合成 seed）
make sql               # 进入 MySQL 交互终端（spacefin 库）
make down              # 停止
```

更多入口见根目录 `Makefile`（`make help`）。合成 seed 由 `seed/generate_seed.py` 生成（确定性、可复现），说明见 [seed/README.md](seed/README.md)。

### 交互体验（Interactive Demo）

平台提供零依赖 Web 驾驶舱（stdlib `http.server` + PyMySQL，端口 8500），10 个页面覆盖资产监控、空间风险、AVM、合规审计与策略沙盒。前端详情见 [tools/frontend/README.md](tools/frontend/README.md)。

**启动**（需先有 MySQL 与合成 seed，见上）：

```bash
# 启动前端（后台，日志落 output/frontend/app.log）
nohup tools/orchestrator/.venv/bin/python tools/frontend/app.py \
    --host 127.0.0.1 --port 8500 >> output/frontend/app.log 2>&1 &
# 浏览器打开 http://127.0.0.1:8500
```

**演示账号（RBAC 四角色，凭据写死在 `app.py` 的 `USERS`，dev-only）**：

| 账号 | 密码 | 角色 | 权限要点 |
|------|------|------|---------|
| `admin` | `admin20020309` | 系统管理员 | 全部权限 |
| `risk` | `risk20020309` | 风控策略经理 | 配置空间惩罚项 / LTV 阈值、确认 / 导出 |
| `da` | `da20020309` | 数据分析师 | 只读 + 导出（PII 自动脱敏） |
| `postloan` | `postloan20020309` | 贷后资产保全 | 仅看 LTV 预警，不可见 1104 报送页（403） |

**推荐点击路径（对应 PoC 设计稿 `docs/poc/raw-prototype/`）**：

| PoC 设计图 | 前端页面 | 可交互点 |
|-----------|---------|---------|
| 资产质量监控驾驶舱 Dashboard | 内置 `驾驶舱` 页 | KPI 卡片 + SVG 柱状图 |
| 区域贷款分布地图 | `P5 空间画像` | **点击地图网格点**弹 tooltip + 右侧高危区明细（最强交互记忆点） |
| 分类详情 | `P3 迁徙矩阵` | 五级分类下钻 |
| LTV 爆仓预警 | 内置 `预警列表` 页 | 两档徽标（强预警级 / 警示级） |
| 政策参数配置 | `P6 策略惩罚` | 空间惩罚项配置（仅 `risk`/`admin`） |

**重点演示项**：
- **P5 空间页**：SVG 地图网格可点选，直观展示空间风险维度——PoC 静态图在此升级为可交互可视化。
- **P7 AVM 页**：估值误差直方图 + 模型版本血缘（`R-UBQ-01`），特征归因读 `attribution_report.json`。
- **P9 合规审计页**：用 `da` 登录导出 → 客户号显示为 `c****{后4位}`，且 `ads_export_audit` 落审计行（AC-06）。
- **P10 策略沙盒页**：顶部红色「未校准」横幅，诚实声明 Critic 基准为合成种子（H3 商用前置）。
- **角色隔离**：用 `postloan` 登录点 1104 报送页会返回 403，直观体现 RBAC。

> 前端依赖 `output/` 下产物（如 `output/avm/avm_report.json`、`output/spatial/spatial_report.json`、`output/risk/dws_risk_class.csv`）；若演示机未跑过 pipeline，对应页显示「产物缺失」空态（已优雅处理，不崩溃）。演示前请确认产物已生成。

## 7 天风险演进演示（事件城市：广州）

平台内置一套 **7 天风险演进剧本**（`docs/demo/script_7d.md`）与验收标准（`docs/demo/acceptance.md`），完整演示「数据底座 → 估值 → LTV → 预警 → 人工处置 → 闭环」链路：

- **剧本主线**：第 4 天广州核心区挂牌价异动下探 → 数据底座 24 小时内传导到抵押物估值（AVM 重训）→ 广州贷款 LTV 集体上穿预警线 → 第 5 天预警达峰 → 风控批量确认与处置 → 第 7 天企稳收口。
- **灌数方式**：`tools/dev/backfill_7d.py --all` 逐日调用**真实引擎**（`tools/risk/main.py --date X --write-db` + `tools/alerting/main.py --date X`），处置记录复用 `ads_alert_confirm`（新增 `disposition_status / disposition_by / disposition_ts` 字段）。
- **数据规模**：客户 / 抵押物 / 贷款三表扩至 **5,000 笔**（广州约 1,250 笔，占比 25%）；`seed/generate_seed.py 5000` 重新生成并灌库。

> **诚实标注（重要）**：仅 `customer / collateral / loan` 三表为脚本合成；`crawl_housing_sale` 4.4 万条房源为**真实爬取**。D4 起的「广州挂牌价下探」是对真实爬取行做单价乘子的**演示脚本扰动**（原值记于 `ads_demo_gz_perturb`，可一键回滚），AVM 重训是真实引擎行为；7 天全链路为演示回填数据，不构成任何真实市场判断。

## 项目性质与范围说明

> 本项目**实质是校招作品集（portfolio）**，但按**最终交付版本（final deliverable）**的标准完整构建与验收：凡能凭代码 / 合成数据达成的技术能力均已落地，不降级为"作品集范围外"。仅真实外部资源依赖项诚实声明为未达成，列为交付缺口，而非视为缺陷。

**定位**：校招作品集；验收口径 = 技术 AC + 可复现性 + 测试 + 文档自洽（非商业 SLA）。能力项以"最终版是否交付"判定，不以"是否商用"裁剪。

**真实外部资源依赖项（诚实声明未达成，非 bug，列为交付缺口）**：
- 真实银行数据授权（Q1/Q2）—— 当前以 demo / mock / 合成种子回填驱动全链路
- 算法备案 / 监管合规审批（C-03）
- 试点行 / 机构试用意向（假设 H4）

**工程简化（已声明的近似，非能力缺失）**：

- Sedona → cKDTree 单机近似（架构等价性由 `tools/spatial/sedona_demo.py` 演示）
- Hive → MinIO + S3 TVF
- POI 以挂牌密度代理
- 通勤以直线距离近似（低估）
- Doris 单副本 + TRUNCATE 重灌
- Flink 无 checkpoint（实时仅预警联动，不闭环，PRD Non-goals）

**已交付能力（最终版补齐）**：
- 数据分类分级落列级元数据 + 导出按级强控（G2，`tools/frontend/data_classification.py`）
- PII 脱敏通道（脚本级脱敏 + `ads_export_audit` 审计，AC-06 实测 0 泄漏）
- 生产健康端点 `/api/metrics` + `manage.sh health`（G8）
- 分布式空间架构演示（G5）

**亮点 / 已实证**：

- AC-07 精度@覆盖率达标：45% 覆盖下 MAPE 9.88% ≤ 10%（全量 14.59%，基线 20.6%）
- 38% 异常估值经三分量归因查明为「基准自指」——`true_market_price` 由早期版本模型自己生成，真实模型误差仅 1/200 笔，故不做校正层
- Kafka + Flink 3s 端到端实时预警

**未验证假设**：H1/H5 已在数据侧实证；H3 已由最小 Critic 原型验证达成（美化偏见 KS 可检测：0.257 → 校准后 0.028 ≤ 0.05，机制演示口径见 [tools/persona/README.md](tools/persona/README.md)，真实基准属商用前置）。

## 合规与数据

- 项目涉及金融业务数据，严格执行合规脱敏流程处理，不涉及个人信息出境。
- 房产与地理数据通过合规开放 API / 地理编码服务获取。
- 合成行为仿真仅用于策略推演与产品设计验证，不作为任何个体授信决策依据。

## 许可证与免责

本仓库用于产品设计与技术交流，示例数据均为脱敏 / 模拟样本，不涉及真实个人金融信息。

---

## 开发环境与版本

> 开发环境 Python 统一到 **3.10**。宿主唯一 Python 环境 = **conda `spark`**（`tools/orchestrator/.venv` 是指向它的符号链接别名，脚本默认路径无需改动）；Docker worker / Airflow 目标机亦为 3.10。

| 工具                            | 版本                                           | 用途                                                                                                                                     |
| ------------------------------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Python（conda 环境 `spark`）    | 3.10.18（`tools/orchestrator/.venv` 是其别名） | **唯一宿主环境**：ETL / 编排 / 测试 / 宿主渲染 / 钩子 / PySpark 产品栈；依赖见 `environment.yml` + `tools/orchestrator/requirements.txt` |
| Python（worker/master 容器）    | 3.10（`python:3.10-slim`）                     | 采集 worker / master 容器                                                                                                                |
| Python（Airflow，Linux 目标机） | 3.10（apache-airflow 2.10.5）                  | 外层编排 DAG                                                                                                                             |
| pre-commit                      | 4.6.1                                          | 提交前 / 提交信息钩子                                                                                                                    |
| Node.js                         | v24.16.0                                       | commitlint 运行环境                                                                                                                      |
| npm                             | 11.13.0                                        | 依赖安装                                                                                                                                 |
| @commitlint/cli                 | 19.8.1                                         | 提交信息校验                                                                                                                             |
| @commitlint/config-conventional | 19.8.1                                         | Conventional Commits 规则                                                                                                                |

> 注：本机为 Linux 宿主（`/home/azureuser`），conda 为 **Miniforge**（位于 `~/miniforge`），唯一环境 `spark`（Python 3.10.18）。`conda` 不在默认 PATH，操作 `spark` 环境前先 `conda activate spark` 或 `source ~/miniforge/etc/profile.d/conda.sh`；Node v24（含 npm/npx）位于 `~/node24/bin`，同样不在默认 PATH，提交前先 `export PATH="$HOME/node24/bin:$PATH"`。

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
