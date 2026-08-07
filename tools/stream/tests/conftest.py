"""tools/stream 测试公共 fixture。被测模块在 streammods.py（唯一模块名，原因见其顶部）。
本文件复用 tools/cdc/tests 的录制型假连接（等价实现放本地，避免跨目录导入）。"""

import pytest


class _FakeCursor:
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
    """录制型假连接：execute 的 SQL 含某片段 → 返回预置结果集。"""

    def __init__(self):
        self.executed = []  # [(sql, params)]
        self.commits = 0
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

    def commit(self):
        self.commits += 1

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
