# 证据层（Evidence Layer）

本目录是 SpaceFin-Agent 的**证据层**：集中登记仓库中所有对外引用数字/结论的来源与可复现性等级，并对文档一致性做自动化校验。

## 目录内容

| 文件 | 说明 |
| ---- | ---- |
| `registry.json` | **指标登记表**（stdlib `json` 可直接解析）。登记每个指标的 `value` / `unit` / `data_version` / `model_version` / `run_date` / `source_artifact` / `reproducibility` / `rerun_command` / `note`；并登记文档一致性 `asserts`。 |
| `AC_BASELINE.md` | AC-01~08 验收基线冻结表（口径 / 期望 / 实测值 / 来源文档 / 复跑方式 / 冻结日期）。 |
| `test_metrics.json` | 测试规模快照（由 `tools/dev/gen_evidence.py` 生成，`reproducibility: verified`）。 |
| `avm_metrics.json` | AVM 指标快照（源 `output/avm/avm_report.json`；产物缺省时为 `unavailable`）。 |
| `crawl_stock.json` | 爬取库存快照（源 `output/guangdong/etl_report.json`；产物缺省时为 `unavailable`）。 |
| `risk_summary.json` | 风险引擎摘要快照（源 `output/risk/risk_report.json`；产物缺省时为 `unavailable`）。 |
| `llm_ops.json` | LLM 运维指标（源 `tools/frontend/tests/llm_eval_report.json`；有评测产物时登记实测值并记 `recorded`，产物缺失时记 `unavailable`）。 |

## 可复现性等级（`reproducibility`）

| 等级 | 含义 |
| ---- | ---- |
| `verified` | 可由仓库文件/命令直接复算；`rerun_command` 在本仓可实际执行并通过。 |
| `recorded` | 来自历史/外部运行报告；原始产物可能未随仓库分发（`output/` 被 `.gitignore` 排除），或完整复跑依赖仓库外资源（如 LLM key），当前环境不可离线复核。 |
| `unavailable` | 未测量 / 未埋点（`value` 为 `null`，`note` 说明需要什么才能埋点）。 |
| `unverified` | 有值但无法溯源（本仓暂未使用该等级）。 |

## 诚实规则（本目录的强约束）

1. **不臆造数值**：每个 `value` 要么来自本目录脚本**当场实测**，要么来自 `registry.json` 中 `source_artifact` 指向的文件/报告路径。
2. **`verified` 必须可复跑**：标记 `verified` 的条目，其 `rerun_command` 必须能在本仓实际执行并得到登记值。配置类条目（如 LTV 阈值）以「读取对应文件」为复跑方式。
3. **产物缺失不删文件**：某快照若因 `output/` 产物被 gitignore 而无法生成，仍要写出该文件，标 `reproducibility: unavailable` 并在 `note` 中诚实说明，**不删除文件**。

## 重新生成 / 校验

```bash
# 生成/刷新全部快照（写入本目录）
python3 tools/dev/gen_evidence.py --out docs/evidence

# 校验：重算并比对磁盘上的快照；任一 verified 指标漂移则 exit 1
python3 tools/dev/gen_evidence.py --check --out docs/evidence

# 可追溯性自动检查（链接 / ADR / registry / 文档一致性 / 黑名单词）
python3 tools/dev/trace_check.py

# 增强检查（锚点命中 / verified 实际复跑 / metric_id 回链 / 产物指纹 / ADR 回退段）
python3 tools/dev/trace_extra.py

# 台账常量一致性（LLM 护栏常量 ↔ 代码）
python3 tools/dev/gen_ledger.py --check
```

## 断言的可选字段

`asserts` 中每条断言除 `must_contain` / `must_not_contain` 外，还可用：

- `metric_ids`：把该文件里出现的数字绑到登记条目；校验「条目存在 + 该条目 `value` 出现在目标文件」。由 `trace_extra.py --metrics` 执行。
- 条目级的可选字段 `rerun_expected`：当某 `verified` 条目的 `rerun_command` 输出与 `value` 口径不同（如需换算）时，显式声明期望串；由 `trace_extra.py --rerun` 使用。

## 证据锚点（替代行号）

ADR / 文档引用仓库文件时，用两段式锚点：文件路径 + `::` + 一个须在目标文件中**逐字命中**的片段。示例：

```markdown
- `README.md` :: `已移除实时层`（精简版范围说明）
```

- `path` 以**仓库根**为基准，找不到时回退到该文档同级目录。
- 校验：`python3 tools/dev/trace_extra.py --anchors`。
- 目的：行号锚点会随文档增删漂移（本项目已实测到该问题），片段锚点则稳定且可机器校验。

## 产物指纹（MANIFEST.json）

PoC 脚本与产物、模型血缘以 `MANIFEST.json` 记录 `sha256`（`docs/poc/**/MANIFEST.json`、`docs/models/MANIFEST.json`），校验：`python3 tools/dev/trace_extra.py --manifests`。

## 维护约定

- `registry.json` 的 `updated_at` 随每次人工修订更新；`verified` 条目的数值若要变更，须同步更新 `rerun_command` 能产出的实测值。
- `asserts` 中每条断言必须**当前成立**（`tools/dev/trace_check.py --consistency` 全绿）；文档改动若使断言失效，须同步修订断言或文档，二者保持一致。
