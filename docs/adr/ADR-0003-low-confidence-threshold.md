# ADR-0003：低置信阈值由原型 0.20 收敛为 75.0，判定为严格大于

## 状态
已采纳

## 迭代
第 3 轮迭代

## 背景（Context）
- 阶段 3 核心原型以 `spatial_feat_missing_pct > 0.20`（0–1 标度）标记低置信。
- 交付到风险引擎时，缺失率统一为 0–100 百分数标度，阈值收敛为 `LOW_CONF_MISSING_PCT`。
- 旧默认 25 在合成种子上会把半数抵押物打成低置信、屏蔽 AC-03 预警——25 对应「任一特征缺失」，75 才对应「空间特征几乎不可用」。
- **观察数据**：合成种子上，旧默认阈值 25 判定时约半数抵押物落入低置信、屏蔽 AC-03 预警；改为阈值 75.0 后实测 **10 笔**低置信、恰好 =75 的 **91 笔**不标记（`ac04_low_conf`）。

## 决策（Decision）
- 阈值定为 **75.0**（0–100 标度）；语义为缺失率**严格大于**阈值才低置信，恰好 = 75 不标记。
- 低置信笔被抑制自动预警、转人工核查（AC-04）。
- 口径与 `tools/spatial/main.py` 的 `missing_ge75`「严重缺失」一致，全项目只用一个标准。

## 备选方案（Alternatives）
- 保留原型阈值 0.20 / 25：否决——会把半数抵押物打成低置信，屏蔽 AC-03 预警。
- 以「任一特征缺失」（≈25）作口径：否决——过于敏感，语义是数据不完整而非不可用。
- 含等号（≥75）判定：否决——与 B-04 / R-UNW-03 的「严格大于」边界口径不一致。

## 影响（Consequences）
**正面**
- 只标记真正「空间特征几乎不可用」的笔，不误杀正常预警。
- 阈值可配置（`RISK_LOW_CONF_MISSING`），边界口径全项目统一。
- **改变后数据**：阈值 = **75.0**（`low_conf_missing_pct`）时，实测 **10 笔**低置信（全部抑制预警）、恰好 =75 的 **91 笔**不标记（`ac04_low_conf`）；AC-03 **200/200** 匹配。

**代价**
- 旧原型报告的「缺失率 >20%」成为历史口径，需在文档中修正说明，否则读者口径漂移。

## 证据（Evidence）
- `docs/poc/core-prototype/README.md` :: `特征缺失率 >20%（0–1 标度，脚本内阈值 0.20）`（原型低置信标记：脚本内阈值 0.20）。
- `docs/poc/core-prototype/mvp_prototype.py` :: `is_low_conf = rec["spatial_feat_missing_pct"] > 0.20`（原型低置信判定公式）。
- `tools/risk/config.py` :: `LOW_CONF_MISSING_PCT = float(`（`LOW_CONF_MISSING_PCT` 默认 75.0；注释给出 75 vs 25 的口径理由）。
- `tools/risk/risk_engine.py` :: `low_conf = missing_pct > config.LOW_CONF_MISSING_PCT`（严格大于判定）。
- `docs/product/06/README.md` :: `低置信阈值口径：**严格大于**`（C 类漂移修正：低置信阈值口径为严格大于 75.0）。
- `docs/product/03/README.md` :: `空间特征缺失率 > 75`（R-UNW-01 口径）；`docs/product/03/README.md` :: `Given 空间特征缺失率 > 75`（AC-04 口径）。

## 取证来源（Provenance）
`documented`——原型与终版阈值均可在代码 / 文档中直接核对，收敛理由记录在阶段 6。

## 回退（Rollback）
- 改回 `tools/risk/config.py` 的 `LOW_CONF_MISSING_PCT`（支持环境变量 `RISK_LOW_CONF_MISSING` 覆盖）即可恢复旧阈值口径。
- 回退后须重跑 AC-04（低置信标记）及相关边界用例，确认「恰好 = 阈值不标记」的严格大于语义未漂移。

## 关联（Related）
- [ADR-0002-avm-selection-convergence.md](ADR-0002-avm-selection-convergence.md)
- [ADR-0005-ltv-two-tier-thresholds.md](ADR-0005-ltv-two-tier-thresholds.md)
- [docs/tech/components/risk-engine.md](../tech/components/risk-engine.md)
- [docs/poc/core-prototype/README.md](../poc/core-prototype/README.md)
