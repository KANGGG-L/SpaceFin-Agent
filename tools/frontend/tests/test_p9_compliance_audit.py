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


# 与 output/avm/attribution_report.json 的真实结构一致（dev-attrib 2026-08-05-r11 产物：
# method=permutation_importance，top_features 数组含 importance_mean，full_importances 字典）。
_REAL_ATTR_PAYLOAD = {
    "version": "2026-08-05-r11",
    "method": "permutation_importance",
    "n_repeats": 5,
    "seed": 42,
    "n_test": 8041,
    "generated_at": "2026-08-05T20:14:00+08:00",
    "top_features": [
        {
            "feature": "comm_mean",
            "importance_mean": 11.312702,
            "importance_std": 0.271125,
            "chinese_name": "小区目标编码均值",
            "description": "同(城市,小区)训练折内 log 单价均值（OOF，防泄漏）",
        },
        {
            "feature": "comm_median",
            "importance_mean": 10.309426,
            "importance_std": 0.18751,
            "chinese_name": "小区目标编码中位数",
            "description": "同(城市,小区)训练折内 log 单价中位数",
        },
        {
            "feature": "city_code",
            "importance_mean": 7.123482,
            "importance_std": 0.124236,
            "chinese_name": "城市编码",
            "description": "广东 21 城码，HistGBR 类别特征",
        },
    ],
    "full_importances": {
        "comm_mean": {"importance_mean": 11.312702, "importance_std": 0.271125},
        "comm_median": {"importance_mean": 10.309426, "importance_std": 0.18751},
        "city_code": {"importance_mean": 7.123482, "importance_std": 0.124236},
    },
}


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


def test_attribution_available_returns_real_report(monkeypatch, conn, ctx, tmp_path):
    """真实报告结构：top_features 数组（importance_mean）+ full_importances 字典。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    _write_attribution(monkeypatch, tmp_path, _REAL_ATTR_PAYLOAD)
    out = mod.get_compliance_audit(ctx)

    assert out["attribution"]["available"] is True
    report = out["attribution"]["report"]
    # 方法字段动态来自报告本身，不硬编码。
    assert report["method"] == "permutation_importance"
    top = report["top_features"]
    assert len(top) == 3
    assert top[0]["feature"] == "comm_mean"
    assert top[0]["importance_mean"] > 0
    assert "importance_std" in top[0]
    # full_importances 是 {特征: {importance_mean, importance_std}} 字典形态。
    assert report["full_importances"]["city_code"]["importance_mean"] == 7.123482


def test_attribution_old_summary_shape_still_supported(monkeypatch, conn, ctx, tmp_path):
    """旧形态（summary.features）保留向后兼容，方法字段同样动态。"""
    monkeypatch.setattr(mod.db, "crawl_conn", lambda: _audit_conn(conn))
    _write_attribution(
        monkeypatch,
        tmp_path,
        {
            "summary": {
                "method": "permutation_importance",
                "features": [{"name": "area", "importance": 0.42}],
            }
        },
    )
    out = mod.get_compliance_audit(ctx)

    assert out["attribution"]["available"] is True
    summary = out["attribution"]["report"]["summary"]
    assert summary["method"] == "permutation_importance"
    assert summary["features"][0]["name"] == "area"


# ---------------------------------------------------------------- detail rows 工具
def test_detail_rows_handles_bad_json():
    assert mod._detail_rows("not-json") is None
    assert mod._detail_rows(None) is None
    assert mod._detail_rows('{"rows": 42}') == 42
