"""R2-7 路线 B · detail 列 TEXT → MEDIUMTEXT 幂等迁移（零真实 DDL）。

用录制型假连接验证 ensure_export_audit_table 的三条不变式：
1. 当前 detail 为 TEXT（非 MEDIUMTEXT）时执行 ALTER ... MODIFY detail MEDIUMTEXT；
2. 已是 MEDIUMTEXT 时跳过 ALTER（幂等，重复调用不报错、不重复改）；
3. ALTER 抛「无需变更」类异常（Redundant / 1060）被静默忽略，不阻断建表主流程。
"""

import db
from fakeconn import FakeConn


def _mediumtext_conn(current_type):
    """假连接：INFORMATION_SCHEMA 返回 current_type，建表/查询均录制无副作用。"""
    conn = FakeConn()
    conn.when("INFORMATION_SCHEMA.COLUMNS", [(current_type,)], columns=["COLUMN_TYPE"])
    return conn


def test_alter_runs_when_detail_is_text(monkeypatch):
    conn = _mediumtext_conn("TEXT")
    monkeypatch.setattr(db, "ddl_conn", lambda: conn)
    db.ensure_export_audit_table()
    assert conn.find_sql("CREATE TABLE", "ads_export_audit"), "建表语句未执行"
    assert conn.find_sql("INFORMATION_SCHEMA.COLUMNS", "detail"), "未查 COLUMN_TYPE"
    assert conn.find_sql("ALTER TABLE", "MODIFY detail MEDIUMTEXT"), (
        "TEXT→MEDIUMTEXT 的 ALTER 未执行"
    )


def test_alter_skipped_when_already_mediumtext(monkeypatch):
    conn = _mediumtext_conn("MEDIUMTEXT")
    monkeypatch.setattr(db, "ddl_conn", lambda: conn)
    db.ensure_export_audit_table()
    assert not conn.find_sql("ALTER TABLE", "MODIFY detail MEDIUMTEXT"), (
        "已是 MEDIUMTEXT 仍执行了 ALTER（迁移应幂等）"
    )


def test_alter_redundant_exception_is_ignored(monkeypatch):
    """ALTER 报 Redundant/1060（列已是目标类型）属「无需变更」，必须静默忽略。"""

    class _BoomCursor:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=None):
            if "MODIFY detail MEDIUMTEXT" in " ".join(sql.split()):
                raise RuntimeError("1060 Duplicate column name / Redundant")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _BoomOnAlter(FakeConn):
        def cursor(self):
            return _BoomCursor(super().cursor())

    boom = _BoomOnAlter()
    boom.when("INFORMATION_SCHEMA.COLUMNS", [("text",)], columns=["COLUMN_TYPE"])
    monkeypatch.setattr(db, "ddl_conn", lambda: boom)
    # 不应抛异常：冗余异常被降级为告警，建表主流程照常完成。
    db.ensure_export_audit_table()
    assert boom.find_sql("CREATE TABLE", "ads_export_audit"), "冗余异常拖垮了建表主流程"
