"""G4 · 导出审计留痕测试（assert audit row written per export）。

不验证脱敏内容（G2 已覆盖），只验证：每次导出都会写一条 ads_export_audit 行，
且 detail 里带上筛选参数与行数、result=success、ip 来自请求来源。

用轻量 stub handler 替代真实 HTTP 请求；db.alerts / db.write_audit 通过
monkeypatch 接管——审计行写入是本测试断言的对象，不做 mock（记录真实调用参数）。
"""

import urllib.parse

import app as frontend_app
import db


class _StubHandler:
    """最小 handler：仅供 _handle_export 调用所需属性。"""

    def __init__(self, ip="203.0.113.7"):
        self.client_address = (ip, 0)
        self.sent = {}

    def _send(self, code, body=b"", ctype="text/csv", extra=None):
        self.sent = {"code": code, "body": body, "ctype": ctype, "extra": extra}


def _fake_alerts(*args, **kwargs):
    return {
        "total": 3,
        "rows": [
            {
                "src": "offline",
                "loan_id": 1,
                "customer_id": "C10001",
                "collateral_id": "K200",
                "ltv": 0.9,
                "loan_balance": 100.0,
                "market_valuation": 111.0,
                "risk_class": "次级",
                "is_high_risk_zone": 0,
                "alert_date": "2026-08-05",
                "property_addr": "广州市黄埔区",
                "confirmed": None,
                "alert_level": "warn",
            },
            {
                "src": "offline",
                "loan_id": 2,
                "customer_id": "C10002",
                "collateral_id": "K201",
                "ltv": 0.95,
                "loan_balance": 200.0,
                "market_valuation": 210.0,
                "risk_class": "可疑",
                "is_high_risk_zone": 1,
                "alert_date": "2026-08-05",
                "property_addr": "深圳市南山区",
                "confirmed": None,
                "alert_level": "strong",
            },
            {
                "src": "offline",
                "loan_id": 3,
                "customer_id": "C10003",
                "collateral_id": "K202",
                "ltv": 0.8,
                "loan_balance": 300.0,
                "market_valuation": 375.0,
                "risk_class": "正常",
                "is_high_risk_zone": 0,
                "alert_date": "2026-08-05",
                "property_addr": "东莞市",
                "confirmed": None,
                "alert_level": None,
            },
        ],
    }


def test_export_writes_audit_row(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "alerts", _fake_alerts)
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))

    handler = _StubHandler()
    parsed = urllib.parse.urlparse("/api/alerts/export?risk_class=可疑")
    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}

    frontend_app.SpaceFinApp._handle_export(handler, parsed, user)

    # 必须有且仅有一条审计写入。
    assert len(calls) == 1
    action, username, role, detail, result, ip = calls[0]
    assert action == "export"
    assert username == "admin"
    assert role == "admin"
    assert result == "success"
    assert ip == "203.0.113.7"

    import json

    detail_obj = json.loads(detail)
    assert detail_obj["risk_class"] == "可疑"
    assert detail_obj["rows"] == 3  # 行数应带出

    # CSV 返回体应包含脱敏后的客户号（c****）且保留 loan_id 明文（G2 口径）。
    body = handler.sent["body"].decode("utf-8")
    assert "c****0001" in body
    assert "c****0002" in body
    assert "c****0003" in body
    assert ",1," in body or ",1\n" in body  # loan_id 明文
