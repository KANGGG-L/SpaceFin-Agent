"""tools/frontend 页面测试公共 fixture（零 DB / 零 HTTP）。

被测页面模块在 pages/ 下，import 时会 `import db`（直读 MySQL 的连接层）。
测试把 `db.crawl_conn` 替换成录制型假连接（复刻 tools/cdc/tests 的思路），
让页面 handler 在无 MySQL 环境下也能验证聚合逻辑与降级路径。
"""

import os
import sys

import pytest

# 让 `import pages` 在任意 cwd 下都能命中 tools/frontend。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)


class _FakeCursor:
    """录制型游标：execute 的 SQL 含某片段 → 返回预置结果集。"""

    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        rows, desc = self._conn._result_for(sql)
        self._rows = list(rows)
        self.description = desc
        return len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    """录制型假连接：按 SQL 片段返回预置结果集，并记录全部执行过的 SQL。"""

    def __init__(self):
        self.executed = []  # [(sql, params)]
        self._rules = []

    def when(self, fragment, rows, columns=None):
        desc = [(c,) + (None,) * 6 for c in columns] if columns else None
        self._rules.append((fragment, rows or [], desc))
        return self

    def _result_for(self, sql):
        for fragment, rows, desc in self._rules:
            if fragment in sql:
                return rows, desc
        return [], None

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass

    def find_sql(self, *keywords):
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None


@pytest.fixture
def conn():
    return FakeConn()


@pytest.fixture
def ctx():
    """模拟 app.py 分发给插件的 RouteCtx（只读字段，页面 handler 只用这些）。"""

    class _Ctx:
        query = {}
        body = {}
        ip = "127.0.0.1"
        user = {"user": "admin", "role": "admin", "label": "系统管理员"}

    return _Ctx()
