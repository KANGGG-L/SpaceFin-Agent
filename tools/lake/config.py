"""湖仓接入统一配置：Doris / MinIO / MySQL 连接参数。

与 tools/risk/config.py 同风格：仓库根 .env 只放 MySQL 与业务键，Doris/MinIO
为本组件新增的本地开发参数，给默认值即可（部署参数见 deploy/doris-minio/docker-compose.yml）。
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 与 tools/risk/config.py 保持同一套 .env 解析逻辑（读不写）
def load_env() -> dict:
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


_ENV = load_env()

# ---- MySQL 源（与 tools/risk/config.py 同一套连接参数）----
MYSQL = {
    "host": _ENV.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(_ENV.get("MYSQL_PORT", "3306")),
    "user": "root",
    "password": _ENV.get("MYSQL_ROOT_PASSWORD", ""),
    "database": "spacefin",
}
CRAWL_DB = "spacefin_crawler"

# ---- Doris（端口见 deploy/doris-minio/docker-compose.yml）----
DORIS = {
    "host": _ENV.get("DORIS_HOST", "127.0.0.1"),
    "query_port": int(_ENV.get("DORIS_QUERY_PORT", "9030")),
    "user": _ENV.get("DORIS_USER", "root"),
    "password": _ENV.get("DORIS_PASSWORD", ""),
    # BE 的 HTTP 端口（Stream Load 走它，宿主 127.0.0.1 直连）
    "be_http_host": _ENV.get("DORIS_BE_HTTP_HOST", "127.0.0.1"),
    "be_http_port": int(_ENV.get("DORIS_BE_HTTP_PORT", "8040")),
}

# ---- MinIO 对象存储（湖的底座；凭据与 deploy/doris-minio/docker-compose.yml 一致）----
MINIO = {
    "endpoint": _ENV.get("MINIO_ENDPOINT", "http://127.0.0.1:9000"),
    "access_key": _ENV.get("MINIO_ACCESS_KEY", "spacefin_minio"),
    "secret_key": _ENV.get("MINIO_SECRET_KEY", "spacefin_minio_dev_only"),
    "bucket": _ENV.get("MINIO_BUCKET", "housing"),
}

# 数据湖本地快照：tools/lake/sync.py 上传的 Parquet 源目录（仓库内 data_lake/housing）
LAKE_SOURCE_DIR = os.path.join(REPO_ROOT, "data_lake", "housing")

# 分层库名
LAYER_DBS = ["ods", "dwd", "dws", "ads"]
