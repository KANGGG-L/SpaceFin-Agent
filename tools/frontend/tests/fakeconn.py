"""录制型假连接（唯一模块名，规避 tools/frontend/tests/conftest 与
tools/persona/tests/conftest 同名冲突：合并运行时 `from conftest import`
会命中先注册的那一个）。

复刻 tools/cdc/tests 的思路：按 SQL 片段回放预置结果集，记录全部执行过的
SQL，无真实 MySQL。普通页面测试改用 conftest 的 `conn` fixture；
需要手工构造多组假库的测试（test_report_actions / test_revamp_cockpit）
直接 `from fakeconn import FakeConn`。
"""

import os
import sys

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

    def executemany(self, sql, seq_params):
        # 批量写：与 execute 同口径录制（SQL + 参数序列），供测试断言写入内容。
        self._conn.executed.append((" ".join(sql.split()), seq_params))
        return len(seq_params)

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

    # ---- 事务三件套：供写路径（单事务 DELETE+INSERT）测试使用，均为无副作用录制 ----
    def commit(self):
        self.committed = getattr(self, "committed", 0) + 1

    def rollback(self):
        self.rolled_back = getattr(self, "rolled_back", 0) + 1

    def autocommit(self, enabled):
        self.autocommit_mode = enabled

    def find_sql(self, *keywords):
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None
