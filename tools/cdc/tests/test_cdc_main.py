"""tools/cdc/main.py 的最小测试集：位点持久化/断点续读、事件序列化、告警判定。

零 DB：BinLogStreamReader 由 monkeypatch 换成假流，pymysql 连接换成录制型假连接。
"""

import sys
from datetime import date, datetime
from decimal import Decimal

import cdcmods
import pytest

cdc_main = cdcmods.cdc_main


# ---------------------------------------------------------------- _fix_str / _sanitize
_MOJIBAKE = "中文".encode().decode("latin-1")  # utf8mb4 被 latin-1 错解码的真实乱码串


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", "abc123"),  # 纯 ASCII 反转后不变
        ("中文", "中文"),  # 已正确解码的非 ASCII encode('latin-1') 抛错 → 原样返回
        ("café", "café"),  # latin-1 字节重编码后不是合法 utf-8 → 原样返回
        (_MOJIBAKE, "中文"),  # utf8mb4 被 latin-1 错解码的乱码 → 还原
    ],
)
def test_fix_str(raw, expected):
    assert cdc_main._fix_str(raw) == expected


def test_sanitize_converts_native_types():
    src = {
        "loan_id": 1,
        "amount": Decimal("1500000.50"),
        "signed_at": datetime(2026, 8, 5, 12, 0, 0),
        "due_date": date(2026, 9, 1),
        "blob": b"\xe4\xb8\xad",
        "tags": ["a", 2, None],
    }
    out = cdc_main._sanitize(src)
    assert out["amount"] == 1500000.5
    assert out["signed_at"] == "2026-08-05T12:00:00"
    assert out["due_date"] == "2026-09-01"
    assert out["blob"] == "中"  # b"\xe4\xb8\xad" 即「中」的 utf-8 字节
    assert out["tags"] == ["a", 2, None]
    assert isinstance(out["signed_at"], str)


def test_sanitize_marks_circular_reference():
    d = {"k": 1}
    d["self"] = d
    out = cdc_main._sanitize(d)
    assert out["k"] == 1
    assert out["self"] == "<circular:dict>"


def test_sanitize_applies_mojibake_fix():
    assert cdc_main._sanitize(_MOJIBAKE) == "中文"


# ---------------------------------------------------------------- _rename_cols
def test_rename_cols_maps_unknown_cols_by_index():
    row = {"UNKNOWN_COL0": 1, "UNKNOWN_COL1": "x"}
    assert cdc_main._rename_cols(row, ["loan_id", "customer_id"]) == {
        "loan_id": 1,
        "customer_id": "x",
    }


def test_rename_cols_keeps_known_and_out_of_range_keys():
    row = {"UNKNOWN_COL0": 1, "UNKNOWN_COL9": 9, "loan_id": 2}
    out = cdc_main._rename_cols(row, ["loan_id"])
    assert out["loan_id"] == 2
    assert out["UNKNOWN_COL9"] == 9  # 超出列名表 → 保留原名


def test_rename_cols_empty_row_passthrough():
    assert cdc_main._rename_cols({}, ["a"]) == {}


# ---------------------------------------------------------------- _pos_ahead（binlog 位点比较）
@pytest.mark.parametrize(
    "fa, pa, fb, pb, expected",
    [
        (None, 0, "binlog.000001", 4, False),  # 任一为空 → 不领先
        ("binlog.000003", 4, "binlog.000002", 999, True),  # 文件名字典序即时间序
        ("binlog.000002", 999, "binlog.000003", 4, False),
        ("binlog.000005", 200, "binlog.000005", 100, True),  # 同名文件按 pos
        ("binlog.000005", 100, "binlog.000005", 100, False),  # 严格领先，相等不算
    ],
)
def test_pos_ahead(fa, pa, fb, pb, expected):
    assert cdc_main._pos_ahead(fa, pa, fb, pb) is expected


# ---------------------------------------------------------------- 位点持久化（断点续读核心）
def test_write_position_upserts_and_commits(conn):
    cdc_main.write_position(conn, "binlog.000007", 777)
    sql, params = conn.find_sql("INSERT INTO ods_cdc_position")
    assert sql is not None
    # repl_key='binlog' 是 SQL 字面量；%s 只绑 log_file/log_pos
    assert "VALUES ('binlog', %s, %s)" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params == ("binlog.000007", 777)
    assert conn.commits == 1


def test_read_position_returns_persisted_row(conn):
    conn.when("log_file, log_pos, updated_at", [("binlog.000003", 100, "2026-08-05 00:00:00")])
    assert cdc_main.read_position(conn) == ("binlog.000003", 100, "2026-08-05 00:00:00")


def test_read_position_empty_returns_none(conn):
    # 首启：无持久化位点 → None，main() 走「从主库当前头开始」
    conn.when("log_file, log_pos, updated_at", [])
    assert cdc_main.read_position(conn) is None


# ---------------------------------------------------------------- 幂等建表
@pytest.mark.parametrize(
    "fn, table",
    [
        (cdc_main._ensure_log_table, cdc_main.LOG_TABLE),
        (cdc_main._ensure_position_table, cdc_main.POSITION_TABLE),
        (cdc_main._ensure_alert_table, cdc_main.ALERT_TABLE),
    ],
)
def test_ensure_tables_idempotent_ddl(conn, fn, table):
    fn(conn)
    sql, _ = conn.find_sql("CREATE TABLE IF NOT EXISTS", table)
    assert sql is not None
    assert conn.commits == 1


# ---------------------------------------------------------------- 主库位点 / 告警
def test_master_position(conn):
    conn.when("SHOW MASTER STATUS", [("binlog.000009", 123)])
    assert cdc_main.master_position(conn) == ("binlog.000009", 123)


def test_master_position_empty_returns_none_zero(conn):
    conn.when("SHOW MASTER STATUS", [])
    assert cdc_main.master_position(conn) == (None, 0)


def test_write_alert_throttled_when_recent_exists(conn):
    conn.when("SELECT COUNT(*) FROM ads_cdc_alert", [(1,)])
    assert cdc_main.write_alert(conn, "binlog_lag", "detail") is False
    assert conn.find_sql("INSERT INTO ads_cdc_alert") is None  # 节流窗口内 → 不落库


def test_write_alert_inserts_when_clear(conn):
    conn.when("SELECT COUNT(*) FROM ads_cdc_alert", [(0,)])
    assert cdc_main.write_alert(conn, "binlog_lag", "detail") is True
    sql, params = conn.find_sql("INSERT INTO ads_cdc_alert")
    assert params == ("binlog_lag", "detail")
    assert conn.commits == 1


def test_check_alerts_no_position_and_low_consumer_lag(conn):
    conn.when("log_file, log_pos, updated_at", [])
    conn.when("ods_cdc_consumer_offset", [(10,)])  # lag = 10 - 0 = 10 < 1000
    assert cdc_main._check_alerts(conn) == []


def test_check_alerts_stale_position_and_consumer_lag_both_alert(conn):
    conn.when("log_file, log_pos, updated_at", [("binlog.000005", 100, "2026-08-05 00:00:00")])
    conn.when("SHOW MASTER STATUS", [("binlog.000005", 500)])  # 主库领先
    conn.when("TIMESTAMPDIFF", [("2000",)])  # 停滞 2000s > 1800s
    conn.when("ods_cdc_consumer_offset", [(1500,)])  # lag = 1500 > 1000
    conn.when("SELECT COUNT(*) FROM ads_cdc_alert", [(0,)])  # 两次 write_alert 都不被节流
    written = cdc_main._check_alerts(conn)
    assert written == ["binlog_lag", "consumer_lag"]


def test_check_alerts_master_equal_no_lag_alert(conn):
    conn.when("log_file, log_pos, updated_at", [("binlog.000006", 900, "2026-08-05 00:00:00")])
    conn.when("SHOW MASTER STATUS", [("binlog.000006", 900)])  # 与 CDC 位点持平
    conn.when("ods_cdc_consumer_offset", [(10,)])
    assert cdc_main._check_alerts(conn) == []


def test_check_alerts_stale_below_threshold_no_lag_alert(conn):
    conn.when("log_file, log_pos, updated_at", [("binlog.000005", 100, "2026-08-05 00:00:00")])
    conn.when("SHOW MASTER STATUS", [("binlog.000005", 500)])
    conn.when("TIMESTAMPDIFF", [("100",)])  # 停滞 100s <= 1800s → 不告警
    conn.when("ods_cdc_consumer_offset", [(10,)])
    assert cdc_main._check_alerts(conn) == []


# ---------------------------------------------------------------- main() --once 断点续跑
def test_main_once_resumes_from_persisted_position_and_advances(monkeypatch, conn, capsys):
    """整链路断点语义：启动读回位点 → 从该位点续读 → 事件落 ODS/日志表 → 新位点落表。"""

    class FakeRowEvent:
        def __init__(self, table, rows):
            self.table = table
            self.rows = rows

    class FakeStream:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.log_file = "binlog.000007"
            self.log_pos = 777
            self.closed = False
            self._events = iter(
                [FakeRowEvent("loan", [{"values": {"loan_id": 1, "balance": 1000.0}}])]
            )
            FakeStream.instances.append(self)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._events)

        def close(self):
            self.closed = True

    class DummyThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    lake_calls, log_calls = [], []

    monkeypatch.setattr(cdc_main, "WriteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "UpdateRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "DeleteRowsEvent", FakeRowEvent)
    monkeypatch.setattr(cdc_main, "BinLogStreamReader", FakeStream)
    monkeypatch.setattr(cdc_main.threading, "Thread", DummyThread)
    monkeypatch.setattr(cdc_main, "_column_names", lambda c, s, t: ["loan_id", "balance"])
    monkeypatch.setattr(cdc_main, "_write_ods_lake", lambda *a, **k: lake_calls.append(a))
    monkeypatch.setattr(cdc_main, "_insert_log", lambda *a, **k: log_calls.append(a))
    monkeypatch.setattr(cdc_main.pymysql, "connect", lambda **k: conn)

    # 持久化位点：上次停在 binlog.000003:100
    conn.when("log_file, log_pos, updated_at", [("binlog.000003", 100, "2026-08-05 00:00:00")])

    monkeypatch.setattr(sys, "argv", ["cdc_main.py", "--once"])
    cdc_main.main()

    stream = FakeStream.instances[0]
    # 断点续读：BinLogStreamReader 收到持久化位点
    assert stream.kwargs["log_file"] == "binlog.000003"
    assert stream.kwargs["log_pos"] == 100
    assert stream.closed
    # 事件先落 ODS 湖与日志表
    assert lake_calls and log_calls
    # 再进位点：新位点是流的最新位点
    sql, params = conn.find_sql("INSERT INTO ods_cdc_position")
    assert params == ("binlog.000007", 777)
    assert "done, 1 events" in capsys.readouterr().out
