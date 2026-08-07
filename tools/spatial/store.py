"""S3 L2 空间特征读写层：DWD/业务库读取 + ADS/DWS 建表与幂等写入。

读写边界：读 DWD 用 app 账号（crawl_params，只读）；读业务库 collateral/loan
用 root（business_params）；ADS/DWS 建表与写库必须用 root（root_crawl_params，
DDL 需要 root）。与 tools/risk/store.py 的约定一致。

幂等语义：两表均以业务键做主键（zone_id / (entity_type, entity_id)），
INSERT ... ON DUPLICATE KEY UPDATE 覆盖旧值；跑任意多次结果一致，可对账。
"""

from __future__ import annotations

import config
import pymysql

# 单批写入行数：executemany 一次过大时 MySQL 包体受限，分批更稳
_BATCH = 1000

ZONE_COLS = [
    "zone_id",
    "zone_type",
    "city",
    "center_lng",
    "center_lat",
    "sample_count",
    "median_unit_price",
    "median_ltv",
    "price_dev_vs_city",
    "is_high_risk_zone",
    "high_risk_rule",
    "build_date",
]

FEATURE_COLS = [
    "entity_type",
    "entity_id",
    "district",
    "lat",
    "lng",
    "poi_density",
    "commute_min",
    "zone_id",
    "price_deviation",
    "spatial_feat_missing_pct",
    "build_date",
]

ZONE_DDL = """
CREATE TABLE IF NOT EXISTS ads_spatial_zone (
    zone_id VARCHAR(64) NOT NULL,
    zone_type VARCHAR(8) NOT NULL,
    city VARCHAR(8),
    center_lng DECIMAL(10,6),
    center_lat DECIMAL(10,6),
    sample_count INT,
    median_unit_price DECIMAL(12,2),
    median_ltv DECIMAL(8,4),
    price_dev_vs_city DECIMAL(10,4),
    is_high_risk_zone TINYINT,
    high_risk_rule VARCHAR(16),
    build_date DATE NOT NULL,
    etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (zone_id, build_date),
    KEY idx_zone_city (city),
    KEY idx_zone_risk (is_high_risk_zone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

FEATURE_DDL = """
CREATE TABLE IF NOT EXISTS dws_spatial_feature (
    entity_type VARCHAR(16) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    district VARCHAR(8),
    lat DECIMAL(10,6),
    lng DECIMAL(10,6),
    poi_density DECIMAL(10,4),
    commute_min DECIMAL(8,2),
    zone_id VARCHAR(64),
    price_deviation DECIMAL(10,4),
    spatial_feat_missing_pct DECIMAL(5,2),
    build_date DATE NOT NULL,
    etl_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entity_type, entity_id, build_date),
    KEY idx_feat_zone (zone_id),
    KEY idx_feat_missing (spatial_feat_missing_pct)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_tables(conn) -> None:
    """幂等建表（DDL 需 root 连接）。"""
    cur = conn.cursor()
    cur.execute(ZONE_DDL)
    cur.execute(FEATURE_DDL)
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def load_sale_rows(conn) -> list[dict]:
    """读 DWD sale 挂牌行：仅取有坐标（经纬度非空）且有有效单价的行。

    坐标覆盖仅约 29%（geocode hit），无坐标行无法参与空间计算，剔除。
    用 (district, community, url_key) 做实体键；过滤广东 bbox 外的离群点
    （坐标来自外市污染房源，见 avm/README 误差分解）。
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT url_key, district, community, latitude, longitude, unit_price_yuan
        FROM crawl_housing_sale
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND unit_price_yuan > 0
          AND latitude BETWEEN %s AND %s
          AND longitude BETWEEN %s AND %s
        """,
        (
            config.GD_BBOX["lat_min"],
            config.GD_BBOX["lat_max"],
            config.GD_BBOX["lng_min"],
            config.GD_BBOX["lng_max"],
        ),
    )
    rows = [
        {
            "url_key": r[0],
            "district": r[1],
            "community": r[2],
            "lat": float(r[3]),
            "lng": float(r[4]),
            "unit_price_yuan": float(r[5]),
        }
        for r in cur.fetchall()
        if r[1]  # district 城市码为空的行无意义
    ]
    cur.close()
    return rows


def load_collaterals(conn) -> list[dict]:
    """读业务库抵押物主档（含合成坐标）。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT collateral_id, property_addr, lat, lng, area, true_market_price FROM collateral"
    )
    rows = [
        {
            "collateral_id": int(r[0]),
            "property_addr": r[1],
            "lat": float(r[2]) if r[2] is not None else None,
            "lng": float(r[3]) if r[3] is not None else None,
            "area": float(r[4]) if r[4] is not None else None,
            "true_market_price": float(r[5]) if r[5] is not None else None,
        }
        for r in cur.fetchall()
    ]
    cur.close()
    return rows


def load_loans(conn) -> list[dict]:
    """读贷款台账（LTV 规则 B 需要余额）。"""
    cur = conn.cursor()
    cur.execute("SELECT loan_id, collateral_id, balance FROM loan")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# 写入（幂等 upsert）
# ---------------------------------------------------------------------------


def _zone_tuple(z: dict, date: str) -> tuple:
    return tuple(
        [
            z["zone_id"],
            z["zone_type"],
            z["city"],
            z["center_lng"],
            z["center_lat"],
            z["sample_count"],
            z["median_unit_price"],
            z["median_ltv"],
            z["price_dev_vs_city"],
            z["is_high_risk_zone"],
            z["high_risk_rule"],
            date,
        ]
    )


def _feature_tuple(f: dict, date: str) -> tuple:
    return tuple(
        [
            f["entity_type"],
            f["entity_id"],
            f["district"],
            f["lat"],
            f["lng"],
            f["poi_density"],
            f["commute_min"],
            f["zone_id"],
            f["price_deviation"],
            f["spatial_feat_missing_pct"],
            date,
        ]
    )


def upsert_zones(conn, zones: list[dict], date: str) -> int:
    if not zones:
        return 0
    sql = (
        f"INSERT INTO ads_spatial_zone ({','.join(ZONE_COLS)}) "
        f"VALUES ({','.join(['%s'] * len(ZONE_COLS))}) "
        "ON DUPLICATE KEY UPDATE "
        " zone_type=VALUES(zone_type), city=VALUES(city), center_lng=VALUES(center_lng), "
        " center_lat=VALUES(center_lat), sample_count=VALUES(sample_count), "
        " median_unit_price=VALUES(median_unit_price), median_ltv=VALUES(median_ltv), "
        " price_dev_vs_city=VALUES(price_dev_vs_city), "
        " is_high_risk_zone=VALUES(is_high_risk_zone), "
        " high_risk_rule=VALUES(high_risk_rule), etl_ts=CURRENT_TIMESTAMP"
    )
    cur = conn.cursor()
    data = [_zone_tuple(z, date) for z in zones]
    for i in range(0, len(data), _BATCH):
        cur.executemany(sql, data[i : i + _BATCH])
    conn.commit()
    cur.close()
    return len(zones)


def upsert_features(conn, features: list[dict], date: str) -> int:
    if not features:
        return 0
    sql = (
        f"INSERT INTO dws_spatial_feature ({','.join(FEATURE_COLS)}) "
        f"VALUES ({','.join(['%s'] * len(FEATURE_COLS))}) "
        "ON DUPLICATE KEY UPDATE "
        " district=VALUES(district), lat=VALUES(lat), lng=VALUES(lng), "
        " poi_density=VALUES(poi_density), commute_min=VALUES(commute_min), "
        " zone_id=VALUES(zone_id), price_deviation=VALUES(price_deviation), "
        " spatial_feat_missing_pct=VALUES(spatial_feat_missing_pct), etl_ts=CURRENT_TIMESTAMP"
    )
    cur = conn.cursor()
    data = [_feature_tuple(f, date) for f in features]
    for i in range(0, len(data), _BATCH):
        cur.executemany(sql, data[i : i + _BATCH])
    conn.commit()
    cur.close()
    return len(features)


def connect(params: dict) -> pymysql.connections.Connection:
    return pymysql.connect(**params, charset="utf8mb4")
