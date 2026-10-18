# ADR-0007：评估产物不入库，指标按证据等级分级

## 状态
已采纳

## 迭代
第 7 轮迭代

## 背景（Context）
- AVM 模型与评估产物体积大（模型 joblib 约 22MB）且可由脚本重建，不适合入库。
- 文档中大量数字来自历史运行，产物**未随仓库分发**，若不标注来源会误导读者。
- 需要一个「哪些数字可复算、哪些不可复算」的诚实口径。
- **观察数据**：AVM 模型 joblib 约 **22MB**；`output/`、`data_lake/` 产物体积大且可由脚本重建，不宜入库。

## 决策（Decision）
- `output/`、`data_lake/` 整目录由 `.gitignore` 排除，模型与评估产物**不随仓库分发**。
- 文档数字分两级：**已验证**（仓库内文件可直接复算）与 **文档记录**（来自历史运行报告，产物未分发、当前环境不可复现）。
- 蒸馏后的证据登记规划落在 `docs/evidence/`（registry + 快照），由 CI 复算并归档为 artifact（见可追溯性契约）。

## 备选方案（Alternatives）
- 用 Git LFS 托管大模型产物：未启用——`.gitattributes` 声明未提交、LFS 无跟踪对象，clone 后不会自动获得模型。
- 全量入库：否决——大二进制污染仓库，且产物可脚本重建。
- 不做证据分级：否决——无法区分可复算与不可复算数字，可信度不可判。

## 影响（Consequences）
**正面**
- 仓库轻量、产物可本地重建；数字引用有明确可信度标签。
- CI 复算 + artifact 归档为「不可复算」这一结构限制补了一层可追溯。
- **改变后数据**：仓库内可复算（`verified`）**13** 条、文档记录（`recorded`）**25** 条、未埋点（`unavailable`）**0** 条，共 **38** 条（`docs/evidence/registry.json`）；CI 复算并归档 artifact。

**代价**
- clone 后需本机重跑（如 `tools/avm/train.py`）才能获得模型；多数效果数字为「文档记录」。
- `docs/evidence/` 目录尚未物化，契约已定但登记实现待补。

## 证据（Evidence）
- `.gitignore` :: `output/`（`output/` 目录排除）、`.gitignore` :: `data_lake/`（`data_lake/` 目录排除）、`.gitignore` :: `*.joblib`（模型 / 数据产物排除）。
- `README.md` :: `**指标与证据等级**`、`README.md` :: `已验证**（可由仓库文件直接复算）`（「指标与证据等级」：已验证 vs 文档记录）；`README.md` :: `**AVM 模型产物（当前不随仓库分发）**`（AVM 模型产物不随仓库分发）。
- `docs/overview/data-and-domain.md` :: `## 评估证据与可复现性`（两级证据等级、已定义逻辑 vs 已运行结果、评估局限）。
- `docs/TRACEABILITY.md` :: `证据登记 docs/evidence/`、`docs/TRACEABILITY.md` :: `docs/evidence/registry.json`、`docs/TRACEABILITY.md` :: `验收基准（AC-01～08 的期望值与复跑方式）统一冻结在`（`docs/evidence/` 证据登记契约）。
- `.github/workflows/ci.yml` :: `python tools/dev/gen_evidence.py --out docs/evidence`（复算并归档）；`.github/workflows/ci.yml` :: `name: spacefin-evidence`（证据快照 artifact）。

## 取证来源（Provenance）
`documented`——排除规则与证据分级表可在仓库文件直接核对；`docs/evidence/` 为已声明契约（目录尚未创建，属待补项）。

## 回退（Rollback）
- 恢复产物入库：取消 `.gitignore` 对 `output/` 与 `data_lake/` 的整目录排除，让模型与评估产物重新随仓库分发。
- 重新生成 `docs/evidence/*.json` 快照（`tools/dev/gen_evidence.py --out docs/evidence`）并对齐 `registry.json` 的证据分级。

## 关联（Related）
- [ADR-0001-realtime-layer-removal.md](ADR-0001-realtime-layer-removal.md)
- [ADR-0002-avm-selection-convergence.md](ADR-0002-avm-selection-convergence.md)
- [docs/TRACEABILITY.md](../TRACEABILITY.md)
- [docs/overview/data-and-domain.md](../overview/data-and-domain.md)
