"""ensure_tables 幂等建表 + 存量表缺列补列。

CREATE TABLE IF NOT EXISTS 只保证建新表；旧环境（S6 前的 ads_alert_inbox 缺
alert_level）重跑时必须幂等补列，否则 drivers.py 的 INSERT 会 Unknown column。
"""

from alertmods import alerting


def _existing_columns(*names):
    """构造 information_schema.COLUMNS 查询的返回行（元组化列名）。"""
    return [(n,) for n in names]


def _run_ensure(conn):
    alerting.ensure_tables(conn)
    return conn


def test_ensure_tables_backfills_missing_alert_level(conn):
    """旧表缺 alert_level → 幂等补 VARCHAR(8) 列。"""
    conn.on(
        "COLUMN_NAME",
        _existing_columns(
            "id",
            "loan_id",
            "customer_id",
            "collateral_id",
            "loan_balance",
            "market_valuation",
            "ltv",
            "risk_class",
            "is_high_risk_zone",
            "alert_date",
        ),
    )
    _run_ensure(conn)

    alters = conn.all_sql("ALTER TABLE", "ADD COLUMN", "alert_level")
    assert len(alters) == 1
    assert "VARCHAR(8)" in alters[0][0]


def test_ensure_tables_skips_backfill_when_column_present(conn):
    """新表/已有 alert_level → 不再 ALTER。"""
    conn.on(
        "COLUMN_NAME",
        _existing_columns(
            "id",
            "loan_id",
            "customer_id",
            "collateral_id",
            "loan_balance",
            "market_valuation",
            "ltv",
            "risk_class",
            "is_high_risk_zone",
            "alert_level",
            "alert_date",
        ),
    )
    _run_ensure(conn)

    assert conn.find_sql("ALTER TABLE", "ADD COLUMN") is None


def test_ensure_tables_creates_both_tables(conn):
    """新环境：两张表 CREATE TABLE IF NOT EXISTS 都执行。"""
    conn.on(
        "COLUMN_NAME",
        _existing_columns(
            "id",
            "loan_id",
            "customer_id",
            "collateral_id",
            "loan_balance",
            "market_valuation",
            "ltv",
            "risk_class",
            "is_high_risk_zone",
            "alert_level",
            "alert_date",
        ),
    )
    _run_ensure(conn)

    assert conn.find_sql("CREATE TABLE", "ads_alert_dispatch") is not None
    assert conn.find_sql("CREATE TABLE", "ads_alert_inbox") is not None
