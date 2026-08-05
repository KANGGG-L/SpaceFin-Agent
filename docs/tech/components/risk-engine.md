# 组件技术说明 · 风险引擎（估值→LTV→五级分类→贷后预警，AC-02/03/04）

> **状态**：✅ 已实施（2026-08-05 落地，S2/S3 接入）
> **能力地图层级**：L1 风险决策层（依赖 L2 空间特征、S2 AVM）
> **所属系统**：tools/risk（全量入口）+ tools/cdc/consumer.py（增量入口）

---

## 1. 它解决什么问题

把「每笔贷款有没有风险、风险多大、要不要人工介入」变成可落库、可审计的结论：

1. **估值（AC-02）**：抵押物当前市值 = 三级回退（AVM → DWD 行情 → 业务库 true_market_price）；
2. **LTV（AC-02）**：贷款余额 / 抵押物估值，是贷后风险的核心标尺；
3. **五级分类**：按 LTV 分 正常/关注/次级/可疑/损失，供 1104 G11 报送（见 reporting-1104.md）；
4. **贷后预警（AC-03）**：LTV 超强预警线（0.85）且非低置信 → 写 `ads_ltv_alerts` 推贷后保全；
5. **低置信（AC-04）**：空间特征严重缺失（缺失率 > 75，严格大于，恰好=75 不标记）→ 抑制自动预警、转人工核查；
6. **异常估值（R-UNW-03）**：AVM 估值与参考基准偏差 >30% → 写人工核查告警；
7. **血缘（R-UBQ-01）**：每行带 `model_version`，模型缺失/无版本号 → 「不可溯源」告警。

## 2. 估值三级回退

```
① AVM（S2 模型）──命中前提：property_addr 能解析出广东城市码 + area>0
   │  未命中
   ▼
② DWD 行情──(城市码, 地名) 中位单价 × 面积；DWD_MIN_SAMPLES(20) 过滤单样本键
   │  未命中
   ▼
③ true_market_price──业务库合成价，兜底
```

- **AVM**：惰性加载 `output/avm/model.joblib`，模型/依赖缺失返回 None 不炸链，
  自然回退下一级。城市码缺失（合成地址）直接放弃——AVM 对未知城市会回退全局中位价，
  强行套用会让 LTV 失真。
- **DWD**：只在 AVM 未命中时尝试，否则 `dwd_hit` 会被误置 True、回退数变负数。
  样本门槛 `RISK_DWD_MIN_SAMPLES=20`：库里 78% 的键只有 1 行，单条挂牌冒充行情会造出
  离谱估值（实测 (dg,东城) 单行 57,066 元/㎡，高出基准 7.4 倍）。
- **列名陷阱**：`crawl_housing_sale.district` 存的是**城市码**、`community` 存的才是
  市内地名，SQL 里 AS 别名按真实语义重命名，不要照列名理解。
- **命中率天花板**：抵押物地址只能解析到**区级**，DWD 的 community 是**小区级**（1 万个
  去重值），两套地名体系只在 6 个「区名恰好被当 community 落库」的键上相交
  （惠城/清城/榕城/源城/江城/禅城，255–919 行真区级聚合，命中质量可靠）。
  这是数据粒度差异，不是 bug；提高命中应补爬虫侧行政区字段，而非维护假映射表。

## 3. 判定规则

### 3.1 LTV 与预警（AC-02/03）

```
LTV = loan.balance / 抵押物估值        （估值 ≤ 0 → LTV = None，保守归「次级」）
alert = LTV > LTV_RED_LINE(0.85) 且 非 low_confidence
```

- **严格大于**：LTV 恰好 = 0.85 压线不触发（B-01 等号边界）。
- 阈值环境变量 `RISK_LTV_RED_LINE`（默认 0.85）。
- **PRD 两档预警已实现**（警示线 0.75 警示级 / 强预警线 0.85 强预警级，R-EVT-02）：
  `tools/risk/config.py` 中 `LTV_WARN_LINE=0.75`（warn）与 `LTV_RED_LINE=0.85`（strong）
  均已存在且可配置；`risk_engine.py` 含两分支逻辑（LTV > 警示线 → warn，LTV > 强预警线
  → strong 覆盖 warn），`alert` 布尔保持兼容下游。两份阈值均为严格大于（B-01/B-02 等号边界满足）。

### 3.2 五级分类（`config.CLASS_LTV_UPPER`）

| 分类 | LTV 区间 | 环境变量（默认） |
|---|---|---|
| 正常 | ≤ 0.60 | RISK_LTV_NORMAL (0.60) |
| 关注 | 0.60 < LTV ≤ 0.75 | RISK_LTV_ATTN (0.75) |
| 次级 | 0.75 < LTV ≤ 0.85 | RISK_LTV_SUBSTD (0.85) |
| 可疑 | 0.85 < LTV ≤ 1.00 | RISK_LTV_DOUBT (1.00) |
| 损失 | > 1.00（或 LTV 为 None/负，保守） | — |

分类上界**含边界**（`≤` 进本级），与预警/异常估值的「严格大于」是两个不同的比较语义。

### 3.3 低置信（AC-04）

```
low_confidence = spatial_feat_missing_pct > LOW_CONF_MISSING_PCT(75.0)
```

- 阈值 0–100 百分数标度，`RISK_LOW_CONF_MISSING`（默认 75.0），**严格大于**（缺失率恰好 = 75 **不标记**，与 AC-04/B-04 边界一致）。
- 触发后**抑制自动预警**：`alert` 恒 False，不写 `ads_ltv_alerts`，转人工核查。
- 取值 75 与 tools/spatial 的 missing_ge75「严重缺失」口径一致；旧默认 25 在合成种子上
  会把半数抵押物打成低置信、全量屏蔽 AC-03 预警——25 对应「任一特征缺失」，75 才是
  「空间特征几乎不可用」。
- 模块 docstring（risk_engine.py）已说明实际阈值 75（严格大于）：「空间特征缺失率**严格大于** 75（0–100 标度，恰好=75 不低置信）」，旧「>= 25%」为过期注释。

### 3.4 高危区叠加（S3 接入）

```
is_high_risk_zone = 1 且 risk_class ∈ {正常, 关注} → risk_class = 关注
```

空间惩罚项：落入高危区（价格洼地 / LTV 集中区块）的贷款**至少升到「关注」**；
次级及以上的不降级不升档。

### 3.5 异常估值（R-UNW-03）

```
仅 AVM 命中行可判：
deviation_pct = |AVM估值 − true_market_price| / true_market_price
abnormal_valuation = deviation_pct > VALUATION_DEVIATION_THRESHOLD(0.30)
```

- **严格大于**：偏差恰好 = 30% 不标（B-04 边界）。
- AVM 未命中时 `deviation_pct` 置 **None**（不是 0）：回退链（DWD/true_market_price）
  与基准同源，偏差恒为 0，算了只会把异常噪声化。
- 触发后写 `ads_risk_valuation_alerts`（alert_code='R-UNW-03'），走人工核查。

### 3.6 血缘（R-UBQ-01）

`model_version` 取自 AVM 模型产物的 `version` 键；模型缺失或产物无版本号 → `'unknown'`，
写 `ads_risk_valuation_alerts`（alert_code='R-UBQ-01'，detail「模型版本缺失，估值结论不可溯源」）。

## 4. 数据口径与标度统一坑

- **业务时区**：所有 stat_date / alert_date 按 Asia/Shanghai（`config.BUSINESS_TZ`），
  不用宿主本地时区——本机 UTC、MySQL 容器 +08:00、Airflow `{{ ds }}` 三者不一致，
  用本地时区会在 UTC 16:00-24:00 的批次里把「业务第二天」标成第一天，五级占比分母错乱。
- **CITY_MAP 放 config**：全量（main.py）与增量（consumer.py）共用同一份城市映射，
  放入口脚本会导致两处漂移。
- **双标度统一（SPF-AC04 修复）**：业务库 `collateral.spatial_feat_missing_pct` 存的是
  **0–1 小数**，而 `dws_spatial_feature` 同名列与 `config.LOW_CONF_MISSING_PCT` 都是
  **0–100 百分数**。`store.load_collaterals` 在**唯一数据入口**统一为 0–100
  （`v <= 1.0 → v × 100`），业务逻辑（risk_engine）不再感知标度。旧实现不做换算，
  `0.30 >= 25.0` 恒 False → AC-04 低置信路径全库从未触发（死代码）。
- **空间特征覆盖（S3）**：`poi_density/commute_min` 有有效值即覆盖；缺失率有该实体特征
  即覆盖（不依赖 zone 命中——落在网格外恰恰说明空间信息不足，应如实上报）；
  `is_high_risk_zone` 仅在 zone 命中时覆盖，防止随机坐标整批误判高危。

## 5. 表结构

### dws_risk_class（每笔打宽明细，PK=loan_id，UPSERT 幂等）

`loan_id / customer_id / collateral_id / balance / interest_rate / market_valuation /
ltv / risk_class / low_confidence / is_high_risk_zone / alert /
valuation_deviation_pct / abnormal_valuation / model_version / etl_ts`

存量表补的偏差/异常/版本列全部允许 NULL——老数据无偏差列保持诚实，不强制填默认值。

### ads_ltv_alerts（LTV 预警，每日每笔一条）

`id(AI) / loan_id / customer_id / collateral_id / loan_balance / market_valuation /
ltv / risk_class / is_high_risk_zone / alert_date / etl_ts`，idx_loan / idx_date。
只写 `alert=1` 且非低置信的笔；替换语义「先删本批 loan_id+当日，再插」。

### ads_risk_valuation_alerts（人工核查告警）

`id(AI) / alert_code(R-UBQ-01|R-UNW-03) / loan_id / collateral_id / model_version /
valuation_deviation_pct / detail / alert_date / etl_ts`，idx_date / idx_loan。
与 LTV 预警同批「先删本批当日、再插」，一笔可同时命中两类（血缘缺失 + 偏差超标）。

### ads_risk_class（五级汇总，PK=(stat_date, risk_class)）

`stat_date / risk_class / loan_count / balance_total / balance_pct / etl_ts`。
从 `dws_risk_class` 现状 SQL 聚合重算，DELETE 当日再插——占比分母始终是全量余额。
此表即 reporting-1104 的报送数据源。

## 6. 全量 / 增量共用 store 收口（口径一致的关键）

`tools/risk/main.py`（全量）与 `tools/cdc/consumer.py`（增量）**只调用 store.py 的函数**，
不各自写 SQL：

| 环节 | 共用函数 | 语义 |
|---|---|---|
| 计算 | `store.compute_rows` | 打宽（估值→LTV→五级→预警），增量/全量同一函数 |
| 明细 | `store.upsert_dws` | 按 loan_id ON DUPLICATE KEY UPDATE 幂等覆盖 |
| 预警 | `store.replace_alerts` | 只删**本批 loan_id + 当日**再插，不清全表 |
| 删除 | `store.delete_loans` | 贷款被删 → DWS 明细 + 两张告警表三表同清（防幽灵敞口/幽灵告警） |
| 汇总 | `store.refresh_ads_risk_class` | 从 DWS 现状聚合，增量改一笔也要刷新占比分母 |

拆出 store 的动机：两套 SQL 各写一遍必然漂移（字段顺序、五级口径、预警去重规则），
一旦漂移，增量结果与全量结果对不上，验收时无法互证。

**增量影响面推导**（consumer.py）：loan 变更 → 该笔；collateral 变更 → 挂在它上面的
所有贷款（估值变了 LTV 全变）；customer 变更 → 名下所有贷款；DELETE loan → 清三表；
collateral/customer 被删 → 名下贷款按「无抵押物」保守重算。空间特征每次增量重载
（S3 表每日重建，不能缓存陈旧快照）；AVM 模型与全量共用同一产物，保证 model_version
与偏差口径两路径一致。

## 7. 运行方式

```bash
PY=tools/orchestrator/.venv/bin/python

# 全量重算（默认只落 CSV；--date 默认 Asia/Shanghai 业务日）
$PY tools/risk/main.py --date 2026-08-05
# 写 MySQL ADS 四表（建表需 root，读 DWD 走 app 账号）
$PY tools/risk/main.py --date 2026-08-05 --write-db
# 只算不写库（验收用）
$PY tools/risk/main.py --date 2026-08-05 --dry-run

# CDC 增量消费（与全量共用 store，口径一致）
$PY tools/cdc/consumer.py --once                 # 消费一批退出
$PY tools/cdc/consumer.py --loop --interval 20   # 常驻轮询
$PY tools/cdc/consumer.py --once --from-offset 0 # 从头重放对账
```

**输出**：
- 文件：`{out_dir}/dws_risk_class.csv`、`ads_ltv_alerts.csv`、`risk_report.json`
  （含 valuation_src 命中分布 / ltv_distribution / by_class / alerts_count /
  valuation_deviation / model_version / lineage_alerts）；
- MySQL：`dws_risk_class` / `ads_ltv_alerts` / `ads_risk_valuation_alerts` / `ads_risk_class`。

`--write-db` 全量路径还会清掉业务库已不存在的贷款（增量 DELETE 漏消费时的兜底对账）。

## 8. 输出样例与验收记录（2026-08-05）

| 项 | 结果 |
|---|---|
| 全量重算 | 200 笔落 dws_risk_class；五级汇总与 reporting-1104 三出口一致 |
| 五级分类汇总 | 正常 127 / 关注 47 / 次级 10 / 可疑 10 / 损失 6，合计 200 笔 / 81,849,436.14 |
| DWD 行情命中 | 22/200（11%），真区级键 6 个；命中行 DWD估值/true_market_price 中位 0.95 |
| 估值回退构成 | 合成地址（上海）无广东城市码 → AVM 大量 miss，主要由 true_market_price 兜底 |
| 低置信 | 合成种子坐标多为上海随机撒点、空间特征先天不足，低置信比例高（AC-04 预期行为） |
| 幂等重跑 | dws 按 loan_id upsert、ads_risk_class 按 (stat_date, risk_class) 覆盖，结果稳定 |

## 9. 已知局限

- **PRD 两档预警已实现**：R-EVT-02/TC-03 定义「警示线 0.75 警示级 / 强预警线 0.85
  强预警级，两档可配置、严格大于」，`tools/risk` 已实现双档——`config.LTV_WARN_LINE=0.75`
  （warn）与 `config.LTV_RED_LINE=0.85`（strong）均存在且可配置，`risk_engine.py` 含两分支
  逻辑（LTV 严格大于警示线 → warn，严格大于强预警线 → strong 覆盖 warn）。B-01/B-02 等号边界均满足。
- **低置信边界是「严格大于 75」**：`missing_pct > config.LOW_CONF_MISSING_PCT(75.0)`，
  缺失率恰好 = 75 不标记，与 AC-04/B-04 边界一致。
- **docstring 已更新**：risk_engine.py 模块 docstring 已说明「缺失率严格大于 75（恰好=75 不低置信）」，与 config 默认 75 一致。
- **估值基准自指**：参考基准 `true_market_price` 本身由模型版本自产（08-04 版），
  R-UNW-03 的偏差本质是「版本漂移」信号而非绝对市场误差，**勿据此做估值校正层**。
- **合成地址下估值链退化**：200 笔地址均为上海合成值、无广东城市码，AVM/DWD 大量 miss，
  LTV 主要由合成兜底价驱动；换真实广东抵押物数据后命中分布才有判别力。
- **异常估值不可判时置 None 而非 0**：AVM 未命中 → deviation_pct=NULL，统计口径随之
  收窄为「仅 AVM 命中行」，risk_report 的 computed 数会小于总笔数，是刻意为之。
