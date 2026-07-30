#!/usr/bin/env python3
"""
SpaceFin Agent · 合成信贷 seed 数据生成器（Sprint 0）
=====================================================

目的：
  为本地 MySQL 业务源库生成**合成**信贷样本（客户 / 抵押物 / 贷款三表），
  输出一份可直接被 MySQL 初始化执行的 SQL（sql/init/02_seed.sql）。

设计要点：
  - 仅依赖 Python 标准库，零第三方依赖。
  - **完全确定性**：每表使用固定随机种子，放款日期相对固定基准日生成，
    因此重复运行产出的 SQL 逐字节一致（可安全提交、可复现）。
  - 字段分布与 docs/poc 的原型保持一致，便于后续 AVM / LTV 链路衔接。

合规：
  - 全部为合成数据，不含任何真实个人金融信息。

用法：
  python3 seed/generate_seed.py          # 默认每表 200 条
  python3 seed/generate_seed.py 500      # 指定每表条数
  或：make seed-gen
"""

import math
import os
import random
import sys
from datetime import date, timedelta

# 固定基准日：保证生成的放款日期不随"今天"漂移，输出可复现。
REFERENCE_DATE = date(2024, 1, 1)
RISK_CLASSES = ("正常", "关注", "次级", "可疑", "损失")


def _sql_str(value):
    """把 Python 字符串安全地转为 SQL 字符串字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


def gen_customers(n, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append(
            {
                "customer_id": 10000 + i,
                "credit_score": int(round(rng.gauss(680, 60))),
                "income_monthly": round(rng.uniform(4000, 25000), 2),
                "debt_ratio": round(rng.uniform(0.1, 0.8), 2),
            }
        )
    return rows


def gen_collaterals(n, seed=2):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        lat = 31.0 + rng.uniform(0, 0.5)
        lng = 121.0 + rng.uniform(0, 0.5)
        area = rng.uniform(40, 140)
        age = rng.uniform(0, 30)

        # 空间变化系数（与 docs/poc 一致，模拟"地理学第一定律"的空间非平稳性）
        beta_area = 60 + 50 * math.sin(lat * 6.0) * math.cos(lng * 6.0)
        beta0 = 20 + 20 * math.cos(lat * 5.0)
        beta_age = -4.0
        true_market = beta0 + beta_area * area + beta_age * age + rng.gauss(0, 4)

        rows.append(
            {
                "collateral_id": 20000 + i,
                "property_addr": f"合成地址-{20000 + i}",
                "lat": lat,
                "lng": lng,
                "area": area,
                "age": age,
                "true_market_price": round(true_market, 2),
                "poi_density": rng.uniform(0, 1),
                "commute_min": rng.uniform(10, 90),
                "is_high_risk_zone": 1 if rng.random() < 0.15 else 0,
                "spatial_feat_missing_pct": round(rng.uniform(0, 0.3), 2),
            }
        )
    return rows


def gen_loans(customers, collaterals, seed=3):
    rng = random.Random(seed)
    n = max(len(customers), len(collaterals))
    coll_map = {c["collateral_id"]: c for c in collaterals}
    rows = []
    for i in range(n):
        customer_id = customers[i % len(customers)]["customer_id"]
        collateral_id = collaterals[i % len(collaterals)]["collateral_id"]
        true_market = coll_map[collateral_id]["true_market_price"]

        loan_amount = round(true_market * rng.uniform(0.4, 0.9), 2)
        balance = round(loan_amount * rng.uniform(0.3, 1.0), 2)

        # 五级分类分布：正常 80% / 关注 12% / 次级 5% / 可疑 2% / 损失 1%
        r = rng.random()
        if r < 0.80:
            risk_class = "正常"
        elif r < 0.92:
            risk_class = "关注"
        elif r < 0.97:
            risk_class = "次级"
        elif r < 0.99:
            risk_class = "可疑"
        else:
            risk_class = "损失"

        origination = REFERENCE_DATE - timedelta(days=rng.randint(0, 1095))  # 基准日前约 3 年内

        rows.append(
            {
                "loan_id": 30000 + i,
                "customer_id": customer_id,
                "collateral_id": collateral_id,
                "loan_amount": loan_amount,
                "balance": balance,
                "interest_rate": round(rng.uniform(3.5, 8.0), 2),
                "risk_class": risk_class,
                "origination_date": origination.isoformat(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# SQL 序列化（分批 INSERT，兼顾可读性与体积）
# ---------------------------------------------------------------------------
HEADER = """-- ============================================================================
-- SpaceFin Agent · 合成 seed 数据（自动生成，请勿手工编辑）
-- ----------------------------------------------------------------------------
-- 生成命令: python3 seed/generate_seed.py  (或 make seed-gen)
-- 数据性质: 全合成样本，不含任何真实个人金融信息。
-- 用途    : MySQL 首次初始化时由 /docker-entrypoint-initdb.d 自动导入。
-- 规模    : 每表 {n} 条。
-- ============================================================================

USE spacefin;
""".strip()


def _batched_insert(table, columns, value_rows, batch=50):
    """把 value_rows（已格式化的 SQL 值元组字符串列表）写成分批 INSERT 语句。"""
    lines = []
    cols = ", ".join(columns)
    for start in range(0, len(value_rows), batch):
        chunk = value_rows[start : start + batch]
        lines.append(f"INSERT INTO {table} ({cols}) VALUES")
        lines.append(",\n".join(chunk) + ";")
        lines.append("")
    return "\n".join(lines)


def to_sql(customers, collaterals, loans):
    parts = [HEADER.format(n=len(loans)), ""]

    cust_vals = [
        "({customer_id}, {credit_score}, {income_monthly:.2f}, {debt_ratio:.2f})".format(**c)
        for c in customers
    ]
    parts.append(
        _batched_insert(
            "customer",
            ["customer_id", "credit_score", "income_monthly", "debt_ratio"],
            cust_vals,
        )
    )

    coll_vals = [
        (
            "({collateral_id}, {addr}, {lat:.6f}, {lng:.6f}, {area:.6f}, {age:.6f}, "
            "{true_market_price:.2f}, {poi_density:.6f}, {commute_min:.6f}, "
            "{is_high_risk_zone}, {spatial_feat_missing_pct:.2f})"
        ).format(addr=_sql_str(c["property_addr"]), **c)
        for c in collaterals
    ]
    parts.append(
        _batched_insert(
            "collateral",
            [
                "collateral_id",
                "property_addr",
                "lat",
                "lng",
                "area",
                "age",
                "true_market_price",
                "poi_density",
                "commute_min",
                "is_high_risk_zone",
                "spatial_feat_missing_pct",
            ],
            coll_vals,
        )
    )

    loan_vals = []
    for ln in loans:
        row = dict(ln)
        row["risk_class"] = _sql_str(ln["risk_class"])
        row["origination_date"] = _sql_str(ln["origination_date"])
        loan_vals.append(
            (
                "({loan_id}, {customer_id}, {collateral_id}, {loan_amount:.2f}, {balance:.2f}, "
                "{interest_rate:.2f}, {risk_class}, {origination_date})"
            ).format(**row)
        )
    parts.append(
        _batched_insert(
            "loan",
            [
                "loan_id",
                "customer_id",
                "collateral_id",
                "loan_amount",
                "balance",
                "interest_rate",
                "risk_class",
                "origination_date",
            ],
            loan_vals,
        )
    )

    return "\n".join(parts).rstrip() + "\n"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200

    customers = gen_customers(n, seed=1)
    collaterals = gen_collaterals(n, seed=2)
    loans = gen_loans(customers, collaterals, seed=3)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "..", "sql", "init", "02_seed.sql")
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(to_sql(customers, collaterals, loans))

    # 摘要与健全性检查
    dist = {rc: sum(1 for ln in loans if ln["risk_class"] == rc) for rc in RISK_CLASSES}
    avg_price = sum(c["true_market_price"] for c in collaterals) / len(collaterals)
    high_risk = sum(c["is_high_risk_zone"] for c in collaterals)

    print("=" * 60)
    print("SpaceFin Agent · 合成 seed 生成器")
    print("=" * 60)
    print(f"  customer   : {len(customers)} 条")
    print(f"  collateral : {len(collaterals)} 条  (高危区 {high_risk} 处)")
    print(f"  loan       : {len(loans)} 条")
    print(f"  抵押物均价 : {avg_price:,.0f}")
    print("  五级分类   : " + " / ".join(f"{rc} {dist[rc]}" for rc in RISK_CLASSES))
    print(f"  输出文件   : {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
