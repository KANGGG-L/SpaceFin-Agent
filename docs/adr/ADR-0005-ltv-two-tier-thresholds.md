# ADR-0005：LTV 预警由单档改为两档（0.75 警示 / 0.85 强预警）

## 状态
已采纳

## 迭代
第 5 轮迭代

## 背景（Context）
- 早期风险引擎报告把 LTV 预警描述为**单档**。
- 代码实际已实现两档：`LTV_WARN_LINE` 与 `LTV_RED_LINE`，边界为严格大于。
- 阶段 6 文档收口时，将该「代码已实现、旧报告写错」识别为 C 类文档漂移并修正。
- **观察数据**：AC-03 实测预警分级 **200/200** 匹配（strong **11** / warn **6**）（`ac03_alert_match`）。

## 决策（Decision）
- 两档阈值：`LTV_WARN_LINE = 0.75`（警示级）、`LTV_RED_LINE = 0.85`（强预警级），均可配置。
- 判定为**严格大于**：LTV > 0.85 → `strong`；严格大于 0.75 → `warn`；恰好等于阈值不触发；`strong` 覆盖 `warn`。
- 低置信笔不参与自动预警；预警经 T+1 批处理推送贷后保全（语义为「不闭环」）。

## 备选方案（Alternatives）
- 单档阈值：否决——无法区分预警紧迫度，贷后分级处置需求无法满足。
- 含等号（≥）判定：否决——与 B-04 / R-UNW-03 的「严格大于」边界口径不一致。
- 三档及以上：否决——超出当前业务需要，两档已覆盖「警示 / 强预警」分级。

## 影响（Consequences）
**正面**
- 预警分级明确，贷后可按级别响应；阈值可配置便于验收调参。
- 边界口径全链路统一（严格大于）。
- **改变后数据**：两档阈值 **0.75**（`ltv_warn_line`）/ **0.85**（`ltv_strong_line`），严格大于触发；AC-03 200/200 匹配。

**代价**
- 旧报告的单档描述需更正；AC-03 验收须按两档口径重跑。

## 证据（Evidence）
- `tools/risk/config.py` :: `LTV_WARN_LINE = float(os.getenv(`、`tools/risk/config.py` :: `LTV_RED_LINE = float(`（`LTV_WARN_LINE=0.75`、`LTV_RED_LINE=0.85`；注释「严格大于」触发）。
- `tools/risk/risk_engine.py` :: `if ltv > config.LTV_RED_LINE:`（两档判定逻辑，`strong` 覆盖 `warn`，等于阈值不触发）。
- `docs/product/06/README.md` :: `LTV 两档预警**已实现**`（C 类漂移修正：两档预警已实现，并非只有单档）。
- `docs/product/03/README.md` :: `两档预警比对（默认警示线 0.75 / 强预警线 0.85，可配置）`、`docs/product/03/README.md` :: `When LTV 比值超过强预警线（默认 0.85）`、`docs/product/03/README.md` :: `0.75 < LTV ≤ 0.85 为「警示级」`（PRD 两档口径与 AC-03）。
- `docs/overview/data-and-domain.md` :: `并对 LTV 设**两档线**：警示 0.75 / 强预警 0.85`（LTV 两档线口径）。

## 取证来源（Provenance）
`documented`——阈值与边界比较可在配置与引擎代码直接核对，修正记录在阶段 6。

## 回退（Rollback）
- 改 `tools/risk/config.py` 的 `LTV_WARN_LINE` / `LTV_RED_LINE`（可经环境变量 `RISK_LTV_WARN_LINE` / `RISK_LTV_RED_LINE` 覆盖）即可回到单档或调整档位。
- 回退后须重跑 AC-03（预警分级匹配）与 AC-07（AVM 精度），复核分级口径。

## 关联（Related）
- [ADR-0003-low-confidence-threshold.md](ADR-0003-low-confidence-threshold.md)
- [docs/tech/components/risk-engine.md](../tech/components/risk-engine.md)
- [docs/product/06/README.md](../product/06/README.md)
- [docs/overview/data-and-domain.md](../overview/data-and-domain.md)
