"""S5 驾驶舱改造（信任卡 / 处置闭环 / 钻取 API）测试（零 DB：全部 mock 或纯函数）。

覆盖本批新增能力：
1. db._confirm_row_from：确认/处置记录结构（含「处置字段未建好」的降级推导）；
2. db.dashboard()：trust_cards（爬取规模/新鲜度/解析成功率/模型版本）+ alert_funnel；
3. db.alerts()：disposition 字段随行返回；
4. app._handle_detail / _handle_dispose：新 API 的参数校验、成功路径与审计留痕。

连库方式与 conftest 一致：FakeConn 按 SQL 片段回放预置结果集，无真实 MySQL。
"""

import json
import urllib.parse

import app as frontend_app
import db
import pytest
from conftest import FakeConn

# ---------------- _confirm_row_from（纯函数） ----------------


def test_confirm_row_with_disposition_columns():
    cols = {"disposition_status", "disposition_ts", "disposition_by"}
    row = (
        101,
        "2026-08-05",
        "offline",
        "risk",
        "2026-08-05 10:00:00",
        "disposed",
        "2026-08-05 11:00:00",
        "risk",
    )
    out = db._confirm_row_from(row, cols)
    assert out["confirmed_by"] == "risk"
    assert out["confirmed_ts"] == "2026-08-05 10:00:00"
    assert out["disposition"] == {
        "status": "disposed",
        "by": "risk",
        "ts": "2026-08-05 11:00:00",
    }


def test_confirm_row_without_disposition_columns_derives_confirmed():
    """处置字段未建好（data-dev 尚未同步）时，由 confirmed_by 推导为 confirmed。"""
    row = (101, "2026-08-05", "offline", "risk", "2026-08-05 10:00:00")
    out = db._confirm_row_from(row, set())
    assert out["disposition"]["status"] == "confirmed"
    assert out["disposition"]["by"] == "risk"


def test_dispose_alert_rejects_invalid_status(monkeypatch):
    """dispose_alert 只接受 disposed / recovered，其余值直接 ValueError。"""

    def _no_op(*a, **k):
        pass

    monkeypatch.setattr(db, "ensure_alert_confirm_table", _no_op)
    monkeypatch.setattr(db, "crawl_conn", lambda: _no_op)
    with pytest.raises(ValueError):
        db.dispose_alert(1, "2026-08-05", "offline", "pending", "risk")


# ---------------- db.dashboard()：信任卡 + 漏斗 ----------------


def _dashboard_fake_conn(has_disposition=True):
    conn = FakeConn()
    conn.when("COALESCE(SUM(balance)", [(200, 100000000.0, 10, 5, 3)])
    conn.when(
        "FROM ads_risk_class",
        [
            ("正常", 100, 1000.0, 0.5),
            ("关注", 50, 500.0, 0.25),
            ("次级", 30, 200.0, 0.15),
            ("可疑", 15, 100.0, 0.07),
            ("损失", 5, 50.0, 0.03),
        ],
    )
    conn.when("COALESCE(ltv, -1)", [(0.55,), (0.9,), (None,), (-1.0,)])
    conn.when("SELECT src, risk_class", [("offline", "次级", 6), ("offline", "可疑", 4)])
    conn.when("SELECT 'stream', risk_class", [("stream", "次级", 10), ("stream", "损失", 5)])
    conn.when("SELECT COUNT(*) FROM ads_ltv_alerts", [(34,)])
    conn.when("SELECT COUNT(*) FROM ads_stream_ltv_alerts", [(160,)])
    conn.when("SELECT r.collateral_id", [(1, 100000.0, 0), (2, 200000.0, 1)])
    conn.when("FROM collateral", [(1, "广州市黄埔区"), (2, "深圳市南山区")])
    conn.when("SELECT COUNT(*) FROM crawl_housing_sale", [(44369,)])
    conn.when("SELECT COUNT(*) FROM crawl_housing_rent", [(4244,)])
    conn.when("SELECT COUNT(*) FROM community_coords", [(13255,)])
    conn.when("SELECT COUNT(DISTINCT district)", [(21,)])
    conn.when(
        "TIMESTAMPDIFF(SECOND, MAX(etl_ts), NOW()) FROM crawl_housing_sale",
        [(44369, "2026-08-05 02:00:00", 160000)],
    )
    conn.when(
        "TIMESTAMPDIFF(SECOND, MAX(etl_ts), NOW()) FROM dws_risk_class",
        [(200, "2026-08-06 03:00:00", 68700)],
    )
    conn.when(
        "TIMESTAMPDIFF(SECOND, MAX(etl_ts), NOW()) FROM ads_ltv_alerts",
        [(34, "2026-08-06 03:00:00", 68700)],
    )
    conn.when(
        "geocode_status, COUNT(*) FROM crawl_housing_sale",
        [("hit", 12000), ("pending", 30000), ("miss", 500)],
    )
    conn.when(
        "geocode_status, COUNT(*) FROM crawl_housing_rent",
        [("hit", 1000), ("pending", 3000), ("miss", 100)],
    )
    conn.when(
        "GROUP BY model_version ORDER BY MAX(etl_ts) DESC",
        [("2026-08-05-r11", "2026-08-06 03:00:00", 200)],
    )
    conn.when("SELECT COUNT(*) FROM ads_alert_confirm", [(3,)])
    if has_disposition:
        conn.when(
            "SHOW COLUMNS", [("disposition_status",), ("disposition_ts",), ("disposition_by",)]
        )
        conn.when("disposition_status IN", [("disposed", 1), ("recovered", 1)])
    else:
        conn.when("SHOW COLUMNS", [])  # 空列 = 无处置字段
    return conn


def test_dashboard_trust_cards_and_funnel(monkeypatch):
    conn = _dashboard_fake_conn()
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(db, "biz_conn", lambda: conn)

    d = db.dashboard()
    assert "trust_cards" in d and "alert_funnel" in d

    tc = d["trust_cards"]
    assert tc["crawl_scale"] == {"sale": 44369, "rent": 4244, "coords": 13255, "cities": 21}
    assert tc["freshness"]["overall"] == "warn"
    by_layer = {ly["layer"]: ly for ly in tc["freshness"]["layers"]}
    assert by_layer["DWD"]["status"] == "warn"
    assert by_layer["DWS"]["status"] == "ok"
    assert by_layer["ADS"]["status"] == "ok"
    assert tc["parse_success"] == {
        "success": 13000,
        "failed": 600,
        "pending": 33000,
        "rate": 95.59,
    }
    assert tc["model_version"]["version"] == "2026-08-05-r11"
    assert tc["model_version"]["loan_count"] == 200

    # 漏斗：预警 = 离线 34 + 实时 160；确认/处置/恢复来自 ads_alert_confirm。
    assert d["alert_funnel"] == {
        "alert": 194,
        "confirmed": 3,
        "disposed": 1,
        "recovered": 1,
    }


def test_dashboard_funnel_degrades_without_disposition_column(monkeypatch):
    """处置字段缺失（data-dev 未同步）时漏斗不报错，disposed/recovered 降级为 0。"""
    conn = _dashboard_fake_conn(has_disposition=False)
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(db, "biz_conn", lambda: conn)

    d = db.dashboard()
    assert d["alert_funnel"]["disposed"] == 0
    assert d["alert_funnel"]["recovered"] == 0
    assert d["alert_funnel"]["confirmed"] == 3


# ---------------- db.alerts()：disposition 随行返回 ----------------


def test_alerts_rows_carry_disposition(monkeypatch):
    conn = FakeConn()
    conn.when("SELECT COUNT(*)", [(1,)])
    conn.when(
        "ORDER BY alert_date DESC",
        [("offline", 1, 101, "C1", 1, 100.0, 111.0, 0.9, "次级", 0, "2026-08-05", "warn")],
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
    conn.when("FROM collateral", [(1, "广州市黄埔区")])
    conn.when("SHOW COLUMNS", [("disposition_status",), ("disposition_ts",), ("disposition_by",)])
    conn.when(
        "FROM ads_alert_confirm",
        [
            (
                101,
                "2026-08-05",
                "offline",
                "risk",
                "2026-08-05 10:00:00",
                "disposed",
                "2026-08-05 11:00:00",
                "risk",
            )
        ],
    )
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(db, "biz_conn", lambda: conn)

    out = db.alerts(page=1, page_size=10)
    row = out["rows"][0]
    assert row["confirmed"]["confirmed_by"] == "risk"
    assert row["disposition"] == {
        "status": "disposed",
        "by": "risk",
        "ts": "2026-08-05 11:00:00",
    }


def test_alerts_unconfirmed_row_disposition_pending(monkeypatch):
    conn = FakeConn()
    conn.when("SELECT COUNT(*)", [(1,)])
    conn.when(
        "ORDER BY alert_date DESC",
        [("offline", 1, 101, "C1", 1, 100.0, 111.0, 0.9, "次级", 0, "2026-08-05", "warn")],
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
    conn.when("FROM collateral", [(1, "广州市黄埔区")])
    conn.when("SHOW COLUMNS", [("disposition_status",), ("disposition_ts",), ("disposition_by",)])
    conn.when("FROM ads_alert_confirm", [])  # 无确认记录
    monkeypatch.setattr(db, "crawl_conn", lambda: conn)
    monkeypatch.setattr(db, "biz_conn", lambda: conn)

    row = db.alerts(page=1, page_size=10)["rows"][0]
    assert row["confirmed"] is None
    assert row["disposition"]["status"] == "pending"


# ---------------- app handler：/api/alerts/detail / /api/alerts/dispose ----------------


class _Handler:
    """最小 handler stub：实现 _send/_send_json/_send_error/_parse_body 供 handler 调用。"""

    def __init__(self, body=None):
        self.body = body or {}
        self.client_address = ("203.0.113.7", 0)
        self.sent = {}

    def _parse_body(self):
        return self.body

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.sent = {"code": code, "body": body, "ctype": ctype, "extra": extra}

    def _send_json(self, code, data, extra=None):
        self._send(
            code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), extra=extra
        )

    def _send_error(self, code, msg):
        self._send_json(code, {"error": msg})


def _parse(path):
    return urllib.parse.urlparse(path)


def test_handle_detail_success(monkeypatch):
    payload = {
        "alert": {"loan_id": 101, "ltv": 0.9, "src": "offline"},
        "property_addr": "广州市",
        "risk_factors": {"valuation_deviation_pct": 0.12, "low_confidence": False},
        "confirm": None,
    }
    monkeypatch.setattr(db, "alert_detail", lambda *a, **k: payload)

    h = _Handler()
    frontend_app.SpaceFinApp._handle_detail(
        h, _parse("/api/alerts/detail?loan_id=101&alert_date=2026-08-05&source=offline")
    )
    assert h.sent["code"] == 200
    out = json.loads(h.sent["body"])
    assert out["alert"]["loan_id"] == 101
    assert out["risk_factors"]["valuation_deviation_pct"] == 0.12


def test_handle_detail_missing_params_400(monkeypatch):
    monkeypatch.setattr(db, "alert_detail", lambda *a, **k: None)
    h = _Handler()
    frontend_app.SpaceFinApp._handle_detail(h, _parse("/api/alerts/detail?loan_id=101"))
    assert h.sent["code"] == 400
    assert "error" in json.loads(h.sent["body"])


def test_handle_detail_not_found_404(monkeypatch):
    monkeypatch.setattr(db, "alert_detail", lambda *a, **k: None)
    h = _Handler()
    frontend_app.SpaceFinApp._handle_detail(
        h, _parse("/api/alerts/detail?loan_id=999&alert_date=2026-08-05&source=offline")
    )
    assert h.sent["code"] == 404


def test_handle_dispose_success(monkeypatch):
    calls = {"dispose": [], "audit": []}
    monkeypatch.setattr(db, "dispose_alert", lambda *a, **k: calls["dispose"].append(a))
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls["audit"].append(a))

    user = {"user": "risk", "role": "risk", "label": "风控策略经理"}
    h = _Handler(
        {"loan_id": 101, "alert_date": "2026-08-05", "source": "offline", "status": "disposed"}
    )
    frontend_app.SpaceFinApp._handle_dispose(h, user)

    assert h.sent["code"] == 200
    out = json.loads(h.sent["body"])
    assert out["ok"] is True and out["status"] == "disposed"
    # dispose_alert 收到的参数：loan_id / alert_date / source / status / user
    assert calls["dispose"][0][:4] == (101, "2026-08-05", "offline", "disposed")
    # 审计：action=dispose / result=success / who=risk / ip 来自 client_address
    action, username, role, detail, result, ip = calls["audit"][0]
    assert action == "dispose" and username == "risk" and result == "success"
    assert ip == "203.0.113.7"
    assert json.loads(detail)["status"] == "disposed"


def test_handle_dispose_invalid_status_400(monkeypatch):
    calls = {"audit": []}
    monkeypatch.setattr(db, "dispose_alert", lambda *a, **k: pytest.fail("不应调用"))
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls["audit"].append(a))

    user = {"user": "risk", "role": "risk", "label": "风控策略经理"}
    h = _Handler(
        {"loan_id": 101, "alert_date": "2026-08-05", "source": "offline", "status": "junk"}
    )
    frontend_app.SpaceFinApp._handle_dispose(h, user)

    assert h.sent["code"] == 400
    # 失败也留审计，方便追溯异常调用来源（与 confirm 失败留痕口径一致）。
    assert calls["audit"][0][4] == "failure"
