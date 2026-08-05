#!/usr/bin/env python3
"""
SpaceFin Agent · 合成信贷 seed 数据生成器（Sprint 0，广东对齐版 · 对准 DWD 实有键）
=================================================================================

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

**本版（对准 DWD 实有键）**：
  DWD 估值按 (city_code, 区名) 查 spacefin_crawler.crawl_housing_sale 的
  (district, community)，其中 community 绝大多数是小区名、只有少数是区级聚合。
  为了让合成地址真正命中 DWD，本版为 17 城各配 2-4 个 **DWD 实有键**：优先
  区级聚合键（惠城 / 禅城 / 榕城 / 源城 / 清城 / 江城 / 汕尾城 / 台城 / 坡头城 /
  油城 / 肇城 / 龙岗中心城…），辅以高样本真实小区键（恒大城 / 碧桂园太阳城 /
  星湖商业城 / 富力城 / 雅居乐花园 / 保利紫山花园…）。地址写作
  f"{城市名}市{key}区{key}{门牌}号"（如「惠州市惠城区惠城123号」），保证首个
  政区 token 剥掉后缀后恰好等于 DWD 键 → 100% 命中对应行情。
  键长约束：valuation 的政区 token 正则只允许 ≤6 字，故所有键均 ≤6 字（含
  碧桂园华附凤凰城 / 卧龙五洲世纪城 / 碧桂园城邦花园等 7+ 字键全部弃用）。

  排除噪音描述词键：`community` 里有一批不是真实地名的噪音键
  （次新小区 / 热门小区 / 对花园 / 南向对花园 / 望花园 / 新城 / 东城 / 西城 /
  金城 / 阳光城 / 同城 / 良村 / 附城…），写进地址会很怪，全部排除。

  排除城市：**zs/yf/zh/dg 四城不配 DWD 键**——AVM 数据清洗已证实这四城 DWD
  community 是 100% 外市错标，用其键等于把错误行情喂进估值链。这四城保留现有
  真实行政区地址，估值自然回退到 AVM / true_market_price。

设计要点：
  - 仅依赖 Python 标准库，零第三方依赖。
  - **完全确定性**：每表使用固定随机种子，放款日期相对固定基准日生成，
    因此重复运行产出的 SQL 逐字节一致（可安全提交、可复现）。
  - 城市分布：21 城按固定轮序铺满（n=200 时每城约 9-10 套），贷款/客户分布
    沿抵押物索引对齐，保证三表外键关系完整。
  - true_market_price 以该抵押物选中的 DWD 键单价中位数（元/㎡）为基准加噪声合成；
    无键城市（zs/yf/zh/dg）以 AVM 隐含单价（CITY_UNIT_PRICE）为基准——让业务
    库兜底价与 AVM/DWD 估值的量级一致，避免「全量回退」时 LTV 整体失真。
  - true_market_price 的合成噪声带为 ±10%（单价乘子 U(0.9, 1.1)，2026-08-05
    起由 ±35% U(0.75,1.35) 收窄）——原 ±35% 使 R-UNW-03 异常估值（AVM vs 参考价
    偏差>30%）高达 45.5%，demo 观感差；收窄后异常率回落至正常水平。
  - 字段分布与 docs/poc 的原型保持一致，便于 AVM / LTV 链路衔接。

合规：
  - 全部为合成数据，地址以 DWD 实有键为骨架拼装，不含任何真实个人金融信息。

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
# 仅用于**无 DWD 键城市**（zs/yf/zh/dg，见模块文档「排除城市」）。
# 取值来源：AVM 模型产物（output/avm/model.joblib，2026-08-04 训练）在每市
# 主城区中心坐标、100㎡、10 年房龄、未知小区下的隐含单价——即"兜底价"与风险引擎
# 实际采用的主估值（AVM）同量级。若直接用 DWD 原始中位单价（如 sz 42407、zs 26924、
# dg 20910），与 AVM 估值相差数倍，balance/AVM 算出的 LTV 会被整体推高，
# 五级分类大面积偏移到损失级，验收口径失真。
# 有键城市的 true_market_price 直接用其选中键的 DWD 中位单价（见 DWD_KEYS）。
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

# ---------------------------------------------------------------------------
# 每城 DWD 实有键映射：{城市码: [(DWD community 键, 该键样本单价中位数元/㎡), ...]}
# 数据来源：按 (district, community) 分组取 unit_price_yuan 中位数（偶数样本取中间
#   两值平均），2026-08-05 从 spacefin_mysql 实查（MySQL 无 MEDIAN()，用窗口函数
#   ROW_NUMBER + COUNT OVER PARTITION 实现）：
#     SELECT district, community, ROUND(AVG(mid)) FROM (
#       SELECT district, community, unit_price_yuan mid,
#              ROW_NUMBER() OVER(PARTITION BY district,community ORDER BY unit_price_yuan) rn,
#              COUNT(*) OVER(PARTITION BY district,community) cnt
#       FROM spacefin_crawler.crawl_housing_sale
#       WHERE community IS NOT NULL AND unit_price_yuan>0
#     ) t WHERE rn IN (FLOOR((cnt+1)/2), FLOOR((cnt+2)/2))
#     GROUP BY district, community;
# 键的挑选口径：
#   - 每城 2-4 个**真实、可读**的键：优先区级聚合（惠城/禅城/榕城/源城/清城/江城/
#     汕尾城/台城/坡头城/油城/肇城/龙岗中心城…），辅以高样本真实小区。
#   - 排除噪音描述词键（次新小区/热门小区/对花园/望花园/新城/东城/西城/金城/阳光城/
#     同城/良村/附城…）——不是真实地名，写进地址会很怪。
#   - 排除 >6 字键：valuation._ADMIN_TOKEN_RE 只吃 ≤6 字政区 token（如
#     碧桂园华附凤凰城 8 字 / 卧龙五洲世纪城 7 字 / 碧桂园城邦花园 7 字都进不去）。
#   - zs/yf/zh/dg 四城不配键（DWD 100% 外市错标，见模块文档「排除城市」）。
# 中位数列只用于 true_market_price 基准，与估值链实际采用的 DWD 中位数同口径
# （也贴合 AVM 目标编码与 0.45 分位数损失贴近 median 而非 avg 的训练口径）。
# ---------------------------------------------------------------------------
DWD_KEYS = {
    "gz": [("增城", 10052), ("万科城", 40225)],
    "sz": [("龙岗中心城", 26477)],
    "fs": [("禅城", 13453), ("陈村", 12537), ("保利紫山花园", 12442)],
    "hui": [
        ("惠城", 10491),
        ("中洲天御花园", 10514),
        ("中信凯旋城", 9432),
        ("中海水岸城", 10507),
    ],
    "hy": [("源城", 5837), ("雅居乐花园", 5583), ("紫金城", 5313)],
    "jm": [("台城", 5373), ("明泰城", 7064), ("奕聪花园", 7060)],
    "zq": [("肇城", 8449), ("华英城", 8099), ("敏捷城", 8151), ("顺宝天誉花园", 9127)],
    "qy": [("清城", 4388), ("凤城", 5000), ("碧桂园山湖城", 6943), ("清新城", 3297)],
    "sg": [("恒大城", 4905), ("碧桂园太阳城", 6439), ("摩尔城", 7174)],
    "st": [("星湖商业城", 10774), ("濠江城", 7496), ("珠港新城", 10031)],
    "sw": [
        ("汕尾城", 7907),
        ("海城", 6112),
        ("振业时代花园", 8246),
        ("碧桂园时代城", 7505),
    ],
    "jy": [("榕城", 6999), ("普宁城", 7683), ("御景城", 10496), ("博雅苑", 8100)],
    "cz": [("恒大城", 6944), ("碧桂园华侨城", 7500), ("万达城", 5409)],
    "mz": [("富力城", 6240), ("兴宁商业城", 6266), ("芹洋花园", 5680)],
    "yj": [("江城", 5757), ("春城", 5068), ("京源城", 6185), ("中集国际城", 5018)],
    "mm": [("油城", 6964), ("城光世纪城", 6885), ("恒福尚城", 8382), ("荔晶新城", 7938)],
    "zj": [("坡头城", 5538), ("廉江城", 5870), ("麻章城", 5242), ("西粤京基城", 8935)],
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
        # 城市按固定轮序铺满 21 城；键/行政区/小区名由确定性 rng 抽取。
        city_name, city_code, districts, (lat_lo, lat_hi, lng_lo, lng_hi) = CITIES[i % n_cities]
        building = rng.randint(1, 200)
        lat = rng.uniform(lat_lo, lat_hi)
        lng = rng.uniform(lng_lo, lng_hi)
        area = rng.uniform(40, 140)
        age = rng.uniform(0, 30)

        if city_code in DWD_KEYS:
            # 有键城市：地址以「key+区」开头，剥后缀后恰好等于 DWD 键 → 必然命中。
            # true_market_price 以该键 DWD 中位单价为基准，与估值链同口径。
            key, median_up = rng.choice(DWD_KEYS[city_code])
            property_addr = f"{city_name}市{key}区{key}{building}号"
            unit = median_up * rng.uniform(0.9, 1.1)
        else:
            # 无键城市（zs/yf/zh/dg，DWD 100% 外市错标）：保留真实行政区地址，
            # 估值自然回退到 AVM / true_market_price。
            district = rng.choice(districts)
            community = rng.choice(COMMUNITY_NAMES)
            property_addr = f"{city_name}市{district}{community}{building}号"
            unit = CITY_UNIT_PRICE[city_code] * rng.uniform(0.9, 1.1)

        true_market = round(unit * area, 2)

        rows.append(
            {
                "collateral_id": 20000 + i,
                "property_addr": property_addr,
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


def _check_dwd_hit(collaterals):
    """自检：用与 tools/risk/valuation.py 相同的三段式解析，断言所有有键城市
    的地址都能解析回本城 DWD 键。防键长超限 / 配方写错导致的静默漏配。"""
    import re as _re

    city_re = _re.compile(r"^[\u4e00-\u9fa5]{2,4}市")
    token_re = _re.compile(r"^([\u4e00-\u9fa5]{1,6}?(?:区|县|市|街道|镇))")
    suffix_re = _re.compile(r"(?:区|县|市|街道|镇)$")
    keyed_by_code = {c: [k for k, _ in ks] for c, ks in DWD_KEYS.items()}

    n_hit = 0
    for i, c in enumerate(collaterals):
        code = CITIES[i % len(CITIES)][1]
        if code not in keyed_by_code:
            continue
        body = city_re.sub("", c["property_addr"], count=1)
        m = token_re.match(body)
        token = m.group(1) if m else ""
        stripped = suffix_re.sub("", token)
        district = stripped if len(stripped) >= 2 else token
        assert district in keyed_by_code[code], (
            f"{code} 地址 {c['property_addr']!r} 解析出 {district!r}，"
            f"不在 DWD 键 {keyed_by_code[code]} 内"
        )
        n_hit += 1
    return n_hit


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
-- 地址口径: collateral.property_addr 为广东 21 城合成地址，有键城市以「key+区」开头
--           保证命中 DWD 实有键（zs/yf/zh/dg 四城无键，估值自然回退；见 seed/generate_seed.py）。
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
    dwd_hits = _check_dwd_hit(collaterals)
    city_counts = {}
    for i, _c in enumerate(collaterals):
        code = CITIES[i % len(CITIES)][1]
        city_counts[code] = city_counts.get(code, 0) + 1

    print("=" * 60)
    print("SpaceFin Agent · 合成 seed 生成器（广东 21 城版 · DWD 实有键对齐）")
    print("=" * 60)
    print(f"  customer   : {len(customers)} 条")
    print(f"  collateral : {len(collaterals)} 条  (高危区 {high_risk} 处)")
    print(f"  loan       : {len(loans)} 条")
    print(f"  抵押物均价 : {avg_price:,.0f}")
    print(
        f"  DWD 键命中 : {dwd_hits}/{len(collaterals)} "
        f"({dwd_hits * 100 // max(len(collaterals), 1)}% of 有键城市样本)"
    )
    print(f"  城市覆盖   : {len(city_counts)} 城")
    print("  五级分类   : " + " / ".join(f"{rc} {dist[rc]}" for rc in RISK_CLASSES))
    print(f"  输出文件   : {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
