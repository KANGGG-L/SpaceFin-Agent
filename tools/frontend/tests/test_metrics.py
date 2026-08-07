"""G8 · 健康度采集单测（无需 live server）。

collect_metrics() 是模块级函数，可直接调用：
1. 返回 dict 含全部 5 个键；
2. DB 不可用时 cdc_position_advance_ok=False / last_alert_count=0 优雅降级，不抛异常；
3. 内存超阈值(>MEM_DEGRADED_MB)时 status='degraded'，否则 'ok'（用 monkeypatch 控内存与 DB）。
"""

from types import SimpleNamespace

import app as frontend_app
import db


def test_metrics_keys_present():
    m = frontend_app.collect_metrics()
    assert set(m.keys()) == {
        "status",
        "uptime",
        "memory_mb",
        "cdc_position_advance_ok",
        "last_alert_count",
    }
    assert m["uptime"] >= 0


def test_metrics_db_unavailable_degrades_gracefully(monkeypatch):
    """DB 抖动：crawl_conn 抛异常 → 两个 DB 字段降级为 False/0，不 500。"""

    def _boom(*a, **k):
        raise RuntimeError("mysql down")

    monkeypatch.setattr(db, "crawl_conn", _boom)
    # 内存用真实 psutil（远低于阈值），status 应为 ok。
    m = frontend_app.collect_metrics()
    assert m["cdc_position_advance_ok"] is False
    assert m["last_alert_count"] == 0
    assert m["status"] == "ok"


def test_metrics_status_degraded_when_memory_high(monkeypatch):
    """内存被注入到超阈值 → status='degraded'；DB 不可用时 DB 字段仍优雅降级。"""

    def _boom(*a, **k):
        raise RuntimeError("mysql down")

    monkeypatch.setattr(db, "crawl_conn", _boom)

    # 让内存读数强制超阈值：patch os.getpid / psutil 路径都难，直接 patch collect_metrics
    # 内部的 memory_mb 计算不可达，改为 patch psutil 模块使其返回高 RSS。
    import sys

    fake_psutil = SimpleNamespace(
        Process=lambda pid: SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=5 * 1024 * 1024 * 1024)
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    m = frontend_app.collect_metrics()
    assert m["memory_mb"] > frontend_app.MEM_DEGRADED_MB
    assert m["status"] == "degraded"
    assert m["cdc_position_advance_ok"] is False


def test_metrics_status_ok_when_memory_low_and_db_fine(monkeypatch):
    """DB 正常且内存低 → status='ok' 且 cdc_position_advance_ok 依据位点评判。"""

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return self._rows.pop(0)

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            # 第一次调用返回位点 age=10（<1800 推进中），第二次返回告警计数=2。
            return _Cur([(10,), (2,)])

        def close(self):
            pass

    monkeypatch.setattr(db, "crawl_conn", lambda: _Conn())

    m = frontend_app.collect_metrics()
    assert m["cdc_position_advance_ok"] is True
    assert m["last_alert_count"] == 2
    assert m["status"] == "ok"
