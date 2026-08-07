"""tools/stream/init_db.py 与 sql/ltv_realtime.sql 的最小测试集。

零 DB / 零 Flink：init_db 的 pymysql 连接换成录制型假连接；SQL 文件只做字符串断言 +
Python 侧语义复现（与 tools/risk/config 的阈值交叉验证，防止口径漂移）。
"""

import os

import streammods
from streammods import init_db

_SQL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "ltv_realtime.sql"
)


def _sql_text() -> str:
    with open(_SQL_PATH, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- init_db.py
def test_ddl_idempotent_and_event_id_pk():
    assert "CREATE TABLE IF NOT EXISTS ads_stream_ltv_alerts" in init_db.DDL
    assert "PRIMARY KEY (event_id)" in init_db.DDL  # 重放幂等：同 event_id 覆盖


def test_ddl_columns_match_sink_select():
    """inbox 表列与 Flink SQL 写入列一一对应（漂移会让作业跑不起来）。"""
    sql = _sql_text()
    for col in (
        "event_id",
        "loan_id",
        "customer_id",
        "collateral_id",
        "loan_balance",
        "market_valuation",
        "ltv",
        "risk_class",
        "is_high_risk_zone",
        "alert_date",
    ):
        assert col in init_db.DDL, f"DDL 缺列 {col}"
    for ref in ("e.balance", "d.market_valuation", "CAST(e.balance / d.market_valuation"):
        assert ref in sql, f"SQL 缺计算 {ref}"


def test_init_db_main_executes_ddl_and_commits(monkeypatch, conn, capsys):
    monkeypatch.setattr(init_db.pymysql, "connect", lambda **k: conn)
    init_db.main()
    sql, _ = conn.find_sql("CREATE TABLE IF NOT EXISTS ads_stream_ltv_alerts")
    assert sql is not None
    assert conn.commits == 1
    assert "ready" in capsys.readouterr().out


# ---------------------------------------------------------------- sql/ltv_realtime.sql
def test_sql_alert_line_matches_config_red_line():
    """实时越线线 0.85 与 tools/risk/config.LTV_RED_LINE 一致。"""
    sql = _sql_text()
    assert f"(e.balance / d.market_valuation) > {streammods.producer.config.LTV_RED_LINE}" in sql
    assert "e.balance / d.market_valuation" in sql
    assert "d.market_valuation > 0" in sql
    assert "d.market_valuation IS NOT NULL" in sql


def test_sql_filters_scope():
    """只推送 loan 的新增/变更事件，DELETE 与无关表不进来。"""
    sql = _sql_text()
    assert "e.table_name = 'loan'" in sql
    assert "e.event_type IN ('INSERT', 'UPDATE')" in sql


def test_sql_case_boundaries_match_config_classify():
    """SQL 里 CASE 的五级边界（0.60/0.75/0.85/1.00）与 config.CLASS_LTV_UPPER 一致。"""
    sql = _sql_text()
    upper = streammods.producer.config.CLASS_LTV_UPPER
    for label, value in (("正常", 0.60), ("关注", 0.75), ("次级", 0.85), ("可疑", 1.00)):
        assert f"<= {value:.2f}" in sql, f"SQL 缺边界 {value:.2f}"
        assert upper[label] == value, f"{label} 阈值在 config 与 SQL 间漂移"

    # Python 侧复现 SQL CASE → 与 config.classify 全网格一致
    def sql_class(ltv):
        for label in ("正常", "关注", "次级", "可疑"):
            if ltv <= upper[label]:
                return label
        return "损失"

    for ltv in [0.0, 0.59, 0.60, 0.61, 0.74, 0.75, 0.76, 0.84, 0.85, 0.86, 1.00, 1.01, 3.0]:
        assert sql_class(ltv) == streammods.producer.config.classify(ltv), f"ltv={ltv}"
