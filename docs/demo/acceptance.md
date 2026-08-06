# SpaceFin-Agent 演示 · 7 天剧本验收标准（acceptance.md）

> 配套剧本见 `docs/demo/script_7d.md`。本文件定义**可自动/可人工执行**的验收项，
> 分三组：**数据侧**（SQL 可验）、**前端侧**（页面可验）、**金额口径**（对账可验）。
> 每项给出：验收指令、阈值、通过标准。
>
> 判定总原则：**D4 vs D3 必须出现显著跳变，D7 vs D1 必须可见差异，全链路闭环数据必须齐全。**

---

## A. 数据侧（SQL / 引擎产物可验）

### A1. 7 天日期齐全

| # | 检查项 | SQL / 命令 | 通过标准 |
|---|---|---|---|
| A1.1 | `ads_risk_class` 覆盖 7 个业务日 | `SELECT DISTINCT stat_date FROM ads_risk_class ORDER BY stat_date` | 恰为 `2026-08-01` ~ `2026-08-07` 共 7 个日期，每天 5 行（五级），合计 35 行 |
| A1.2 | `ads_ltv_alerts` 覆盖 7 个业务日 | `SELECT DISTINCT alert_date FROM ads_ltv_alerts ORDER BY alert_date` | 7 个日期均存在；08-05 当日行数为 7 天内最大 |
| A1.3 | `ads_stream_ltv_alerts`（实时）覆盖 7 日 | `SELECT DISTINCT alert_date FROM ads_stream_ltv_alerts` | 7 个日期均存在（如未起实时链路，此项降级为「D4–D7 至少 2 个日期」并单独说明） |
| A1.4 | `ads_1104_g11`（如跑报送）覆盖 7 日 | `SELECT DISTINCT stat_date FROM ads_1104_g11` | 7 个日期均存在，每日 6 行（五级 + 合计） |
| A1.5 | `dws_risk_class` 快照为 D7 状态 | `SELECT COUNT(*) FROM dws_risk_class` + `MAX(etl_ts)` | 5,000 行；`etl_ts` 最新 = 08-07 批（快照表无日期列，验收最新状态即可） |

### A2. 广州事件传导（核心验收）

| # | 检查项 | 验收口径 | 通过标准 |
|---|---|---|---|
| A2.1 | **D4 广州 LTV 均值显著高于 D3** | 对 `dws_risk_class` 快照，JOIN `collateral` 取「广州市…」前缀贷款，分别重跑 D3/D4 口径取均值；或从 `output/risk/risk_report.json` 分城市统计 | **D4 广州 LTV 均值 ≥ D3 × 1.08**（目标 D3≈0.50 → D4≈0.56） |
| A2.2 | **D4 预警笔数增长倍数** | 全量离线预警：08-04 当日 `ads_ltv_alerts` 行数 ÷ 08-03 当日行数 | **≥ 1.5 倍**（目标 118/68 ≈ 1.74） |
| A2.3 | **D5 广州 strong 预警持续增长** | 广州 LTV>0.85 笔数：08-05 ÷ 08-04 | **≥ 1.3 倍**（目标 47/28 ≈ 1.68） |
| A2.4 | **D5 为全量预警峰值** | `SELECT alert_date, COUNT(*) FROM ads_ltv_alerts GROUP BY alert_date` | 08-05 行数最大（目标 ≈168） |
| A2.5 | **事件源头可追溯** | 抽查 `crawl_housing_sale` 广州天河区房源 `unit_price_yuan` | 08-04 后重新灌入/更新的天河房源单价较 08-03 平均下降 **8%–12%** |
| A2.6 | **模型版本切换** | `SELECT DISTINCT model_version FROM dws_risk_class` | D1–D3 批为 `2026-08-05-r11`（或灌数当日基线版本），D4 起为重训新版本（如 `2026-08-04-r12`）；不存在 `unknown`（R-UBQ-01 不触发） |

### A3. 处置闭环

| # | 检查项 | 验收口径 | 通过标准 |
|---|---|---|---|
| A3.1 | 确认记录存在 | `SELECT COUNT(*) FROM ads_alert_confirm` | **≥ 60 条**（目标 70），D5 起有记录 |
| A3.2 | 处置可追溯 | `ads_export_audit` 中 `action='confirm'` 或处置相关记录 | D5–D7 有确认/处置审计留痕 |
| A3.3 | 处置后预警回落 | D6 全量预警 ÷ D5 全量预警 | **≤ 0.75**（目标 112/168 ≈ 0.67）；D7 ≤ 0.6 × D5 |
| A3.4 | 处置动作作用于 LTV | 抽查 D6 处置过的贷款（`ads_alert_confirm` 中的 loan_id）：D7 快照中该批 `ltv` 均值较 D5 下降 | 处置后 LTV 均值下降 **≥ 0.03** |
| A3.5 | strong 预警收口 | D4–D5 的广州 strong 预警（LTV>0.85，累计约 75 笔）按 `(loan_id, alert_date, src)` 在 `ads_alert_confirm` 中命中确认的占比 | **≥ 80%**（累计确认 70 笔可覆盖） |

---

## B. 前端侧（驾驶舱 :8500 页面可验）

### B1. 四信任卡数值正确

| # | 信任卡 | 期望值 | 通过标准 |
|---|---|---|---|
| B1.1 | 爬取规模 | 44,369 条 / gz 1,454 条 | 页面展示 = 库内 `COUNT(*)`（7 天内不变或随 D2 增量单调不减） |
| B1.2 | 数据新鲜度 | 最近挂牌/ETL 时间 | 展示时间为 08-07 或更近，7 天内，非陈旧（>7 天判定失败） |
| B1.3 | geocode 成功率 | ≈32.8% | 数值 = 有坐标房源 / 总房源，**≥ 25%**，且与 `community_coords`/`dws_spatial_feature` 口径一致 |
| B1.4 | 模型版本 | `2026-08-05-r11` → `2026-08-04-r12` | D4 事件后版本号变化，与 `dws_risk_class.model_version` 一致 |

### B2. 闭环漏斗

| # | 检查项 | 通过标准 |
|---|---|---|
| B2.1 | 各阶段量 > 0 | 漏斗五阶段「预警 → 确认 → 处置 → 解除」各阶段计数 **> 0**（预警=ads_ltv_alerts 累计、确认=ads_alert_confirm、处置=审计留痕、解除=重算后退出预警） |
| B2.2 | 漏斗数值单调 | 上一阶段 ≥ 下一阶段（预警 ≥ 确认 ≥ 处置 ≥ 解除），无反向异常 |
| B2.3 | 钻取可跳转 | 从预警漏斗点击可跳转到预警列表/详情页，URL 与筛选参数正确（`/api/alerts` 带 `date_from/date_to/source` 参数） |

### B3. 页面时间轴差异

| # | 检查项 | 通过标准 |
|---|---|---|
| B3.1 | 五级分布趋势 | 驾驶舱五级汇总按 stat_date 切换时，08-04/05 的次级+可疑笔数 > 08-03，08-07 回落但仍 > 08-03 |
| B3.2 | LTV 直方图 | 08-04/05 快照中 `0.85–0.90`、`0.90–1.00` 桶计数显著高于 08-03（≥1.5 倍） |
| B3.3 | 城市分布 | 广州行的 `high_risk_loans` / 预警占比在 08-04/05 显著高于其它城市（≥2 倍），08-07 回落 |
| B3.4 | 预警列表日期过滤 | 选择 08-01~08-03 与 08-04~08-07 两个区间，列表数量/风险类构成可见明显差异 |

---

## C. 金额口径（对账可验）

| # | 检查项 | 验收指令 | 通过标准 |
|---|---|---|---|
| C1 | 明细 = 汇总 | `SELECT SUM(balance) FROM loan`（5,000 笔）vs `SELECT SUM(balance_total) FROM ads_risk_class WHERE stat_date='2026-08-07'` | 差额 **≤ 0.01**（元） |
| C2 | DWS 快照 = ADS 汇总 | `SELECT SUM(balance) FROM dws_risk_class` vs `ads_risk_class`（08-07 五级 balance_total 之和） | 差额 **≤ 0.01** |
| C3 | 每日占比自洽 | `ads_risk_class.balance_pct` 与按 balance_total 重算的占比 | 逐日 |balance_pct − balance_total/合计| **≤ 0.0001** |
| C4 | 7 日余额守恒 | 无处置日（D1–D5）`ads_risk_class` 每日 balance_total 合计 | 逐日差额 **≤ 0.01**（D6/D7 因处置还款允许下降，下降额可解释） |
| C5 | 预警余额口径 | 任意抽样 3 条 `ads_ltv_alerts.loan_balance` vs `dws_risk_class.balance`（同 loan_id） | 一致，差额 ≤ 0.01 |

---

## D. 边界与诚实性校验（防假戏真做）

| # | 检查项 | 通过标准 |
|---|---|---|
| D1 | 合成/真实边界标注 | 剧本文档与演示中明确标注：客户/抵押物/贷款为合成；爬取数据为真实；广州下探为模拟事件 |
| D2 | 引擎未篡改 | `git diff` 中 `tools/risk/` 无新增逻辑改动（灌数只改数据，不改引擎代码） |
| D3 | 低置信行为正常 | `dws_risk_class.low_confidence=1` 的笔数存在且与合成坐标随机性匹配（约 40%–60%），预警全部来自非低置信行 |
| D4 | 异常估值告警不失控 | `ads_risk_valuation_alerts` 中 R-UNW-03 条数较 200 笔基线（91/200≈45%）不出现数量级异常放大（5,000 笔口径 ≤ 40% 或可解释） |

---

## 执行建议

1. **数据侧 A 组**：data-dev 灌数完成后用 A1/A2/A3 SQL 全量跑一遍，输出一份 `acceptance_checklist` 打勾表。
2. **前端侧 B 组**：frontend-dev 在 8500 端口验收，逐卡逐漏斗核对。
3. **金额口径 C 组**：三处对账用 SQL `ABS(...)` 断言，容差统一 0.01。
4. 任一 A2 主指标（A2.1/A2.2/A2.3）不达标 = 事件传导未生效，需回到灌数脚本排查（挂牌价乘子 / AVM 重训 / 引擎 --date），**不得通过改验收阈值掩盖**。
