# 合成 Seed 数据生成器（Sprint 0）

本目录用于为本地 MySQL 业务源库生成**合成**信贷样本，产出可被 MySQL 初始化直接执行的 SQL（`sql/init/02_seed.sql`）。

## 运行

```bash
python3 seed/generate_seed.py              # 默认每表 200 条（仅生成 SQL）
python3 seed/generate_seed.py 5000         # 5000 笔（7 天演示规模）
python3 seed/generate_seed.py 5000 --write-db   # 生成并直接灌入 MySQL 业务库
# 或
make seed-gen
```

仅依赖 Python 标准库；若运行环境有 sklearn/joblib（如 `tools/orchestrator/.venv`），
自动接入 AVM 估值让基线日 LTV 精确等于目标 LTV（无模型环境回退 `true_market_price`）。

## 产出

`sql/init/02_seed.sql`：三张表的分批 `INSERT` 语句。该文件是生成产物，**已提交入库**作为参考输出；MySQL 容器首次初始化（数据卷为空）时由 `/docker-entrypoint-initdb.d` 自动导入。

## 确定性 / 可复现

生成器对每张表使用**固定随机种子**，放款日期相对**固定基准日（2024-01-01）**生成，因此重复运行产出的 SQL **逐字节一致**（可用 `md5sum` 验证）。这避免了"换台机器重跑就 diff"的问题。

## 三张表与分布

| 表           | 主键                   | 关键字段与分布（合成）                                                                                                                                                                                                                                                                                                                                                            |
| ------------ | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `customer`   | customer_id (10000+)   | credit_score ~ N(680, 60)；income_monthly ~ U(4000, 25000)；debt_ratio ~ U(0.1, 0.8)                                                                                                                                                                                                                                                                                              |
| `collateral` | collateral_id (20000+) | **广东 21 城地址**（城市+区+小区名+门牌，如「惠州市惠城区惠城24号」），经纬度落在对应城市坐标框内；面积 U(40,140)、房龄 U(0,30)；`true_market_price` 有键城市以其选中 DWD 键均价为基准、无键城市以 AVM 模型隐含单价（见生成器 `CITY_UNIT_PRICE` 注释）为基准加 ±10% 噪声合成；poi_density、commute_min、is_high_risk_zone(≈15%)、spatial_feat_missing_pct（约 50% > 75%，对齐 AC-04 低置信演示口径） |
| `loan`       | loan_id (30000+)       | 关联 customer/collateral；loan_amount、balance、interest_rate；`balance = 目标 LTV × 抵押物估值`（目标 LTV 按城市分组设计，广州按揭敞口偏大、其余城市健康，见生成器 `_target_ltv`）；`risk_class` 为申报口径、引擎按 LTV 实时重算覆盖；origination_date 在基准日前约 3 年内 |

城市分布（2026-08-06 起）：**广州占比 ≈25%**（7 天演示事件城市，剧本「广州 LTV 上穿」需要
足够样本），其余 20 城按 `crawl_housing_sale` 真实爬取分布铺满；由 `CITY_WEIGHTS` 控制，
恢复 21 城等分轮序只需把 `gz` 权重改为 ≈4.76。

地址对齐广东的原因：风险引擎的三级回退（AVM → DWD → true_market_price）要求抵押物地址能
解析出广东城市码，合成地址无城市码时估值永远回退兜底（dwd_hits=0、avm_hits=0）。详见
`docs/tech/components/cdc-downstream.md` 第 7 节。

DWD 命中口径（2026-08-05 起）：有键城市（17 城）的地址写作 `{城市}市{key}区{key}{门牌}号`，
`key` 为 `crawl_housing_sale.community` 的**实有键**（优先区级聚合，如 惠城/禅城/榕城/汕尾城，
辅以高样本真实小区），剥掉「区」后缀后 100% 命中 DWD 行情（实测 161/200 → 80.5%，其余
39 笔恰为 zs/yf/zh/dg 四城，见生成器 `DWD_KEYS` 与模块文档）。噪音描述词键（次新小区/新城/
阳光城…）与 >6 字键已被排除，`zs/yf/zh/dg` 四城 DWD 为 100% 外市错标、不配键。

字段口径与 `sql/init/01_schema.sql` 严格对齐，分布与 `docs/poc/core-prototype/mvp_prototype.py` 保持一致，便于后续 AVM / LTV 链路衔接。

## 合规（数据红线）

本目录全部为**合成数据**，不含任何真实个人金融信息；`property_addr` 以 DWD 实有键为骨架
拼装（键为公开行情库的区级聚合/小区名，仅作 schema 演示与估值链命中用），不含任何真实
个人门牌/隐私信息。

这对应项目数据策略的硬约束——**数据分两类，红线分明**：

- **个人贷款数据**（客户 / 征信 / 贷款台账等 PII，即本目录三张表）：**一律合成 populate**，任何 Sprint 都不得替换为真实个人信息、也不得通过爬虫采集。
- **公开渠道数据**（房产挂牌/成交、小区、POI、坐标）：以爬虫等公开渠道获取（见 `tools/anjuke_crawler/`），不含 PII，属另一条合规路径。

后续接入任何数据源前，先确认它落在哪一类——个人数据只能合成，公开数据走公开渠道。

## 诚实标注（7 天演示回填）

7 天演示（`docs/demo/script_7d.md`）使用的 5,000 笔数据中：

- **仅客户 / 抵押物 / 贷款三表为脚本合成**；`crawl_housing_sale` 4.4 万条房源为真实爬取。
- 演示的「广州挂牌价下探」是对真实爬取行做单价乘子的**脚本扰动**（非真实市场信息），
  由 `tools/dev/backfill_7d.py` 执行、原值记录在 `ads_demo_gz_perturb` 表可回滚。
- 7 天 `ads_risk_class` / `ads_ltv_alerts` 为**演示回填产物**，用途是展示引擎传导闭环，
  不构成任何真实市场或授信结论。

## 导入注意（charset）

`02_seed.sql` 文件头已带 `SET NAMES utf8mb4;`，`01_schema.sql` 同样——容器首次初始化时
mysql 客户端默认字符集可能是 latin1，没有这行会把中文地址按 latin1 存储成乱码，城市名
无法被估值链解析（历史踩坑，见 `01_schema.sql` 头注释）。若手工导入请用
`mysql --default-character-set=utf8mb4 < 02_seed.sql`。
