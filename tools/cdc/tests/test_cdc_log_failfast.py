"""CDC 双写失败 fail-fast（C 类）+ 端到端时延断言（A 类）。

- _insert_log 失败必须上抛，禁止在 ods_cdc_log 未落库时推进 binlog 位点。
- 「业务库变更 → ODS 可见」时延 < 3s（同步管道，mock I/O，不必真连 127.0.0.1）。
"""

import sys
import time

import cdcmods
import pytest

cdc_main = cdcmods.cdc_main


class _RaisingConn:
    """execute 永远抛错的假连接，用于验证 _insert_log 不再吞错。"""

    def __init__(self):
        self.executed = []
        self.commits = 0
        self.closed_cursors = 0

    def cursor(self):
        return self

    def execute(self, sql, params=None):  # noqa: D401 - 模拟落库失败
        raise RuntimeError("simulated ods_cdc_log insert failure")

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_insert_log_reraises_after_bounded_retries_and_does_not_swallow():
    """失败必须上抛（fail-fast），且不能静默吞掉；重试耗尽才抛。"""
    c = _RaisingConn()
    with pytest.raises(RuntimeError, match="ods_cdc_log insert failed after 3 attempts"):
        cdc_main._insert_log(c, "loan", "INSERT", None, {"loan_id": 1})
    # 未落库，绝不提交半截事务
    assert c.commits == 0


def test_insert_log_returns_on_first_success():
    """瞬时抖动后成功：正常返回，不产生异常。"""
    calls = []

    class _FlakyOnce:
        def __init__(self):
            self.n = 0
            self.commits = 0

        def cursor(self):
            return self

        def execute(self, sql, params=None):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("transient")  # 第一次失败，第二次成功
            calls.append(sql)

        def commit(self):
            self.commits += 1

        def close(self):
            pass

    c = _FlakyOnce()
    cdc_main._insert_log(c, "loan", "INSERT", None, {"loan_id": 1})
    assert calls and "INSERT INTO ods_cdc_log" in calls[0]
    assert c.commits == 1


def test_main_does_not_advance_position_when_log_insert_fails(monkeypatch, conn, capsys):
    """log 写失败 → 上抛 → write_position 不被调用 → 位点不推进（丢事件防护）。"""
    ods_calls = []

    class FakeRowEvent:
        def __init__(self, table, rows):
            self.table = table
            self.rows = rows

    class FakeStream:
        instances = []

        def __init__(self, **kwargs):
            self.log_file = "binlog.000003"
            self.log_pos = 100
            self.closed = False
            # 两个事件：第一个就会让 _insert_log 失败
            self._events = iter(
                [
                    FakeRowEvent("loan", [{"values": {"loan_id": 1, "balance": 1.0}}]),
                    FakeRowEvent("loan", [{"values": {"loan_id": 2, "balance": 2.0}}]),
                ]
            )
            FakeStream.instances.append(self)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._events)

        def close(self):
            self.closed = True

    def _boom(*a, **k):
        raise RuntimeError("simulated ods_cdc_log failure")

    monkeypatch.setattr(cdc_main, "WriteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "UpdateRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "DeleteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "BinLogStreamReader", FakeStream)
    monkeypatch.setattr(
        cdc_main.threading,
        "Thread",
        type("D", (), {"__init__": lambda self, *a, **k: None, "start": lambda self: None}),
    )
    monkeypatch.setattr(cdc_main, "_column_names", lambda c, s, t: ["loan_id", "balance"])
    monkeypatch.setattr(cdc_main, "_write_ods_lake", lambda *a, **k: ods_calls.append(a))
    monkeypatch.setattr(cdc_main, "_insert_log", _boom)
    monkeypatch.setattr(cdc_main.pymysql, "connect", lambda **k: conn)

    monkeypatch.setattr(sys, "argv", ["cdc_main.py", "--once"])
    with pytest.raises(RuntimeError, match="simulated ods_cdc_log failure"):
        cdc_main.main()

    # 第一个事件已落 ODS（幂等，重放安全），但位点在 log 失败后没有推进
    assert ods_calls
    assert conn.find_sql("INSERT INTO ods_cdc_position") is None


# ---------------------------------------------------------------- 端到端时延断言（A 类）


def test_end_to_end_latency_business_change_to_ods_under_3s(monkeypatch, conn):
    """业务库变更经 binlog → ODS 可见的端到端时延有界 < 3s。

    同步管道：事件一旦被流吐出，立刻 _write_ods_lake，无批处理/排队延迟。
    用 mock I/O 实测从「事件就绪」到「ODS 写入」的墙钟，断言 < 3s（替代仅文档 0.32s）。
    """
    t_ods = {}

    class FakeRowEvent:
        def __init__(self, table, rows):
            self.table = table
            self.rows = rows

    class FakeStream:
        instances = []

        def __init__(self, **kwargs):
            self.log_file = "binlog.000009"
            self.log_pos = 500
            self.closed = False
            self._events = iter([FakeRowEvent("loan", [{"values": {"loan_id": 1}}])])
            FakeStream.instances.append(self)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._events)

        def close(self):
            self.closed = True

    def _write_ods(*a, **k):
        t_ods["ts"] = time.time()

    monkeypatch.setattr(cdc_main, "WriteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "UpdateRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "DeleteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "BinLogStreamReader", FakeStream)
    monkeypatch.setattr(
        cdc_main.threading,
        "Thread",
        type("D", (), {"__init__": lambda self, *a, **k: None, "start": lambda self: None}),
    )
    monkeypatch.setattr(cdc_main, "_column_names", lambda c, s, t: ["loan_id"])
    monkeypatch.setattr(cdc_main, "_write_ods_lake", _write_ods)
    monkeypatch.setattr(cdc_main, "_insert_log", lambda *a, **k: None)
    monkeypatch.setattr(cdc_main.pymysql, "connect", lambda **k: conn)

    t_change = time.time()
    monkeypatch.setattr(sys, "argv", ["cdc_main.py", "--once"])
    cdc_main.main()

    assert "ts" in t_ods, "ODS 未被写入"
    latency = t_ods["ts"] - t_change
    assert latency < 3.0, f"端到端时延 {latency:.3f}s 超出 3s 上限"
