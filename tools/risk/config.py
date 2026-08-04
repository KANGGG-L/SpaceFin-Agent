"""风险引擎配置：DB 连接（复用仓库 .env）+ 可配置阈值。"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
