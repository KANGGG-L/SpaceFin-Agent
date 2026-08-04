#!/usr/bin/env python3
"""
SpaceFin Agent · 合成信贷 seed 数据生成器（Sprint 0，广东对齐版）
=============================================================

目的：
  为本地 MySQL 业务源库生成**合成**信贷样本（客户 / 抵押物 / 贷款三表），
  输出一份可直接被 MySQL 初始化执行的 SQL（sql/init/02_seed.sql）。

为什么从「上海合成地址」改成「广东 21 城地址」：
  风险引擎的三级回退（AVM → DWD → true_market_price）要求抵押物地址能解析出
  广东城市码：AVM/DWD 都是按广东行情训练的，地址里没有城市名就永远命中不了，
  valuation 全部回退到业务库兜底价（dwd_hits=0、avm_hits=0，见 docs/tech/
  components/cdc-downstream.md 已知局限）。本版把 collateral 换成广东 21 城
  真实风格地址（城市 + 行政区 + 小区名）与对应城市坐标框内的经纬度，让估值链
  第一次真正走到 AVM/DWD。

设计要点：
  - 仅依赖 Python 标准库，零第三方依赖。
  - **完全确定性**：每表使用固定随机种子，放款日期相对固定基准日生成，
    因此重复运行产出的 SQL 逐字节一致（可安全提交、可复现）。
  - 城市分布：21 城按固定轮序铺满（n=200 时每城约 9-10 套），贷款/客户分布
    沿抵押物索引对齐，保证三表外键关系完整。
  - true_market_price 以 DWD 各城市中位单价（元/㎡）为基准加噪声合成：
    让业务库兜底价与 AVM/DWD 估值的量级一致，避免「全量回退」时 LTV 整体
    失真（若兜底价与行情价差 3 倍，五级分布会被整体推偏）。
  - 字段分布与 docs/poc 的原型保持一致，便于 AVM / LTV 链路衔接。

合规：
  - 全部为合成数据，地址为真实风格的虚构小区名，不含任何真实个人金融信息
    与真实楼盘私有信息。

用法：
  python3 seed/generate_seed.py          # 默认每表 200 条
  python3 seed/generate_seed.py 500      # 指定每表条数
  或：make seed-gen
"""

import os
import random
import sys
from datetime import date, timedelta

# 固定基准日：保证生成的放款日期不随"今天"漂移，输出可复现。
REFERENCE_DATE = date(2024, 1, 1)
RISK_CLASSES = ("正常", "关注", "次级", "可疑", "损失")

# ---------------------------------------------------------------------------
# 广东 21 城地址池
# 每项：(城市名, 城市码, 行政区集合, 坐标框 (lat_lo, lat_hi, lng_lo, lng_hi))
# 城市码与 tools/risk/config.py 的 CITY_MAP 严格一致；坐标框取各市主城区
# 大致经纬度范围，保证合成坐标落在对应城市境内（AVM 邻域空间特征才有效）。
# ---------------------------------------------------------------------------
CITIES = [
    (
        "广州",
        "gz",
        ("天河区", "越秀区", "海珠区", "白云区", "番禺区", "荔湾区", "黄埔区"),
        (23.02, 23.58, 113.18, 113.68),
    ),
    (
        "深圳",
        "sz",
        ("福田区", "南山区", "罗湖区", "宝安区", "龙岗区"),
        (22.45, 22.87, 113.75, 114.60),
    ),
    ("佛山", "fs", ("南海区", "禅城区", "顺德区", "三水区"), (22.75, 23.55, 112.85, 113.30)),
    (
        "东莞",
        "dg",
        ("莞城街道", "南城街道", "东城街道", "万江街道"),
        (22.70, 23.15, 113.55, 114.20),
    ),
    ("珠海", "zh", ("香洲区", "金湾区", "斗门区"), (21.95, 22.40, 113.10, 113.60)),
    ("中山", "zs", ("石岐街道", "东区街道", "西区街道"), (22.25, 22.75, 113.20, 113.65)),
    ("惠州", "hui", ("惠城区", "惠阳区", "大亚湾区", "博罗县"), (22.75, 23.60, 113.90, 114.85)),
    ("江门", "jm", ("蓬江区", "江海区", "新会区"), (22.05, 22.75, 112.65, 113.30)),
    ("肇庆", "zq", ("端州区", "鼎湖区", "高要区"), (22.85, 23.75, 111.80, 112.85)),
    ("清远", "qy", ("清城区", "清新区"), (23.55, 24.60, 112.55, 113.60)),
    ("韶关", "sg", ("浈江区", "武江区", "曲江区"), (24.15, 25.35, 113.25, 114.60)),
    ("汕头", "st", ("金平区", "龙湖区", "濠江区", "潮阳区"), (23.10, 23.55, 116.35, 116.85)),
    ("汕尾", "sw", ("城区", "海丰县"), (22.70, 23.10, 115.20, 115.90)),
    ("揭阳", "jy", ("榕城区", "揭东区", "普宁市"), (23.20, 23.70, 115.85, 116.55)),
    ("潮州", "cz", ("湘桥区", "潮安区"), (23.55, 23.90, 116.50, 117.00)),
    ("梅州", "mz", ("梅江区", "梅县区", "兴宁市"), (24.00, 24.70, 115.70, 116.50)),
    ("河源", "hy", ("源城区", "东源县"), (23.45, 24.55, 114.35, 115.25)),
    ("阳江", "yj", ("江城区", "阳东区", "阳春市"), (21.75, 22.30, 111.60, 112.30)),
    ("茂名", "mm", ("茂南区", "电白区", "高州市"), (21.50, 22.25, 110.60, 111.55)),
    ("湛江", "zj", ("赤坎区", "霞山区", "坡头区", "麻章区"), (20.80, 21.70, 109.90, 110.75)),
    ("云浮", "yf", ("云城区", "新兴县", "罗定市"), (22.60, 23.20, 111.35, 112.25)),
]

# 真实风格的小区名池（虚构，仅作为合成地址的尾缀，不与真实楼盘对应）。
COMMUNITY_NAMES = (
    "珠江新城御景台",
    "天汇国际公馆",
    "万科城市之光",
    "保利紫云府",
    "中海锦城",
    "星河湾半岛",
    "华润悦府",
    "富力盈翠华庭",
    "时代天韵花园",
    "雅居乐剑桥郡",
    "碧桂园凤凰城",
    "金地格林花园",
    "绿城桂语兰庭",
    "龙湖天宸原著",
    "招商雍华府",
    "合景泰富天銮",
    "越秀星汇云锦",
    "恒大御景半岛",
    "融创望江府",
    "奥园冠军城",
    "敏捷锦绣世家",
    "佳兆业水岸新都",
    "旭辉江山云出",
    "金科集美天宸",
    "路劲隽悦府",
    "美的云峰花园",
    "绿地国际花都",
    "世茂璀璨天城",
    "新力琥珀园",
    "颐和公馆",
    "景峰尚寓",
    "枫丹白鹭湾",
    "翡翠绿洲",
    "水岸朗晴花园",
    "金色家园",
    "锦绣华庭",
    "书香雅苑",
    "阳光丽景",
    "翠湖春天",
    "天骄华府",
)

# 各城市合成单价基准（元/㎡），用于生成 true_market_price（兜底价）。
# 取值来源：AVM 模型产物（output/avm/model.joblib，2026-08-04 训练）在每市
# 主城区中心坐标、100㎡、10 年房龄、未知小区下的隐含单价——即"兜底价"与风险引擎
# 实际采用的主估值（AVM）同量级。若直接用 DWD 原始中位单价（如 sz 42407、zs 26924、
# dg 20910），与 AVM 估值相差数倍，balance/AVM 算出的 LTV 会被整体推高，
# 五级分类大面积偏移到损失级，验收口径失真。
CITY_UNIT_PRICE = {
    "gz": 35226,
    "sz": 18638,
    "fs": 14379,
    "dg": 7939,
    "zh": 12939,
    "zs": 12809,
    "hui": 11434,
    "jm": 9490,
    "zq": 6480,
    "qy": 5426,
    "sg": 6014,
    "st": 7404,
    "sw": 7679,
    "jy": 8312,
    "cz": 9378,
    "mz": 6418,
    "hy": 5089,
    "yj": 5268,
    "mm": 6592,
    "zj": 8149,
    "yf": 7424,
}


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
    n_cities = len(CITIES)
    rows = []
    for i in range(n):
        # 城市按固定轮序铺满 21 城；行政区和小区名由确定性 rng 抽取。
        city_name, city_code, districts, (lat_lo, lat_hi, lng_lo, lng_hi) = CITIES[i % n_cities]
        district = rng.choice(districts)
        community = rng.choice(COMMUNITY_NAMES)
        # 门牌号让地址更接近真实写法，也避免整表地址雷同。
        building = rng.randint(1, 200)

        lat = rng.uniform(lat_lo, lat_hi)
        lng = rng.uniform(lng_lo, lng_hi)
        area = rng.uniform(40, 140)
        age = rng.uniform(0, 30)

        # 以城市行情中位单价为基准加噪声合成市场价：与 AVM/DWD 估值同量级。
        unit = CITY_UNIT_PRICE[city_code] * rng.uniform(0.75, 1.35)
        true_market = round(unit * area, 2)

        rows.append(
            {
                "collateral_id": 20000 + i,
                "property_addr": f"{city_name}市{district}{community}{building}号",
                "lat": lat,
                "lng": lng,
                "area": area,
                "age": age,
                "true_market_price": true_market,
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
-- 地址口径: collateral.property_addr 为广东 21 城真实风格合成地址（城市+区+小区），
--           使 AVM/DWD 行情估值可命中（见 seed/generate_seed.py 模块文档）。
-- 注意    : 本文件由 /docker-entrypoint-initdb.d 在容器首次初始化时导入，mysql
--           客户端默认字符集可能是 latin1；文件头已放 SET NAMES utf8mb4 兜底，
--           否则中文地址会被按 latin1 存储成乱码、城市名无法被估值链解析。
-- ============================================================================

SET NAMES utf8mb4;

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
    city_counts = {}
    for i, _c in enumerate(collaterals):
        code = CITIES[i % len(CITIES)][1]
        city_counts[code] = city_counts.get(code, 0) + 1

    print("=" * 60)
    print("SpaceFin Agent · 合成 seed 生成器（广东 21 城版）")
    print("=" * 60)
    print(f"  customer   : {len(customers)} 条")
    print(f"  collateral : {len(collaterals)} 条  (高危区 {high_risk} 处)")
    print(f"  loan       : {len(loans)} 条")
    print(f"  抵押物均价 : {avg_price:,.0f}")
    print(f"  城市覆盖   : {len(city_counts)} 城")
    print("  五级分类   : " + " / ".join(f"{rc} {dist[rc]}" for rc in RISK_CLASSES))
    print(f"  输出文件   : {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
