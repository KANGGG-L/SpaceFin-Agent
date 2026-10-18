# 产品演进重建（Product Evolution）

> **本文目的**：基于仓库内可查证的证据，重建本项目从早期构想走到当前状态的演进过程与关键设计权衡，供外部读者理解"为什么是现在这个样子"。
>
> **证据口径（必读）**：
> 1. 本仓库 **Git 历史极短**——`git log` 仅 3 个提交（`Initial commit` → `初始化 CI/CD、开发规范工具链与产品文档仓库` → `Merge dev → main …`），**不足以**还原"某组件何时、为何引入"。
> 2. 因此本文的演进证据分两类：**硬证据**（可核验的代码/配置/文件删除/产物哈希）与 **文档自述**（`docs/product/0X/` 各阶段文档，属阶段性收口的成品，非过程留痕）。
> 3. 凡不能由证据支撑的动机、时间点、顺序，本文一律标注为「**历史证据不足**」，不做推测性补写。
>
> 相关文档：[阶段 0](00/README.md) · [阶段 1](01/README.md) · [阶段 2](02/README.md) · [阶段 3](03/README.md) · [阶段 4](04/README.md) · [阶段 5](05/README.md) · [阶段 6](06/README.md) · [范围决策说明](范围决策说明.md)

---

## 1. 演进总览

本项目以「**机会识别 → 需求规划 → 技术可行性 → PRD → 设计/研发评审 → 测试验收 → 上线复盘**」七个阶段推进，每阶段回答一个核心决策问题并产出对应交付物；工程实现按 L0→L5 分层**按需接入**。

| 阶段 | 核心决策问题 | 主要产出 | 关键变化 | 证据强度 |
| --- | --- | --- | --- | --- |
| 0 机会识别 | 这个机会值得做吗？ | 痛点定义、5 条假设（H1/H3/H5 为生死假设）、Non-goals | 确立「空间维度风控」方向 | 文档自述 |
| 1 需求规划 | 用户真实痛点是什么？ | 用户调研、竞品、数据摸底、RICE 优先级 | MVP 收敛为「集成 AVM + 空间推演」 | 文档自述 |
| 2 技术可行性 | 技术上是否可行？ | 技术选型论证、效果基线、**数据底座 PoC** | 选型为当时候选（Hive+Doris / Sedona / MGWR+GBDT / Kafka+Flink） | 文档自述 + PoC 代码（硬证据） |
| 3 PRD | 具体做成什么样？ | PRD（12 EARS + 8 AC）、**MVP 端到端原型** | **选型收敛**：HistGBR + cKDTree；H3 显式 parked | 文档自述 + 原型代码/CSV（硬证据） |
| 4 设计与研发评审 | 设计与技术方案对齐了吗？ | 评审纪要、待确认清单 Q1–Q5、设计规范 | 确认依赖顺序 L0→L3/L2→LTV→L5；Q1–Q3 保持未确认 | 文档自述 |
| 5 测试验收 | 开发是否在轨道上？ | 测试用例、8 份 AC 验收记录、上线风险 | 8/8 AC 绿（合成种子环境）；指定 AC-07 为验收基准 | 文档自述（验收记录） |
| 6 上线复盘 | 上线复盘 | 交付缺口清单 G1–G10 + H4、文档收口 | 区分「代码可达成」与「外部资源依赖」两类缺口 | 文档自述 |
| 工程演进 | 按需接入 | L0→L5 组件逐层补齐 | **实时层移除（硬证据）**；多处选型收敛 | 代码/文件删除（硬证据） |

> **唯一具备"文件级硬证据"的演进事实是实时层移除**（见 §3）。其余阶段推进的**时间点与先后顺序**属文档自述，Git 层面无法独立验证。

---

## 2. 阶段重建

以下每阶段按 **Context → Previous State → Change → Rationale → Implementation → Product Implications → Trade-offs → Evidence** 组织。`Rationale` 仅记录文档中**可证实**的设计理由；无记录者标注「历史证据不足」。

### 阶段 0：机会识别

- **Context**：互联网金融机构的风控底层逻辑从单一交易数据向多模态、时空交织演进，抵押物估值与借款人社区经济生态成为资产质量关键变量。
- **Previous State**：项目启动前无实现；仅有一个待证伪的判断——「空间维度对信贷风险有增量价值」。
- **Change**：定义三类痛点（P1 抵押物估值靠人工、P2 风控结论缺业务共鸣、P3 通用 LLM 合成画像被美化偏见污染）与 5 条假设，其中 H1（空间模型优于全局基线）、H3（合成画像与真实分布对齐）、H5（数据合规可得）标为**生死假设**——任一条被证伪即调整方向。同时明确 Non-goals。
- **Rationale**（文档自述）：「先验证问题，再讨论方案」，每阶段始于待证伪命题而非功能清单。
- **Implementation**：无代码产出（纯机会识别）。
- **Product Implications**：确立了"风控从数据展示升级为策略沙盒"的产品主张与后续所有阶段的边界。
- **Trade-offs**：5 类角色的机会识别视角，在后续 lite 口径下收敛为单一主线用户（风控策略经理）——见 [阶段 0 说明](00/README.md)。
- **Evidence**：[阶段 0 README](00/README.md)。**历史证据不足**：真实需求调研过程无留痕。

### 阶段 1：需求规划

- **Context**：机会成立后，需回答"用户的真实痛点与优先级"。
- **Previous State**：仅有痛点假设，无用户/竞品/数据的具体认知。
- **Change**：产出用户深度调研、竞品分析、数据现状摸底，并用 RICE 排序确定 MVP 优先级（P0 = 集成 AVM + 空间风险推演）。
- **Rationale**（文档自述）：「先做集成、后自研差异化」——MVP 优先落地可集成能力。
- **Implementation**：无代码；产出调研与优先级文档。
- **Product Implications**：MVP 范围（F1/F3/F10）被固定，直接决定阶段 2 的技术评估对象。
- **Trade-offs**：商业侧规模数字（TAM/SAM/SOM）来源为行业报告推算，**原始材料未附，不可独立核验**。
- **Evidence**：[阶段 1 README](01/README.md)、[用户深度调研](01/用户深度调研.md)、[数据现状摸底](01/数据现状摸底.md)、[优先级判断](01/优先级判断.md)。**历史证据不足**：调研数据源与过程记录不完整。

### 阶段 2：技术可行性评估

- **Context**：需回答"每个能力是否有成熟、可落地的技术路径、排除了什么、基线设在哪"。
- **Previous State**：无实现，仅有需求与优先级。
- **Change**：逐层（L0–L5）给出「候选方案 → 选型与排除 → 风险 → 验证」链路与多方案对比表；**交付数据底座 PoC**（`docs/poc/data-foundation/avm_poc.py`），用全局 OLS 基线对比距离加权局部回归（GWR-lite），论证 H1。当时**终选**为：湖仓一体（Hive+Doris+Multi-Catalog）、Kafka+Flink CEP、Apache Sedona、MGWR+GBDT、PerGent 生成-批评估闭环、1104 模板 + 倒排索引。
- **Rationale**（文档自述）：房地产受"地理学第一定律"支配，存在空间非平稳性，全局模型必然局部失真 → 需空间感知模型（MGWR+GBDT）；亿级坐标空间连接单机崩溃 → 需分布式原语（Sedona）。
- **Implementation**：`avm_poc.py`（stdlib 零依赖，固定随机种子，落盘 `ods/dwd/dws/ads.csv`）。
- **Product Implications**：可行性以"在单人/小团队交付边界内可演示、可度量"为准，避免"全都要"陷阱。
- **Trade-offs**：PoC 使用合成数据，GWR-lite 为固定带宽高斯核的简化版，非生产级 MGWR；**该 PoC 只论证方向正确，不给出生产精度**。
- **Evidence**：[阶段 2 README](02/README.md)（顶部「⚠️ 历史决策快照说明」）、[PoC 说明](../poc/data-foundation/README.md)、`docs/poc/data-foundation/avm_poc.py`。**硬证据**：PoC 代码与 CSV 产物已入库、可复跑；**文档自述**：选型论证。

> **重要**：阶段 2 的选型大部分在最终实现中被收敛/替换（见 §3）。阶段 2 文档已在顶部显式声明为**历史决策快照**，实现在后续阶段收敛。这是本仓库"保留真实决策过程"的体现，而非文档错误。

### 阶段 3：PRD（含选型收敛）

- **Context**：将 L0+L3（数据底座 + AVM）固化为第一版范围、功能清单与验收标准；把 H3 的 Critic ground truth 与 KS 门禁写进需求。
- **Previous State**：阶段 2 的候选选型（MGWR+GBDT / Sedona / Hive / Flink）。
- **Change**：
  1. **选型收敛**：AVM 定为 **HistGBR 单模型 + 坐标最近邻空间特征**（无 `mgwr` 依赖）；空间定为 **scipy cKDTree 单机近似**（无 Sedona）；湖仓用 **Doris + MinIO**（无 Hive）。
  2. **H3 显式 parked 为 stretch**：合成仿真架构可行，但 Critic 的 ground truth（真实不良率/普查基准）不可得，故门禁为"小规模分布对齐 PoC 达标（KS≤0.05）"，否则降级为纯可视化推演工具、不声称分布保真。
  3. 产出**端到端 MVP 原型**（`docs/poc/core-prototype/mvp_prototype.py`），验证两条闭环：资产质量监控闭环（PRD §5.1）与 LTV 爆仓预警闭环（PRD §5.2）。
- **Rationale**（文档自述 + 可核验）：收敛理由是"在交付边界内可演示、可度量"——重依赖（MGWR/Sedona/Flink/Hive）与单人交付边界冲突（阶段 2 风险登记册 R-tech-1 已列明）。
- **Implementation**：`mvp_prototype.py`（stdlib 零依赖）落盘 `ods_loans / dwd_enriched / dws_risk_class / ads_1104_g11 / ads_ltv_alerts.csv`；原型内**低置信阈值为缺失率 >20%（0–1 标度 0.20）**。
- **Product Implications**：PRD 固化了 8 条验收标准（AC-01～08）与 12 条 EARS 需求，成为阶段 5 的验收依据。
- **Trade-offs**：原型不含 UI/看板与实时反欺诈闭环（后者 PRD 明确列为 Non-goal）；H3 未闭环。
- **Evidence**：[阶段 3 README](03/README.md)、`docs/poc/core-prototype/mvp_prototype.py` 及 5 个 CSV（**硬证据**，已入库可复跑）。

### 阶段 4：设计与研发评审

- **Context**：设计稿与技术方案是否对齐？
- **Previous State**：PRD 已定义功能与验收标准，但工程实现路径与排期未对齐。
- **Change**：产出设计评审、研发评审与评审纪要，确认组件依赖顺序 **L0 → L3/L2 → LTV → L5**；遗留待确认项 Q1–Q5，其中 Q1–Q3 保持「未确认」并标注依赖方（未编造结论）。
- **Rationale**：文档自注为「**历史评审快照**」——评审结论以纪要为口径，后续实现可能已推进。
- **Implementation**：评审材料 + 纪要（无独立代码产出）。
- **Product Implications**：把风险登记册转化为 PRD 的"风险与合规"章节。
- **Trade-offs**：Q1–Q3 的未确认状态被保留，而非强行收口。
- **Evidence**：[阶段 4 README](04/README.md)、[设计评审](04/设计评审.md)、[研发评审](04/研发评审.md)、[评审纪要](04/评审纪要.md)。**历史证据不足**：评审时间、参与方无独立留痕。

### 阶段 5：测试验收

- **Context**：测试验收与开发是否在轨道上？
- **Previous State**：PRD 定义了 8 条 AC，但无执行证据。
- **Change**：产出测试用例集、8 份 AC 验收记录（AC-01～08）与上线风险清单；8/8 AC 达成（**仅限合成种子一致环境**）；按测试函数计数呈现规模（全仓 1361 个 `def test_`，其中 `*/tests/` 目录内 1346 个，核心四模块 858 个）。
- **Rationale**：以**可实证的量化口径**验收，而非难以归因的业务北极星（新增不良率下降需对照组/影子模式，属方向性目标）。
- **Implementation**：测试框架 + 全量执行；核心四模块 frontend/risk/alerting/avm。
- **Product Implications**：AC-07 的 canonical 配置被指定为验收基准，后续调整超参须同步重跑 AC-07。
- **Trade-offs**：验收结论限于合成种子环境；真实数据接入后的回归属外部依赖（G1）。README 只声明**测试规模**、不声明**通过率**。
- **Evidence**：[阶段 5 README](05/README.md)、8 份 [AC 验收记录](05/)、[验收与上线风险](05/验收与上线风险.md)。**本次审计实测**：测试规模 1361/1346/858 与文档声明一致（`def test_` 计数）。
- **阈值演化（可核验）**：低置信阈值从阶段 3 原型的**缺失率 >20%** 变为最终实现的**缺失率 >75%**（`tools/risk/config.py` `LOW_CONF_MISSING_PCT=75.0`，严格大于）。理由（文档记录）：旧默认阈值会误伤约半数抵押物。详见 [阶段 6 §5 C 类漂移](06/README.md)。

### 阶段 6：上线复盘

- **Context**：上线效果与最终版交付缺口清点。
- **Previous State**：8/8 AC 已绿，但外部依赖项未评估、文档存在漂移。
- **Change**：
  1. 清点**最终版交付缺口 G1–G10 + H4**，并区分两类：**⛔ 外部资源依赖**（G1 真实数据授权、G3 算法备案、H4 试点意向——诚实声明未达成）与**✅ 代码可达成**（G2 分类分级、G4 PII 脱敏、G5 分布式空间演示、G8 健康端点、G9 倒排索引、G10 Superset）。
  2. **C 类文档漂移修正**：LTV 两档预警（0.75/0.85）已实现（旧报告误写为单档）；低置信阈值严格大于、恰好 =75 不标记；风险引擎陈旧注释纠正；CDC 位点持久化（`ods_cdc_position`）已实现（旧报告称未持久化）。
- **Rationale**：判定口径 = 技术 AC + 可复现性 + 测试 + 文档自洽（非商业 SLA）。
- **Implementation**：文档收口 + 代码补齐（G2/G4/G5/G8/G9/G10 相关模块）。
- **Product Implications**：明确"能力项以**最终版是否交付**判定，不以**是否商用**裁剪"。
- **Trade-offs**：G1/G3/H4 无法凭代码达成，列为交付缺口而非隐藏缺陷。
- **Evidence**：[阶段 6 README](06/README.md)。**本次审计核验**：G9 倒排索引（`sql/doris/01_compliance_audit_inverted.sql` + `tools/compliance/`）与 G4 脱敏（`tools/frontend/data_classification.py`）代码存在。

---

## 3. 关键技术选型的收敛与回退（硬证据）

以下的"从候选到最终"变化，可由**代码/文件删除**直接核验，是本仓最可证实的演进内容：

| 能力 | 阶段 2 候选（历史快照） | 最终实现 | 变化类型 | 证据 |
| --- | --- | --- | --- | --- |
| AVM 模型 | MGWR（多尺度地理加权回归）+ GBDT | HistGBR 单模型 + 坐标最近邻空间特征 | 收敛（降复杂依赖） | [阶段 2 顶部快照说明](02/README.md)、[avm.md](../tech/components/avm.md)、`tools/avm/train.py` |
| 空间计算 | Apache Sedona（PySpark 分布式） | scipy cKDTree 单机近似 | 收敛（架构等价性由演示脚本保留） | `tools/spatial/features.py`、`tools/spatial/sedona_demo.py` |
| 湖仓 | Hive + Doris + Multi-Catalog | Doris + MinIO（Hive 未引入） | 替换 | [阶段 2 顶部快照说明](02/README.md)、`tools/lake/`、`sql/doris/` |
| 实时预警 | Kafka + Flink CEP | T+1 批处理推送 | **移除（回退）** | `git status` 显示 `tools/stream/`、`deploy/kafka-flink/`、`docs/tech/components/kafka-flink-realtime.md` **已删除**；README 声明实时层已移除 |
| 画像仿真 | PerGent 生成-批评估闭环 | Generator → Critic → 校准（合成基准，H3 仍 parked） | 部分落地 | `tools/persona/`、[阶段 3](03/README.md)（H3 parked 说明） |
| 低置信阈值 | 原型缺失率 >20% | 缺失率 >75（严格大于） | 参数收敛 | `docs/poc/core-prototype/README.md` vs `tools/risk/config.py` |
| 前端 RBAC | PRD 5 角色 | 2 角色（`admin` / `postloan`，原 `risk`/`da` 已并入 `admin`） | 收敛 | [阶段 3](03/README.md) 落地注、`tools/frontend/app.py` `USERS`/`PAGE_VISIBILITY` |

> **可核验性说明**：上表"移除/收敛"方向可由文件与代码直接核对；但**发生的确切时间点**无 Git 提交支撑，属**历史证据不足**。

### 3.1 与决策日志（ADR）的对照

上表每条演进事实均已固化为一篇决策记录，含背景 / 备选方案 / 影响 / 文件级证据 / 取证来源标注：

| 演进事实 | 决策记录 |
| --- | --- |
| 实时层移除（Kafka/Flink → T+1） | [ADR-0001](../adr/ADR-0001-realtime-layer-removal.md) |
| AVM 选型收敛（MGWR+GBDT → HistGBR+cKDTree） | [ADR-0002](../adr/ADR-0002-avm-selection-convergence.md) |
| 低置信阈值 0.20 → 75 | [ADR-0003](../adr/ADR-0003-low-confidence-threshold.md) |
| 前端 RBAC 5 → 2 角色 | [ADR-0004](../adr/ADR-0004-rbac-role-reduction.md) |
| LTV 单档 → 两档 + 等号边界语义 | [ADR-0005](../adr/ADR-0005-ltv-two-tier-thresholds.md) |
| LLM 护栏策略（兜底 / 白名单 / 限额） | [ADR-0006](../adr/ADR-0006-llm-guardrail-policy.md) |
| 证据分级 + 产物不分发 | [ADR-0007](../adr/ADR-0007-evidence-grading-and-artifact-exclusion.md) |
| H3 parked（无 ground truth 不声称效果） | [ADR-0008](../adr/ADR-0008-h3-parked-no-ground-truth.md) |

> 可追溯性契约与「能力 → 决策 → 证据 → 版本」索引见 [../TRACEABILITY.md](../TRACEABILITY.md)；指标证据与可信度分级见 [../evidence/registry.json](../evidence/registry.json)；逐项指标的定义与复跑命令见 [../evidence/README.md](../evidence/README.md)。

---

## 4. 工程实现的按需接入（L0→L5）

工程实现并非一次性建成，而是按「业务需要出现时才补入」的原则逐层引入，最终组成 L0→L5 的完整链路（组件清单与状态见根 [README「组件清单」](../../README.md)）。层级顺序在 [架构说明](../overview/architecture.md) 与 Airflow DAG 代码（`airflow/dags/guangdong_daily_crawl.py` 的任务依赖注释）中可核验。

**可核验的终止态**：`seed → MySQL → 采集（orchestrator/Redis）→ ETL → DWD/DWS/ADS → 空间特征 → AVM → 风险引擎 → 预警推送 → 1104 报送 → 前端驾驶舱`，另含 CDC 增量、Doris/MinIO 分析副本、倒排索引与 Superset 探索层。

> **历史证据不足**：各组件"何时引入"的先后顺序无提交记录支撑；上表与本节描述的是**最终态的依赖关系**，不是已证实的引入时间线。

---

## 5. 证据充分性评估（结论）

| 维度 | 证据强度 | 说明 |
| --- | --- | --- |
| 最终态的架构与功能 | **强** | 可由代码、schema、配置直接核验 |
| 最终态的阈值与业务规则 | **强** | `tools/risk/config.py`、`tools/avm/` 等可核对 |
| 选型收敛/回退的方向 | **中—强** | 文件删除、代码存在可核验；原因多为文档自述 |
| 阶段推进的时间点与顺序 | **弱（证据不足）** | Git 仅 3 提交，无法还原"何时/为何" |
| 阶段 0–6 的决策过程 | **弱—中** | 均为事后收口的成品文档；阶段 2/4 已自注为"历史快照" |
| 商业侧数字（TAM/SAM/SOM 等） | **弱** | 来源未附原始材料 |
| 性能/规模指标 | **中** | 有文档记录与报告出处，但产物未随仓库分发（见 [数据与领域概念 · 评估证据](../overview/data-and-domain.md)） |

**一句话结论**：本项目的**产品主张与最终实现**内在自洽且有代码闭环支撑；**演进过程**在 Git 层面证据不足，只能依赖阶段性文档自述与最终态代码反推——其中"实时层移除"与"多处选型收敛"是具备文件级硬证据的演进事实。
