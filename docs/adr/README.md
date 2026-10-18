# 决策日志（ADR，Decision Records）

> 本目录记录 SpaceFin-Agent 中**已落地、且值得追溯**的关键决策。
> 它回答可追溯性契约中的 Q2/Q3：某能力**何时、为何**变成现在这样，以及**变更证据在哪**（见 [../TRACEABILITY.md](../TRACEABILITY.md)）。

## 目的

ADR 只记录**可证实的事实**，不重写历史。本仓库 Git 历史极短（3 个提交），因此：

- 多数决策**无法**引用 commit id / PR 号 / 作者 / 日期——缺证处一律如实标注，**不编造**。
- 每条 ADR 以 `## 取证来源（Provenance）` 收尾，标签五选一：
  - `documented`：有文档或代码记录可指向（删除记录、配置值、代码实现、明确的文档表述）。
  - `self-reported`：仅文档自述（动机、口径理由、"某能力不可得"等无法在仓库内证实的断言）。
  - `unknown`：不可证。
  - `code-inspection`：由代码/配置可直接核对（如阈值常量、判定分支）。
  - `file-evidence`：由仓库内文件/删除记录可核对（如 `.gitignore` 排除项、git 暂存删除）。
- 迭代：写该决策的迭代轮次 `第 N 轮迭代`（N 取 ADR 编号，如 ADR-0002 → 第 2 轮迭代）。

## 必需章节

每条 ADR 必须包含以下十个二级标题（顺序固定）：

| 标题 | 作用 |
| --- | --- |
| `## 状态` | 已采纳 / 已取代 / 已废弃（含取代关系） |
| `## 迭代` | 该决策的迭代轮次（`第 N 轮迭代`，N = ADR 编号） |
| `## 背景（Context）` | 3–6 行：为什么需要做这个决策 |
| `## 决策（Decision）` | 3–8 行：最终选择的行为 |
| `## 备选方案（Alternatives）` | 短列表：被考虑的方案 + 否决原因 |
| `## 影响（Consequences）` | 分「正面 / 代价」两部分 |
| `## 证据（Evidence）` | 指向仓库内真实文件的**锚点**条目（仓库相对路径 + 在目标文件中逐字出现的片段） |
| `## 取证来源（Provenance）` | 一行：`documented` / `self-reported` / `unknown` / `code-inspection` / `file-evidence` + 理由 |
| `## 回退（Rollback）` | 2–5 行：撤销该决策的具体做法（真实文件 / 配置 / 需重跑的验收项） |
| `## 关联（Related）` | 相关 ADR（相对路径）+ 相关 `docs/` 文件 |

## 证据锚点

> **证据锚点**：`## 证据（Evidence）` 段采用两段式锚点——一段**仓库相对路径** + 一段在目标文件中**逐字出现**的片段（两段以双冒号分隔），例：`README.md` :: `已移除实时层`。自检 `tools/dev/trace_extra.py --anchors` 会逐条校验片段是否命中目标文件；**行号不再作为锚点**（行号会随文档演进漂移）。

## 模板（复制粘贴）

```markdown
# ADR-XXXX：<一句话决策主题>

## 状态
已采纳

## 迭代
第 N 轮迭代

## 背景（Context）
<3–6 行>

## 决策（Decision）
<3–8 行>

## 备选方案（Alternatives）
- <方案 A>：否决——<原因>

## 影响（Consequences）
**正面**
- <收益>

**代价**
- <成本>

## 证据（Evidence）
- `<path/to/file>` :: `<目标文件中逐字出现的片段>`
- `<path/to/another>` :: `<片段>`（可选：简短说明）

## 取证来源（Provenance）
`documented` —— <一句话理由>

## 回退（Rollback）
- <改回哪个文件 / 配置即可撤销该决策；须具体到仓库内真实路径>
- <回退后需重跑的验收项（AC-XX）>

## 关联（Related）
- [ADR-XXXX-....md](ADR-XXXX-....md)
- [docs/...](../...)
```

## 索引

| 编号 | 主题 | 状态 | 取证来源 | 链接 |
| --- | --- | --- | --- | --- |
| ADR-0001 | 移除 Kafka/Flink 实时层，预警改 T+1 批处理 | 已采纳 | `documented` | [ADR-0001](ADR-0001-realtime-layer-removal.md) |
| ADR-0002 | 技术选型收敛为 HistGBR + cKDTree（历史候选存档） | 已采纳 | `documented` | [ADR-0002](ADR-0002-avm-selection-convergence.md) |
| ADR-0003 | 低置信阈值收敛为 75.0，判定为严格大于 | 已采纳 | `documented` | [ADR-0003](ADR-0003-low-confidence-threshold.md) |
| ADR-0004 | RBAC 由 5 角色收敛为 admin / postloan | 已采纳 | `documented` | [ADR-0004](ADR-0004-rbac-role-reduction.md) |
| ADR-0005 | LTV 预警由单档改为两档（0.75 / 0.85） | 已采纳 | `documented` | [ADR-0005](ADR-0005-ltv-two-tier-thresholds.md) |
| ADR-0006 | LLM 接入采用「0 依赖守卫层 + 确定性兜底」 | 已采纳 | `documented` | [ADR-0006](ADR-0006-llm-guardrail-policy.md) |
| ADR-0007 | 评估产物不入库，指标按证据等级分级 | 已采纳 | `documented` | [ADR-0007](ADR-0007-evidence-grading-and-artifact-exclusion.md) |
| ADR-0008 | H3 显式 parked：无真实 ground truth，不声称分布保真 | 已采纳 | `self-reported` | [ADR-0008](ADR-0008-h3-parked-no-ground-truth.md) |

> 迭代说明：本仓库 Git 历史极短，无法确证各决策的真实日期，故一律以迭代轮次 `第 N 轮迭代`（N = ADR 编号）标注；文内出现的日期（如阶段文档状态日、快照 `extracted_at`）均非决策日期。
