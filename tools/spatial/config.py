"""S3 空间特征模块配置：DB 连接（复用仓库根 .env）+ 地理参数 + 可解释规则阈值。

连接参数读法与 tools/risk/config.py 的 load_env 一致（同仓库 .env），但本模块
独立复制而非 import：tools/risk 是业务风险层，spatial 是空间层，二者解耦，
避免互相 sys.path 依赖。建表必须用 root（root_crawl_params），读 DWD 用 app 账号。
"""

import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 业务时区口径与 risk 层一致：Asia/Shanghai，见 tools/risk/config.py: BUSINESS_TZ
BUSINESS_TZ = ZoneInfo(os.getenv("SPACEFIN_BUSINESS_TZ", "Asia/Shanghai"))

BUSINESS_DB = "spacefin"
CRAWL_DB = "spacefin_crawler"

# 广东 21 城码（crawl_housing_sale.district 存的就是城市码），与 tools/risk/config.py 一致
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

# 广东 21 城行政中心近似坐标（经度, 纬度）。用于通勤直线距离代理的锚点：
# 无路网数据时，以「到城市中心直线距离 / 平均通勤速度」近似通勤时长。
# 坐标取各市政府/市中心约值，±0.02°（约 2km）量级误差对通勤代理可接受。
CITY_CENTER = {
    "gz": (113.2644, 23.1291),
    "sz": (114.0579, 22.5431),
    "fs": (113.1219, 23.0219),
    "dg": (113.7518, 23.0207),
    "zh": (113.5767, 22.2710),
    "zs": (113.3928, 22.5176),
    "hui": (114.4162, 23.1115),
    "jm": (113.0822, 22.5789),
    "zq": (112.4720, 23.0472),
    "qy": (113.0611, 23.6820),
    "sg": (113.5975, 24.8108),
    "st": (116.6822, 23.3541),
    "sw": (115.3754, 22.7868),
    "jy": (116.3732, 23.5497),
    "cz": (116.6228, 23.6567),
    "mz": (116.1226, 24.2885),
    "hy": (114.7004, 23.7442),
    "yj": (111.9827, 21.8580),
    "mm": (110.9250, 21.6633),
    "zj": (110.3593, 21.2707),
    "yf": (112.0445, 22.9153),
}

# 广东地理范围（用于剔除坐标离群的外市污染行）：纬度 [20,26]、经度 [108,118]
GD_BBOX = {"lat_min": 20.0, "lat_max": 26.0, "lng_min": 108.0, "lng_max": 118.0}


def city_code_from_addr(addr: str | None) -> str | None:
    """「广州市黄埔区…」→ 'gz'；取不到返回 None。

    抵押物只有中文地址（business 库 collateral.property_addr），价格面网格的 city
    是城市码（DWD district）。两边要 join 就必须有一套共用的解析，放在 config 是因为
    CITY_MAP 本身在这里，且 features/store 都 import config。解析口径（「XX市…」前缀
    匹配 CITY_MAP 键）与前端 tools/frontend/pages/p5_spatial.py: _city_code_from_addr
    一致——前端因 sys.path 撞名不能 import 本模块（见该文件注释），只能各持一份实现，
    改口径时两处必须同步。
    """
    if not addr:
        return None
    for name, code in CITY_MAP.items():
        if str(addr).startswith(name):
            return code
    return None


def business_date() -> str:
    """当前业务日期 YYYY-MM-DD（Asia/Shanghai），与 risk 层口径一致。"""
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


def _db_params(env: dict, user_key: str, pw_key: str, database: str) -> dict:
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": env.get(user_key, "root"),
        "password": env.get(pw_key, ""),
        "database": database,
    }


def business_params(env: dict) -> dict:
    """业务库 spacefin（root 连接）：读 collateral / loan。"""
    return _db_params(env, "MYSQL_ROOT_USER", "MYSQL_ROOT_PASSWORD", BUSINESS_DB)


def crawl_params(env: dict) -> dict:
    """房产库 spacefin_crawler（app 账号只读）：读 DWD 挂牌数据。"""
    return _db_params(env, "MYSQL_APP_USER", "MYSQL_APP_PASSWORD", CRAWL_DB)


def root_crawl_params(env: dict) -> dict:
    """root + 房产库：ADS/DWS 建表与写库（DDL 需 root）。"""
    return _db_params(env, "MYSQL_ROOT_USER", "MYSQL_ROOT_PASSWORD", CRAWL_DB)


# ---------------------------------------------------------------------------
# 空间计算参数（固定随机种子保证可复现；全部计算确定无随机，种子用于流程元数据）
# ---------------------------------------------------------------------------
SEED = 42

# 价格面邻域：半径 5km 内的挂牌行中位单价作为「局部价格基准」
NEIGHBOR_RADIUS_KM = 5.0
MIN_NEIGHBORS = 5  # 邻域样本少于该值 → 价格偏差视为缺失

# 空间区块网格：0.02° ≈ 2.2km 经纬网格
GRID_DEG = 0.02
MIN_ZONE_SAMPLES = int(os.getenv("SPATIAL_MIN_ZONE_SAMPLES", "20"))  # 区块最少样本

# 高危区规则 A（价格洼地）：区块中位单价 <= 城市中位单价 * (1 - 阈值)
PRICE_LOW_RATIO = float(os.getenv("SPATIAL_PRICE_LOW_RATIO", "0.25"))

# 高危区规则 B（LTV 集中）：区块内 LTV 中位 > 红线 且 样本足够
LTV_RED_LINE = float(os.getenv("RISK_LTV_RED_LINE", "0.85"))
MIN_LTV_ZONE_SAMPLES = int(os.getenv("SPATIAL_MIN_LTV_SAMPLES", "3"))

# 价格面片区回填「片区抵押 LTV 中位」的最少抵押样本。与 MIN_LTV_ZONE_SAMPLES 同默认值
# 但独立可调：那个门槛决定「要不要判高危」，这个只决定「敢不敢把中位数显示出来」，
# 语义不同，未来收紧展示口径时不该被迫连高危判定一起动。
MIN_LTV_BACKFILL_SAMPLES = int(os.getenv("SPATIAL_MIN_LTV_BACKFILL_SAMPLES", "3"))

# POI 密度代理：半径 2km 内 DWD 挂牌数 / 圆面积（个/平方公里）
POI_RADIUS_KM = 2.0
# 最近挂牌超过该距离 → 该点无本地挂牌覆盖，POI 密度视为缺失
POI_COVERAGE_KM = 20.0

# 通勤近似：直线距离 / 平均通勤速度（km/h），再换算分钟
COMMUTE_SPEED_KMH = float(os.getenv("SPATIAL_COMMUTE_SPEED", "30.0"))
# 到最近城市中心超过该距离 → 不在任何城市覆盖内，通勤视为缺失
COMMUTE_COVERAGE_KM = float(os.getenv("SPATIAL_COMMUTE_COVERAGE", "60.0"))


def city_center_km(code: str) -> tuple[float, float] | None:
    """城市中心经纬度（经度, 纬度）。"""
    c = CITY_CENTER.get(code)
    return c


def grid_key(lng: float, lat: float) -> tuple[float, float]:
    """经纬度 → 网格左下角（经度, 纬度），0.02° 网格。"""
    return math.floor(lng / GRID_DEG) * GRID_DEG, math.floor(lat / GRID_DEG) * GRID_DEG
