"""crawl_quality_gate：决策逻辑 + 端到端 main 流程（monkeypatch DB / Slack）。

不连真实库：用 FakeConn 顶替 pymysql.connect，RecordingPost 顶替 post_slack。
"""

import importlib.util
import os
import sys

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/orchestrator
SPEC = importlib.util.spec_from_file_location(
    "crawl_quality_gate_under_test", os.path.join(ORCH_DIR, "crawl_quality_gate.py")
)
cg = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cg
sys.path.insert(0, ORCH_DIR)  # so `from slack_notify import ...` resolves
SPEC.loader.exec_module(cg)


def test_decide_branches():
    # 硬下限：默认(False) 放行 exit 0；严格(True) 拦停 exit 2
    assert cg.decide(10, 50, 200, False) == (0, "hard")
    assert cg.decide(10, 50, 200, True) == (2, "hard")
    # 软下限：告警放行
    assert cg.decide(100, 50, 200, False) == (0, "soft")
    # 正常
    assert cg.decide(300, 50, 200, False) == (0, "ok")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (self._rows,)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        self.closed = True


class _RecordingPost:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook, text):
        self.calls.append(text)
        return True


def _run(monkeypatch, new_rows, strict, *, hard=50, soft=200, webhook="https://x", mentions=""):
    post = _RecordingPost()
    monkeypatch.setattr(
        cg, "pymysql", type("M", (), {"connect": staticmethod(lambda **k: _FakeConn(new_rows))})()
    )
    monkeypatch.setattr(cg, "post_slack", post)
    monkeypatch.setattr(cg.config, "load_env", staticmethod(lambda: {}))
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
    argv = ["gate", "--date", "2026-08-05", f"--hard-floor={hard}", f"--soft-floor={soft}"]
    if strict:
        argv.append("--strict")
    monkeypatch.setattr(sys, "argv", argv)
    code = cg.main()
    return code, post.calls


def test_main_ok_no_alert(monkeypatch):
    code, calls = _run(monkeypatch, 300, strict=False)
    assert code == 0
    assert calls == []


def test_main_soft_floor_warns_but_passes(monkeypatch):
    code, calls = _run(monkeypatch, 100, strict=False)
    assert code == 0
    assert len(calls) == 1
    assert "偏低" in calls[0]


def test_main_hard_floor_default_passes_with_alert(monkeypatch):
    code, calls = _run(monkeypatch, 10, strict=False)
    assert code == 0
    assert len(calls) == 1
    assert "缺失" in calls[0]


def test_main_hard_floor_strict_blocks(monkeypatch):
    code, calls = _run(monkeypatch, 10, strict=True)
    assert code == 2
    assert len(calls) == 1
    assert "缺失" in calls[0]  # Slack 详情文案，而非 stdout 的「拦停」
