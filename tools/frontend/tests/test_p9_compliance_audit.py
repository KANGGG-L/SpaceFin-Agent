"""P9 合规审计/特征归因页的最小测试集（零 DB：db.crawl_conn 换成录制型假连接）。

覆盖三个验收要点：
1. PAGE 契约正确（admin/risk 可见、路由 /api/compliance_audit）；
2. 导出审计行能正确解析 detail JSON 里的 rows 字段；
3. 归因报告缺失时页面降级为 available=False 且带重建命令，绝不 500。
"""

import datetime
import json

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
    """预置一张含 2 行导出审计 + 1 条阻断告警的假库。"""
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
    conn.when(
        "SELECT COUNT(*) FROM ads_report_alert WHERE alert_level='block'",
        [(3,)],
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
            )
        ],
        columns=_ALERT_COLS,
    )
    return conn


def _missing_attribution(monkeypatch, tmp_path):
    """把归因报告指向不存在的路径，模拟 output/ 未生成（克隆后常态）。"""
    monkeypatch.setattr(mod, "_ATTRIBUTION_FILE", str(tmp_path / "no_attribution.json"))


def _write_attribution(monkeypatch, tmp_path, payload):
    p = tmp_path / "attribution_report.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mod, "_ATTRIBUTION_FILE", str(p))


# ---------------------------------------------------------------- PAGE 契约
def test_page_contract_admin_risk_and_route():
    assert mod.PAGE["id"] == "compliance_audit"
    assert mod.PAGE["roles"] == {"admin", "risk"}
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


def test_report_alerts_only_block_level(monkeypatch, conn, ctx):
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    out = mod.get_compliance_audit(ctx)

    assert out["report_alert_total"] == 3
    assert len(out["report_alerts"]) == 1
    alert = out["report_alerts"][0]
    assert alert["alert_level"] == "block"
    assert alert["check_name"] == "1104_vs_dws:正常:balance"


def test_queries_limit_recent_rows(monkeypatch, conn, ctx):
    """页面只拉最近 PAGE_LIMIT 条，不给 MySQL 全表扫描。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    mod.get_compliance_audit(ctx)
    sql, params = conn.find_sql("ORDER BY id DESC", "LIMIT")
    assert sql is not None
    assert params[0] == mod.PAGE_LIMIT


# ---------------------------------------------------------------- 特征归因降级
def test_attribution_missing_degrades_not_500(monkeypatch, conn, ctx, tmp_path):
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    _missing_attribution(monkeypatch, tmp_path)
    out = mod.get_compliance_audit(ctx)

    assert out["attribution"]["available"] is False
    # 降级提示必须给出重建命令（output/ 是 .gitignore 目录，克隆后需重跑）。
    assert "tools/avm" in out["attribution"]["message"]


def test_attribution_available_returns_report(monkeypatch, conn, ctx, tmp_path):
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    _write_attribution(
        monkeypatch,
        tmp_path,
        {"summary": {"method": "SHAP", "features": [{"name": "area", "importance": 0.42}]}},
    )
    out = mod.get_compliance_audit(ctx)

    assert out["attribution"]["available"] is True
    assert out["attribution"]["report"]["summary"]["method"] == "SHAP"


# ---------------------------------------------------------------- detail rows 工具
def test_detail_rows_handles_bad_json():
    assert mod._detail_rows("not-json") is None
    assert mod._detail_rows(None) is None
    assert mod._detail_rows('{"rows": 42}') == 42
