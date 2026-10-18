"""P9 合规审计页的最小测试集（零 DB：db.crawl_conn 换成录制型假连接）。

覆盖四个验收要点：
1. PAGE 契约正确（admin/risk 可见、路由 /api/compliance_audit）；
2. 导出审计行能正确解析 detail JSON 里的 rows 字段；
3. 报送告警返回所有级别（不再硬编码 alert_level='block'）；
4. 筛选参数正确传递到 SQL。
"""

import datetime

import pages.p9_compliance_audit as mod

_AUDIT_COLS = [
    "id",
    "action",
    "username",
    "role",
    "detail",
    "result",
    "ip",
    "created_at",
]
_ALERT_COLS = [
    "id",
    "report_date",
    "report_type",
    "alert_level",
    "check_name",
    "detail",
    "etl_ts",
]


def _audit_conn(conn):
    """预置一张含 2 行导出审计 + 2 条告警（不同级别）的假库。"""
    conn.when(
        "SELECT COUNT(*) FROM ads_export_audit",
        [(28,)],
    )
    conn.when(
        "SELECT id, action, username, role, detail, result, ip, created_at FROM ads_export_audit",
        [
            (
                28,
                "export",
                "da",
                "da",
                '{"risk_class": null, "rows": 136}',
                "success",
                "127.0.0.1",
                datetime.datetime(2026, 8, 6, 3, 4, 42),
            ),
            (
                27,
                "confirm",
                "risk",
                "risk",
                '{"loan_id": 1}',
                "success",
                "127.0.0.1",
                datetime.datetime(2026, 8, 5, 20, 27, 51),
            ),
        ],
        columns=_AUDIT_COLS,
    )
    # 不再硬编码 WHERE alert_level='block'，返回所有级别
    conn.when(
        "SELECT COUNT(*) FROM ads_report_alert",
        [(5,)],
    )
    conn.when(
        "SELECT id, report_date, report_type, alert_level, check_name, detail, etl_ts "
        "FROM ads_report_alert",
        [
            (
                5,
                datetime.date(2026, 8, 5),
                "1104_g11",
                "block",
                "1104_vs_dws:正常:balance",
                "口径不一致: 1104_vs_dws:正常:balance",
                datetime.datetime(2026, 8, 6, 3, 3, 39),
            ),
            (
                4,
                datetime.date(2026, 8, 4),
                "1104_g11",
                "warn",
                "dws_vs_internal:次级:balance_pct",
                "口径不一致: dws_vs_internal:次级:balance_pct",
                datetime.datetime(2026, 8, 5, 3, 3, 39),
            ),
        ],
        columns=_ALERT_COLS,
    )
    return conn


# ---------------------------------------------------------------- PAGE 契约
def test_page_contract_admin_and_route():
    assert mod.PAGE["id"] == "compliance_audit"
    assert mod.PAGE["roles"] == {"admin"}
    assert ("GET", "/api/compliance_audit") in mod.PAGE["routes"]
    assert mod.PAGE["js"] == "p9_compliance_audit.js"


# ---------------------------------------------------------------- 导出审计
def test_export_audit_parses_rows_from_detail(monkeypatch, conn, ctx):
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    out = mod.get_compliance_audit(ctx)

    assert out["export_total"] == 28
    assert len(out["export_rows"]) == 2
    export = out["export_rows"][0]
    # detail 是 JSON 文本，rows 字段应被抽成数值列供表格展示。
    assert export["action"] == "export"
    assert export["action_label"] == "导出预警 CSV"
    assert export["rows"] == 136
    assert export["result"] == "success"
    assert export["ip"] == "127.0.0.1"
    # 无 rows 字段的 detail（如确认）应得 None 而不是抛异常。
    assert out["export_rows"][1]["rows"] is None


def test_report_alerts_returns_all_levels(monkeypatch, conn, ctx):
    """不再硬编码 alert_level='block'，返回所有级别。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    out = mod.get_compliance_audit(ctx)

    assert out["report_alert_total"] == 5
    assert len(out["report_alerts"]) == 2
    levels = {a["alert_level"] for a in out["report_alerts"]}
    assert "block" in levels
    assert "warn" in levels


def test_queries_limit_recent_rows(monkeypatch, conn, ctx):
    """页面只拉最近 PAGE_LIMIT 条，不给 MySQL 全表扫描。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    mod.get_compliance_audit(ctx)
    sql, params = conn.find_sql("ORDER BY id DESC", "LIMIT")
    assert sql is not None
    assert params[0] == mod.PAGE_LIMIT


# ---------------------------------------------------------------- 筛选参数
def test_filters_date_from(monkeypatch, conn, ctx):
    """date_from 筛选正确传递到 SQL WHERE。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    ctx.query["date_from"] = ["2026-08-01"]
    _out = mod.get_compliance_audit(ctx)
    sql, _ = conn.find_sql("created_at >=")
    assert sql is not None


def test_filters_action(monkeypatch, conn, ctx):
    """action 筛选正确传递到 SQL WHERE。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    ctx.query["action"] = ["export"]
    _out = mod.get_compliance_audit(ctx)
    sql, _ = conn.find_sql("action =")
    assert sql is not None


def test_filters_alert_level(monkeypatch, conn, ctx):
    """alert_level 筛选正确传递到告警 SQL。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    ctx.query["alert_level"] = ["block"]
    _out = mod.get_compliance_audit(ctx)
    sql, _ = conn.find_sql("alert_level =")
    assert sql is not None


# ---------------------------------------------------------------- 特征归因不再存在
def test_attribution_removed_from_page(monkeypatch, conn, ctx):
    """特征归因已移至 P7，P9 不再返回 attribution 字段。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    out = mod.get_compliance_audit(ctx)
    assert "attribution" not in out


# ---------------------------------------------------------------- detail rows 工具
def test_detail_rows_handles_bad_json():
    assert mod._detail_rows("not-json") is None
    assert mod._detail_rows(None) is None
    assert mod._detail_rows('{"rows": 42}') == 42
