# 组件技术说明 · L2 空间特征（S3：价格面 / 高危区 / POI 密度 / 通勤近似）

> **状态**：✅ 已实施（2026-08-05 落地，S3 MVP）
> **能力地图层级**：L2 空间层
> **所属系统**：tools/spatial（单机近似实现）

---

## 1. 它解决什么问题

US-01 要求风险引擎具备空间惩罚项：抵押物落在「高危区」时风险等级至少升一档
（`is_high_risk_zone`），且空间特征缺失率过高时标记低置信、不触发自动预警
（AC-04，`spatial_feat_missing_pct > 75`，0–100 百分数标度，与
`config.LOW_CONF_MISSING_PCT=75` 一致；严格大于，恰好 =75 不低置信）。

S3 规划原案用 Sedona 做分布式空间连接，但本机 4 核 / 15G 内存跑不动
Spark/Sedona/Doris（规划 R-tech-1 已建议 MVP 降级）。本模块用 **scipy cKDTree +
numpy 单机实现**同语义的空间特征：半径邻域中位单价（对应 Sedona 的半径连接+聚合）、
经纬网格区块画像、挂牌密度代理、直线通勤近似。

## 2. 数据口径

| 项           | 口径                                                                                               |
| ------------ | -------------------------------------------------------------------------------------------------- |
| 价格面样本   | `spacefin_crawler.crawl_housing_sale` 中有坐标（lat/lng 非空）且有单价（>0）的行，共 **50,680** 行 |
| 坐标过滤     | 剔除广东 bbox（lat 20–26, lng 108–118）外离群点（外市污染房源）                                    |
| 局部价格基准 | 半径 **5km** cKDTree 邻域中位单价（挂牌行剔除自身；小区级剔除本小区成员），邻域样本 ≥5 才计算      |
| 价格偏差     | `(单价 - 邻域中位) / 邻域中位`，正值价格高地、负值价格洼地                                         |
| 城市均价基准 | 该城市有坐标挂牌行的中位单价（管线在 `store.load_sale_rows` 已先剔除无坐标/无效单价行）            |
| 通勤代理     | 到最近城市中心（`config.CITY_CENTER`，21 城行政中心坐标）的**球面直线距离** ÷ 平均通勤速度 30km/h  |
| POI 密度代理 | 半径 **2km** 内 DWD 挂牌行数 / 圆面积（个/km²），是**挂牌房源密度**，非真实 POI                    |

**为什么不用真实 POI / 路网**：本平台数据源只有房产挂牌，没有 POI 图层与路网数据；
MVP 以挂牌密度近似人流/配套热度、以直线距离近似通勤，均已在文档与代码注释中如实标注。

## 3. 高危区判定规则（可解释）

### 规则 A：价格洼地（广东 DWD 网格）

区块（0.02°≈2.2km 经纬网格，同城市）中位单价显著低于城市均价且样本足够：

```
区块中位单价 <= 城市中位单价 × (1 - 0.25)  且  区块样本 >= 20
→ is_high_risk_zone = 1, high_risk_rule = 'price_low'
```

逻辑：邻域均价显著低于城市均价 → 抵押物折价/处置回收风险高。阈值可用
`SPATIAL_PRICE_LOW_RATIO` / `SPATIAL_MIN_ZONE_SAMPLES` 覆盖。

### 规则 B：LTV 集中（抵押物网格）

抵押物坐标网格（同 0.02° 网格）内 LTV 中位 > 红线且样本足够：

```
区块 LTV 中位 > 0.85（RISK_LTV_RED_LINE）  且  样本 >= 3
→ is_high_risk_zone = 1, high_risk_rule = 'ltv_high'
```

LTV = Σ贷款余额 / true_market_price（业务库合成市值）。当前 seed 的抵押物坐标为
广东 21 城主城区合成值（与同城 DWD 网格可重叠；早期 seed 曾为上海坐标、与广东 DWD
网格不重叠，`features.py` 注释与单测仍以上海坐标作基线），LTV 区块独立成表
（`zone_type='ltv'`）。

## 4. 表结构

### ads_spatial_zone（空间区块画像）

| 字段                    | 类型             | 说明                                                                                                     |
| ----------------------- | ---------------- | -------------------------------------------------------------------------------------------------------- |
| zone_id                 | VARCHAR(64)      | `price-{city}-{lng}-{lat}` / `ltv-{lng}-{lat}`                                                           |
| zone_type               | VARCHAR(8)       | `price`（价格面） / `ltv`（LTV 集中）                                                                    |
| city                    | VARCHAR(8)       | 城市码，ltv 区块为 NULL                                                                                  |
| center_lng / center_lat | DECIMAL(10,6)    | 区块中心经纬度                                                                                           |
| sample_count            | INT              | 区块样本量                                                                                               |
| median_unit_price       | DECIMAL(12,2)    | 区块中位单价（price 型）                                                                                 |
| median_ltv              | DECIMAL(8,4)     | 区块中位 LTV：ltv 型是规则 B 的网格中位；price 型是同 (城市, 网格) 抵押物 LTV 中位回填，样本 < 3 为 NULL |
| ltv_sample_count        | INT              | price 型回填时 = 该网格内抵押笔数；未回填或 ltv 型为 NULL（`build_ltv_zones` 不产出该键）                |
| price_dev_vs_city       | DECIMAL(10,4)    | 区块相对城市中位的偏差                                                                                   |
| is_high_risk_zone       | TINYINT          | 高危标记（规则 A/B）                                                                                     |
| high_risk_rule          | VARCHAR(16)      | 触发规则名                                                                                               |
| build_date / etl_ts     | DATE / TIMESTAMP | 构建日期 / 写入时间                                                                                      |

### dws_spatial_feature（每实体空间特征）

| 字段                     | 类型             | 说明                                                             |
| ------------------------ | ---------------- | ---------------------------------------------------------------- |
| entity_type              | VARCHAR(16)      | `listing`（挂牌行）/ `community`（小区）/ `collateral`（抵押物） |
| entity_id                | VARCHAR(64)      | url_key / `{city}\|{community}` / collateral_id                  |
| district                 | VARCHAR(8)       | 城市码                                                           |
| lat / lng                | DECIMAL(10,6)    | 实体坐标                                                         |
| poi_density              | DECIMAL(10,4)    | 挂牌密度代理（个/km²），无本地覆盖为 NULL                        |
| commute_min              | DECIMAL(8,2)     | 直线通勤分钟，超出覆盖半径（60km）为 NULL                        |
| zone_id                  | VARCHAR(64)      | 命中的区块，不在区块内为 NULL                                    |
| price_deviation          | DECIMAL(10,4)    | 价格偏差（估值偏差）                                             |
| spatial_feat_missing_pct | DECIMAL(5,2)     | 4 个空间特征缺失占比（0–100）                                    |
| build_date / etl_ts      | DATE / TIMESTAMP | 构建日期 / 写入时间                                              |

`spatial_feat_missing_pct` 与业务库 `collateral` 现有字段命名对齐：
`poi_density / commute_min / is_high_risk_zone / spatial_feat_missing_pct`
（`is_high_risk_zone` 由 `zone_id` 归属推导）。字段类型/量纲见「已知局限」。

## 5. 运行方式

```bash
# 唯一 Python 环境
PY=tools/orchestrator/.venv/bin/python

# 计算 + 落库（root 建表 + 幂等 upsert），跑完即退出
$PY tools/spatial/main.py --once

# 指定构建日期 / 只算不写库
$PY tools/spatial/main.py --once --date 2026-08-05
$PY tools/spatial/main.py --once --dry-run
```

输出：MySQL `spacefin_crawler` 的 `ads_spatial_zone` / `dws_spatial_feature`，
以及 `output/spatial/spatial_report.json`（行数、规则触发、缺失率统计）。

**幂等性**：两表均以业务键做主键（zone_id / (entity_type, entity_id) + build_date），
`INSERT ... ON DUPLICATE KEY UPDATE` 覆盖，重复执行结果一致。

## 6. 验收记录（2026-08-30）

| 项                 | 结果                                                                 |
| ------------------ | -------------------------------------------------------------------- |
| DWD 有坐标挂牌行   | 50,680（对比 sale 库存 59,081 的有坐标部分，与 geocode hit 一致）    |
| 价格区块（rule A） | 305 个，其中高危 35 个（`price_low`）                                |
| LTV 区块（rule B） | 222 个，高危 0 个（抵押物网格 LTV 中位均未超红线）                   |
| listing 特征       | 50,680 行                                                            |
| community 特征     | 15,617 行                                                            |
| collateral 特征    | 5000 行，`spatial_feat_missing_pct >= 75`（`missing_ge75`）共 724 行 |
| 重复执行一致性     | 行数不变（527 / 71,297）                                             |

> 注：上表为 2026-08-30 验收快照（seed 5000 笔、sale 库存 59,081）。2026-09-29（commit
> 9381ff1）起 `seed/generate_seed.py` 默认 `n=200`，按新参数重灌后各表行数会随参数变化，
> 本文行数不再代表当前运行态（代码逻辑不变）。

## 7. 已知局限

- **坐标覆盖约 85.8%**：sale 库存 59,081 行中 50,680 行有坐标；约 31% 小区名 NULL，无坐标行不参与空间
  计算（这是数据源现状，非本模块缺陷；补全依赖 geocode 管道）。
- **种子已对齐广东（空间侧现状）**：collateral 坐标为广东 21 城主城区合成值
  （`seed/generate_seed.py` 按各市坐标框均匀抽样），与 DWD 挂牌网格同城、大多可覆盖 →
  `poi_density / commute_min / price_deviation` 可覆盖；未落在挂牌网格的抵押物仍会缺失。
  `spatial_feat_missing_pct` 验收快照 `missing_ge75` = 724 行（≈14.5%），风险引擎按 `> 75`
  置低置信。低置信抑制口径见 [risk-engine.md](risk-engine.md) §3.3。
- **单机近似 vs Sedona**：cKDTree 半径邻域在 1.3 万点规模秒级完成，但距离为等距
  圆柱投影（广东尺度误差 <1%）；数据量上百万级时应迁回 Sedona R-Tree 半径连接。
- **POI 密度是挂牌密度代理**：挂牌集中度高≠配套丰富，只反映房源热度；真实 POI
  需引入地图 POI 数据源。
- **通勤是直线近似**：无路网，直线距离/30km/h 会系统性低估实际通勤（路网系数
  通常 1.4–1.6×），且未区分通勤方式；`CITY_CENTER` 为行政中心约值（±2km）。
- **量纲对齐提示**：`poi_density` 是「个/km²」物理量（业务库 collateral 旧字段是
  合成 0–1 占位值）；`commute_min` 是分钟；`spatial_feat_missing_pct` 是 0–100。
  主 agent 接入风险引擎时应以本模块 dws 表为准，勿混用业务库合成占位字段。
- **LTV 规则样本仍然不足**：2026-08-30 快照 5000 笔种子下 222 个 LTV 区块且均未高危
  （`n_high_risk_ltv_zones=0`）；规则逻辑已实现，需真实广东抵押物数据后才有判别力。

## 8. 行政区边界（admin_boundaries）

> **所属模块**：`tools/spatial/admin_boundaries.py`（零第三方依赖）
> **调用方**：P6 策略推演中心 `tools/frontend/pages/p6_policy.py`（P6 组合压力测试按区分组）
> **验收**：`tools/frontend/tests/test_admin_boundaries.py`

### 8.1 它解决什么问题

为 P6 组合压力测试提供「按抵押物落点归属区/镇」的分组维度。核心方法是 **even-odd 射线法点面匹配**：用零依赖的奇偶规则把每笔抵押物的 (lng, lat) 落到行政区/镇多边形内，得到区名/镇名。

行政区边界来源分两类：
- **普通城市**：阿里 **DataV GeoAtlas**（`areas_v3`，按国标 adcode 拉取区县级 `full.json`）；
- **直筒子市（东莞 dg / 中山 zs，DataV 无区县面）**：改走 **OSM Overpass** 拉取 `admin_level=8` 的镇/街道 relation，再把多条 member way 按共享端点拼接成闭合环（`_stitch_rings`）。

### 8.2 为何按坐标而非按地址

抵押物地址 `property_addr` 在合成数据里是「小区名」风格的文本，真实数据里区名覆盖率仅 ~5.6%——按地址里的「××区」筛会把绝大多数 record 归「未识别」，分组表失去意义。而抵押物坐标（`dws_spatial_feature` 的 collateral lat/lng）是全仓「抵押物落点」的唯一口径（P5 网格命中、空间聚合都吃它），按坐标落区与既有空间链路同源，两处不会漂。

### 8.3 关键函数

| 函数 | 作用 |
| ---- | ---- |
| `load_city_districts(city_code)` | 城市码 → (区/镇列表 `[{name, polygons, bbox}]`, 层级)；数据不可用返回 `None`；层级 `district`/`town` 由名称形状判定（`_detect_level`） |
| `assign_district(lng, lat, districts)` | 单点坐标 → 区名：bbox 预筛 + `_point_in_rings` even-odd 射线法判内外（含洞/多面） |
| `loan_district_grouping(loans, city_code)` | 贷款列表 → (`{loan_id: 区/镇名 \| None}`, 层级)；城市边界不可用返回 `(None, None)`（区别于逐笔匹配不到的 `None` 值映射），调用方据此降级分组维度 |

### 8.4 命名漂移提示

⚠️ 历史遗留：该模块的 git 提交信息多写作「**P5 空间页联动**」，但其模块 docstring 与**唯一调用方均为 P6**（P6 策略推演中心按区分组）。请勿去 P5 找该能力——能力归属是 **P6**，P5 不涉及行政区边界分组。
