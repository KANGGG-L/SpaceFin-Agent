# 组件技术说明 · MySQL 业务源库（L0 · Sprint 0）

> **状态**：✅ 已接入
> **能力地图层级**：L0 数据底座 — 数据源头
> **引入原则**：按需接入（Just-in-Time）。本组件是整条链路的最小起点——先有可查的信贷数据，后续分层 / 估值 / 预警才有输入。

---

## 1. 为何此时引入

整条风控链路（湖仓分层 → AVM 估值 → LTV 预警 → 1104 报送）的输入是**信贷业务数据**（客户 / 抵押物 / 贷款）。在接入任何仓库或计算组件之前，必须先有一个可查询的业务数据源头。因此 Sprint 0 仅引入 MySQL 作为业务源库，**不预先搭任何下游组件**。

## 2. 生产对应物（诚实标注）

| 本项目（可跑实现） | 生产对应物 |
|--------------------|-----------|
| 单节点 MySQL 8.0 容器 + 合成 seed | 金融机构核心业务系统（MySQL / Oracle），承载真实客户、抵押物、贷款台账 |

本组件用单机 MySQL + 合成数据**模拟**核心业务系统的角色与 schema 形态；真实生产环境为机构内网核心库，数据合规获取路径见 `docs/product/01/数据现状摸底.md`（D3 信贷业务数据）。

## 3. 数据模型（schema）

见 `sql/init/01_schema.sql`，三张表：

| 表 | 说明 | 关键字段 |
|----|------|---------|
| `customer` | 客户（借款人）主档 | credit_score / income_monthly / debt_ratio |
| `collateral` | 抵押物（房产）主档 + 空间特征 | lat / lng / area / age / true_market_price / poi_density / commute_min / is_high_risk_zone / spatial_feat_missing_pct |
| `loan` | 贷款台账 | loan_amount / balance / interest_rate / risk_class(五级分类) / origination_date |

设计要点：
- `loan` 通过外键关联 `customer` / `collateral`，并对 `risk_class` 设 `CHECK` 约束限定五级分类取值（正常 / 关注 / 次级 / 可疑 / 损失）。
- 表 / 列均带中文 `COMMENT`，自描述业务含义。
- 字符集 `utf8mb4`，存储引擎 `InnoDB`。

## 4. 如何运行

前置：本机可用 Docker。

```bash
cp .env.example .env        # .env 已被 gitignore，勿提交真实密码
make up                     # 启动 MySQL（首次自动执行 01_schema.sql + 02_seed.sql）
make health                 # 探活
make sql                    # 进入交互终端（spacefin 库）
# 验证：
#   SELECT risk_class, COUNT(*) FROM loan GROUP BY risk_class;
make down                   # 停止（保留数据）
make destroy                # 停止并清空数据卷（下次 up 重新初始化）
```

> **初始化时机**：`sql/init/` 下的脚本仅在**数据卷为空**时由 MySQL 自动执行一次。修改 schema / seed 后需 `make destroy` 再 `make up` 方可重建。

## 5. Seed 数据

由 `seed/generate_seed.py` 生成 `sql/init/02_seed.sql`（确定性、可复现、全合成）。详见 [`seed/README.md`](../../seed/README.md)。重新生成：`make seed-gen`。

## 6. 合规

全部为合成样本，不含真实个人金融信息；不接入任何外部真实数据源。真实数据接入的合规评估在后续阶段（Q1/Q2 数据授权）推进，见 `docs/product/03/README.md` 开放问题。

## 7. 后续演进（下一个组件何时引入）

按 L0 能力规划，下一步的业务需要是"**把源数据沉淀为分层资产、统一口径**"——届时才引入 **Doris（热存 / OLAP）+ MinIO（冷湖 / Parquet）+ Multi-Catalog 联邦查询**，构建 ODS→DWD→DWS→ADS 分层。在该需要出现之前，不提前搭设。
