# 合成 Seed 数据生成器（Sprint 0）

本目录用于为本地 MySQL 业务源库生成**合成**信贷样本，产出可被 MySQL 初始化直接执行的 SQL（`sql/init/02_seed.sql`）。

## 运行

```bash
python3 seed/generate_seed.py          # 默认每表 200 条
python3 seed/generate_seed.py 500      # 指定每表条数
# 或
make seed-gen
```

仅依赖 Python 标准库。

## 产出

`sql/init/02_seed.sql`：三张表的分批 `INSERT` 语句。该文件是生成产物，**已提交入库**作为参考输出；MySQL 容器首次初始化（数据卷为空）时由 `/docker-entrypoint-initdb.d` 自动导入。

## 确定性 / 可复现

生成器对每张表使用**固定随机种子**，放款日期相对**固定基准日（2024-01-01）**生成，因此重复运行产出的 SQL **逐字节一致**（可用 `md5sum` 验证）。这避免了"换台机器重跑就 diff"的问题。

## 三张表与分布

| 表 | 主键 | 关键字段与分布（合成） |
|----|------|----------------------|
| `customer` | customer_id (10000+) | credit_score ~ N(680, 60)；income_monthly ~ U(4000, 25000)；debt_ratio ~ U(0.1, 0.8) |
| `collateral` | collateral_id (20000+) | 经纬度（模拟城市带）、面积 U(40,140)、房龄 U(0,30)；`true_market_price` 由**空间变化系数**合成（与 `docs/poc` 一致，模拟空间非平稳性）；poi_density、commute_min、is_high_risk_zone(≈15%)、spatial_feat_missing_pct |
| `loan` | loan_id (30000+) | 关联 customer/collateral；loan_amount、balance、interest_rate；`risk_class` 五级分类按 80/12/5/2/1 分布；origination_date 在基准日前约 3 年内 |

字段口径与 `sql/init/01_schema.sql` 严格对齐，分布与 `docs/poc/core-prototype/mvp_prototype.py` 保持一致，便于后续 AVM / LTV 链路衔接。

## 合规（数据红线）

本目录全部为**合成数据**，不含任何真实个人金融信息；`property_addr` 为 `合成地址-{id}` 占位，仅作 schema 演示。

这对应项目数据策略的硬约束——**数据分两类，红线分明**：

- **个人贷款数据**（客户 / 征信 / 贷款台账等 PII，即本目录三张表）：**一律合成 populate**，任何 Sprint 都不得替换为真实个人信息、也不得通过爬虫采集。
- **公开渠道数据**（房产挂牌/成交、小区、POI、坐标）：以爬虫等公开渠道获取（见 `tools/anjuke_crawler/`），不含 PII，属另一条合规路径。

后续接入任何数据源前，先确认它落在哪一类——个人数据只能合成，公开数据走公开渠道。
