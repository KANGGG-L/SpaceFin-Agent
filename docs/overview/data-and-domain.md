# 数据与领域概念

> 目标：说清「哪些数据是真的、哪些是合成的」，以及看懂这个项目必须知道的几个金融/数据概念。术语的完整定义（公式、影响因素、使用场景）统一收录在 [../术语说明.md](../术语说明.md)，本文只做导览式摘要。

## 数据红线：两类数据，口径分明

项目对数据来源有一条硬约束（详见 [../../seed/README.md](../../seed/README.md)）：

- **个人贷款数据**（客户 / 抵押物 / 贷款台账，含 PII）：**一律脚本合成**，任何阶段都不使用真实个人信息。
- **公开渠道数据**（房产挂牌行情、小区、坐标）：通过爬虫从公开页面获取，不含 PII。

因此本仓库的业务数据构成是：

| 数据资产                          | 来源口径                                                       |
| --------------------------------- | -------------------------------------------------------------- |
| `customer` / `collateral` / `loan`（业务三表） | **合成**：`seed/generate_seed.py` 固定种子生成，逐字节可复现；7 天演示规模 5,000 笔（广州占比 ≈25%） |
| `crawl_housing_sale`（房源挂牌行情）           | **真实爬取**（安居客公开列表页，广东 21 城）。生产 ODS 贴源存量 59,081 条（DWD 去重后 59,030 条）；7 天演示库取其中 44,369 条 |
| 7 天演示中「广州挂牌价下探」事件    | 对真实爬取行做单价乘子的**脚本扰动**（原值记于 `ads_demo_gz_perturb`，可一键回滚）；AVM 重训是真实引擎行为 |
| `persona` 合成画像的违约基准        | 合成种子（`benchmark_customer.json`），真实基准属商用前置        |
| 全部 DWD/DWS/ADS 派生报表           | 由上述输入经真实引擎计算产出，属演示回填，**不构成任何真实市场判断或授信依据** |

> 与根 README 的统一前提一致：仓库内所有业务数据均为合成样本，文档与页面不再逐处标注「合成 / 演示」。

## 数仓分层（数据长什么样）

核心存储在 MySQL `spacefin_crawler` 库（Doris + MinIO 是同构的 BI 分析副本，四层 24 表）：

| 层     | 含义               | 代表表                                                   | 谁在写                        |
| ------ | ------------------ | -------------------------------------------------------- | ----------------------------- |
| ODS    | 贴源层             | `ods_cdc_log`（binlog 变更日志）、Parquet 数据湖         | `tools/cdc/main.py`、ETL      |
| DWD    | 明细层             | `crawl_housing_sale`（清洗去重后挂牌明细）、`dwd_listing_market_state` | `tools/orchestrator/etl.py`   |
| DWS    | 汇总层             | `dws_risk_class`（逐笔风险明细）、`dws_spatial_feature`  | `tools/risk/`、`tools/spatial/` |
| ADS    | 应用层（页面直读） | `ads_ltv_alerts`、`ads_risk_class`、`ads_1104_g11`、`ads_spatial_zone`、`ads_export_audit` | 各引擎与前端                  |

一个易混点：采集侧的「ODS 数据湖」（Parquet）与 CDC 侧的「ODS 变更日志」（MySQL 表）不是一回事；后者只记变更不存快照，新环境默认空表是**预期而非故障**（demo 数据由批处理直灌，不经 binlog）。

## 核心业务概念（按理解顺序）

1. **五级分类**：监管要求的贷款资产质量口径——正常 / 关注 / 次级 / 可疑 / 损失，后三类合计即不良。本项目的风险引擎按 LTV 与逾期状态每日重算（`tools/risk/`），是 NPL 统计与 1104 报送的基础。
2. **LTV（贷款价值比）**：`贷款余额 ÷ 抵押物估值`。房价下行时估值滞后 → LTV 被系统性低估，这是项目要解决的「LTV 盲区」。系统用 AVM 高频重估抵押物，并对 LTV 设**两档线**：警示 0.75 / 强预警 0.85（可配置，严格大于才触发），超线即 T+1 推送贷后保全。
3. **AVM（自动估值模型）**：用挂牌行情 + 空间特征自动估房价，替代人工评估（3–7 天 / 数百元 → 分钟级）。本项目实现为 HistGBR 单模型 + 坐标最近邻空间特征；验收口径是「精度@覆盖率」：45% 覆盖下 MAPE 9.94% ≤ 10%。
4. **MAPE**：平均绝对百分比误差，AVM 的精度尺子；「精度@覆盖率」指只对模型高置信的样本出数，其余降级人工——两者一起看才不被平均数骗。
5. **1104 G11**：银保监非现场监管报表中的资产质量模板。`tools/reporting/` 生成 G11 并做**三出口口径校验**（模板 vs 明细聚合 vs 汇总自比），任一对不上就阻断报送——合规宁可拒报。
6. **CDC（变更数据捕获）**：监听业务库 binlog，把 `loan/collateral/customer` 的变更秒级同步到数仓（`tools/cdc/`），与 T+1 批处理链路互为印证。
7. **迁徙矩阵（Roll Rate）**：贷款在五级档位间的逐日迁移，用于预测不良演化（前端 P3 页展示，数据来自每日快照）。
8. **低置信标记**：抵押物空间特征缺失率 >75 时，其估值与风险结论标注为低置信——系统诚实暴露数据质量边界，而不是硬算。
9. **合成画像校准（persona / H3 假设）**：通用 LLM 生成的借款人画像存在「美化偏见」。`tools/persona/` 用 Generator → Critic → 校准闭环验证该偏见可被 KS 统计量检测并校正（KS 0.257 → 0.028 ≤ 0.05）；前端 P6 沙盘区块读取其报告并置顶「未校准不得用于决策」横幅。
10. **在架流转率（采集侧观测口径）**：房源「今天没看到 ≠ 已下架」——只有当日有完备观测账本（`crawl_census.complete=1`）才允许判定缺席/下架，「缺省即未知」。这是采集数据可信度的地基设计（术语表 F 节有完整定义）。

## 数据来源的诚实声明

本项目按最终交付标准完整构建，但**真实外部资源依赖项诚实声明为未达成**（交付缺口，非隐藏缺陷）：真实银行数据授权、算法备案 / 监管审批、试点机构意向。工程简化项（Sedona → cKDTree 单机近似、POI 以挂牌密度代理、通勤直线距离近似等）均在根 README「项目性质与范围说明」中逐项列明。

## 评估证据与可复现性

> 本节回答「文档里的数字从哪来、能不能自己复算」——这是评估项目可信度的关键。

仓库的 `output/`、`data_lake/` 目录被 `.gitignore` 整目录排除，**模型与评估产物不随仓库分发**（例如 `output/avm/avm_report.json`、`output/guangdong/etl_report.json`、`output/risk/risk_report.json`、`output/persona/persona_report.json`）。因此数据与指标分两类证据等级，引用时须区分：

| 证据等级 | 含义 | 示例 |
| --- | --- | --- |
| **已验证** | 仓库内文件即可直接复算 | schema 与字段（`sql/init/01_schema.sql`、`tools/orchestrator/sql/crawl_schema.sql`）；风险阈值与边界（`tools/risk/config.py`、`risk_engine.py`）；确定性 seed（`seed/generate_seed.py`，固定种子 + 基准日）；测试规模（全仓 1361 个 `def test_`） |
| **文档记录** | 来自历史运行的报告文件，报告本身不在仓库 | AVM MAPE（AC-07）、爬取规模与出数页率、CDC 时延（AC-01）、R-UNW-03 风暴率、1104 口径一致率（AC-05/08）、persona KS 校准 |

**已定义的评估逻辑 vs 已运行的结果**：

- **已在代码中定义、可复跑（方法）**：AVM 训练/评估（`tools/avm/train.py` 计算并写出 `avm_report.json` 的 MAPE、覆盖率曲线、误差分解）；风险引擎的五级分类与 LTV 两档判定（`tools/risk/`）；1104 三出口口径校验（`tools/reporting/main.py`）；persona 的 KS 检验（`tools/persona/critic.py`）。
- **历史运行结果（文档记录）**：AC-01～08 的实测值记录在 [../product/05/](../product/05/README.md) 的验收记录中；AVM 指标同时记录在 [../tech/components/avm.md](../tech/components/avm.md)。这些数值来自当时的运行环境（MySQL + DWD 数据 + 合成种子），**当前仓库不含产物，无法离线复核**。
- **尚未验证的预期**：真实银行数据接入后的回归效果、真实分布下 H3（合成画像分布保真）——均属外部资源前置，见 [../product/06/README.md](../product/06/README.md) 的交付缺口表。

**数据版本与实验溯源**：模型产物会把 `version`（如 `2026-08-20-r2`）写入 `output/avm/avm_report.json`，风险引擎 `valuation.model_version()` 可追踪当前在跑的模型版本；但仓库**没有独立的数据版本 → 结果登记表**，历史结果所依赖的数据快照（DWD 行数、业务日期）只记录在文档与报告中。

**评估局限**：

- AVM 的 quantile / EB 超参与置信函数选择在测试集上迭代过（见 [../tech/components/avm.md](../tech/components/avm.md) 的标注），存在过拟合测试集的口径风险。
- 采集侧质量指标（出数页率）不落表，只以行数作代理（见 `tools/orchestrator/crawl_quality_gate.py` 注释），无法离线复核。
- 评估覆盖范围为合成种子环境；真实数据回归未验证。

## 延伸阅读

- 术语全表：[../术语说明.md](../术语说明.md)
- 风险引擎口径：[../tech/components/risk-engine.md](../tech/components/risk-engine.md) · AVM：[../tech/components/avm.md](../tech/components/avm.md)
- 数仓与湖：[../tech/components/doris-lake.md](../tech/components/doris-lake.md) · CDC：[../tech/components/cdc-downstream.md](../tech/components/cdc-downstream.md)
- 数据摸底与合规评估（阶段 1）：[../product/01/](../product/01/README.md)
