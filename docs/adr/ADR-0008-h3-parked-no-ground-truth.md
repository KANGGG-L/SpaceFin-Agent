# ADR-0008：H3 显式 parked——无真实 ground truth，不声称分布保真

## 状态
已采纳

## 迭代
第 8 轮迭代

## 背景（Context）
- H3 生死假设：合成画像能如实反映真实经济状态下的不良率分布（KS ≤ 0.05）。
- 真实不良率 / 普查 ground truth **不可得**（外部资源依赖）；PRD 把 H3 显式 parked 为 stretch。
- 现有验证以**合成种子**为基准，只能证明「美化偏见可被检测、可被校准」的机制，不能证明真实分布保真。
- **观察数据**：合成种子上未校准分布 KS **0.257**（`persona_ks_naive`）；目标门禁 KS ≤ **0.05**（`persona_ks_target`）。真实不良率/普查 ground truth 不可得。

## 决策（Decision）
- 不声称合成画像的分布保真：验证基准为合成种子，真实基准属商用前置。
- 进入 V2 的前置门禁 = 小规模分布对齐 PoC 达标（KS ≤ 0.05，`tools/persona/critic.py` 的 `DEFAULT_TARGET_KS`）；否则降级为纯可视化推演工具。
- 未经校准的输出固定标注「未校准」，不得直接用于授信决策（前端沙盘横幅同步）。

## 备选方案（Alternatives）
- 用通用 LLM 直出画像：否决——存在社会期望偏见（P3 痛点）。
- 以合成种子基准宣称分布保真：否决——基准非真实分布，达标不等于保真。
- 直接以真实不良率校准：不可行——ground truth 不可得，属外部资源依赖。

## 影响（Consequences）
**正面**
- 诚实暴露能力边界；生成 → 批评 → 校准机制可演示、可复现。
- 校准机制不依赖真实数据即可验证（分位数映射，固定 seed）。
- **改变后数据**：校准后 KS **0.028** ≤ **0.05**（`persona_ks_calibrated`，合成种子基准）；真实分布保真仍未验证。

**代价**
- 沙盒效果受限于合成基准，真实分布保真未验证（列为交付缺口）。
- KS 只检验单变量边际分布，不检验特征间的联合依赖结构。

## 证据（Evidence）
- `docs/product/03/README.md` :: `故显式 parked 为 stretch`（H3 显式 parked 为 stretch，真实不良率 ground truth 不可得）、`docs/product/03/README.md` :: `H3 显式 parked 为 stretch`（Non-goals：不交付沙盒完整实现）、`docs/product/03/README.md` :: `Critic 的 ground truth 基准取自哪份官方普查/不良率？`（Q3 未确认）。
- `tools/persona/critic.py` :: `DEFAULT_TARGET_KS = 0.05`、`tools/persona/critic.py` :: `def calibrate(benchmark_data, personas, target_ks=DEFAULT_TARGET_KS`（校准闭环，迭代至违约率分布 KS ≤ 0.05）。
- `tools/persona/README.md` :: `本原型的「真实基准」`、`tools/persona/README.md` :: `不是**真实普查/银行数据`（数据来源口径：真实基准 = 合成 seed，非真实普查 / 银行数据）；`tools/persona/README.md` :: `**0.257** (> 0.05)`、`tools/persona/README.md` :: `**0.028** (≤ 0.05)`（KS 0.257 → 0.028，历史运行记录）。
- `README.md` :: `H3 已由最小 Critic 原型验证达成`（H3 已由最小 Critic 原型验证达成，基准为合成种子，真实基准属商用前置）。
- `docs/overview/data-and-domain.md` :: `验证该偏见可被 KS 统计量检测并校正`、`docs/overview/data-and-domain.md` :: `尚未验证的预期`（合成画像校准口径与尚未验证的预期）。

## 取证来源（Provenance）
`self-reported`——「真实 ground truth 不可得」本身无法在仓库内证实或证伪，且分布保真结论仅由本仓文档以合成种子自述，无独立记录可供核对。

## 回退（Rollback）
- 取得真实 ground truth（真实不良率 / 普查基准）后，按 `tools/persona/` 的 Critic 校准链重跑（generator → critic.calibrate），复评 KS 门禁。
- 若仍无真实基准，维持 parked 现状：以合成种子为基准并保留「未校准」标注。

## 关联（Related）
- [ADR-0006-llm-guardrail-policy.md](ADR-0006-llm-guardrail-policy.md)
- [docs/product/03/README.md](../product/03/README.md)
- [docs/TRACEABILITY.md](../TRACEABILITY.md)
- [tools/persona/README.md](../../tools/persona/README.md)
