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
    """读仓库根 .env（仅取 MYSQL_* 相关键，不写回）。"""
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
LTV_RED_LINE = float(os.getenv("RISK_LTV_RED_LINE", "0.85"))  # AC-03：LTV>红线 → 预警
LOW_CONF_MISSING_PCT = float(
    os.getenv("RISK_LOW_CONF_MISSING", "25.0")
)  # AC-04：空间特征缺失率阈值
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
