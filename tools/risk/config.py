"""风险引擎配置：DB 连接（复用仓库 .env）+ 业务日期口径 + 可配置阈值。"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 业务时区：所有 stat_date / alert_date 的口径基准。
# 必须显式指定而非用 time.strftime（本地时区）——本机宿主是 UTC、MySQL 容器是 +08:00、
# Airflow DAG 的 {{ ds }} 又按 Asia/Shanghai 生成，三者不一致。若用本地时区，每天
# 16:00-24:00 UTC 这 8 小时内跑的批次会把「业务上的第二天」标成第一天，同一天的数据
# 被拆进两个 stat_date，五级分类占比的分母随之错乱。
BUSINESS_TZ = ZoneInfo(os.getenv("SPACEFIN_BUSINESS_TZ", "Asia/Shanghai"))


def business_date() -> str:
    """当前业务日期 YYYY-MM-DD（Asia/Shanghai）。CLI 的 --date 默认值统一走这里。"""
    return datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")


def load_env() -> dict:
    """读仓库根 .env（仅取 MYSQL_* 相关键，不写回）。

    允许进程环境 os.environ 中的 MYSQL_* 覆盖 .env 的值——容器化 Airflow 通过
    compose env_file 注入 MYSQL_HOST=host.docker.internal，使 ETL 脚本在桥接网络容器内
    也能连到宿主 MySQL；宿主/采集栈（host 网络）进程无此变量，则回退 .env 的 127.0.0.1。
    """
    env = {}
    path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith("MYSQL_"):
            env[k] = v
    return env


# 业务库（spacefin）与房产库（spacefin_crawler）连接参数
BUSINESS_DB = "spacefin"
CRAWL_DB = "spacefin_crawler"

# 广东 21 城：中文城市名/常用简称 → DWD 城市码（crawl_housing_sale.district 存的就是城市码）
# 放在 config 而非 main：CDC 增量消费与全量重算都要用同一份映射，放入口脚本会导致两处漂移。
CITY_MAP = {
    "广州": "gz",
    "深圳": "sz",
    "佛山": "fs",
    "东莞": "dg",
    "珠海": "zh",
    "中山": "zs",
    "惠州": "hui",
    "江门": "jm",
    "肇庆": "zq",
    "清远": "qy",
    "韶关": "sg",
    "汕头": "st",
    "汕尾": "sw",
    "揭阳": "jy",
    "潮州": "cz",
    "梅州": "mz",
    "河源": "hy",
    "阳江": "yj",
    "茂名": "mm",
    "湛江": "zj",
    "云浮": "yf",
}

# 区级限定词白名单：完整「××区」名（东莞/中山不设区，取其下辖街道）→ 城市码。
# 用途唯一：P6 情景解析把「越秀区房价下跌 10%」这类区级情景归到具体城市，
# 供 record 级地域闸门按抵押物地址里的区名筛选受冲击的贷款（p6_policy）。
# 必须是白名单而非「××区」正则：泛词「核心区 / 城区 / 景区」会误伤
# （「广州核心区房价下跌」是城市级情景，不是区级）；汕尾「城区」同名泛词不收。
# 行政区清单与 seed 数据的地址口径一致（tools/seed 的 CITIES + 真实行政区划）。
DISTRICT_MAP = {
    # 广州
    "越秀区": "gz",
    "荔湾区": "gz",
    "海珠区": "gz",
    "天河区": "gz",
    "白云区": "gz",
    "黄埔区": "gz",
    "番禺区": "gz",
    "花都区": "gz",
    "南沙区": "gz",
    "增城区": "gz",
    "从化区": "gz",
    # 深圳
    "罗湖区": "sz",
    "福田区": "sz",
    "南山区": "sz",
    "宝安区": "sz",
    "龙岗区": "sz",
    "盐田区": "sz",
    "龙华区": "sz",
    "坪山区": "sz",
    "光明区": "sz",
    # 佛山
    "禅城区": "fs",
    "南海区": "fs",
    "顺德区": "fs",
    "三水区": "fs",
    "高明区": "fs",
    # 东莞 / 中山（不设区，街道为其下辖区划）
    "莞城街道": "dg",
    "南城街道": "dg",
    "东城街道": "dg",
    "万江街道": "dg",
    "石岐街道": "zs",
    "东区街道": "zs",
    "西区街道": "zs",
    # 珠海
    "香洲区": "zh",
    "斗门区": "zh",
    "金湾区": "zh",
    # 惠州
    "惠城区": "hui",
    "惠阳区": "hui",
    # 江门
    "蓬江区": "jm",
    "江海区": "jm",
    "新会区": "jm",
    # 肇庆
    "端州区": "zq",
    "鼎湖区": "zq",
    "高要区": "zq",
    # 清远
    "清城区": "qy",
    "清新区": "qy",
    # 韶关
    "武江区": "sg",
    "浈江区": "sg",
    "曲江区": "sg",
    # 汕头
    "龙湖区": "st",
    "金平区": "st",
    "濠江区": "st",
    "潮阳区": "st",
    "潮南区": "st",
    "澄海区": "st",
    # 揭阳
    "榕城区": "jy",
    "揭东区": "jy",
    # 潮州
    "湘桥区": "cz",
    "潮安区": "cz",
    # 梅州
    "梅江区": "mz",
    "梅县区": "mz",
    # 河源
    "源城区": "hy",
    "东源县": "hy",
    # 阳江
    "江城区": "yj",
    "阳东区": "yj",
    # 茂名
    "茂南区": "mm",
    "电白区": "mm",
    # 湛江
    "赤坎区": "zj",
    "霞山区": "zj",
    "坡头区": "zj",
    "麻章区": "zj",
    # 云浮
    "云城区": "yf",
    "云安区": "yf",
}

# 借款人行业枚举（customer.industry 的取值域）。国标 GB/T 4754 门类风格：
# 剔除采矿业 B / 电力热力燃气及水 D / 水利环境公共设施 N / 国际组织 T 四个
# 在个人按揭客群中占比极低的门类，其余 15 个门类全收。
# 为什么 code 用 ASCII slug 而非中文：该值会原样落进策略表
# ads_spatial_policy.scope_value、审计 JSON 与预览导出文件名
# （policy_preview_<scope_value>_*.csv），ASCII 在三个场景都无编码风险；
# 中文展示名经 INDUSTRY_CATEGORIES[code] 解析（与 CITY_MAP 的 code→名同构）。
# 这是行业口径的**单点来源**：种子回填（seed/generate_seed.py）、P6 作用域
# 校验（p6_policy._valid_scope）与前端下拉都从这里取，不允许第二份清单。
INDUSTRY_CATEGORIES = {
    "manufacturing": "制造业",
    "construction": "建筑业",
    "wholesale_retail": "批发和零售业",
    "transport": "交通运输、仓储和邮政业",
    "accommodation_food": "住宿和餐饮业",
    "it_software": "信息传输、软件和信息技术服务业",
    "finance": "金融业",
    "real_estate": "房地产业",
    "business_services": "租赁和商务服务业",
    "sci_tech": "科学研究和技术服务业",
    "education": "教育",
    "health": "卫生和社会工作",
    "culture": "文化、体育和娱乐业",
    "agriculture": "农、林、牧、渔业",
    "public_admin": "公共管理、社会保障和社会组织",
}

# 行业限定词白名单：口语说法/门类全名 → INDUSTRY_CATEGORIES 的 slug。
# 用途：P6 情景解析把「制造业收入下降 15%」这类行业情景归到具体行业，
# 供收入类情景逐笔路由（借款人行业匹配的 record 才参与压力）与叙事对象收窄。
# 白名单而非正则：泛词误伤面大（「产业」「行业」到处都是）。完整门类名在前、
# 口语别名在后，首个命中生效（「金融业」必须先于「金融」被匹配）。
INDUSTRY_MAP = {
    "农、林、牧、渔业": "agriculture",
    "农林牧渔": "agriculture",
    "农业": "agriculture",
    "制造业": "manufacturing",
    "建筑业": "construction",
    "批发和零售业": "wholesale_retail",
    "批发": "wholesale_retail",
    "零售": "wholesale_retail",
    "商贸": "wholesale_retail",
    "交通运输、仓储和邮政业": "transport",
    "交通": "transport",
    "物流": "transport",
    "运输": "transport",
    "仓储": "transport",
    "住宿和餐饮业": "accommodation_food",
    "餐饮": "accommodation_food",
    "住宿": "accommodation_food",
    "酒店": "accommodation_food",
    "信息传输、软件和信息技术服务业": "it_software",
    "信息技术": "it_software",
    "互联网": "it_software",
    "软件": "it_software",
    "金融业": "finance",
    "金融": "finance",
    "房地产业": "real_estate",
    "地产": "real_estate",
    "租赁和商务服务业": "business_services",
    "商务服务": "business_services",
    "租赁": "business_services",
    "科学研究和技术服务业": "sci_tech",
    "科研": "sci_tech",
    "教育": "education",
    "卫生和社会工作": "health",
    "医疗": "health",
    "卫生": "health",
    "文化、体育和娱乐业": "culture",
    "文化娱乐": "culture",
    "娱乐": "culture",
    "体育": "culture",
    "公共管理、社会保障和社会组织": "public_admin",
    "公务员": "public_admin",
    "公共管理": "public_admin",
}


def business_params(env: dict) -> dict:
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": "root",
        "password": env.get("MYSQL_ROOT_PASSWORD", ""),
        "database": BUSINESS_DB,
    }


def crawl_params(env: dict) -> dict:
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": env.get("MYSQL_APP_USER", "spacefin_crawler_app"),
        "password": env.get("MYSQL_APP_PASSWORD", ""),
        "database": CRAWL_DB,
    }


def root_crawl_params(env: dict) -> dict:
    """root + 房产库：仅用于 ADS 建表/写库（DDL 需 root；日常建议给 app 用户补 ADS 表权限）。"""
    return {**business_params(env), "database": CRAWL_DB}


# ---------------- 风险阈值（可被环境变量覆盖，便于验收/压力测试调参）----------------
# AC-03 两档 LTV 预警：警示级 0.75（warn）/ 强预警级 0.85（strong），均为「严格大于」触发。
LTV_WARN_LINE = float(os.getenv("RISK_LTV_WARN_LINE", "0.75"))  # 警示线 0.75：LTV>警示线 → warn
LTV_RED_LINE = float(
    os.getenv("RISK_LTV_RED_LINE", "0.85")
)  # 强预警线 0.85：LTV>强预警线 → strong（覆盖 warn）
LOW_CONF_MISSING_PCT = float(
    os.getenv("RISK_LOW_CONF_MISSING", "75.0")
)  # AC-04：空间特征缺失率阈值（0–100 百分数标度，见 store.load_collaterals 的标度统一）
# 语义：缺失率 **严格大于** 阈值 → low_confidence → 抑制自动预警、转人工核查（恰好=75 不标记）。
# 取值 75 与 tools/spatial/main.py 的 missing_ge75「严重缺失」口径一致，全项目只用一个
# 「空间特征严重缺失」标准。旧默认 25 在合成种子上会把半数抵押物打成低置信、
# 全量屏蔽 AC-03 预警——25 对应的是「任一特征缺失」，75 才是「空间特征几乎不可用」。
# DWD 行情键的最小样本量：少于该行数的 (city, community) 不足以支撑「区域中位单价」。
# 取 20 与 tools/spatial 的 SPATIAL_MIN_ZONE_SAMPLES 一致（同一个「样本量够不够撑起一个
# 价格画像」的判断，全项目只用一个标准）。库里 78% 的键只有 1 行，不设门槛会让单条挂牌
# 冒充区域行情——实测 (dg, 东城) 就是 1 行 57,066 元/㎡ 的同名小区，估值高出基准 7.4 倍。
DWD_MIN_SAMPLES = int(os.getenv("RISK_DWD_MIN_SAMPLES", "20"))
# R-UNW-03：AVM 估值与参考基准（业务库 true_market_price）偏差超过该阈值即标「异常估值」。
# 阈值语义是「严格大于」——偏差恰好 = 阈值时不标记（B-04 边界用例按 PRD 约定，>30% 才标）。
VALUATION_DEVIATION_THRESHOLD = float(os.getenv("RISK_VALUATION_DEV_PCT", "0.30"))

# 五级分类 LTV 上界（> 上界进入下一级；超过「可疑」上界为「损失」）
CLASS_LTV_UPPER = {
    "正常": float(os.getenv("RISK_LTV_NORMAL", "0.60")),
    "关注": float(os.getenv("RISK_LTV_ATTN", "0.75")),
    "次级": float(os.getenv("RISK_LTV_SUBSTD", "0.85")),
    "可疑": float(os.getenv("RISK_LTV_DOUBT", "1.00")),
}
CLASS_ORDER = ["正常", "关注", "次级", "可疑", "损失"]


def classify(ltv: float) -> str:
    """按 LTV 分五级（>可疑上界归损失）。ltv 为 None/非法 → 次级（保守）。"""
    if ltv is None or ltv < 0:
        return "次级"
    for cls in CLASS_ORDER[:-1]:
        if ltv <= CLASS_LTV_UPPER[cls]:
            return cls
    return "损失"
