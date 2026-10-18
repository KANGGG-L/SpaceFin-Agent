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

> 🧭 零基础读者可先读导览文件夹 [docs/overview/](docs/overview/README.md)：项目定位 → 系统架构 → 组件逐一介绍 → 数据与领域概念 → 快速上手。

本仓库按产品生命周期分为 **7 个阶段**（阶段 0–5 + 阶段 6 上线复盘），每个阶段回答一个核心决策问题并产出对应的开发成果。文档统一存放在 `docs/product/0X/`，开发产出（PoC / 原型）在 `docs/poc/`，产品演进的完整重建见 [docs/product/PRODUCT_EVOLUTION.md](docs/product/PRODUCT_EVOLUTION.md)，跨阶段的 MVP 执行计划在 `docs/tech/开发计划.md`。

> 🔎 **想追溯"某个能力为什么变成现在这样"**：入口是 [docs/TRACEABILITY.md](docs/TRACEABILITY.md)——它把每项能力映射到决策记录（`docs/adr/`）、证据条目（`docs/evidence/registry.json`）与版本/回退方式，并定义数字的可信度分级（`verified` / `recorded`）。本地自检：`python tools/dev/trace_check.py`。

> **阶段模型说明**：本仓库以 `docs/product/00`–`05` 的实际产物为权威阶段划分（机会识别 → 需求规划 → 技术可行性 → PRD → 设计/研发评审 → 测试验收），阶段 6（上线复盘）产出见 [docs/product/06/](docs/product/06/README.md)。该划分与产品全流程角色框架（需求规划 / PRD / 设计研发评审 / 研发跟进 / 测试验收 / 上线复盘）一一对应，研发跟进体现为 `docs/tech/开发计划.md` 的跨阶段执行计划。

| 阶段 | 核心决策问题                 | 交付物                                 | 开发产出                                                                                                                                                                   | 状态                                   | 文档                                                                                           |
| ---- | ---------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 0    | 这个机会值得做吗？           | 机会描述 + 假设清单 + 不做清单         | —                                                                                                                                                                          | ✅ 已完成                              | [docs/product/00/](docs/product/00/README.md)                                                  |
| 1    | 用户的真实痛点是什么？       | 用户画像、竞品分析、数据摸底、优先级   | —                                                                                                                                                                          | ✅ 已完成                              | [docs/product/01/](docs/product/01/README.md)                                                  |
| 2    | 技术上是否可行？             | 技术评估 + 效果基线                    | 数据底座 PoC 代码                                                                                                                                                          | ✅ 已完成                              | [docs/product/02/](docs/product/02/README.md)                                                  |
| 3    | 具体做成什么样？             | PRD（含验收标准）                      | 核心功能原型实现                                                                                                                                                           | ✅ 已完成                              | [docs/product/03/](docs/product/03/README.md)                                                  |
| 4    | 设计和技术方案对齐了吗？     | 评审纪要、待确认清单、设计规范         | 设计/研发评审材料（已组织评审回填）                                                                                                                                        | ✅ 材料+纪要已产出（结论待干系人确认） | [docs/product/04/](docs/product/04/README.md)                                                  |
| 5    | 测试验收与开发是否在轨道上？ | 测试用例、验收清单、上线风险、开发计划 | 测试框架 + 全量执行（8/8 AC 绿，**仅限合成种子一致环境**；核心四模块 frontend 604 + risk 141 + alerting 60 + avm 53 = 858 个 `def test_`（63 个测试文件）；全仓 `def test_` 合计 1361 个（其中 `*/tests/` 测试目录下 1346 个、101 个测试文件；`test_*.py` 共 109 个文件） | ✅ 已完成                              | [docs/product/05/](docs/product/05/README.md) · [docs/tech/开发计划.md](docs/tech/开发计划.md) |
| 6    | 上线复盘                     | 复盘报告、交付缺口清单                 | 阶段 6 复盘 + 最终版补齐（交付缺口已清点，可行项已补齐）                                                                                                                             | ✅ 已完成                              | [docs/product/06/](docs/product/06/README.md)                                                  |

## 方法论：从构想走向落地

本项目的推进遵循三条原则，贯穿全部阶段，覆盖产品设计与开发实现：

1. **先验证问题，再讨论方案**。每个阶段始于一个待证伪的命题，而非一个待实现的功能列表。阶段 0 的 5 条核心假设中，3 条被标为"生死假设"——只要一条被证伪，就推倒重来或调整方向。
2. **技术选型是阶段 2 的产物，不是阶段 0 的预设**。在确认机会和需求之前，不锁定任何具体技术路径。当前文档只讨论"需要什么能力"，不预设"用什么工具实现"。
3. **每个阶段产出都是下一阶段的输入**。不做"一锤子"方案，而是逐步收敛不确定性——从模糊想法 → 可验证假设 → 用户需求 → 方案设计 → 代码实现 → 交付上线。

> ### 🪶 精简版（lite）范围说明
>
> 本仓库当前为**精简版（lite）**——以核心风险闭环为主线，收窄技术栈范围：
>
> - **已移除实时层**：Kafka + Flink 实时预警链路（`tools/stream/`、`deploy/kafka-flink/`）已删除。预警改为 **T+1 批处理推送**（Airflow 排程），核心闭环不受影响。
> - **保留核心**：AVM 自动估值 → LTV 两档预警 → 1104 报送 → 前端驾驶舱，全部可跑。
> - **保留湖仓**：Doris + MinIO 仍保留，但只是 BI 副本（见下方「数据架构一句话」），**核心不依赖它**。
> - **新增 AI 能力**：策略沙盒接入 LangChain Agent（OpenAI 兼容网关，模型可配），支持「输入假设 → 推演」交互（详见 [前端 README](tools/frontend/README.md) 与 [AI 链路说明](docs/tech/ai-decompose-and-anthropic.md)）。
> - **数据口径**：除公开渠道爬取的房产挂牌行情（`crawl_housing_sale/_rent`）外，其余业务数据（客户 / 抵押物 / 贷款及派生报表）均为**脚本合成样本**，不含任何真实个人金融信息。以此为统一前提，**后续文档 / 页面 / 代码不再逐处标注"合成 / 演示"**，仅在必要时说明数据来源口径。
>
> #### 数据架构一句话
>
> 「核心闭环（估值 → 风控 → 报送 → 前端）读写同一个 MySQL 实例，但分两个库：**spacefin**（业务种子表 customer/collateral/loan）与 **spacefin_crawler**（爬虫原始表 + 全部 DWD/DWS/ADS/ODS 数仓表）；Doris + MinIO 只是给 Superset BI 看板用的**分析副本**，可独立于核心存在。MySQL 这一条是主链，Doris 作为『可选分析层』带过即可——它坏了核心照跑。」

> ### 🎯 交付范围界定
>
> 本项目为**独立研究与工程实践项目（非商用系统）**，「交付范围」明确限定为 **lite 核心闭环**：`AVM 自动估值 → LTV 两档预警 → 1104 报送 → 前端驾驶舱`，全部可运行、可演示。
>
> 以下能力**已评估 / 探索过，但明确不在交付范围**——属刻意的范围决策，不视为能力缺失：
>
> - **Kafka / Flink 实时层**：在当前交付周期内，T+1 批处理已满足核心闭环的时效需求；实时联动的投入量级更高，推迟。
> - **Kubernetes 横向扩展**：需真实集群与运维资源，非单机环境可交付；当前由「容器编排采集系统 + Redis 队列」覆盖横向扩展能力。
> - **代理池反爬爬虫**：依赖真实代理配额（青果 qg 短效等），配额耗尽即不可复现，不属可控资源。
> - **Doris / MinIO 湖仓 + Superset BI**：仅为分析副本 / 探索性看板，核心链（读写 MySQL）不依赖它。
>
> 取舍原则：优先保证核心闭环可复现、可归因，再按需扩展技术栈。详见 [范围决策说明](docs/product/范围决策说明.md)。

### 当前状态：

```
├── 阶段 0 ✅ 机会识别（已完成）
├── 阶段 1 ✅ 需求调研（已完成）
├── 阶段 2 ✅ 技术可行性 → 数据底座 PoC（已完成）
├── 阶段 3 ✅ PRD + 核心功能原型（已完成）
├── 阶段 4 ✅ 设计与研发评审（材料 + 评审纪要已产出）
├── 阶段 5 ✅ 测试验收（8/8 AC 全绿，仅限合成种子一致环境；精简版移除 Kafka/Flink 实时层测试；核心四模块 858 个 `def test_`；全仓 `def test_` 合计 1361 个，其中 `*/tests/` 目录内 1346 个）
└── 阶段 6 ✅ 上线复盘（交付缺口已清点，可行项已补齐）
```

> 测试运行方式（`Makefile` 未提供 test target）：
>
> - 全仓测试：`python3 -m pytest tools/ -q`
> - 核心闭环四模块：`python3 -m pytest tools/frontend/tests tools/risk/tests tools/alerting/tests tools/avm/tests -q`
> - 统计用例数：`grep -rh --include='test_*.py' 'def test_' . | wc -l`（全仓 1361；仅 `*/tests/` 测试目录内 1346）
>
> 本 README 只声明测试规模与运行方式，不声明通过率。

## 工程实现（按需接入，循序渐进）

产品决策文档之外，本平台的可运行工程实现在确有需要时逐层引入：每个组件在其业务需要出现时才补入，并同步补充[组件技术说明](docs/tech/components/)与下表登记，最终组成一条按 L0→L5 分层的完整链路。

### 组件清单（按层级）

| 组件 | 层级 | 状态 | 技术说明 |
| ---- | ---- | ---- | -------- |
| MySQL 业务源库 | L0 数据源头 | ✅ 已接入 | [mysql-source.md](docs/tech/components/mysql-source.md) |
| 安居客采集器 | L0 公开数据获取 | ✅ 已接入 | [anjuke-crawler.md](docs/tech/components/anjuke-crawler.md) |
| Redis 任务队列 | 采集中间件 | ✅ 代码集成 | [redis-task-queue.md](docs/tech/components/redis-task-queue.md) |
| jhao104/proxy_pool 代理池 | 采集反爬（IP 轮换） | ✅ 客户端集成（探索，非交付范围） | [proxy-pool.md](docs/tech/components/proxy-pool.md) |
| 容器编排采集系统 | 分布式调度 + 宿主渲染 | ✅ 已接入 | [crawler-orchestrator.md](docs/tech/components/crawler-orchestrator.md) |
| Airflow 外层编排 | 每日定时触发 + 收尾（DAG 00:30）+ 故障韧性（失败派发 / 采集闸门 / 严格排空 / 终态审计，默认关） | ✅ 已接入 | [airflow/README.md](airflow/README.md) · [airflow.md](docs/tech/components/airflow.md) |
| ETL 数据管道 | 跨日去重 + DWD 入库 + ODS 湖 | ✅ 已接入 | [crawler-etl.md](docs/tech/components/crawler-etl.md) |
| Kubernetes 横向扩展（可选） | 采集横向扩展（K8s 替代路径） | ⏸ 清单已备（hostPath 待改），待 K8s 集群（探索，非交付范围） | [k8s-crawler-cluster.md](docs/tech/components/k8s-crawler-cluster.md) |
| MySQL binlog CDC | L0 变更接入 | ✅ 已接入 | [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| CDC 下游消费链 | L0 增量同步 | ✅ 已接入（改一笔 loan → ODS 可见：AC-01 正式验收记录 **0.32s**，验收线 ≤10s） | [cdc-downstream.md](docs/tech/components/cdc-downstream.md) |
| Doris + MinIO 湖仓 | L0 分层（BI 副本） | ✅ 已接入（ODS/DWD/DWS/ADS 四层 24 表；BI 分析副本，核心链不依赖 Doris，全走 MySQL；探索，非交付范围） | [doris-lake.md](docs/tech/components/doris-lake.md) |
| Kafka + Flink 实时 | L1 | ❌ 已移除（精简版：实时预警改为 T+1 批处理推送；实时层原属探索，非交付范围） | — |
| AVM（GBDT+空间特征） | L3 | ✅ 已接入（模型 `2026-08-20-r2`：45% 覆盖 MAPE 9.94% ≤10%；全量 15.00%；基线 20.58%） | [avm.md](docs/tech/components/avm.md) · [tools/avm/README.md](tools/avm/README.md) |
| 风险引擎（LTV 两档预警 / 五级分类 / 低置信） | L3 | ✅ 已接入（LTV 警示线 0.75 / 强预警线 0.85，均可配置，等号边界严格大于不触发；低置信：空间特征缺失率 >75） | [risk-engine.md](docs/tech/components/risk-engine.md) |
| L2 空间特征（高危区/POI/通勤） | L2 | ✅ 已接入（单机近似，降级 Sedona） | [spatial-feature.md](docs/tech/components/spatial-feature.md) |
| 贷后预警推送 | L4 应用接口 | ✅ 已接入（T+1 推送：site_inbox + 文件 + 可选 postloan_http webhook；Airflow 已排程） | [alerting-iv05.md](docs/tech/components/alerting-iv05.md) |
| 1104 报送（三出口校验） | L5 合规 | ✅ 已接入 | [reporting-1104.md](docs/tech/components/reporting-1104.md) |
| 前端驾驶舱 | L5 展示 | ✅ 已接入（端口 8500，零依赖插件架构） | [frontend.md](docs/tech/components/frontend.md) · [frontend README](tools/frontend/README.md) |
| 运维（容器恢复 + 资源管家） | 运维 | ✅ 已接入 | [ops.md](docs/tech/components/ops.md) |
| 倒排索引（审计检索） | L5 | ✅ 已接入（Doris 中文倒排索引，毫秒级敏感词检索，3 测试绿） | [inverted-index.md](docs/tech/components/inverted-index.md) · [sql/doris/01_compliance_audit_inverted.sql](sql/doris/01_compliance_audit_inverted.sql) · [tools/compliance/](tools/compliance/) |
| Superset 看板（BI 层） | L5 | ✅ 已接入（Superset 4.1.2 已起、连 Doris ADS，3 图表+示例仪表盘已建；探索，非交付范围） | [superset.md](docs/tech/components/superset.md) · [deploy/superset/](deploy/superset/) |

> 注：采集横向扩展当前由「容器编排采集系统」（master 主备 + 多 worker + Redis 任务队列）提供，已 ✅ 接入；上表「Kubernetes 横向扩展」为可选的更大规模 K8s 路径，本环境无 K8s 集群，且 worker 清单 `hostPath` 须改为节点真实路径后方可 `kubectl apply`。

> ### 🛰️ ODS 变更日志 vs ODS 数据湖（CDC，I-01）
>
> 本仓有两处都叫「ODS」、但**不是一回事**，容易混淆：
>
> - **ODS 数据湖（Parquet）**：由 ETL 管道（`tools/orchestrator/etl.py`）把爬取的房源 JSONL 落盘到 `data_lake/cdc/ods/`，属采集侧，与下方无关。
> - **ODS 变更日志（MySQL 表，`spacefin_crawler.ods_cdc_log` / `ods_cdc_position` / `ods_cdc_consumer_offset`）**：这是 **binlog CDC 的贴源层**，只记录 `spacefin` 业务库 `loan/collateral/customer` 的 INSERT/UPDATE/DELETE，**不存当前快照**。
>
> ⚠️ **默认空表是预期，不是故障**：本仓库 demo 数据由批处理（`tools/risk/main.py`）直灌 DWS，**不经过 binlog**，所以 ODS 表在新环境必然是空的——页面（P1 数据底座）会标成灰「空表」而非红「异常」，语义是「还没产出 / 无变更流入」。要让它非空、且能自动记录，两种方式：
>
> - **初始快照补数（一次性）**：`python tools/cdc/bootstrap_ods.py` 把当前三张主表全量合成成 INSERT 事件写入 `ods_cdc_log`（相当于 CDC 的「初始全量快照」阶段），并落好 `ods_cdc_position` 位点；加 `--force` 可清空重补。随后 `python tools/cdc/consumer.py --once` 把这些事件幂等重放到 DWS/ADS，结果与批处理一致、可互证。
> - **实时自动记录**：常驻 `python tools/cdc/main.py`（blocking 监听 binlog）→ 源库任何变更秒级落入 ODS；`python tools/cdc/consumer.py --loop`（或 `--once`）增量重放至 DWS/ADS。生产由 systemd 单元 `spacefin-cdc.service` / `spacefin-cdc-consumer.service` 托管；无 systemd 的环境手动常驻启动即可（binlog 用户与密码可通过 `MYSQL_CDC_USER` / `MYSQL_CDC_PASSWORD` 覆盖）。
>
> 配套修复：`consumer.py` 的 `write_offset` 已补写 `updated_at`（否则 `ods_cdc_consumer_offset.updated_at` 为 NULL，会让 P1 页 `_cdc_state` 在 `int(row[3])` 处 500）；P1 页 `_cdc_state` 现已对 NULL 时间戳容错，符合本页「降级不 500」原则。

### 工作流加固（DAG 故障韧性）

日终 DAG `guangdong_daily_crawl` 在「拉起 + 盯完成 + 收尾」之外，提供四类韧性能力，**默认关、仅告警不挡**，避免单点故障误杀整条日终链路：

- **失败派发**：任意任务失败触发 `on_failure_callback`，向 Slack incoming-webhook 推送覆盖产品 / 开发 / QA / 审核的告警；配置 `SPACEFIN_ALERT_SLACK_WEBHOOK` 后生效。
- **采集质量闸门**（`crawl_quality_gate`）：按「新增」与「带页码证据的刷新」双口径统计当日 `crawl_housing_sale/rent`，**须在 `etl_finalize` 之后运行**。重爬存量市场时新增恒为 0、但有刷新证据属正常稳态（告警放行）；只有 `new==0 且 refreshed==0`（当天一行都没扫到）才 `exit 2` 拦停。
- **严格排空**（`drain_workers`）：排空遇非预期残工默认尽力告警，`SPACEFIN_DRAIN_STRICT=1` 才抛错；排空后由 `stop_crawl_stack` 任务关闭采集栈容器，避免常驻占用内存。
- **终态失败审计**（`alerting_audit`）：推送后只读核查 `ads_alert_dispatch` 终态失败并告警，避免贷后推送失败对值班人员不可见。

实现见 `tools/orchestrator/slack_notify.py`、`tools/orchestrator/crawl_quality_gate.py`、`tools/alerting/audit_final_failed.py`；配置项见 `.env.example`。

## 采集系统现状与落地

自研容器编排采集系统（`tools/orchestrator/`）已完成开发与功能验证，主体（调度 / 每城 IP 预算 / 增量断点续爬 / ETL / Airflow 编排 / Linux 新机部署）均已实现；日链路已在开发机端到端跑通一次真实调度 run（闸门 → ETL → 空间 → 风险 → 快照 → 告警 → lake_sync → Doris 对账全绿，`dag_run success`）。当前运行态为 sale-only + 免费池（见下方运行策略），全量（42 任务）重跑验收待代理配额到位。

- **调度架构**：master 主备（Redis 抢锁选主，standby 自动接管）+ 5 个泛化 worker + 宿主渲染服务，无第三方调度框架依赖。
- **代理体系**：双代理池（短效配额优先 + 免费池兜底），全程强制走 IP 池，渲染服务对无代理请求返回 403，杜绝直连宿主 IP；配额耗尽时仅用免费池。
- **任务裁剪（TASK_TYPES）**：`tools/orchestrator/master.py` 的任务清单由 `TASK_TYPES` 环境变量驱动，默认 `sale,fangyuan` 共 42 任务；当前运行态为 `TASK_TYPES=sale`（21 任务）——fangyuan 依赖宿主 Chrome 渲染 + 代理配额，配额耗尽期间长期无产出，先止损空转；配额续期后把 `.env` 改回 `TASK_TYPES=sale,fangyuan` 并重建采集栈镜像即可恢复。
- **外层编排**：已接入 Airflow（DAG `guangdong_daily_crawl`，每天 00:30 触发），只做「拉起 + 盯完成 + 收尾」，不替换 Redis 实时派单；排空后 `stop_crawl_stack` 任务爬完即关采集栈，避免常驻占用内存；其故障韧性见上「工作流加固」。
- **ETL 管道**：已落地（DWD 入库 + ODS 数据湖 Parquet + geocode 异步最终一致补全，跨日去重与市场留存指标）。
- **全量运行结果（配额充足时的完整实测）**：sale 21 城 186,635 行 / 出数页率 99.2%；fangyuan 出数页率提升 11.8 倍（2.82%→33.33%），页均产出 18.27 行。⚠️ 上述数字为代理配额充足时的完整实测，配额耗尽环境下 `crawl_housing_sale` 仅 36,652 行，相应全量指标不可在该环境复现。**证据口径**：这些数字来自历史运行记录（etl_report / 采集账本），原始产物未随仓库分发，且「出数页率」不落 DWD 表（见 `tools/orchestrator/crawl_quality_gate.py` 注释），**当前环境无法离线复核**。
- **空转根因修复（7 项全落地）**：fangyuan 分页越界、验证码误判空页、Chrome 空壳页识别、探针自伤、fd 上限、补池重试、渲染槽并发。
- **当前瓶颈与运行策略（外部资源依赖）**：全量爬取依赖代理配额。当前策略：sale 只用免费池维持日刷新（`QG_ENABLED=0` + `TASK_TYPES=sale`），fangyuan 暂停；配额续期后恢复 42 任务全量。

落地验收清单：

| 项 | 状态 |
| ---- | ---- |
| 定时触发（Airflow DAG `guangdong_daily_crawl`，00:30） | ✅ 已实现 |
| ETL 跨日去重 / 入库 / 数据湖落盘（DAG 收尾自动执行） | ✅ 已实现 |
| 轮次策略（读完 / 预算耗尽即终态） | ✅ 已实现 |
| 每城 IP 预算（波次状态机 floor 5页 / rescue / depth；sale 500 / fangyuan 500，广深各 60 其余 19 城各 20） | ✅ 已实现 |
| 增量断点续爬 + 回扫头部 2-3 页 | ✅ 已实现 |
| Linux 新机部署（systemd、Airflow 同机、环境装依赖） | ✅ 已实现（开发机验证；标准 Linux 新机端到端调度未验证，见 [airflow/README.md](airflow/README.md) §1） |
| 工作流加固（失败派发 / 采集闸门 / 严格排空 / 终态审计，默认关） | ✅ 已实现 |
| 资源治理（排空后 `stop_crawl_stack` 爬完即关采集栈，防内存占用） | ✅ 已实现 |

> ✅ **渲染链路已验证（开发机）**：fangyuan 渲染依赖宿主 Chrome。容器 Chrome 曾被反爬按指纹软拦截（返回空心壳页，无 `zu-itemmod`），但空壳页识别已落地，`tools/orchestrator/render_smoke_test.sh` 在开发机已通过；**Linux 宿主 Chrome 反爬从未实测**（见 [airflow/README.md](airflow/README.md) §4）。新机部署前须先跑一次渲染冒烟测试。

### 本地运行

前置：本机可用 Docker。Linux 直接用 `docker`；WSL / Windows 用 `docker.exe` + Docker Desktop，并确认容器内 `host.docker.internal` 能解析到宿主（WSL2 下 Docker Desktop 默认已支持）。

本仓提供两条落地路径，按你想要的"完成度"选：

#### 路径 A · 最小可跑（只看驾驶舱，不采集）

只需 MySQL 业务库 + 前端，适合快速看 UI / RBAC / 合成 seed 驱动的页面。**前置条件**：前端会读取 `spacefin_crawler` 库，需先创建该库与 `spacefin_crawler_app` 账号（由 `tools/orchestrator/sql/crawl_schema.sql` 建立，ETL 会注入 `MYSQL_APP_PASSWORD`）；连接参数通过环境变量 `MYSQL_APP_USER / MYSQL_APP_PASSWORD / MYSQL_HOST` 注入（可在 `.env` 设置，代码内含默认值）。完整上手见 [docs/overview/getting-started.md](docs/overview/getting-started.md)。

```bash
cp .env.example .env   # .env 已被 gitignore，勿提交真实密码
make up                # 启动 MySQL 业务源库（首次自动建表 + 灌入合成 seed）
make sql               # 可选：进入 MySQL 交互终端（spacefin 库）
make down              # 停止
```

然后按下方「交互体验」起前端、用演示账号登录即可。

> ⚠️ 未跑过 pipeline 时，P5 空间页 / P7 AVM 页 / P3 迁徙矩阵等依赖 `output/` 产物的页面会显示「产物缺失」空态（已优雅处理，不崩溃）。要看到完整数据，走路径 B。
>
> 📦 P7 归因产物体（`output/avm/bias_diag/`）同样不进 git，且依赖风险引擎先产出 `dws_risk_class`——路径 B 第 7 步用 `make avm-diag` 幂等生成（见下）。

> 📦 **AVM 模型产物（当前不随仓库分发）**：模型 `output/avm/model.joblib`（约 22MB）位于 `output/` 目录，该目录被 `.gitignore` 整目录排除（`origin/main` 亦不含 `output/`）。工作区虽已在 `.gitattributes` 中声明将 `output/avm/model.joblib` 交由 Git LFS 管理，但该声明尚未提交、LFS 亦无已跟踪对象，**因此 clone 后不会自动获得模型**。使用 AVM / 风险引擎前需在本机生成产物：`python3 tools/avm/train.py`（依赖已入库的 DWD 房源数据；训练后 `output/avm/avm_report.json` 会记录模型 version 与指标）。

#### 路径 B · 全量本地链路（采集 + ETL + 空间 + 前端）

> **关键认知**：`make up` **只起 MySQL**。Redis 任务队列、宿主 Chrome 渲染、ETL、空间特征都**不会自动起来**，必须按下面顺序手动确认。完整渲染服务搭建见 [airflow/README.md](airflow/README.md) §3（依赖前置）与 §4（宿主 Chrome 反爬）。

> 📊 **Superset BI 看板需独立部署**：`make up` 只起 MySQL 业务库；Superset 看板需另行 `docker compose up -d superset`（镜像本地构建 + PG 元数据），本环境默认未运行。它与前端驾驶舱是「固化决策视图 vs 探索性 BI」的互补关系——前者是核心闭环固化为 Web 的展示层，后者用于对 Doris ADS 做自由探索分析。

按序执行，每步都带"怎么确认它好了"的校验点：

1. **建 Redis 网络与容器（从零部署必做）**

   ```bash
   make infra        # 幂等：网络已存在则忽略，容器已存在则启动
   ```

   ✅ 校验：`docker network ls` 含 `spacefin-net`；`docker ps` 含 `spacefin-redis`（6379 映射）。

2. **起 MySQL 业务库**

   ```bash
   make up
   ```

   ✅ 校验：`make sql` 能连上 `spacefin` 库。

3. **起宿主 Chrome 渲染服务（fangyuan / rent 必依赖）**
   - 容器内通过 `host.docker.internal:8899/render` 访问宿主渲染；宿主机本机探活用 `127.0.0.1:8899` 的 `/health` 或 `/render`。
   - 服务未起时，fangyuan / rent 页面爬不到数据（返回空壳页，无 `zu-itemmod`）；master 会**明确告警「渲染服务不可达」并终态失败**，不会静默烧代理。

   ```bash
   bash tools/orchestrator/render_smoke_test.sh   # 探活：/health 可达即门禁通过
   ```

   ✅ 校验：冒烟脚本输出渲染服务可达；迁移新机验收需确认能拿到 `zu-itemmod`（详见 §4）。

4. **跑采集（调度手动拉起 / Airflow 二选一）**
   - 手动拉起 master + worker：详见 [crawler-orchestrator.md](docs/tech/components/crawler-orchestrator.md)。
   - 或接 Airflow：DAG `guangdong_daily_crawl` 每天 00:30 触发，只做「拉起 + 盯完成 + 收尾」。

5. **ETL 必须在爬取完成之后再跑（最容易踩的坑）**
   - ⚠️ 不要只对爬取早期的快照跑一次 ETL——此时大部分已爬数据还留在原始 JSONL 里，进不了库（实测早期快照只入了约 14%）。
   - 正确做法：等采集跑完再手动 `tools/orchestrator/etl.py --backfill`，或交给 Airflow DAG 的 `etl_finalize` 任务在收尾阶段自动触发。

6. **跑空间特征**（需要 P5 空间页时）

   ```bash
   python tools/spatial/main.py --once
   ```

   ✅ 校验：`ads_spatial_zone` 有数据。

7. **生成 P7 归因产物体（需要 AVM 归因时；幂等，可反复跑）**

   > 前端 P7「异常估值归因」区块读取 `output/avm/bias_diag/` 的 `rows.json` + `attribution.json`。
   > 这两个产物**不进 git**（`output/` 整目录忽略），换机 clone / 重训后不会自带，必现「离线归因分析尚未生成」空态。
   > 用幂等入口一键生成：AVM 模型指纹（`avm_report.json` 的 version/trained_at）未变 && 产物齐全 → 跳过；模型重训或产物缺失 → 自动按序重建。

   ```bash
   make avm-diag   # 幂等；也可 `python3 tools/avm/gen_attribution.py`
   ```

   > ⚠️ **前置时序**：归因分解读 `dws_risk_class`（风险引擎 T+1 批处理产出），须在风险引擎跑完后执行；
   > 采集/ETL 只影响房源，不会填充 dws_risk_class。无 make 的环境直接跑脚本体命令即可。
   > ✅ 校验：`output/avm/bias_diag/rows.json` 与 `attribution.json` 存在，且页面 P7 归因区块不再显示「尚未生成」。

8. **起前端 + 验收**
   ```bash
   nohup python3 tools/frontend/app.py \
       --host 127.0.0.1 --port 8500 >> output/frontend/app.log 2>&1 &
   ```
   ✅ 校验：登录后访问 P1 / P5 / P6 及各 `/api/*` 均返回 200。

#### 运维 FAQ（常见现象与处理）

- **`output/` 权限**：`output/` 可能被 Docker 以 root 创建；宿主机非 root 用户直接跑 ETL 写 `etl_processed.json` 会报 `PermissionError`。建议在容器内以 root 跑 ETL，或 `chown` 把 `output/` 还给当前用户。
- **P1 数据底座 ODS 显示「空表」**：ODS 变更日志只记 binlog 变更、不存快照；demo 数据由批处理直灌 DWS、不走 binlog，新环境 ODS 必为空，属预期（页面「空表」与「异常」区分着色）。需要记录变更时，先 `python tools/cdc/bootstrap_ods.py` 做初始快照，再常驻 `python tools/cdc/main.py`（详见上方「ODS 变更日志 vs ODS 数据湖」）。
- **Doris `TRUNCATE` 是异步的**：`tools/lake/sync.py` 的装载是「先 `TRUNCATE` 再 stream load」，但 Doris `TRUNCATE` 返回时 BE 尚未真正清空，紧接着装载会把旧行与新行叠加（ODS 行数翻倍）。代码已用 `truncate_table_wait()`（TRUNCATE 后轮询 `SELECT COUNT` 到 0 再装载）规避；若仍出现翻倍，先手动 `TRUNCATE` 相关表再重跑。
- **采集闸门在存量市场「新增为 0」是正常稳态**：重爬已入库房源时 `first_seen_date={{ds}}` 恒为 0；闸门只在 `new==0 且 refreshed==0`（当天一行都没扫到）时才拦停，`new==0 但 refreshed>0` 告警放行。

更多入口见根目录 `Makefile`（`make help`）。合成 seed 由 `seed/generate_seed.py` 生成（确定性、可复现），说明见 [seed/README.md](seed/README.md)。

#### 路径 C · 本地开发热重载（前后端改代码自动重启）

适合改 `tools/frontend/*` 或 `tools/orchestrator/master.py` 时边写边验证，无需手动重启进程。

仓库自带两个零依赖工具（纯 stdlib，不往环境塞任何新包）：

- `dev.sh`：一键确保 MySQL + Redis 起好，再 exec 热重载监督器。**不依赖 `make`**（本机 WSL 若没装 make 也能用）。
- `dev_server.py`：前端(8500) + 后端 master(5102) 文件监听监督器，源码改动即重启对应进程。

```bash
./dev.sh                          # 拉起前后端 + 热重载（前端 :8500 / 后端 :5102）
./dev.sh --no-backend             # 只拉前端
python3 dev_server.py --backend-port 5102   # 自定义端口
```

设计要点（避免与 Docker 采集栈打架）：

- 原生后端 master 默认跑 **5102**，避开 Docker orchestrator 栈里 `master-primary` 占用的 **5100**；两者可并存、互不影响。
- 原生 master 连 **Redis db=1**（`REDIS_DB`，默认 1），与 Docker master（db=0）隔离，**不抢 `spacefin:master:leader` 锁**，不会打断真实采集链路。
- 前端监听整个 `tools/frontend`（含 `db.py` / `data_classification.py` / `pages` / `static`），覆盖内置 `SF_HOTRELOAD` 漏掉的盲区；保持 `SF_HOTRELOAD` 关闭，避免双重重启。

日志：`output/dev/frontend.log`、`output/dev/backend.log`。停止：`pkill -f dev_server.py`（连带回收子进程）。

> 若端口被占用，监督器启动即报错并提示 `ss -ltnp | grep :<port>` 与 `docker stop spacefin-crawler-master-primary`，不会静默重启循环。

### 交互体验（Interactive Demo）

平台提供零依赖 Web 驾驶舱（stdlib `http.server` + PyMySQL，端口 8500），9 个导航页（P10 策略沙盒已并入 P6 策略推演中心）覆盖资产监控、空间风险、AVM、合规审计与策略沙盒。前端详情见 [tools/frontend/README.md](tools/frontend/README.md)。

**启动**（需先有 MySQL 与合成 seed，见上）：

```bash
# 启动前端（后台，日志落 output/frontend/app.log）
nohup python3 tools/frontend/app.py \
    --host 127.0.0.1 --port 8500 >> output/frontend/app.log 2>&1 &
# 浏览器打开 http://127.0.0.1:8500
```

**演示账号（RBAC 两角色：`app.py` 的 `_ROLE_LABELS` 实际仅 `admin`/`postloan`，风控/分析权限已并入 `admin`）**：

| 账号       | 密码       | 角色         | 权限要点                                                                          |
| ---------- | ---------- | ------------ | --------------------------------------------------------------------------------- |
| `admin`    | `admin`    | 系统管理员   | 全部权限（含原 `risk`/`da` 的空间惩罚项配置、LTV 阈值、确认与导出、PII 脱敏导出） |
| `postloan` | `postloan` | 贷后资产保全 | 仅看 LTV 预警，不可见 1104 报送页（403）                                          |

> 口令来源（`app.py` `_resolve_passwords`）：设 `SF_DEV_MODE=1` 时用「账号名即密码」的弱口令（即上表口径，仅限本地演示）；未设时启动随机生成并落盘到 `output/frontend/credentials.json`，也可用环境变量 `SF_PWD_ADMIN` / `SF_PWD_POSTLOAN` 覆盖。

**推荐点击路径（对应 PoC 设计稿 `docs/poc/raw-prototype/`）**：

| PoC 设计图                   | 前端页面           | 可交互点                                                        |
| ---------------------------- | ------------------ | --------------------------------------------------------------- |
| 资产质量监控驾驶舱 Dashboard | 内置 `驾驶舱` 页   | KPI 卡片 + SVG 柱状图                                           |
| 区域贷款分布地图             | `P5 空间画像`      | **点击地图网格点**弹 tooltip + 右侧高危区明细（重点交互点） |
| 分类详情                     | `P3 迁徙矩阵`      | 五级分类下钻                                                    |
| LTV 爆仓预警                 | 内置 `预警列表` 页 | 两档徽标（强预警级 / 警示级）                                   |
| 政策参数配置                 | `P6 策略推演中心`  | 空间惩罚项配置（仅 `admin`）                                    |

**重点演示项**：

- **P5 空间页**：SVG 地图网格可点选，直观展示空间风险维度——PoC 静态图在此升级为可交互可视化。
- **P7 AVM 页**：估值误差直方图 + 模型版本血缘（`R-UBQ-01`），特征归因读 `output/avm/bias_diag/attribution.json`（三分量离线诊断）。
- **P9 合规审计页**：用 `admin` 登录导出 → 客户号显示为 `c****{后4位}`，且 `ads_export_audit` 落审计行（AC-06）。
- **P10 策略沙盒**（已并入 `P6 策略推演中心`，导航无独立入口）：沙盘区块顶部显示校准状态横幅（`已校准` / `未校准`），Critic 基准为合成种子（H3）。
- **角色隔离**：用 `postloan` 登录点 1104 报送页会返回 403，直观体现 RBAC。

> 前端依赖 `output/` 下产物（如 `output/avm/avm_report.json`、`output/spatial/spatial_report.json`、`output/risk/dws_risk_class.csv`）；若演示机未跑过 pipeline，对应页显示「产物缺失」空态（已优雅处理，不崩溃）。演示前请确认产物已生成。

#### 演示自检与远程分享（`tools/ops/`）

- `tools/ops/demo_precheck.sh`：演示前一键自检（建议演示前一天与开演前各跑一次）。默认全量检查（含隧道端到端实测，约 40 秒），`--fast` 只查本机状态；退出码 `0`=全部通过、`1`=存在阻断项（大概率翻车项）、`WARN` 不阻断但需留意。
- `tools/ops/cloudflared_demo.sh`：用 Cloudflare 免费快速隧道把本机前端（默认 `http://127.0.0.1:8500`）临时暴露到公网，便于他人远程访问演示，无需开放端口或配置域名。每次 URL 随机变化、进程停止即失效，仅适合临时演示；数据栈仍只绑回环/内网，不随隧道暴露。

## 7 天风险演进演示（事件城市：广州）

平台内置一套 **7 天风险演进剧本**与验收标准，完整演示「数据底座 → 估值 → LTV → 预警 → 人工处置 → 闭环」链路：

- **剧本主线**：第 4 天广州核心区挂牌价异动下探 → 数据底座 24 小时内传导到抵押物估值（AVM 重训）→ 广州贷款 LTV 集体上穿预警线 → 第 5 天预警达峰 → 风控批量确认与处置 → 第 7 天企稳收口。
- **灌数方式**：`tools/dev/backfill_7d.py --all` 逐日调用**真实引擎**（`tools/risk/main.py --date X --write-db` + `tools/alerting/main.py --date X`），处置记录复用 `ads_alert_confirm`（新增 `disposition_status / disposition_by / disposition_ts` 字段）。
- **数据规模**：客户 / 抵押物 / 贷款三表扩至 **5,000 笔**（广州约 1,250 笔，占比 25%）；`seed/generate_seed.py 5000` 重新生成并灌库。

> **数据来源口径**：`customer / collateral / loan` 三表为脚本合成（业务主档）；`crawl_housing_sale` 为**真实爬取**房源——历史运行记录中，生产环境 ODS 贴源存量（crawl_housing_sale）截至 2026-08-31 为 59,081 条（DWD 去重后 dwd_housing_sale = 59,030 条）（引自 `output/guangdong/etl_report.json` 的 `stock.sale_total`；该报告位于被 gitignore 的 `output/`，未随仓库分发，数字为历史运行记录）；7 天演示库取其中 **44,369** 条（广东 21 城）。D4 起的「广州挂牌价下探」是对真实爬取行做单价乘子的**脚本扰动**（原值记于 `ads_demo_gz_perturb`，可一键回滚），AVM 重训是真实引擎行为；7 天全链路为演示回填数据，不构成任何真实市场判断（数据构成见上「精简版范围说明 · 数据口径」）。

## 项目性质与范围说明

> 本项目为**独立研究与工程实践项目（非商用系统）**，按**最终交付版本（final deliverable）**的标准完整构建与验收：凡能凭代码 / 合成数据达成的技术能力均已落地，不做降级处理。仅真实外部资源依赖项诚实声明为未达成，列为交付缺口，而非视为缺陷。

**定位**：非商用研究性项目；验收口径 = 技术 AC + 可复现性 + 测试 + 文档自洽（非商业 SLA）。能力项以"最终版是否交付"判定，不以"是否商用"裁剪。

**真实外部资源依赖项（诚实声明未达成，非 bug，列为交付缺口）**：

- 真实银行数据授权（Q1/Q2）—— 当前以 demo / mock / 合成种子回填驱动全链路
- 算法备案 / 监管合规审批（C-03）
- 试点行 / 机构试用意向（假设 H4）

**工程简化（已声明的近似，非能力缺失）**：

- Sedona → cKDTree 单机近似（架构等价性由 `tools/spatial/sedona_demo.py` 演示）
- Hive → MinIO + S3 TVF（Hive 仅为早期备选，未实际引入；湖仓用 Doris 实现）
- POI 以挂牌密度代理
- 通勤以直线距离近似（低估）
- Doris 单副本 + TRUNCATE 重灌（⚠️ Doris `TRUNCATE` 是**异步**的，重灌前必须等到 BE 真正清空，否则旧行残留与新行叠加致 ODS 行数翻倍——见下方「运维 FAQ」）
- Flink 无 checkpoint（实时层 Kafka/Flink 已于精简版移除；预警改为 T+1 批处理推送，不闭环，PRD Non-goals 仍成立）

**已交付能力（最终版补齐）**：

- 数据分类分级落列级元数据 + 导出按级强控（`tools/frontend/data_classification.py`）
- PII 脱敏通道（脚本级脱敏 + `ads_export_audit` 审计，AC-06 实测 0 泄漏）
- 生产健康端点 `/api/metrics` + `manage.sh health`
- 分布式空间架构演示（`tools/spatial/sedona_demo.py`）
- 工作流加固：DAG 失败派发（四角色 Slack）/ 采集质量闸门 / 严格排空 / 终态审计（默认关、warn-only）

**指标与证据等级**：

本仓库 `output/`、`data_lake/` 均被 `.gitignore` 整目录排除，模型与评估产物**不随仓库分发**，因此文档中的数字分两类，引用时须区分：

| 证据等级 | 含义 | 本仓示例 |
| --- | --- | --- |
| **已验证**（可由仓库文件直接复算） | 仓库内文件即可复现 | 测试规模 1361 / 1346 / 858 个 `def test_`、测试文件 109 个；schema、阈值、配置项 |
| **文档记录**（产物未分发 / 复跑依赖外部资源，当前环境不可复现） | 来自历史运行的报告文件，报告本身不在仓库；或完整复跑需仓库外资源 | AVM MAPE、爬取规模与出数页率、CDC 时延、R-UNW-03 风暴率、AC-01～08 验收实测值、persona KS、LLM 评测指标（复跑需 LLM_KEY） |

> 引用「文档记录」类数字时均标注来源文档；如需复现，须在本机重建运行环境（MySQL / Redis / DWD 数据）并重跑对应引擎。

**关键结论（附证据等级）**：

- **AVM 精度@覆盖率**（文档记录，模型 `2026-08-20-r2`）：45% 覆盖下 MAPE 9.94% ≤ 10%（全量 15.00%，基线 20.58%）；AC-07 原始验收记录基于 canonical `2026-08-05-r11`（45% 覆盖 9.88% / 全量 14.59% / 基线 20.64%）。两版模型的指标均可追溯至 `output/avm/avm_report.json`（未分发）。
- **异常估值归因**（文档记录）：38% 异常估值经三分量归因查明为「基准自指」——`true_market_price` 由早期版本模型自己生成（无键城市仍如此），真实模型误差仅 1/200 笔，故不做校正层。生产全量 5000 笔 R-UNW-03 风暴（52.8%）主因为 AVM 回退城市/全局中位兜底与小区级基准的**比较粒度错配**，已在 `risk_engine.enrich_loan` 修复（仅小区级 AVM 命中才计偏差），告警降至 ~19.7%。相关产物 `output/avm/bias_diag/`、`output/risk/risk_report.json` 未随仓库分发。
- **LTV 两档预警**（逻辑已验证）：警示线 0.75 / 强预警线 0.85，经 T+1 批处理推送至贷后保全（语义为"不闭环"，见 PRD Non-goals）；阈值与边界比较可在 `tools/risk/config.py` / `risk_engine.py` 直接核对。

**未验证假设**：H1/H5 已在数据侧实证；H3 已由最小 Critic 原型验证达成（美化偏见 KS 可检测：0.257 → 校准后 0.028 ≤ 0.05，基准为合成种子，机制口径见 [tools/persona/README.md](tools/persona/README.md)，真实基准属商用前置）。

## 合规与数据

- 项目涉及金融业务数据，严格执行合规脱敏流程处理，不涉及个人信息出境。
- 房产与地理数据通过合规开放 API / 地理编码服务获取。
- 合成行为仿真仅用于策略推演与产品设计验证，不作为任何个体授信决策依据。

## 许可证与免责

**本仓库许可证：[MIT](LICENSE)。**本仓库用于产品设计与技术交流，示例数据均为脱敏 / 模拟样本，不涉及真实个人金融信息。

本仓库包含或依赖的第三方组件与资源，均按其原始许可使用，署名信息如下：

- **Leaflet**（`tools/frontend/static/vendor/leaflet/`）：BSD-2-Clause，完整版权声明见同目录 `LICENSE`。
- **地图瓦片**：空间页底图瓦片来自 OpenStreetMap、Carto（`*.basemaps.cartocdn.com`）与高德（autonavi，`download_tiles.py` 默认源），可离线自托管；页面须保留各瓦片服务的 attribution 标注，并在公开使用时遵守其服务条款。
- **Doris / Superset / MinIO**：以各自官方镜像与许可引入，仅作可选分析层（BI 副本），不属于核心闭环。

---

## 开发环境与版本

> 开发与运行目标 Python 为 **3.10**（Docker worker / Airflow 目标机使用 `python:3.10-slim`）。**注意**：仓库**不随 git 分发** `tools/orchestrator/.venv`，请使用**已装好依赖的** `python3` 运行采集 / ETL / 测试 / 前端（依赖见 `tools/orchestrator/requirements.txt`）；宿主默认 `python3` 可能不是 3.10。仓库脚本会优先使用 `tools/orchestrator/.venv/bin/python`，不存在则回退到 `python3`。

| 工具 | 版本 | 用途 |
| ---- | ---- | ---- |
| Python（宿主运行环境） | 目标 3.10；宿主默认 `python3` 视机器而定 | ETL / 编排 / 测试 / 宿主渲染 / 钩子 / PySpark 产品栈；依赖见 `environment.yml` + `tools/orchestrator/requirements.txt` |
| Python（worker/master 容器） | 3.10（`python:3.10-slim`） | 采集 worker / master 容器 |
| Python（Airflow，Linux 目标机） | 3.10 | 外层编排 DAG（DAG 可编译；本仓库未随附 apache-airflow 安装，`make up` 不起 Airflow） |
| pre-commit | 4.6.1 | 提交前 / 提交信息钩子 |
| Node.js | v24.16.0 | commitlint 运行环境 |
| npm | 11.13.0 | 依赖安装 |
| @commitlint/cli | 19.8.1 | 提交信息校验 |
| @commitlint/config-conventional | 19.8.1 | Conventional Commits 规则 |
| Git LFS | 可选（若启用） | 供选择性托管 `output/avm/model.joblib`（约 22MB）。**当前未启用**：`output/` 被 `.gitignore` 排除、`.gitattributes` 的 LFS 声明未提交，故**模型不随仓库分发**；如需启用须先 `git lfs install` 并提交 LFS 配置，否则请用 `tools/avm/train.py` 本机生成 |

> 注：`airflow/` 为仓库内 DAG 与相关脚本目录；如需启用调度，需另行安装 `apache-airflow` 并部署 scheduler/webserver。

初始化（在仓库根目录执行）：

```bash
# 选择一个已装好依赖的 Python（目标 3.10；默认 python3 可能不是 3.10）
# 注：AVM 模型 output/avm/model.joblib 不随仓库分发（output/ 被 gitignore），需本机 `python3 tools/avm/train.py` 生成
pip install -r tools/orchestrator/requirements.txt   # 采集/ETL/前端运行依赖
pip install pre-commit
pre-commit install
pre-commit install --hook-type commit-msg   # 启用提交信息校验
npm install
```
