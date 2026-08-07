"""加载 tools/alerting 的平铺模块 + 假 DB 连接。测试从这里取被测对象。

命名与加载方式的原因同 tools/risk/tests/riskmods.py：用唯一模块名导出被测对象，
避免多个 tests 目录同时收集时 `from conftest import ...` 串到别的目录（仓库现存问题）；
tools/alerting/main.py 是平铺名 `main`，还会把 ../risk 塞进 sys.path 去 `import config`、
从同目录 `import drivers`，这些平铺名都与其它 tools/* 目录撞名，加载前统一清掉。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ALERTING_DIR = os.path.dirname(TESTS_DIR)
RISK_DIR = os.path.join(os.path.dirname(ALERTING_DIR), "risk")

_FLAT_NAMES = ("config", "main", "drivers", "valuation", "risk_engine", "store")


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    for d in (RISK_DIR, ALERTING_DIR):
        while d in sys.path:
            sys.path.remove(d)
    sys.path.insert(0, RISK_DIR)
    sys.path.insert(0, ALERTING_DIR)

    path = os.path.join(ALERTING_DIR, "main.py")
    spec = importlib.util.spec_from_file_location("alerting_main_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # main.py 执行时已把正确的 drivers 装进 sys.modules，取出来留给测试用
    return mod, sys.modules["drivers"]


alerting, drivers = _load()
config = alerting.config


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
        got = self._conn._route(_norm(sql))
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
    """录制型假连接。

    与 risk/reporting 的版本不同，这里按 SQL 关键字路由结果集而不是排队：
    run_dispatch 的调用次序取决于状态机分支（去重/终态失败会跳过 upsert），
    用队列会让预置顺序和分支耦合，测试变脆。
    """

    def __init__(self):
        self.executed = []
        self.executed_many = []
        self.commits = 0
        self.closed_cursors = 0
        self._routes = []  # [(keyword, rows, description)]

    def on(self, keyword, rows, columns=None):
        """SQL 里出现 keyword 时返回这批行。同一 keyword 可注册多次，按序消费。"""
        desc = [(c,) + (None,) * 6 for c in columns] if columns else None
        self._routes.append([keyword, rows, desc, False])
        return self

    def _route(self, sql):
        for entry in self._routes:
            kw, rows, desc, used = entry
            if kw in sql and not used:
                entry[3] = True
                return rows, desc
        return None

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

    def all_sql(self, *keywords):
        return [(s, p) for s, p in self.executed if all(k in s for k in keywords)]


# ------------------------------------------------------------------ 驱动替身


class RecordingDriver:
    """记录收到的预警；可按 loan_id 指定失败。"""

    name = "recording"

    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    def send(self, alert):
        if alert["loan_id"] in self.fail_on:
            raise RuntimeError(f"下游拒收 loan_id={alert['loan_id']}")
        self.sent.append(alert)


ALERT_COLS = [
    "id",
    "loan_id",
    "customer_id",
    "collateral_id",
    "loan_balance",
    "market_valuation",
    "ltv",
    "risk_class",
    "is_high_risk_zone",
    "alert_level",
    "alert_date",
]


def alert_row(loan_id, date="2026-08-05", ltv=0.92, alert_level=None):
    """一行 ads_ltv_alerts（元组形式，供 FakeConn 当查询结果返回）。"""
    return (
        loan_id,
        loan_id,
        9000 + loan_id,
        500 + loan_id,
        1_000_000.0,
        1_050_000.0,
        ltv,
        "可疑",
        0,
        alert_level,
        date,
    )


def dispatch_row(loan_id, status, attempts, date="2026-08-05", max_retries=3):
    """一行 ads_alert_dispatch（台账既有状态）。"""
    return (loan_id, date, status, attempts, max_retries)


DISPATCH_COLS = ["loan_id", "alert_date", "status", "attempt_count", "max_retries"]
