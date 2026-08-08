"""LTV 预警并发计数原子化（C 类）：SQL 契约 + 并发正确性。

不连真实 MySQL（铁律禁止改数据）。并发正确性用一个 faithfully 模拟 MySQL 行锁 +
`INSERT ... ON DUPLICATE KEY UPDATE attempt_count = attempt_count + 1` 的线程安全假连接
来验证：同一 (loan_id, alert_date) 的多次并发 dispatch，最终 attempt_count 等于真实尝试次数。
"""

import threading

import alertmods

alerting = alertmods.alerting
ALERT_COLS = alertmods.ALERT_COLS
DISPATCH_COLS = alertmods.DISPATCH_COLS


def _norm(sql):
    return " ".join(sql.split()).lower()


def _has(norm, frag):
    return frag in norm


def test_dispatch_upsert_uses_atomic_increment():
    """UPSERT 必须把 attempt_count 改为 `attempt_count + 1`，而非 VALUES(attempt_count)。"""
    sql = alerting.DISPATCH_UPSERT_SQL
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "attempt_count = attempt_count + 1" in sql
    assert "attempt_count=VALUES(attempt_count)" not in sql.replace(" ", "")


def test_lock_dispatch_row_uses_for_update(conn):
    """_lock_dispatch_row 必须带 FOR UPDATE 并按 (loan_id, alert_date) 行锁过滤。"""
    conn.on("FROM ads_alert_dispatch", [alertmods.dispatch_row(1, "failed", 2)], DISPATCH_COLS)
    row = alerting._lock_dispatch_row(conn, 1, "2026-08-05")
    assert row == {"status": "failed", "attempt_count": 2, "max_retries": 3}
    sql, params = conn.find_sql("FOR UPDATE")
    assert sql is not None
    assert "loan_id=%s AND alert_date=%s" in sql
    assert params == (1, "2026-08-05")


def test_lock_dispatch_row_returns_none_when_absent(conn):
    conn.on("FROM ads_alert_dispatch", [], DISPATCH_COLS)
    assert alerting._lock_dispatch_row(conn, 99, "2026-08-05") is None


# ---------------------------------------------------------------- 并发正确性（线程安全假连接）


class _LCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self.description = None

    def execute(self, sql, params=None):
        self._conn.executed.append((_norm(sql), params))
        norm = _norm(sql)
        if _has(norm, "from ads_ltv_alerts"):
            self._rows = [list(a) for a in self._conn._alerts]
            self.description = [(c,) for c in ALERT_COLS]
        elif _has(norm, "for update") and _has(norm, "ads_alert_dispatch"):
            loan_id, alert_date = params
            key = (loan_id, alert_date)
            lk = self._conn._lock_for(key)
            lk.acquire()  # 模拟 SELECT ... FOR UPDATE 加行锁
            self._conn._held.add(key)
            rec = self._conn._store.get(key)
            self._rows = (
                [
                    (
                        rec["loan_id"],
                        rec["alert_date"],
                        rec["status"],
                        rec["attempt_count"],
                        rec["max_retries"],
                    )
                ]
                if rec
                else []
            )
            self.description = [(c,) for c in DISPATCH_COLS]
        elif _has(norm, "from ads_alert_dispatch"):
            # 批读（load_dispatch，无锁）：返回全部台账行
            self._rows = [
                (r["loan_id"], r["alert_date"], r["status"], r["attempt_count"], r["max_retries"])
                for r in self._conn._store.values()
            ]
            self.description = [(c,) for c in DISPATCH_COLS]
        elif _has(norm, "insert into ads_alert_dispatch"):
            # INSERT ... ON DUPLICATE KEY UPDATE attempt_count = attempt_count + 1
            (loan_id, alert_date, _dd, status, attempt_count, max_retries, last_error, _ts) = params
            key = (loan_id, alert_date)
            rec = self._conn._store.get(key)
            if rec is None:
                self._conn._store[key] = {
                    "loan_id": loan_id,
                    "alert_date": alert_date,
                    "status": status,
                    "attempt_count": attempt_count,
                    "max_retries": max_retries,
                    "last_error": last_error,
                }
            else:
                rec["status"] = status
                rec["max_retries"] = max_retries
                rec["last_error"] = last_error
                rec["attempt_count"] = rec["attempt_count"] + 1  # 原子自增
        return len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class LockingFakeConn:
    """线程安全假连接：模拟 MySQL 行锁（FOR UPDATE 持有至 commit）+ 原子自增 UPSERT。"""

    def __init__(self, alerts):
        self._alerts = list(alerts)
        self._store = {}
        self._locks = {}
        self._held = set()
        self._guard = threading.Lock()
        self.commits = 0
        self.executed = []

    def _lock_for(self, key):
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def cursor(self):
        return _LCursor(self)

    def commit(self):
        for k in list(self._held):
            self._lock_for(k).release()
        self._held.clear()
        self.commits += 1

    def close(self):
        pass


def _seed(store, loan_id, date, status, attempts, max_retries=3):
    store[(loan_id, date)] = {
        "loan_id": loan_id,
        "alert_date": date,
        "status": status,
        "attempt_count": attempts,
        "max_retries": max_retries,
        "last_error": "",
    }


def test_concurrent_dispatch_does_not_undercount_fresh_alert():
    """同一 (loan_id,alert_date) 两条并发失败 dispatch（此前无台账）→ attempt_count == 2。

    成功路径下第二条会因为 T+1 去重跳过（不增计数，属正确行为），故用 force_fail 让两条
    都进入失败重试分支，才能暴露「并发少计」回归。
    """
    conn = LockingFakeConn([alertmods.alert_row(1)])
    drivers = [alertmods.RecordingDriver(), alertmods.RecordingDriver()]

    def _run(d):
        alerting.run_dispatch(conn, [d], "2026-08-05", 3, True)

    t1 = threading.Thread(target=_run, args=(drivers[0],))
    t2 = threading.Thread(target=_run, args=(drivers[1],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    rec = conn._store[(1, "2026-08-05")]
    assert rec["attempt_count"] == 2, f"并发少计：{rec['attempt_count']}"
    assert rec["status"] == "failed"


def test_concurrent_dispatch_does_not_undercount_with_prior_failed():
    """此前已有 1 次失败（attempt_count=1），两条并发失败 dispatch → 最终 == 3。"""
    conn = LockingFakeConn([alertmods.alert_row(1)])
    _seed(conn._store, 1, "2026-08-05", "failed", 1)
    drivers = [alertmods.RecordingDriver(), alertmods.RecordingDriver()]

    def _run(d):
        alerting.run_dispatch(conn, [d], "2026-08-05", 3, True)

    t1 = threading.Thread(target=_run, args=(drivers[0],))
    t2 = threading.Thread(target=_run, args=(drivers[1],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    rec = conn._store[(1, "2026-08-05")]
    assert rec["attempt_count"] == 3, f"并发少计：{rec['attempt_count']}"
