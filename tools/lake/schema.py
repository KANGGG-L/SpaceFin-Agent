"""Doris 湖仓分层表定义（ODS / DWD / DWS / ADS）与 DDL 生成。

分层口径：
  - ODS：贴源快照。MySQL 各业务表 + 数据湖 Parquet（ods_housing_sale_lake）原样落仓，
        仅做类型映射（MySQL 类型 → Doris 类型），不做业务加工。
  - DWD：清洗明细。对 ODS 做去重（url_key 取最新）、剔除无效报价/面积、补算楼龄。
  - DWS：多维聚合。按城市/风险类别等维度聚合出统计指标。
  - ADS：应用层。面向报表/驾驶舱的最终输出（城市均价、五级分类、G11 报送）。
"""

# ---- Doris 建表模板（单 BE 开发环境：replication_num=1）----
_TABLE_PROPS = 'PROPERTIES ("replication_num" = "1")'


def ddl_for(db: str, table: str, columns: list, key: list, buckets: int = 1) -> str:
    """根据 (列名, 类型, 约束) 三元组生成 CREATE TABLE IF NOT EXISTS 语句。

    Doris 要求 key 列 NOT NULL（DUPLICATE KEY 模型），非 key 列默认可空，
    与 MySQL 源的可空性不必完全对齐——贴源层以「读得进来」优先。
    """
    col_lines = []
    for name, typ, constraint in columns:
        col_lines.append(f"    `{name}` {typ} {constraint}".rstrip())
    key_expr = ", ".join(f"`{k}`" for k in key)
    return (
        f"CREATE TABLE IF NOT EXISTS {db}.{table} (\n"
        + ",\n".join(col_lines)
        + f"\n) DUPLICATE KEY({key_expr})\n"
        f"DISTRIBUTED BY HASH(`{key[0]}`) BUCKETS {buckets}\n"
        f"{_TABLE_PROPS};"
    )


# ============================================================================
# ODS 贴源表（列顺序与 MySQL 源表 ordinal_position 一致，供 Stream Load CSV 对齐）
# ============================================================================
# (列名, Doris类型, 约束)；timestamp→DATETIME、text→STRING 为仅有的类型映射
ODS_TABLES = {
    "ods_loan": {
        "columns": [
            ("loan_id", "INT", "NOT NULL"),
            ("customer_id", "INT", "NOT NULL"),
            ("collateral_id", "INT", "NOT NULL"),
            ("loan_amount", "DECIMAL(14,2)", ""),
            ("balance", "DECIMAL(14,2)", ""),
            ("interest_rate", "DECIMAL(5,2)", ""),
            ("risk_class", "VARCHAR(8)", "NOT NULL"),
            ("origination_date", "DATE", ""),
            ("created_at", "DATETIME", "NOT NULL"),
        ],
        "key": ["loan_id"],
    },
    "ods_collateral": {
        "columns": [
            ("collateral_id", "INT", "NOT NULL"),
            ("property_addr", "VARCHAR(128)", ""),
            ("lat", "DOUBLE", ""),
            ("lng", "DOUBLE", ""),
            ("area", "DOUBLE", ""),
            ("age", "DOUBLE", ""),
            ("true_market_price", "DECIMAL(14,2)", ""),
            ("poi_density", "DOUBLE", ""),
            ("commute_min", "DOUBLE", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("spatial_feat_missing_pct", "DECIMAL(4,2)", ""),
        ],
        "key": ["collateral_id"],
    },
    "ods_customer": {
        "columns": [
            ("customer_id", "INT", "NOT NULL"),
            ("credit_score", "DOUBLE", ""),
            ("income_monthly", "DECIMAL(12,2)", ""),
            ("debt_ratio", "DECIMAL(4,2)", ""),
            ("created_at", "DATETIME", "NOT NULL"),
        ],
        "key": ["customer_id"],
    },
    "ods_housing_sale": {
        "columns": [
            ("url_key", "VARCHAR(64)", "NOT NULL"),
            ("url", "VARCHAR(1024)", "NOT NULL"),
            ("title", "VARCHAR(255)", ""),
            ("community", "VARCHAR(255)", ""),
            ("district", "VARCHAR(32)", ""),
            ("bedrooms", "INT", ""),
            ("halls", "INT", ""),
            ("bathrooms", "INT", ""),
            ("area_sqm", "DECIMAL(10,2)", ""),
            ("direction", "VARCHAR(16)", ""),
            ("floor", "VARCHAR(32)", ""),
            ("building_year", "INT", ""),
            ("building_age", "INT", ""),
            ("parking_count", "INT", ""),
            ("total_price_wan", "DECIMAL(12,2)", ""),
            ("unit_price_yuan", "INT", ""),
            ("latitude", "DECIMAL(10,6)", ""),
            ("longitude", "DECIMAL(10,6)", ""),
            ("first_seen_date", "DATE", "NOT NULL"),
            ("last_seen_date", "DATE", "NOT NULL"),
            ("days_on_market", "INT", "NOT NULL"),
            ("geocode_status", "VARCHAR(16)", ""),
            ("source", "VARCHAR(16)", ""),
            ("etl_ts", "DATETIME", "NOT NULL"),
        ],
        "key": ["url_key"],
        "buckets": 3,
    },
    "ods_housing_rent": {
        "columns": [
            ("url_key", "VARCHAR(64)", "NOT NULL"),
            ("url", "VARCHAR(1024)", "NOT NULL"),
            ("title", "VARCHAR(255)", ""),
            ("community", "VARCHAR(255)", ""),
            ("district", "VARCHAR(32)", ""),
            ("bedrooms", "INT", ""),
            ("halls", "INT", ""),
            ("bathrooms", "INT", ""),
            ("area_sqm", "DECIMAL(10,2)", ""),
            ("direction", "VARCHAR(16)", ""),
            ("floor", "VARCHAR(32)", ""),
            ("monthly_rent_yuan", "INT", ""),
            ("rent_type", "VARCHAR(16)", ""),
            ("latitude", "DECIMAL(10,6)", ""),
            ("longitude", "DECIMAL(10,6)", ""),
            ("first_seen_date", "DATE", "NOT NULL"),
            ("last_seen_date", "DATE", "NOT NULL"),
            ("days_on_market", "INT", "NOT NULL"),
            ("geocode_status", "VARCHAR(16)", ""),
            ("source", "VARCHAR(16)", ""),
            ("etl_ts", "DATETIME", "NOT NULL"),
        ],
        "key": ["url_key"],
        "buckets": 3,
    },
    "ods_dws_risk_class": {
        "columns": [
            ("loan_id", "INT", "NOT NULL"),
            ("customer_id", "INT", ""),
            ("collateral_id", "INT", ""),
            ("balance", "DECIMAL(14,2)", ""),
            ("interest_rate", "DECIMAL(5,2)", ""),
            ("market_valuation", "DECIMAL(14,2)", ""),
            ("ltv", "DECIMAL(8,4)", ""),
            ("risk_class", "VARCHAR(8)", ""),
            ("low_confidence", "TINYINT", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("alert", "TINYINT", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["loan_id"],
    },
    "ods_dws_spatial_feature": {
        "columns": [
            ("entity_type", "VARCHAR(16)", "NOT NULL"),
            # entity_id 存「城市码|房源标题」，含中文，VARCHAR 按字节计数（Doris 限制），
            # 实测最长 94 字节，留余量取 128
            ("entity_id", "VARCHAR(128)", "NOT NULL"),
            ("district", "VARCHAR(8)", ""),
            ("lat", "DECIMAL(10,6)", ""),
            ("lng", "DECIMAL(10,6)", ""),
            ("poi_density", "DECIMAL(10,4)", ""),
            ("commute_min", "DECIMAL(8,2)", ""),
            ("zone_id", "VARCHAR(64)", ""),
            ("price_deviation", "DECIMAL(10,4)", ""),
            ("spatial_feat_missing_pct", "DECIMAL(5,2)", ""),
            ("build_date", "DATE", "NOT NULL"),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["entity_type", "entity_id"],
    },
    "ods_community_coords": {
        "columns": [
            ("city", "VARCHAR(16)", ""),
            ("community", "VARCHAR(255)", ""),
            ("lat", "DECIMAL(10,6)", ""),
            ("lng", "DECIMAL(10,6)", ""),
            ("status", "VARCHAR(16)", ""),
            ("source", "VARCHAR(16)", ""),
            ("level", "INT", ""),
            ("query_count", "INT", ""),
            ("last_queried_at", "DATETIME", ""),
            ("updated_at", "DATETIME", ""),
        ],
        "key": ["city", "community"],
    },
    "ods_ads_risk_class": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("risk_class", "VARCHAR(8)", "NOT NULL"),
            ("loan_count", "INT", ""),
            ("balance_total", "DECIMAL(16,2)", ""),
            ("balance_pct", "DECIMAL(8,4)", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["stat_date", "risk_class"],
    },
    "ods_ads_1104_g11": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("risk_class", "VARCHAR(8)", "NOT NULL"),
            ("loan_count", "INT", ""),
            ("balance_total", "DECIMAL(16,2)", ""),
            ("balance_pct", "DECIMAL(8,4)", ""),
            ("is_total", "TINYINT", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["stat_date", "risk_class"],
    },
    "ods_ads_alert_dispatch": {
        "columns": [
            ("id", "INT", "NOT NULL"),
            ("loan_id", "INT", ""),
            ("alert_date", "DATE", ""),
            ("dispatch_date", "DATE", ""),
            ("status", "VARCHAR(16)", ""),
            ("attempt_count", "INT", ""),
            ("max_retries", "INT", ""),
            ("last_error", "VARCHAR(255)", ""),
            ("dispatch_ts", "DATETIME", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["id"],
    },
    "ods_ads_alert_inbox": {
        "columns": [
            ("id", "INT", "NOT NULL"),
            ("loan_id", "INT", ""),
            ("customer_id", "INT", ""),
            ("collateral_id", "INT", ""),
            ("loan_balance", "DECIMAL(14,2)", ""),
            ("market_valuation", "DECIMAL(14,2)", ""),
            ("ltv", "DECIMAL(8,4)", ""),
            ("risk_class", "VARCHAR(8)", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("alert_date", "DATE", ""),
            ("received_ts", "DATETIME", ""),
        ],
        "key": ["id"],
    },
    "ods_ads_ltv_alerts": {
        "columns": [
            ("id", "INT", "NOT NULL"),
            ("loan_id", "INT", ""),
            ("customer_id", "INT", ""),
            ("collateral_id", "INT", ""),
            ("loan_balance", "DECIMAL(14,2)", ""),
            ("market_valuation", "DECIMAL(14,2)", ""),
            ("ltv", "DECIMAL(8,4)", ""),
            ("risk_class", "VARCHAR(8)", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("alert_date", "DATE", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["id"],
    },
    "ods_ads_spatial_zone": {
        "columns": [
            ("zone_id", "VARCHAR(64)", "NOT NULL"),
            ("zone_type", "VARCHAR(8)", ""),
            ("city", "VARCHAR(8)", ""),
            ("center_lng", "DECIMAL(10,6)", ""),
            ("center_lat", "DECIMAL(10,6)", ""),
            ("sample_count", "INT", ""),
            ("median_unit_price", "DECIMAL(12,2)", ""),
            ("median_ltv", "DECIMAL(8,4)", ""),
            ("price_dev_vs_city", "DECIMAL(10,4)", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("high_risk_rule", "VARCHAR(16)", ""),
            ("build_date", "DATE", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["zone_id"],
    },
    "ods_cdc_consumer_offset": {
        "columns": [
            ("consumer", "VARCHAR(64)", "NOT NULL"),
            ("last_id", "BIGINT", ""),
            ("updated_at", "DATETIME", ""),
        ],
        "key": ["consumer"],
    },
    "ods_cdc_log": {
        "columns": [
            ("id", "BIGINT", "NOT NULL"),
            ("table_name", "VARCHAR(32)", ""),
            ("event_type", "VARCHAR(16)", ""),
            ("before_json", "STRING", ""),
            ("after_json", "STRING", ""),
            ("cdc_ts", "DATETIME", ""),
        ],
        "key": ["id"],
    },
}

# 数据湖 Parquet（dt=2026-08-04 快照）贴源表：字段来自 parquet schema（district 即城市码）
LAKE_TABLE = {
    "ods_housing_sale_lake": {
        "columns": [
            # 注意：Doris 不允许 STRING 作为 key 列，key 列必须用定长 VARCHAR
            ("url_key", "VARCHAR(64)", "NOT NULL"),
            ("url", "STRING", "NOT NULL"),
            ("title", "STRING", ""),
            ("community", "STRING", ""),
            ("district", "STRING", ""),
            ("bedrooms", "INT", ""),
            ("halls", "INT", ""),
            ("bathrooms", "INT", ""),
            ("area_sqm", "DOUBLE", ""),
            ("direction", "STRING", ""),
            ("floor", "STRING", ""),
            ("building_year", "INT", ""),
            ("building_age", "INT", ""),
            ("parking_count", "INT", ""),
            ("total_price_wan", "DOUBLE", ""),
            ("unit_price_yuan", "INT", ""),
            ("latitude", "STRING", ""),
            ("longitude", "STRING", ""),
            ("first_seen_date", "STRING", ""),
            ("last_seen_date", "STRING", ""),
            ("source", "STRING", ""),
            ("snapshot_dt", "DATE", "NOT NULL"),
        ],
        "key": ["url_key"],
        "buckets": 3,
    },
}

# MySQL 源表清单（db, 表, Doris 目标表）
MYSQL_SOURCES = [
    ("spacefin", "loan", "ods_loan"),
    ("spacefin", "collateral", "ods_collateral"),
    ("spacefin", "customer", "ods_customer"),
    ("spacefin_crawler", "crawl_housing_sale", "ods_housing_sale"),
    ("spacefin_crawler", "crawl_housing_rent", "ods_housing_rent"),
    ("spacefin_crawler", "dws_risk_class", "ods_dws_risk_class"),
    ("spacefin_crawler", "dws_spatial_feature", "ods_dws_spatial_feature"),
    ("spacefin_crawler", "community_coords", "ods_community_coords"),
    ("spacefin_crawler", "ads_risk_class", "ods_ads_risk_class"),
    ("spacefin_crawler", "ads_1104_g11", "ods_ads_1104_g11"),
    ("spacefin_crawler", "ads_alert_dispatch", "ods_ads_alert_dispatch"),
    ("spacefin_crawler", "ads_alert_inbox", "ods_ads_alert_inbox"),
    ("spacefin_crawler", "ads_ltv_alerts", "ods_ads_ltv_alerts"),
    ("spacefin_crawler", "ads_spatial_zone", "ods_ads_spatial_zone"),
    ("spacefin_crawler", "ods_cdc_consumer_offset", "ods_cdc_consumer_offset"),
    ("spacefin_crawler", "ods_cdc_log", "ods_cdc_log"),
]

# ============================================================================
# DWD 清洗明细
# ============================================================================
DWD_TABLES = {
    "dwd_housing_sale": {
        "columns": [
            ("url_key", "VARCHAR(64)", "NOT NULL"),
            ("title", "VARCHAR(255)", ""),
            ("community", "VARCHAR(255)", ""),
            ("city_code", "VARCHAR(8)", ""),
            ("district", "VARCHAR(32)", ""),
            ("bedrooms", "INT", ""),
            ("halls", "INT", ""),
            ("bathrooms", "INT", ""),
            ("area_sqm", "DECIMAL(10,2)", ""),
            ("direction", "VARCHAR(16)", ""),
            ("floor", "VARCHAR(32)", ""),
            ("building_year", "INT", ""),
            ("building_age", "INT", ""),
            ("parking_count", "INT", ""),
            ("total_price_wan", "DECIMAL(12,2)", ""),
            ("unit_price_yuan", "INT", ""),
            ("latitude", "DECIMAL(10,6)", ""),
            ("longitude", "DECIMAL(10,6)", ""),
            ("first_seen_date", "DATE", ""),
            ("last_seen_date", "DATE", ""),
            ("days_on_market", "INT", ""),
            ("geocode_status", "VARCHAR(16)", ""),
            ("source", "VARCHAR(16)", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["url_key"],
        "buckets": 3,
    },
    "dwd_loan_detail": {
        "columns": [
            ("loan_id", "INT", "NOT NULL"),
            ("customer_id", "INT", ""),
            ("collateral_id", "INT", ""),
            ("loan_amount", "DECIMAL(14,2)", ""),
            ("balance", "DECIMAL(14,2)", ""),
            ("interest_rate", "DECIMAL(5,2)", ""),
            ("risk_class", "VARCHAR(8)", ""),
            ("origination_date", "DATE", ""),
            ("credit_score", "DOUBLE", ""),
            ("income_monthly", "DECIMAL(12,2)", ""),
            ("debt_ratio", "DECIMAL(4,2)", ""),
            ("property_addr", "VARCHAR(128)", ""),
            ("lat", "DOUBLE", ""),
            ("lng", "DOUBLE", ""),
            ("area", "DOUBLE", ""),
            ("age", "DOUBLE", ""),
            ("true_market_price", "DECIMAL(14,2)", ""),
            ("poi_density", "DOUBLE", ""),
            ("commute_min", "DOUBLE", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("spatial_feat_missing_pct", "DECIMAL(4,2)", ""),
        ],
        "key": ["loan_id"],
    },
}

# DWD 加工 SQL：清洗语义写死为「同一 url_key 保留 last_seen_date 最新一条」+
# 「剔除无效报价/面积」+「楼龄缺失时由建造年份推算」；全部幂等（重跑先清空目标表）
DWD_FILL_SQL = {
    "dwd_housing_sale": (
        "INSERT INTO dwd.dwd_housing_sale "
        "SELECT url_key, title, community, district, district AS city_code, bedrooms, halls, "
        "bathrooms, area_sqm, direction, floor, building_year, "
        "IFNULL(building_age, YEAR(first_seen_date) - building_year) AS building_age, "
        "parking_count, total_price_wan, unit_price_yuan, latitude, longitude, "
        "first_seen_date, last_seen_date, days_on_market, geocode_status, source, etl_ts "
        "FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY url_key "
        "         ORDER BY last_seen_date DESC, etl_ts DESC) AS rn "
        "  FROM ods.ods_housing_sale "
        "  WHERE unit_price_yuan > 0 AND area_sqm > 0"
        ") t WHERE rn = 1"
    ),
    "dwd_loan_detail": (
        "INSERT INTO dwd.dwd_loan_detail "
        "SELECT l.loan_id, l.customer_id, l.collateral_id, l.loan_amount, l.balance, "
        "l.interest_rate, l.risk_class, l.origination_date, "
        "c.credit_score, c.income_monthly, c.debt_ratio, "
        "co.property_addr, co.lat, co.lng, co.area, co.age, co.true_market_price, "
        "co.poi_density, co.commute_min, co.is_high_risk_zone, co.spatial_feat_missing_pct "
        "FROM ods.ods_loan l "
        "LEFT JOIN ods.ods_customer c ON l.customer_id = c.customer_id "
        "LEFT JOIN ods.ods_collateral co ON l.collateral_id = co.collateral_id"
    ),
}

# ============================================================================
# DWS 多维聚合
# ============================================================================
DWS_TABLES = {
    "dws_city_price_stats": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("city_code", "VARCHAR(8)", "NOT NULL"),
            ("listing_count", "INT", ""),
            ("avg_unit_price_yuan", "DECIMAL(12,2)", ""),
            ("median_unit_price_yuan", "DECIMAL(12,2)", ""),
            ("avg_area_sqm", "DECIMAL(10,2)", ""),
            ("avg_total_price_wan", "DECIMAL(12,2)", ""),
        ],
        "key": ["stat_date", "city_code"],
    },
    "dws_risk_class": {
        "columns": [
            ("loan_id", "INT", "NOT NULL"),
            ("customer_id", "INT", ""),
            ("collateral_id", "INT", ""),
            ("balance", "DECIMAL(14,2)", ""),
            ("interest_rate", "DECIMAL(5,2)", ""),
            ("market_valuation", "DECIMAL(14,2)", ""),
            ("ltv", "DECIMAL(8,4)", ""),
            ("risk_class", "VARCHAR(8)", ""),
            ("low_confidence", "TINYINT", ""),
            ("is_high_risk_zone", "TINYINT", ""),
            ("alert", "TINYINT", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["loan_id"],
    },
}

DWS_FILL_SQL = {
    # 以 stat_date 参数化为最近同步日期（sync.py 计算为 DWD 清洗口径对应的 last_seen 最大日）
    "dws_city_price_stats": (
        "INSERT INTO dws.dws_city_price_stats "
        "SELECT '{stat_date}' AS stat_date, city_code, COUNT(*) AS listing_count, "
        "ROUND(AVG(unit_price_yuan), 2) AS avg_unit_price_yuan, "
        "ROUND(PERCENTILE(unit_price_yuan, 0.5), 2) AS median_unit_price_yuan, "
        "ROUND(AVG(area_sqm), 2) AS avg_area_sqm, "
        "ROUND(AVG(total_price_wan), 2) AS avg_total_price_wan "
        "FROM dwd.dwd_housing_sale "
        "WHERE unit_price_yuan > 0 "
        "GROUP BY city_code"
    ),
    "dws_risk_class": (
        "INSERT INTO dws.dws_risk_class "
        "SELECT loan_id, customer_id, collateral_id, balance, interest_rate, "
        "market_valuation, ltv, risk_class, low_confidence, is_high_risk_zone, alert, etl_ts "
        "FROM ods.ods_dws_risk_class"
    ),
}

# ============================================================================
# ADS 应用层
# ============================================================================
ADS_TABLES = {
    "ads_risk_class": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("risk_class", "VARCHAR(8)", "NOT NULL"),
            ("loan_count", "INT", ""),
            ("balance_total", "DECIMAL(16,2)", ""),
            ("balance_pct", "DECIMAL(8,4)", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["stat_date", "risk_class"],
    },
    "ads_city_avg_price": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("city_code", "VARCHAR(8)", "NOT NULL"),
            ("listing_count", "INT", ""),
            ("avg_unit_price_yuan", "DECIMAL(12,2)", ""),
            ("median_unit_price_yuan", "DECIMAL(12,2)", ""),
        ],
        "key": ["stat_date", "city_code"],
    },
    "ads_1104_g11": {
        "columns": [
            ("stat_date", "DATE", "NOT NULL"),
            ("risk_class", "VARCHAR(8)", "NOT NULL"),
            ("loan_count", "INT", ""),
            ("balance_total", "DECIMAL(16,2)", ""),
            ("balance_pct", "DECIMAL(8,4)", ""),
            ("is_total", "TINYINT", ""),
            ("etl_ts", "DATETIME", ""),
        ],
        "key": ["stat_date", "risk_class"],
    },
}

ADS_FILL_SQL = {
    # 五级分类占比：直接取自上游已产出的 ods_ads_risk_class（stat_date 按参数过滤）
    "ads_risk_class": (
        "INSERT INTO ads.ads_risk_class "
        "SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, etl_ts "
        "FROM ods.ods_ads_risk_class WHERE stat_date = '{stat_date}'"
    ),
    "ads_city_avg_price": (
        "INSERT INTO ads.ads_city_avg_price "
        "SELECT stat_date, city_code, listing_count, avg_unit_price_yuan, median_unit_price_yuan "
        "FROM dws.dws_city_price_stats WHERE stat_date = '{stat_date}'"
    ),
    "ads_1104_g11": (
        "INSERT INTO ads.ads_1104_g11 "
        "SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, is_total, etl_ts "
        "FROM ods.ods_ads_1104_g11 WHERE stat_date = '{stat_date}'"
    ),
}
