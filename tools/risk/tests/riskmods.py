"""加载 tools/risk 的平铺模块 + 假 DB 连接。测试从这里取被测对象。

为什么不直接写在 conftest.py 里、让测试 `from conftest import ...`：
仓库里已有 tools/orchestrator/tests/conftest.py 与 tools/anjuke_crawler/tests/conftest.py，
两者都用 `from conftest import ...`。这些目录都没有 __init__.py，pytest 的 prepend
导入模式下 `conftest` 是个平铺模块名，同一会话里跑多个目录时先到先得，后面的目录会
拿到别人的 conftest（现状：`pytest tools/orchestrator/tests tools/anjuke_crawler/tests`
会直接 ImportError）。本文件用唯一模块名规避该冲突，跨目录整仓跑不受影响。

同理，业务代码用的是 `import config` / `import risk_engine` 这种平铺导入，而
tools/risk/config.py 与 tools/spatial/config.py 同名、三个 main.py 同名。故加载前
先清掉这些平铺名并把 tools/risk 顶到 sys.path[0]；模块加载完后在自己的 globals 里
持有正确引用，之后别的目录再污染平铺名也不影响已加载的模块。
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
RISK_DIR = os.path.dirname(TESTS_DIR)

_FLAT_NAMES = ("config", "valuation", "risk_engine", "store", "main")


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    while RISK_DIR in sys.path:
        sys.path.remove(RISK_DIR)
    sys.path.insert(0, RISK_DIR)

    import config as _config
    import risk_engine as _risk_engine
    import store as _store
    import valuation as _valuation

    return _config, _valuation, _risk_engine, _store


config, valuation, risk_engine, store = _load()


# ------------------------------------------------------------------ 假 DB
#
# store.py / main.py 的读写函数都收 pymysql 连接。这里仿 DB-API 的最小子集，
# 把执行过的 SQL 与参数全录下来：测试只断言「发出了什么语义的 SQL、参数对不对」，
# 不去验证 MySQL 自身行为，也就不需要真库。


class FakeCursor:
    """DB-API 游标最小子集。结果集由 FakeConn.queue_result 预置，每次 execute 弹一批。

    未预置时 fetchall 返回空表——对应「表还没建 / 当日无数据」这类真实场景。
    """

    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((_norm(sql), params))
        got = self._conn._next_result()
        self._rows, self.description = got if got is not None else ([], None)
        return len(self._rows)

    def executemany(self, sql, seq):
        seq = list(seq)
        self._conn.executed_many.append((_norm(sql), seq))
        self._rows, self.description = [], None
        return len(seq)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self._conn.closed_cursors += 1


class FakeConn:
    """录制型假连接：记录所有 SQL / 参数 / commit 次数，按队列返回预置结果集。"""

    def __init__(self):
        self.executed = []  # [(sql, params)]
        self.executed_many = []  # [(sql, [params, ...])]
        self.commits = 0
        self.closed_cursors = 0
        self._results = []

    def queue_result(self, rows, columns=None):
        """预置下一次 execute 的结果集；给了 columns 才有 cursor.description。"""
        desc = [(c,) + (None,) * 6 for c in columns] if columns else None
        self._results.append((rows, desc))
        return self

    def _next_result(self):
        return self._results.pop(0) if self._results else None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    # ---- 断言辅助
    def find_sql(self, *keywords):
        """第一条包含全部 keyword 的 (sql, params)，找不到返回 None。"""
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None

    def find_many(self, *keywords):
        """第一条包含全部 keyword 的 executemany (sql, rows)，找不到返回 None。"""
        for sql, rows in self.executed_many:
            if all(k in sql for k in keywords):
                return sql, rows
        return None


class RaisingCursor(FakeCursor):
    """execute 直接抛错的游标：用于「表不存在」等降级路径。"""

    def execute(self, sql, params=None):
        raise RuntimeError(f"no such table (simulated): {_norm(sql)[:60]}")


class RaisingConn(FakeConn):
    def cursor(self):
        return RaisingCursor(self)


def _norm(sql):
    """把 SQL 压成单行，方便用关键字子串断言。"""
    return " ".join(sql.split())
