# 产品演进可追溯性（Product Evolution Traceability）

> **本文件是可追溯性的唯一入口**。它定义"可追溯"的验收口径，并把每一项能力映射到它的决策记录、证据条目与版本/回退方式。
>
> 配套：[决策日志 docs/adr/](adr/README.md) · [证据登记 docs/evidence/](evidence/README.md) · [版本台账 docs/models/](models/) · [演进重建 docs/product/PRODUCT_EVOLUTION.md](product/PRODUCT_EVOLUTION.md)

---

## 1. 契约：第三方须能回答的 5 个问题

只拿到本仓库的读者，对任意一项当前能力都应能回答：

| # | 问题 | 载体 |
| --- | --- | --- |
| Q1 | 它现在的行为是什么？ | README / `docs/overview/` / `docs/tech/components/` / 源码 |
| Q2 | 它**何时、为何**变成这样？ | `docs/adr/ADR-*.md`（含取证来源标注） |
| Q3 | 变更证据在哪？ | ADR 的 `## 证据（Evidence）` 段（文件路径 / 行号 / 删除记录） |
| Q4 | 效果数字从哪来、能否复算？ | `docs/evidence/registry.json` + `docs/evidence/*.json` 快照 |
| Q5 | 出问题怎么退回已知良好状态？ | `docs/models/` 版本台账 + 阈值/配置冻结值 |

### 证据可信度分级（贯穿全仓库）

| 级别 | 含义 | 要求 |
| --- | --- | --- |
| `verified` | 可由仓库内文件或命令直接复算 | 必须有可执行的 `rerun_command` |
| `recorded` | 来自历史/外部运行报告；产物可能未随仓库分发，或完整复跑依赖仓库外资源（如 LLM key） | 必须标明 `source_artifact` 与记录它的文档 |
| `unavailable` | 未测量或未埋点 | 必须说明需要什么才能测量 |
| `unverified` | 有值但无法溯源 | 不应存在；发现即视为缺陷 |

> **规矩**：`recorded` 不得写成 `verified`；没有 ground truth 的能力不得声称效果达标（见 [ADR-0008](adr/ADR-0008-h3-parked-no-ground-truth.md)）。

---

## 2. 追溯索引（能力 → 决策 → 证据 → 版本）

`ADR` 与 `metric_id` 均可点击/检索；`—` 表示该能力不需要决策记录（可由配置或代码直接核对）。

| 能力 | Q2/Q3 决策 | Q4 证据条目（`docs/evidence/registry.json`） | Q5 版本 / 回退 |
| --- | --- | --- | --- |
| AVM 自动估值 | [ADR-0002](adr/ADR-0002-avm-selection-convergence.md) | `avm_mape_at_45pct_r2`、`avm_mape_full_r2`、`avm_baseline_r2`、`avm_mape_at_45pct_r11`、`avm_error_floor_oracle` | [AVM 模型卡](models/AVM_MODEL_CARD.md)；`output/avm/avm_report.json` 的 `version` |
| LTV 两档预警（0.75 / 0.85） | [ADR-0005](adr/ADR-0005-ltv-two-tier-thresholds.md) | `ltv_warn_line`、`ltv_strong_line` | `tools/risk/config.py` 冻结值（可配置） |
| 五级分类 | — | `ac02_consistency` | `tools/risk/config.py` `classify()` |
| 低置信标记（缺失率 >75） | [ADR-0003](adr/ADR-0003-low-confidence-threshold.md) | `low_conf_missing_pct`、`ac04_low_conf` | `tools/risk/config.py` `LOW_CONF_MISSING_PCT` |
| 异常估值 R-UNW-03 | [ADR-0002](adr/ADR-0002-avm-selection-convergence.md) | `runw03_abnormal_rate_before`、`runw03_abnormal_rate_after` | `risk_engine.enrich_loan` 的粒度判定 |
| 1104 报送三出口校验 | — | `ac05_block`、`ac08_consistency` | `tools/reporting/main.py` |
| CDC 增量同步（≤10s） | — | `cdc_ods_latency_seconds` | `ods_cdc_position` 位点 |
| 实时层（已移除 → T+1） | [ADR-0001](adr/ADR-0001-realtime-layer-removal.md) | —（行为变更而非指标） | T+1 批处理；实时层代码已删除 |
| 空间特征（cKDTree 近似） | [ADR-0002](adr/ADR-0002-avm-selection-convergence.md) | —（近似项，见 README 工程简化） | `tools/spatial/features.py`；`sedona_demo.py` 保留架构等价性 |
| 湖仓 Doris + MinIO（BI 副本） | [ADR-0007](adr/ADR-0007-evidence-grading-and-artifact-exclusion.md) | `crawl_stock_sale_total`、`dwd_sale_total`、`demo_rows_gd21` | `tools/lake/`；核心链不依赖 |
| 前端驾驶舱（9 页 + 2 角色 RBAC） | [ADR-0004](adr/ADR-0004-rbac-role-reduction.md) | `frontend_nav_pages`、`rbac_roles` | `tools/frontend/app.py` `USERS` / `PAGE_VISIBILITY` |
| 数据分类分级 + PII 脱敏 + 审计 | [ADR-0007](adr/ADR-0007-evidence-grading-and-artifact-exclusion.md) | `ac06_masked` | `tools/frontend/data_classification.py` |
| LLM 链路（P6 起草 / P10 沙盒） | [ADR-0006](adr/ADR-0006-llm-guardrail-policy.md) | `llm_fallback_rate`（实测 41.67%）、`llm_guard_block_rate`（实测 13.89%）、`llm_eval_case_pass_rate`（实测 95.65%）、`llm_p95_latency_s`（实测 40.71s） | [LLM 台账](models/LLM_REGISTRY.md)（回退链 + 预算 + 护栏常量） |
| 合成画像与 Critic 校准 | [ADR-0008](adr/ADR-0008-h3-parked-no-ground-truth.md) | `persona_ks_naive`、`persona_ks_calibrated`、`persona_ks_target` | `tools/persona/`；基准为合成种子 |
| 采集规模与出数页率 | — | `crawl_full_run_rows`、`crawl_yield_page_rate` | 采集栈镜像 + `TASK_TYPES` |
| 测试规模 | — | `test_total_def_test`、`test_in_tests_dirs`、`test_files_total`、`test_core_four_modules` | `python -m pytest tools/ -q` |
| MySQL 业务源库（L0） | — | — | `sql/init/01_schema.sql`、`sql/init/02_seed.sql` |
| 安居客采集器 | — | — | `tools/anjuke_crawler/` |
| Redis 任务队列 / 代理池 | — | — | `tools/orchestrator/`（`REDIS_DB` 隔离） |
| 容器编排采集系统（master 主备 + worker） | — | `crawl_full_run_rows`、`crawl_yield_page_rate` | `tools/orchestrator/master.py`、`TASK_TYPES` |
| Airflow 外层编排（DAG 00:30） | — | — | `airflow/dags/guangdong_daily_crawl.py` |
| ETL 数据管道（跨日去重 + DWD + ODS 湖） | — | `crawl_stock_sale_total`、`dwd_sale_total` | `tools/orchestrator/etl.py` |
| 贷后预警推送（T+1） | — | — | `tools/alerting/main.py`、`.env` `postloan_http` |
| 运维（容器恢复 + 资源管家） | — | — | `tools/ops/` |
| 倒排索引（审计检索） | — | — | `sql/doris/01_compliance_audit_inverted.sql`、`tools/compliance/` |
| Superset 看板（BI 层，探索） | — | — | `deploy/superset/` |
| PoC / 模型产物指纹 | — | `MANIFEST.json` 指纹条目 | `docs/poc/**/MANIFEST.json`、`docs/models/MANIFEST.json` |
| 配置 / 阈值事实（风险五级 · AVM 超参 · 采集预算 · 空间近似 · 前端 · 库表） | — | 28 条 `verified` 条目（前缀 `risk_` / `avm_` / `crawl_` / `spatial_` / `frontend_` / `db_` / `lake_`） | 代码常量，`trace_extra.py --rerun` 实际复跑 |

验收基准（AC-01～08 的期望值与复跑方式）统一冻结在 [docs/evidence/AC_BASELINE.md](evidence/AC_BASELINE.md)。

---

## 3. 如何自检

```bash
python tools/dev/trace_check.py            # 链接 / ADR 字段 / 证据登记 / 文档一致性 / 术语黑名单
python tools/dev/trace_extra.py            # 锚点命中 / verified 复跑 / metric 回链 / 产物指纹 / ADR 回退段
python tools/dev/gen_evidence.py           # 生成 docs/evidence/*.json 快照
python tools/dev/gen_evidence.py --check   # 校验指标漂移（verified 项）
python tools/dev/gen_ledger.py --check     # LLM 护栏常量台账 ↔ 代码数值一致性
```

CI 在 PR 上执行同一套检查（见 [.github/workflows/ci.yml](../.github/workflows/ci.yml)），并把生成的证据快照作为 artifact 归档——这是"产物不随仓库分发"这一结构性限制的补丁：**指标仍然可被复算，因为复算结果每次都在 CI 上留痕**。

## 4. 新增或修改能力时的义务

见 [CONTRIBUTING.md](../CONTRIBUTING.md) §7「变更可追溯性」：架构/选型变化须补 ADR；阈值/模型/Prompt 变化须补 ADR 或证据条目；面向外部的数字须能在 `registry.json` 找到同名 `metric_id`。

自检层面还有以下**由脚本强制**的义务（漂移即 CI 红）：

- **证据锚点**：ADR / 文档引用仓库文件时改用两段式锚点——先写文件路径，再用 `::` 连接一个**须逐字命中目标文件**的片段，**不再用易漂移的行号**；`trace_extra.py --anchors` 校验（写法示例见 [evidence/README.md](evidence/README.md)）。
- **数字回链**：`asserts` 可用 `metric_ids` 把文档里的数字绑到登记条目，`trace_extra.py --metrics` 校验「条目存在 + 值出现在该文件」。
- **复跑一致性**：`verified` 条目的 `rerun_command` 会被**实际执行**并与登记值比对（`trace_extra.py --rerun`）；若命令产物与登记值口径不同，用可选字段 `rerun_expected` 显式声明。
- **产物指纹**：PoC / 模型血缘以 `MANIFEST.json` 记录 sha256（`trace_extra.py --manifests`）。
- **ADR 回退**：每篇 ADR 须含 `## 回退（Rollback）`（`trace_extra.py --adr-rollback`）。
- **台账常量**：LLM 护栏常量台账与代码数值的一致性由 `gen_ledger.py --check` 强制。
