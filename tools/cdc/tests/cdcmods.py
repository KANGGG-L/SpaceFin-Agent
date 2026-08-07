"""加载 tools/cdc 的平铺模块 + 录制型假 DB。测试从这里取被测对象。

tools/cdc/main.py 与 consumer.py 都是平铺模块（consumer 内部 `import config` /
`import store` / `import valuation`），且仓库里存在多个同名 main/config/store
（tools/risk 等）。沿用 tools/risk/tests/riskmods.py 的约定：用唯一模块名 cdcmods
加载，规避跨目录 conftest 撞名（禁止 from conftest import 的原因见 riskmods 顶部）。

隔离原则：本目录不连任何数据库、不连 Kafka/Flink。
- main.py 的 BinLogStreamReader 由测试 monkeypatch 成假流（零真 binlog）。
- consumer.py 依赖的 tools/risk store/valuation 只在模块加载时导入；store 的反查函数
  在用例里用 monkeypatch 替换（零真业务库）。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
CDC_DIR = os.path.dirname(TESTS_DIR)
RISK_DIR = os.path.join(os.path.dirname(CDC_DIR), "risk")

# consumer.py 平铺 import 的模块名；加载前清掉，避免被其它目录同名模块污染。
_FLAT_NAMES = ("config", "store", "valuation", "main")


def _load_file(unique_name: str, path: str):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    for d in (RISK_DIR, CDC_DIR):
        while d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)

    cdc_main = _load_file("cdc_main", os.path.join(CDC_DIR, "main.py"))
    cdc_consumer = _load_file("cdc_consumer", os.path.join(CDC_DIR, "consumer.py"))
    return cdc_main, cdc_consumer


cdc_main, cdc_consumer = _load()


# ------------------------------------------------------------------ 录制型假 DB
#
# main.py / consumer.py 的读写函数都收 pymysql 连接。这里仿 DB-API 的最小子集，
# 按「SQL 片段」匹配返回预置结果集，并记录执行过的 (sql, params)。
# 测试只断言「发出了什么语义的 SQL、参数对不对」，不验证 MySQL 自身行为。


def _norm(sql):
    """把 SQL 压成单行，方便用关键字子串断言。"""
    return " ".join(sql.split())


class CdcFakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((_norm(sql), params))
        rows, desc = self._conn._result_for(sql)
        self._rows = list(rows)
        self.description = desc
        return len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self._conn.closed_cursors += 1


class CdcFakeConn:
    """录制型假连接：execute 的 SQL 含某片段 → 返回该片段预置的结果集（首条规则命中）。"""

    def __init__(self):
        self.executed = []  # [(sql, params)]
        self.commits = 0
        self.closed_cursors = 0
        self._rules = []  # [(fragment, rows, description)]

    def when(self, fragment, rows, columns=None):
        """注册结果规则；columns 给定才有 cursor.description（fetch_events 需要）。"""
        desc = [(c,) + (None,) * 6 for c in columns] if columns else None
        self._rules.append((fragment, rows or [], desc))
        return self

    def _result_for(self, sql):
        for fragment, rows, desc in self._rules:
            if fragment in sql:
                return rows, desc
        return [], None

    def cursor(self):
        return CdcFakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def find_sql(self, *keywords):
        """第一条包含全部 keyword 的 (sql, params)，找不到返回 None。"""
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None
