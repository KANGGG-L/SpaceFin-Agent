"""audit_final_failed（G4）：终态失败计数 + Slack 告警分支（假 DB / Slack）。

不连真实库：FakeConn 顶替 pymysql.connect，RecordingPost 顶替 post_slack。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTING_DIR = os.path.dirname(TESTS_DIR)
ORCH_DIR = os.path.join(os.path.dirname(ALERTING_DIR), "orchestrator")
RISK_DIR = os.path.join(os.path.dirname(ALERTING_DIR), "risk")


def _load():
    for d in (RISK_DIR, ORCH_DIR, ALERTING_DIR):
        while d in sys.path:
            sys.path.remove(d)
    sys.path.insert(0, RISK_DIR)
    sys.path.insert(0, ORCH_DIR)
    sys.path.insert(0, ALERTING_DIR)
    spec = importlib.util.spec_from_file_location(
        "audit_final_failed_under_test", os.path.join(ALERTING_DIR, "audit_final_failed.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


af = _load()


class _FakeCursor:
    def __init__(self, rows, conn):
        self._rows = rows
        self._conn = conn

    def execute(self, sql, params=None):
        self.sql = sql
        self._conn.last_sql = sql

    def fetchone(self):
        return (self._rows,)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.last_sql = ""

    def cursor(self):
        return _FakeCursor(self._rows, self)

    def close(self):
        pass


class _RecordingPost:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook, text):
        self.calls.append(text)
        return True


def _run(monkeypatch, n, webhook="https://x", mentions=""):
    post = _RecordingPost()
    monkeypatch.setattr(
        af, "pymysql", type("M", (), {"connect": staticmethod(lambda **k: _FakeConn(n))})()
    )
    monkeypatch.setattr(af, "post_slack", post)
    monkeypatch.setattr(
        os,
        "getenv",
        staticmethod(
            lambda k, d="": (
                webhook
                if k == "SPACEFIN_ALERT_SLACK_WEBHOOK"
                else (mentions if k == "SPACEFIN_ALERT_MENTIONS" else d)
            )
        ),
    )
    monkeypatch.setattr(sys, "argv", ["audit", "--date", "2026-08-05"])
    code = af.main()
    return code, post.calls


def test_count_final_failed_runs_query():
    conn = _FakeConn(3)
    assert af.count_final_failed(conn, "2026-08-05") == 3
    # 确认命中 ads_alert_dispatch 终态失败口径
    assert "ads_alert_dispatch" in conn.last_sql
    assert "status='failed'" in conn.last_sql
    assert "attempt_count" in conn.last_sql


def test_main_no_failures_no_alert(monkeypatch):
    code, calls = _run(monkeypatch, 0)
    assert code == 0
    assert calls == []


def test_main_has_failures_alerts(monkeypatch):
    code, calls = _run(monkeypatch, 2)
    assert code == 0
    assert len(calls) == 1
    assert "终态失败" in calls[0]
    assert "2 条" in calls[0]
    # 负责角色应为产品/审核
    assert "产品/审核" in calls[0]
