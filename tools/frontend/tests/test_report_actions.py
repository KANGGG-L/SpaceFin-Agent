"""1104 报送页「可操作闭环」测试：recheck / rebuild / alerts q 搜索（零 DB：FakeConn 录制型）。

覆盖契约：
1. POST /api/report/recheck：实时复算口径一致性，一致/不一致两条路径，返回含 checked_at；
2. POST /api/report/rebuild：从 dws_risk_class 聚合重建 ads_1104_g11 快照（单事务
   DELETE+INSERT），返回 rebuilt_rows/consistent，且必须落 report_rebuild 审计；
   admin 才可，postloan 等角色 403；表结构缺列/非五级档位 → 明确错误而非 500；
3. GET /api/alerts 新增 q 搜索：keyword 参数透传 db.alerts()，且 WHERE 走参数化 LIKE。
"""

import datetime
import json
import urllib.parse

import app as frontend_app
import db
import pytest
from fakeconn import FakeConn

# ---------------------------------------------------------------- 假库构造


def _recheck_conn(mismatch=False):
    """recheck 用的假库：g11 快照 3 行（五级两档 + 合计）+ dws 聚合 + NOW()。"""
    conn = FakeConn()
    conn.when("SELECT MAX(stat_date) FROM ads_1104_g11", [(datetime.date(2026, 8, 6),)])
    g11_normal_count = 101 if mismatch else 100
    conn.when(
        "SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, is_total, etl_ts "
        "FROM ads_1104_g11",
        [
            ("2026-08-06", "正常", g11_normal_count, 1000.0, 0.5, 0, "2026-08-06 02:00:00"),
            ("2026-08-06", "关注", 50, 500.0, 0.25, 0, "2026-08-06 02:00:00"),
            ("2026-08-06", "合计", 150, 1500.0, 1.0, 1, "2026-08-06 02:00:00"),
        ],
    )
    conn.when(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) FROM dws_risk_class",
        [("正常", 100, 1000.0), ("关注", 50, 500.0)],
    )
    conn.when("SELECT NOW()", [(datetime.datetime(2026, 8, 6, 4, 0, 0),)])
    return conn


_G11_COLS = [
    ("stat_date",),
    ("risk_class",),
    ("loan_count",),
    ("balance_total",),
    ("balance_pct",),
    ("is_total",),
    ("etl_ts",),
]


def _rebuild_conn():
    """rebuild 用的假库：dws 明细五级齐全 + 结构完整 + NOW()。"""
    conn = FakeConn()
    conn.when("SELECT MAX(stat_date) FROM ads_1104_g11", [(datetime.date(2026, 8, 6),)])
    conn.when("SHOW COLUMNS FROM ads_1104_g11", _G11_COLS)
    conn.when(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) FROM dws_risk_class",
        [
            ("正常", 100, 1000.0),
            ("关注", 50, 500.0),
            ("次级", 20, 200.0),
            ("可疑", 10, 100.0),
            ("损失", 5, 50.0),
        ],
    )
    conn.when("SELECT NOW()", [(datetime.datetime(2026, 8, 6, 4, 30, 0),)])
    return conn


# ---------------------------------------------------------------- db.report_recheck


def test_recheck_consistent(monkeypatch):
    conn = _recheck_conn()
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    out = db.report_recheck()  # date 缺省 → 取 MAX(stat_date)
    assert out["date"] == "2026-08-06"
    assert out["consistent"] is True
    assert out["mismatches"] == []
    # checked_at 来自 SQL 侧 NOW()，不带 naive 时间在 Python 里减。
    assert out["checked_at"] == "2026-08-06 04:00:00"


def test_recheck_inconsistent(monkeypatch):
    conn = _recheck_conn(mismatch=True)
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    out = db.report_recheck("2026-08-06")
    assert out["consistent"] is False
    assert "g11_vs_dws:正常:loan_count" in out["mismatches"]
    assert out["checked_at"] is not None


# ---------------------------------------------------------------- db.report_rebuild


def test_rebuild_rebuilds_snapshot_atomically(monkeypatch):
    conn = _rebuild_conn()
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    out = db.report_rebuild("2026-08-06")
    assert out["date"] == "2026-08-06"
    assert out["rebuilt_rows"] == 6  # 五级 + 合计行
    assert out["consistent"] is True  # 重建后与明细同源，恒一致
    assert out["mismatches"] == []
    assert out["etl_ts"] == "2026-08-06 04:30:00"

    # 单事务：关自动提交、DELETE + INSERT 落在同一连接、commit 恰好一次。
    assert conn.autocommit_mode is False
    assert conn.find_sql("DELETE FROM ads_1104_g11") is not None
    ins_sql, ins_params = conn.find_sql("INSERT INTO ads_1104_g11")
    assert ins_sql is not None
    assert len(ins_params) == 6
    assert ins_params[0][1] == "正常"  # CLASS_ORDER 排序，合计行在最后
    assert ins_params[-1][1] == "合计"
    assert conn.committed == 1


def test_rebuild_missing_column_raises_clear_error(monkeypatch):
    """ads_1104_g11 缺列 → ValueError（明确错误而非 500），且不产生 DELETE。"""
    conn = FakeConn()
    conn.when("SHOW COLUMNS FROM ads_1104_g11", _G11_COLS[:-2])  # 缺 is_total（必需列）
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    with pytest.raises(ValueError, match="缺少必要列"):
        db.report_rebuild("2026-08-06")
    assert conn.find_sql("DELETE FROM ads_1104_g11") is None


def test_rebuild_unknown_risk_class_raises(monkeypatch):
    """dws_risk_class 出现非五级档位 → 阻断重建（与 tools/reporting/main.py 同口径）。"""
    conn = FakeConn()
    conn.when("SHOW COLUMNS FROM ads_1104_g11", _G11_COLS)
    conn.when(
        "SELECT risk_class, COUNT(*), COALESCE(SUM(balance),0) FROM dws_risk_class",
        [("正常", 100, 1000.0), ("杂项", 5, 50.0)],
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    with pytest.raises(ValueError, match="非五级"):
        db.report_rebuild("2026-08-06")


# ---------------------------------------------------------------- db.report（回归）


def test_report_still_returns_rows_alerts(monkeypatch):
    """重构后 report() 行为不变：行 + 校验 + 阻断告警历史。"""
    conn = _recheck_conn()
    conn.when(
        "SELECT id, report_date, alert_level, check_name, detail, etl_ts FROM ads_report_alert",
        [
            (
                7,
                datetime.date(2026, 8, 6),
                "block",
                "1104_vs_dws:正常:balance",
                "口径不一致: 1104_vs_dws:正常:balance",
                datetime.datetime(2026, 8, 6, 3, 3, 39),
            )
        ],
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)

    out = db.report("2026-08-06")
    assert out["date"] == "2026-08-06"
    assert len(out["rows"]) == 3
    assert out["consistent"] is True
    assert out["alerts"][0]["check_name"] == "1104_vs_dws:正常:balance"


# ---------------------------------------------------------------- app 路由与权限


class _RoutedHandler(frontend_app.SpaceFinApp):
    """不建真实 socket 的 handler stub：够 do_POST 沿真实分发走到目标路由即可。

    与既有测试同套路：直接挂 SpaceFinApp 的方法，重写发送/取数口子。
    """

    def __init__(self, user, body=None, path=""):
        self._user = user
        self._body = body or {}
        self.path = path
        self.client_address = ("203.0.113.7", 0)
        self.sent = None

    def _current_user(self):
        return self._user

    def _parse_body(self):
        return self._body

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.sent = {"code": code, "body": body, "ctype": ctype, "extra": extra}

    def _send_json(self, code, data, extra=None):
        self._send(
            code,
            json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
            extra=extra,
        )

    def _send_error(self, code, msg):
        self._send_json(code, {"error": msg})


def test_handle_recheck_route_allows_admin(monkeypatch):
    """recheck 只读重算：融合后仅 admin 可访问（与 report 同权限）。"""
    monkeypatch.setattr(
        db,
        "report_recheck",
        lambda *a, **k: {
            "date": "2026-08-06",
            "consistent": True,
            "mismatches": [],
            "checked_at": "2026-08-06 04:00:00",
        },
    )
    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, body={"date": "2026-08-06"}, path="/api/report/recheck")
    h.do_POST()

    assert h.sent["code"] == 200
    out = json.loads(h.sent["body"])
    assert out["checked_at"] == "2026-08-06 04:00:00"


def test_handle_rebuild_success_writes_audit(monkeypatch):
    """rebuild 成功路径：返回 rebuilt_rows/consistent，且落 report_rebuild 审计。"""
    calls = []
    monkeypatch.setattr(
        db,
        "report_rebuild",
        lambda *a, **k: {
            "date": "2026-08-06",
            "rebuilt_rows": 6,
            "consistent": True,
            "mismatches": [],
            "etl_ts": "2026-08-06 04:30:00",
        },
    )
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))

    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, body={"date": "2026-08-06"}, path="/api/report/rebuild")
    h.do_POST()

    assert h.sent["code"] == 200
    out = json.loads(h.sent["body"])
    assert out["rebuilt_rows"] == 6 and out["consistent"] is True
    # 审计：action=report_rebuild / result=success / who=admin / ip 来自请求来源。
    assert len(calls) == 1
    action, username, role, detail, result, ip = calls[0]
    assert action == "report_rebuild"
    assert username == "admin" and role == "admin"
    assert result == "success"
    assert ip == "203.0.113.7"
    assert detail == "重建 G11 快照 2026-08-06 6 行"


def test_handle_rebuild_denied_for_postloan_403(monkeypatch):
    """rebuild 写操作：非 admin（如 postloan）→ 403，不触库。"""
    monkeypatch.setattr(db, "report_rebuild", lambda *a, **k: pytest.fail("不应调用写库"))
    user = {"user": "postloan", "role": "postloan", "label": "贷后资产保全"}
    h = _RoutedHandler(user, body={"date": "2026-08-06"}, path="/api/report/rebuild")
    h.do_POST()

    assert h.sent["code"] == 403
    assert "error" in json.loads(h.sent["body"])


def test_handle_rebuild_invalid_date_400_with_failure_audit(monkeypatch):
    """date 参数不合法 → 400 且留 failure 审计（与 confirm/dispose 失败留痕同口径）。"""
    calls = []
    monkeypatch.setattr(db, "report_rebuild", lambda *a, **k: pytest.fail("不应调用"))
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))

    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, body={"date": "garbage"}, path="/api/report/rebuild")
    h.do_POST()

    assert h.sent["code"] == 400
    assert len(calls) == 1
    assert calls[0][0] == "report_rebuild" and calls[0][4] == "failure"


# ---------------------------------------------------------------- alerts q 搜索


def test_handle_alerts_passes_keyword(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        db,
        "alerts",
        lambda **kw: captured.update(kw) or {"total": 0, "page": 1, "page_size": 20, "rows": []},
    )
    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, path="/api/alerts?q=loan123")
    h._handle_alerts(urllib.parse.urlparse("/api/alerts?q=loan123"))

    assert captured["keyword"] == "loan123"


def test_handle_alerts_without_q_keyword_none(monkeypatch):
    """q 缺省时行为不变：keyword 透传为 None，不追加任何搜索条件。"""
    captured = {}
    monkeypatch.setattr(
        db,
        "alerts",
        lambda **kw: captured.update(kw) or {"total": 0, "page": 1, "page_size": 20, "rows": []},
    )
    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, path="/api/alerts")
    h._handle_alerts(urllib.parse.urlparse("/api/alerts"))

    assert captured.get("keyword") is None


def test_alerts_keyword_builds_parameterized_like(monkeypatch):
    """db.alerts(keyword=...) 的 WHERE 走参数化 LIKE，值带 % 包裹，绝不拼 SQL。"""
    conn = FakeConn()
    conn.when("SELECT COUNT(*)", [(0,)])
    # 页查询也需要列头（COUNT=0 时仍执行 SELECT *，无 description 会崩）。
    conn.when(
        "ORDER BY alert_date DESC",
        [],
        columns=[
            "src",
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
            "alert_level",
        ],
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(db, "biz_conn", lambda: conn)

    db.alerts(keyword="ABC", page=1, page_size=10)

    sql, params = conn.find_sql("loan_id LIKE")
    assert sql is not None
    assert "customer_id LIKE" in sql
    assert "LIKE %s" in sql  # 占位符，值走参数
    assert params[0] == "%ABC%" and params[1] == "%ABC%"
