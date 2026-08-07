"""加载 tools/reporting 的平铺模块 + 假 DB 连接。测试从这里取被测对象。

命名与加载方式的原因同 tools/risk/tests/riskmods.py：
- 用唯一模块名（不是 `conftest`）导出被测对象，避免多个 tests 目录同时收集时
  `from conftest import ...` 拿到别的目录的 conftest（仓库现存问题）。
- tools/reporting/main.py 是平铺名 `main`，与 tools/risk/main.py、tools/alerting/main.py
  撞名；它自己还会把 ../risk 塞进 sys.path 去 `import config`，而 tools/spatial/config.py
  同名。故加载前清掉这些平铺名，加载后模块 globals 里持有的引用即为正确对象。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTING_DIR = os.path.dirname(TESTS_DIR)
RISK_DIR = os.path.join(os.path.dirname(REPORTING_DIR), "risk")

_FLAT_NAMES = ("config", "main", "valuation", "risk_engine", "store", "drivers")


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    for d in (RISK_DIR, REPORTING_DIR):
        while d in sys.path:
            sys.path.remove(d)
    sys.path.insert(0, RISK_DIR)
    sys.path.insert(0, REPORTING_DIR)

    # 用唯一名注册，避免占着平铺名 `main` 影响别的目录
    path = os.path.join(REPORTING_DIR, "main.py")
    spec = importlib.util.spec_from_file_location("reporting_main_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


g11 = _load()
config = g11.config


# ------------------------------------------------------------------ 假 DB


def _norm(sql):
    return " ".join(sql.split())


class FakeCursor:
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
    """录制型假连接：记录 SQL / 参数 / commit，按队列返回预置结果集。"""

    def __init__(self):
        self.executed = []
        self.executed_many = []
        self.commits = 0
        self.closed_cursors = 0
        self._results = []

    def queue_result(self, rows, columns=None):
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

    def find_sql(self, *keywords):
        for sql, params in self.executed:
            if all(k in sql for k in keywords):
                return sql, params
        return None

    def find_many(self, *keywords):
        for sql, rows in self.executed_many:
            if all(k in sql for k in keywords):
                return sql, rows
        return None


# ------------------------------------------------------------------ 构造辅助


def internal_of(**by_class):
    """构造 load_internal 形状的出口①：{档位: {count, balance, balance_pct}}。

    未指定的档位补零行，与 load_internal 的缺级补零行为一致。
    """
    out = {}
    for cls in config.CLASS_ORDER:
        v = by_class.get(cls)
        if v is None:
            out[cls] = {"count": 0, "balance": 0.0, "balance_pct": 0.0}
        else:
            cnt, bal, pct = v
            out[cls] = {"count": cnt, "balance": bal, "balance_pct": pct}
    return out


def dws_of(internal, **overrides):
    """从出口①派生出口②（明细聚合：count/balance/balance_pct），可局部覆盖以制造漂移。

    占比按余额重算（与 load_dws_agg 口径一致，分母为全量余额、保留 4 位小数），
    因此覆盖 balance 后各档占比自动跟着重算；占比本身不直接接受覆盖。
    """
    out = {cls: {"count": v["count"], "balance": v["balance"]} for cls, v in internal.items()}
    for cls, (cnt, bal) in overrides.items():
        out[cls] = {"count": cnt, "balance": bal}
    total = sum(v["balance"] for v in out.values())
    for v in out.values():
        v["balance_pct"] = round(v["balance"] / total, 4) if total else 0.0
    return out
