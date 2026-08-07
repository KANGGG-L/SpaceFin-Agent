#!/usr/bin/env python3
"""
SpaceFin Agent · 合成信贷 seed 数据生成器（Sprint 0，广东对齐版 · 对准 DWD 实有键）
=================================================================================

目的：
  为本地 MySQL 业务源库生成**合成**信贷样本（客户 / 抵押物 / 贷款三表），
  输出一份可直接被 MySQL 初始化执行的 SQL（sql/init/02_seed.sql），
  或通过 --write-db 直接灌入运行中的业务库（7 天演示回填用）。

为什么从「上海合成地址」改成「广东 21 城地址」：
  风险引擎的三级回退（AVM → DWD → true_market_price）要求抵押物地址能解析出
  广东城市码：AVM/DWD 都是按广东行情训练的，地址里没有城市名就永远命中不了，
  valuation 全部回退到业务库兜底价（dwd_hits=0、avm_hits=0，见 docs/tech/
  components/cdc-downstream.md 已知局限）。本版把 collateral 换成广东 21 城
  真实风格地址（城市 + 行政区 + 小区名）与对应城市坐标框内的经纬度，让估值链
  第一次真正走到 AVM/DWD。

**本版（对准 DWD 实有键 + 演示回填，2026-08-06）**：
  在「DWD 实有键」策略基础上为 7 天演示剧本（docs/demo/script_7d.md）扩展：
  1. **城市加权**：广州占比提升到 ≈25%（1250/5000），其余 20 城按爬取分布铺满
     ——演示事件城市是广州，剧本的「广州 LTV 上穿」需要足够样本支撑；
     原「21 城轮序」在 n=200 时代每城均分，剧本演进后改为加权。
  2. **LTV 按目标分布生成**：balance = 目标 LTV × 抵押物估值（估值优先取 AVM
     实时预测，无模型环境回退 true_market_price）。这样引擎重算的 LTV 在基线
     日精确等于目标 LTV，事件日估值下探后自然上穿预警线（真实引擎传导）。
  3. **低置信率按验收口径设计**：spatial_feat_missing_pct 按约 50% 落在 >75%
     （0-100 标度）设计，对应 acceptance.md D3 的「低置信 40%-60%」。
  4. **真实小区名补充**：广州抵押物地址在小概率上采样 crawl_housing_sale 的
     真实小区名（增城 / 万科城为 DWD 实有键，其余靠 AVM 城市中位兜底）。

  其余沿用既有设计（见下）。

设计要点：
  - 仅依赖 Python 标准库，零第三方依赖；若运行环境有 sklearn/joblib（如
    tools/orchestrator/.venv），自动接入 AVM 估值以获得精确的基线 LTV。
  - **完全确定性**：每表使用固定随机种子，放款日期相对固定基准日生成，
    因此重复运行产出的数据逐字节一致（可安全提交、可复现）。
  - true_market_price 以该抵押物选中的 DWD 键单价中位数（元/㎡）为基准加噪声合成；
    无键城市（zs/yf/zh/dg）以 AVM 隐含单价（CITY_UNIT_PRICE）为基准——让业务
    库兜底价与 AVM/DWD 估值的量级一致，避免「全量回退」时 LTV 整体失真。
  - 字段分布与 docs/poc 的原型保持一致，便于 AVM / LTV 链路衔接。

合规：
  - 全部为合成数据，地址以 DWD 实有键为骨架拼装，不含任何真实个人金融信息。

用法：
  python3 seed/generate_seed.py               # 默认每表 200 条（仅 SQL）
  python3 seed/generate_seed.py 5000          # 5000 笔，仅生成 SQL
  python3 seed/generate_seed.py 5000 --write-db   # 生成并直接灌入 MySQL 业务库
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


# ---------------------------------------------------------------------------
# 城市加权分布（2026-08-06 起，演示剧本用）
# 广州提升到 25%（事件城市，剧本「广州 LTV 上穿」需要足够样本）；
# 其余 20 城共享 75%，彼此比例对齐 crawl_housing_sale 的真实爬取分布
# （sw/hui/mz/st 等非珠三角城市爬取量更大）。代码内会做归一化。
# 若想恢复 21 城等分轮序，把下面 gz 改为 100/21 ≈ 4.76 即可。
# ---------------------------------------------------------------------------
CITY_WEIGHTS = {
    "gz": 25.0,  # 剧本事件城市：占比 25%（≈1250/5000）
    "sz": 2.87,
    "fs": 4.89,
    "dg": 0.23,
    "zh": 1.32,
    "zs": 2.95,
    "hui": 5.04,
    "jm": 4.81,
    "zq": 4.81,
    "qy": 3.72,
    "sg": 4.65,
    "st": 4.96,
    "sw": 5.04,
    "jy": 4.26,
    "cz": 3.49,
    "mz": 4.96,
    "hy": 2.71,
    "yj": 3.72,
    "mm": 4.81,
    "zj": 4.73,
    "yf": 1.16,
}

# 广州真实小区名补充池（采样自 crawl_housing_sale 广州区真实 community，2026-08-06）：
# - 增城 / 万科城 是 DWD 实有键（样本 ≥ 20，命中 DWD 行情）。
# - 其余为真实小区名（样本 < 20，DWD 不命中，但地址含「广州」→ AVM 必命中，
#   重训后经广州城市/小区编码传导下探）。政区 token 均 ≤ 5 字，满足估值正则 ≤6 字。
GZ_COMMUNITIES = (
    "增城",
    "万科城",
    "骏景花园",
    "珠江新城",
    "员村",
    "五羊新城",
    "穗花新村",
    "东华西路",
    "科学城",
    "知识城",
    "广钢新城",
    "亚运城",
    "金碧新城",
    "奥园城",
    "富力城",
    "合和新城",
    "雅居乐花园",
)


def _sql_str(value):
    """把 Python 字符串安全地转为 SQL 字符串字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# 目标 LTV 分布（演示剧本基线口径，2026-08-06）
# balance = 目标 LTV × 抵押物估值（AVM 优先 / true_market_price 兜底）。
# 分布按城市分组设计：
#   - 广州：抵押敞口偏大的按揭客群，LTV 上尾显著（基线 ~15% 落在 >0.75），
#     事件日估值下探 ~10% 后 LTV 整体 ×1.11，上穿预警线的笔数随之放大。
#   - 其余城市：健康房贷画像，上尾仅 ~2%，事件日不受影响（基线稳定）。
# 返回目标 LTV；rng 由外层传入保证确定性。
# ---------------------------------------------------------------------------
def _target_ltv(city_code: str, rng) -> float:
    if city_code == "gz":
        # 广州：按揭敞口偏大客群。上尾集中分布在 0.68-0.90，事件日估值下探 ~10%
        # （LTV 整体 ×1.11）后，0.68-0.75 段的贷款批量上穿 0.75 预警线 → 预警量放大，
        # 与剧本「广州 LTV 集体上穿」一致。
        r = rng.random()
        if r < 0.50:
            return rng.uniform(0.30, 0.55)
        if r < 0.76:
            return rng.uniform(0.55, 0.68)
        if r < 0.90:
            return rng.uniform(0.68, 0.78)
        if r < 0.97:
            return rng.uniform(0.78, 0.90)
        return rng.uniform(0.90, 1.05)
    # 其余城市：健康房贷画像，上尾仅 ~1%，事件日不受影响 → 基线稳定、比值对比干净。
    r = rng.random()
    if r < 0.92:
        return rng.uniform(0.25, 0.52)
    if r < 0.98:
        return rng.uniform(0.52, 0.66)
    if r < 0.998:
        return rng.uniform(0.66, 0.80)
    return rng.uniform(0.80, 1.00)


def _weighted_cities(n: int, seed: int):
    """按 CITY_WEIGHTS 确定性地抽样 n 个城市码（rng 固定种子，可复现）。"""
    rng = random.Random(seed)
    codes = [c[1] for c in CITIES]
    total = sum(CITY_WEIGHTS[c] for c in codes)
    weights = [CITY_WEIGHTS[c] / total for c in codes]
    return rng.choices(codes, weights=weights, k=n)


def _avm_env():
    """惰性接入 tools/risk 的 AVM 估值（有 sklearn/joblib 时）；失败返回 None。

    返回 (valuation 模块, city_map, model) 或 (None, None, None)。
    仅在运行环境可用时启用——生成器本身仍是零第三方依赖。
    """
    try:
        import os as _os
        import sys as _sys

        _risk_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools", "risk"
        )
        if _risk_dir not in _sys.path:
            _sys.path.insert(0, _risk_dir)
        import config as _cfg  # noqa: F401
        import valuation as _val

        model = _val.load_avm_model()
        if model is None:
            return None, None, None
        return _val, _cfg.CITY_MAP, model
    except Exception:
        return None, None, None


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
    city_by_idx = _weighted_cities(n, seed=seed)
    rows = []
    for i in range(n):
        # 城市按 CITY_WEIGHTS 加权分配（广州 ≈25%）；键/行政区/小区名由确定性 rng 抽取。
        city_code = city_by_idx[i]
        city_name, _, districts, (lat_lo, lat_hi, lng_lo, lng_hi) = next(
            c for c in CITIES if c[1] == city_code
        )
        building = rng.randint(1, 200)
        lat = rng.uniform(lat_lo, lat_hi)
        lng = rng.uniform(lng_lo, lng_hi)
        area = rng.uniform(40, 140)
        age = rng.uniform(0, 30)

        if city_code == "gz":
            # 广州：采样真实小区名补充池（增城/万科城为 DWD 实有键 → DWD 命中；
            # 其余真实小区样本不足 → AVM 城市中位兜底，重训后随广州编码下探）。
            key = rng.choice(GZ_COMMUNITIES)
            property_addr = f"{city_name}市{key}区{key}{building}号"
            key_med = dict(DWD_KEYS["gz"]).get(key)
            unit = (key_med or CITY_UNIT_PRICE[city_code]) * rng.uniform(0.9, 1.1)
        elif city_code in DWD_KEYS:
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

        # 空间特征缺失率（0-1 标度，store 侧 ×100 对齐 0-100 阈值 75）：
        # 合成坐标随机撒点，约 50% 落入样本稀疏区 → 缺失率 >75 → AC-04 低置信。
        # 与 acceptance.md D3「低置信 40%-60%」对齐，保证预警都来自非低置信行。
        if rng.random() < 0.50:
            missing_pct = rng.uniform(0.40, 0.74)
        else:
            missing_pct = rng.uniform(0.75, 0.98)

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
                "spatial_feat_missing_pct": round(missing_pct, 2),
            }
        )
    return rows


def _check_dwd_hit(collaterals):
    """自检：用与 tools/risk/valuation.py 相同的三段式解析，统计有键城市的 DWD 命中率。

    版本演进说明：广州地址改为「真实小区名补充池」采样后，仅增城/万科城等 DWD 实有键
    保证命中，其余真实小区样本不足、DWD 不命中属预期（走 AVM 城市中位），不再对
    「全部有键城市地址都命中」做硬断言，改为报告命中率，防键长超限/配方写错导致的
    静默漏配。"""
    import re as _re

    city_re = _re.compile(r"^[\u4e00-\u9fa5]{2,4}市")
    token_re = _re.compile(r"^([\u4e00-\u9fa5]{1,6}?(?:区|县|市|街道|镇))")
    suffix_re = _re.compile(r"(?:区|县|市|街道|镇)$")
    keyed_by_code = {c: [k for k, _ in ks] for c, ks in DWD_KEYS.items()}

    n_keyed = 0
    n_hit = 0
    for c in collaterals:
        # 城市由 gen_collaterals 的加权分配决定，这里按地址反解城市码统计。
        code = _city_code_of(c["property_addr"])
        if code not in keyed_by_code:
            continue
        n_keyed += 1
        body = city_re.sub("", c["property_addr"], count=1)
        m = token_re.match(body)
        token = m.group(1) if m else ""
        stripped = suffix_re.sub("", token)
        district = stripped if len(stripped) >= 2 else token
        if district in keyed_by_code[code]:
            n_hit += 1
    return n_hit, n_keyed


def _city_code_of(addr: str) -> str:
    """从地址文本反解城市码（与 valuation._city_code_from_addr 同逻辑，本地实现防依赖）。"""
    for city_name, code, _districts, _bbox in CITIES:
        if city_name in addr:
            return code
    return ""


def gen_loans(customers, collaterals, seed=3, avm_env=None):
    """按目标 LTV 分布生成贷款：balance = 目标 LTV × 抵押物估值。

    avm_env = (valuation 模块, city_map, model) 或 None：有 AVM 时用 AVM 预测估值
    （引擎重算 LTV 精确等于目标 LTV），否则回退 true_market_price（量级一致，近似）。
    """
    rng = random.Random(seed)
    n = max(len(customers), len(collaterals))
    coll_map = {c["collateral_id"]: c for c in collaterals}
    city_by_idx = _weighted_cities(n, seed=seed)
    val_mod, city_map, model = avm_env or (None, None, None)

    rows = []
    for i in range(n):
        customer_id = customers[i % len(customers)]["customer_id"]
        collateral_id = collaterals[i % len(collaterals)]["collateral_id"]
        col = coll_map[collateral_id]
        city_code = city_by_idx[i]

        target_ltv = _target_ltv(city_code, rng)
        if val_mod is not None:
            est = val_mod.valuation_from_avm(model, col, city_map) or float(
                col["true_market_price"] or 0.0
            )
        else:
            est = float(col["true_market_price"] or 0.0)
        balance = round(target_ltv * est, 2)
        # 放款额 = 余额 / 未还比例（70%-98%），即大部分贷款仍处于本金高位。
        loan_amount = round(balance / rng.uniform(0.70, 0.98), 2)

        # 申报五级分类（仅主档展示口径，引擎按 LTV 实时重算覆盖，见 docs/demo/script_7d.md）
        if target_ltv <= 0.60:
            risk_class = "正常"
        elif target_ltv <= 0.75:
            risk_class = "关注"
        elif target_ltv <= 0.85:
            risk_class = "次级"
        elif target_ltv <= 1.00:
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
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 200
    write_db = "--write-db" in sys.argv

    customers = gen_customers(n, seed=1)
    collaterals = gen_collaterals(n, seed=2)
    avm_env = _avm_env()
    loans = gen_loans(customers, collaterals, seed=3, avm_env=avm_env)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "..", "sql", "init", "02_seed.sql")
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(to_sql(customers, collaterals, loans))

    if write_db:
        _load_to_mysql(customers, collaterals, loans)

    # 摘要与健全性检查
    dist = {rc: sum(1 for ln in loans if ln["risk_class"] == rc) for rc in RISK_CLASSES}
    avg_price = sum(c["true_market_price"] for c in collaterals) / len(collaterals)
    high_risk = sum(c["is_high_risk_zone"] for c in collaterals)
    low_conf = sum(1 for c in collaterals if c["spatial_feat_missing_pct"] > 0.75)
    dwd_hits, dwd_keyed = _check_dwd_hit(collaterals)
    city_codes = _weighted_cities(n, seed=2)
    city_counts = {}
    for code in city_codes:
        city_counts[code] = city_counts.get(code, 0) + 1

    print("=" * 60)
    print("SpaceFin Agent · 合成 seed 生成器（广东 21 城版 · DWD 实有键对齐 · 广州加权）")
    print("=" * 60)
    print(f"  customer   : {len(customers)} 条")
    print(f"  collateral : {len(collaterals)} 条  (高危区 {high_risk} 处)")
    print(f"  loan       : {len(loans)} 条")
    print(f"  抵押物均价 : {avg_price:,.0f}")
    print(
        f"  DWD 键命中 : {dwd_hits}/{dwd_keyed} "
        f"({dwd_hits * 100 // max(dwd_keyed, 1)}% of 有键样本, 其余走 AVM)"
    )
    print(
        f"  低置信(缺失>75%) : {low_conf}/{len(collaterals)} ({low_conf * 100 // max(len(collaterals), 1)}%)"
    )
    print(f"  城市覆盖   : {len(city_counts)} 城 (gz={city_counts.get('gz', 0)})")
    print("  申报五级   : " + " / ".join(f"{rc} {dist[rc]}" for rc in RISK_CLASSES))
    print(
        f"  AVM 估值   : {'启用(引擎重算 LTV=目标 LTV)' if avm_env[2] else '未启用(回退 true_market_price)'}"
    )
    print(f"  输出文件   : {out_path}")
    print(f"  --write-db : {'已灌入 MySQL spacefin 业务库' if write_db else '否'}")
    print("=" * 60)


def _load_to_mysql(customers, collaterals, loans):
    """把合成三表直接灌入 MySQL 业务库（--write-db）。

    幂等口径：先 TRUNCATE 三张业务表再批量 INSERT（配合外键顺序 customer→collateral→loan）。
    连接参数复用 tools/risk/config（读取仓库根 .env）。"""
    import pymysql  # 仅灌库模式依赖 PyMySQL

    sys.path.insert(
        0,
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "risk"),
    )
    import config

    env = config.load_env()
    conn = pymysql.connect(**config.business_params(env), charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ("loan", "collateral", "customer"):
            cur.execute(f"TRUNCATE TABLE {t}")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        cur.executemany(
            "INSERT INTO customer (customer_id, credit_score, income_monthly, debt_ratio) "
            "VALUES (%s,%s,%s,%s)",
            [
                (c["customer_id"], c["credit_score"], c["income_monthly"], c["debt_ratio"])
                for c in customers
            ],
        )
        cur.executemany(
            "INSERT INTO collateral (collateral_id, property_addr, lat, lng, area, age, "
            " true_market_price, poi_density, commute_min, is_high_risk_zone, spatial_feat_missing_pct) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    c["collateral_id"],
                    c["property_addr"],
                    c["lat"],
                    c["lng"],
                    c["area"],
                    c["age"],
                    c["true_market_price"],
                    c["poi_density"],
                    c["commute_min"],
                    c["is_high_risk_zone"],
                    c["spatial_feat_missing_pct"],
                )
                for c in collaterals
            ],
        )
        cur.executemany(
            "INSERT INTO loan (loan_id, customer_id, collateral_id, loan_amount, balance, "
            " interest_rate, risk_class, origination_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    ln["loan_id"],
                    ln["customer_id"],
                    ln["collateral_id"],
                    ln["loan_amount"],
                    ln["balance"],
                    ln["interest_rate"],
                    ln["risk_class"],
                    ln["origination_date"],
                )
                for ln in loans
            ],
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
