# AC 验收基线冻结（AC Baseline）

> **冻结日期**：2026-08-05（各 AC 正式验收记录日期一致；AC-01 端到端实测发生于 2026-08-05/06 UTC）。
> **冻结环境**：**合成种子（200 笔）· 广东 DWD 对齐版 · 模型 `2026-08-05-r11`**（业务库 `spacefin` 200 笔客户/抵押物/贷款 + `spacefin_crawler` 数仓；见 `docs/product/06/README.md` §0）。
> **口径声明**：本基线全部结论来自上述**合成种子一致环境**的独立端到端实测；真实数据接入后的回归属外部依赖（G1），非本基线范围。
> **AC-07 canonical 配置**：本基线中 **AC-07 的权威验收口径为 28 特征 canonical 配置**（`HistGBR + quantile(0.45) + 小区编码 EB 收缩`）。原始验收以 canonical `2026-08-05-r11` 为准（45% 覆盖 MAPE 9.88%）；当前已发布模型 `2026-08-20-r2` 同属 canonical 28 特征配置（45% 覆盖 MAPE 9.94%）。后续如需调整超参/特征，须同步重跑 AC-07 验收（见 `docs/product/06/README.md` §6 维护约定）。
> **来源**：`docs/product/05/AC-01~08-验收记录.md`（逐条正式验收记录）+ `docs/product/06/README.md` §1 验收结论表。

## 基线表

| ID | 验收口径 | 期望 | 实测值（2026-08-05） | 来源文档 | 复跑命令 / 方式 | 冻结日期 |
| -- | -------- | ---- | ------------------- | -------- | -------------- | -------- |
| AC-01 | CDC 同步至 ODS（≤10s、去重） | 变更 ≤10s 内 ODS 可见且不重复 | **0.32s ≤ 10s**，窗口内恰 1 条无误重 | `docs/product/05/AC-01-验收记录.md` | 独立端到端实测：改一笔业务库 `loan` → wall-clock 计时到 `ods_cdc_log` 可见；轮询连接用 `autocommit=True`（避免 REPEATABLE READ 读快照漏读） | 2026-08-05 |
| AC-02 | 日终五级分类一致率 ≥ 99.9% | 一致率 ≥ 99.9% | **100%（123/123）** | `docs/product/05/AC-02-验收记录.md` | 独立手工抽样核对：`python tools/risk/main.py --date <d> --write-db` 后，从业务库原始数据独立重算估值链→LTV→分档，与 `dws_risk_class` 逐笔比对（抽样 123 笔） | 2026-08-05 |
| AC-03 | LTV 两档预警（>0.75 警示级 / >0.85 强预警级） | 等号边界严格大于；预警等级与独立重算一致 | **200/200** alert_level 匹配（strong 11 / warn 6） | `docs/product/05/AC-03-验收记录.md` | 独立路径复核（`/tmp/ac0304_manual_check.py`）+ 构造边界专项（`risk_engine.enrich_loan` 压线输入）；引擎侧核对 `ads_ltv_alerts` | 2026-08-05 |
| AC-04 | 特征缺失率 > 75 → 低置信不自动预警（恰好 = 75 不标记） | 严格大于才标记；低置信抑制预警 | **10 笔低置信**且全部抑制预警；**恰好 = 75 的 91 笔不标记** | `docs/product/05/AC-04-验收记录.md` | 独立路径复核（读 `dws_spatial_feature`，独立按 `missing_pct > 75` 分档）+ 构造边界专项（75.0/75.1/74.9） | 2026-08-05 |
| AC-05 | 1104 口径不一致 → 阻断 + 告警 | 不一致则阻断（exit≠0）并写告警 | 正常报送 **exit 0**；`--simulate-mismatch` 演练 **exit 1** 且 **3 项**口径不一致告警落 `ads_report_alert` | `docs/product/05/AC-05-验收记录.md` | `python tools/reporting/main.py --date <d>`（对照组）；`python tools/reporting/main.py --date <d> --simulate-mismatch`（阻断演练，内存级偏移不污染数据） | 2026-08-05 |
| AC-06 | PII 导出脱敏 + 审计日志 | 输出已脱敏且审计已记录 | **136/136** 客户号脱敏（`c****XXXX`），业务库真实客户号 **0 泄漏** | `docs/product/05/AC-06-验收记录.md` | 独立实测：登录驾驶舱 → `GET /api/alerts/export` → 正则校验脱敏（`^c\*{4}\d{4}$`）+ 反向查业务库真实客户号 0 命中 + 查 `ads_export_audit` | 2026-08-05 |
| AC-07 | AVM 精度@覆盖率：45% 覆盖 MAPE ≤ 10% | 覆盖率 ≥ 45% 时 MAPE ≤ 10% | **9.88%**（MdAPE 6.885%，n=3618）；全量 14.59%；基线 20.64%；oracle 下界 12.65% | `docs/product/05/AC-07-验收记录.md` | canonical 超参**独立重跑**：`python tools/avm/train.py --loss quantile --quantile 0.45 --smooth-mode eb`（seed=42，28 特征 canonical 配置）；⚠️ 默认参数（squared_error/fixed）复现不达标 | 2026-08-05 |
| AC-08 | 报表口径一致率 = 100% | 任意日终一致率 = 100% | **27/27（100.000%）** | `docs/product/05/AC-08-验收记录.md` | 报表出口（`ads_1104_g11`）vs 明细独立 SQL `GROUP BY risk_class` 聚合，两条独立路径同日同指标逐字段比对（容差：余额 ≤0.01、占比 ≤0.0001、笔数严格相等） | 2026-08-05 |

## 说明

- 上表「实测值」与 `docs/product/06/README.md` §1 验收结论表逐字一致；引用时以对应 `docs/product/05/AC-0X-验收记录.md` 为权威来源。
- 诸 AC 的**验收产物**（`output/`、`dws_risk_class`、`ads_*` 等）**未随仓库分发**，属「文档记录」级证据（见 `registry.json` 的 `recorded` 条目）；如需复现须在本机重建运行环境（MySQL + 合成种子 + DWD 数据）并按「复跑命令/方式」重跑对应引擎。
- **仓库内可复算项**（测试规模、schema、阈值、配置、导航页数）不属本表冻结范围，见 `registry.json` 的 `verified` 条目与 `test_metrics.json` 快照。
