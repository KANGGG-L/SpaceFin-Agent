"""crawl_quality_gate：决策逻辑 + 端到端 main 流程（monkeypatch DB / Slack）。

不连真实库：用 FakeConn 顶替 pymysql.connect，RecordingPost 顶替 post_slack。
"""

import importlib.util
import os
import sys

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/orchestrator
SPEC = importlib.util.spec_from_file_location(
    "crawl_quality_gate_under_test", os.path.join(ORCH_DIR, "crawl_quality_gate.py")
)
cg = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cg
sys.path.insert(0, ORCH_DIR)  # so `from slack_notify import ...` resolves
SPEC.loader.exec_module(cg)


def test_decide_branches():
    # 硬下限：默认(False) 放行 exit 0；严格(True) 拦停 exit 2
    assert cg.decide(10, 50, 200, False) == (0, "hard")
    assert cg.decide(10, 50, 200, True) == (2, "hard")
    # 软下限：告警放行
    assert cg.decide(100, 50, 200, False) == (0, "soft")
    # 正常
    assert cg.decide(300, 50, 200, False) == (0, "ok")
    # 0 新增且 0 刷新 → 拦停（不论 strict）——修复「0 行被当硬下限静默放行」的假成功
    assert cg.decide(0, 50, 200, False) == (2, "zero")
    assert cg.decide(0, 50, 200, True) == (2, "zero")
    assert cg.decide(0, 50, 200, False, refreshed=0) == (2, "zero")
    # 0 新增但存量已刷新 → 告警放行（爬穿稳态，不能断快照）
    assert cg.decide(0, 50, 200, False, refreshed=25241) == (0, "refreshed")
    assert cg.decide(0, 50, 200, True, refreshed=25241) == (0, "refreshed")


PAGE_EVIDENCE = "last_seen_page IS NOT NULL"

# 42 个 (城,型) 全部 complete=1：账本齐全、当日每城每型都有完备观测的理想态。
ALL_CENSUS_COMPLETE = [(c, t, 1) for c in cg.CITIES for t in cg.CRAWL_TYPE_ORDER]


class _FakeCursor:
    def __init__(
        self,
        rows,
        refreshed=0,
        refreshed_no_page=0,
        zero_cities=None,
        has_page_column=True,
        census_rows=None,
        census_table_exists=True,
    ):
        self._rows = rows
        self._refreshed = refreshed
        self._refreshed_no_page = refreshed_no_page
        self._zero_cities = set(zero_cities or [])
        self._has_page_column = has_page_column
        self._census_rows = census_rows
        self._census_table_exists = census_table_exists

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        # 观测列存在性预检（main 在三个口径之前先跑一次）
        if "information_schema" in self.sql:
            if "crawl_census" in self.sql:
                return (1 if self._census_table_exists else 0,)
            return (1 if self._has_page_column else 0,)
        # 区分新增（first_seen_date）与刷新（last_seen_date）两条 COUNT
        if "first_seen_date" in self.sql:
            return (self._rows,)
        # 页码谓词写反（IS NULL）必须炸，而不是悄悄把 legacy 行放回刷新口径
        if "last_seen_page" in self.sql and PAGE_EVIDENCE not in self.sql:
            raise AssertionError(f"刷新口径缺少页码证据要求: {self.sql}")
        # 无页码的行 = last_seen_date 只是 ETL 重跑日、今天并没有真的扫到它。
        # 它们只有在 SQL 不带页码谓词时才被算进 refreshed——那正是 S1 前的假绿行为。
        if PAGE_EVIDENCE in self.sql:
            return (self._refreshed,)
        return (self._refreshed + self._refreshed_no_page,)

    def fetchall(self):
        if "crawl_census" in self.sql:
            # None = 账本表存在但当日无行（最保守：等于全城无证据）
            return self._census_rows or []
        # 0 行城市名单与总行数解耦：只有不在名单里的城市返回非零计数
        return [(c, 1) for c in cg.CITIES if c not in self._zero_cities]

    def close(self):
        pass


class _FakeConn:
    def __init__(
        self,
        rows,
        refreshed=0,
        refreshed_no_page=0,
        zero_cities=None,
        has_page_column=True,
        census_rows=None,
        census_table_exists=True,
    ):
        self._rows = rows
        self._refreshed = refreshed
        self._refreshed_no_page = refreshed_no_page
        self._zero_cities = zero_cities
        self._has_page_column = has_page_column
        self._census_rows = census_rows
        self._census_table_exists = census_table_exists
        self.closed = False

    def cursor(self):
        return _FakeCursor(
            self._rows,
            self._refreshed,
            self._refreshed_no_page,
            self._zero_cities,
            self._has_page_column,
            self._census_rows,
            self._census_table_exists,
        )

    def close(self):
        self.closed = True


class _RecordingPost:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook, text):
        self.calls.append(text)
        return True


def _run(
    monkeypatch,
    new_rows,
    strict,
    *,
    hard=50,
    soft=200,
    webhook="https://x",
    mentions="",
    zero_cities=None,
    refreshed=0,
    refreshed_no_page=0,
    has_page_column=True,
    census_rows=None,
    census_table_exists=True,
):
    post = _RecordingPost()
    monkeypatch.setattr(
        cg,
        "pymysql",
        type(
            "M",
            (),
            {
                "connect": lambda *a, **k: _FakeConn(
                    new_rows,
                    refreshed,
                    refreshed_no_page,
                    zero_cities,
                    has_page_column,
                    census_rows,
                    census_table_exists,
                )
            },
        )(),
    )
    monkeypatch.setattr(cg, "post_slack", post)
    monkeypatch.setattr(cg.config, "load_env", lambda: {})
    monkeypatch.setattr(
        os,
        "getenv",
        lambda k, d="": (
            webhook
            if k == "SPACEFIN_ALERT_SLACK_WEBHOOK"
            else (mentions if k == "SPACEFIN_ALERT_MENTIONS" else d)
        ),
    )
    argv = ["gate", "--date", "2026-08-05", f"--hard-floor={hard}", f"--soft-floor={soft}"]
    if strict:
        argv.append("--strict")
    monkeypatch.setattr(sys, "argv", argv)
    code = cg.main()
    return code, post.calls


def test_main_ok_no_alert(monkeypatch):
    # 账本齐全（42 个 (城,型) 均 complete=1）且无 0 行城市 → 完全静默
    code, calls = _run(monkeypatch, 300, strict=False, census_rows=ALL_CENSUS_COMPLETE)
    assert code == 0
    assert calls == []


def test_main_soft_floor_warns_but_passes(monkeypatch):
    code, calls = _run(monkeypatch, 100, strict=False)
    assert code == 0
    assert len(calls) == 1
    assert "偏低" in calls[0]


def test_main_hard_floor_lenient_warns_and_passes(monkeypatch):
    code, calls = _run(monkeypatch, 10, strict=False)
    assert code == 0
    assert len(calls) == 1
    assert "≤ 硬下限" in calls[0]


def test_main_hard_floor_strict_blocks_exit_2(monkeypatch):
    code, calls = _run(monkeypatch, 10, strict=True)
    assert code == 2
    assert len(calls) == 1
    assert "≤ 硬下限" in calls[0]


def test_main_ok_with_zero_row_cities_warns(monkeypatch):
    code, calls = _run(monkeypatch, 300, strict=False, zero_cities=["yf", "sw"])
    assert code == 0
    assert len(calls) == 1
    assert "2 个城市新增为 0" in calls[0]
    assert "yf" in calls[0] and "sw" in calls[0]


def test_main_zero_rows_always_blocks(monkeypatch):
    # 非严格模式下 0 新增且 0 刷新也必须 exit 2（此前会误放行→假成功）
    code, calls = _run(monkeypatch, 0, strict=False, refreshed=0)
    assert code == 2
    assert len(calls) == 1
    assert "未产出任何数据" in calls[0] and "拦停" in calls[0]
    # 严格模式同样 exit 2
    code, calls = _run(monkeypatch, 0, strict=True, refreshed=0)
    assert code == 2


def test_main_zero_new_but_refreshed_passes_with_warn(monkeypatch):
    # 爬穿稳态：无新房源但存量被刷新 → 放行 + 告警（detail 里两个数字都要有）
    # 注：fake 对 sale/rent 两表各返回 refreshed，合计 = 2 * refreshed
    code, calls = _run(monkeypatch, 0, strict=False, refreshed=12620)
    assert code == 0
    assert len(calls) == 1
    assert "无新房源" in calls[0]
    assert "新增爬取行数 = 0" in calls[0] and "刷新行数 = 25240" in calls[0]
    # 严格模式下同样放行（不是故障，不该拦）
    assert _run(monkeypatch, 0, strict=True, refreshed=12620)[0] == 0


def test_main_zero_new_refreshed_with_zero_cities_still_passes(monkeypatch):
    # 0 行城市名单只作告警，不拦截
    code, calls = _run(monkeypatch, 0, strict=False, refreshed=100, zero_cities=["yf", "sw"])
    assert code == 0
    assert len(calls) == 1
    assert "2 个城市新增为 0" in calls[0]
    assert "仅告警" in calls[0]


def test_refreshed_query_requires_page_evidence_on_both_tables():
    """口径本身：两张表的刷新 COUNT 都必须带页码谓词。

    少了它，`last_seen_date` 就退化成「ETL 恰好又读了这批文件」的函数——
    2026-08-30 实测零页面爬取仍报 refreshed=25,241 并放行。
    """
    seen = []

    class _Rec:
        def execute(self, sql, params=None):
            seen.append(sql)

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    class _RecConn:
        def cursor(self):
            return _Rec()

    cg.count_refreshed_rows(_RecConn(), "2026-08-30")
    assert [s.split(" FROM ")[1].split(" ")[0] for s in seen] == [
        "crawl_housing_sale",
        "crawl_housing_rent",
    ]
    for sql in seen:
        assert "last_seen_date=%s" in sql
        assert PAGE_EVIDENCE in sql


def test_main_legacy_refresh_without_page_evidence_blocks(monkeypatch):
    # 全部「刷新」都来自没有页码的历史行 → 不算今日观测，等价于零采集，必须拦停
    code, calls = _run(monkeypatch, 0, strict=False, refreshed_no_page=12620)
    assert code == 2
    assert len(calls) == 1
    assert "未产出任何数据" in calls[0]
    assert "last_seen_page" in calls[0]
    # 严格模式同样拦停
    assert _run(monkeypatch, 0, strict=True, refreshed_no_page=12620)[0] == 2


def test_main_mixed_rows_count_only_page_evidenced_refresh(monkeypatch):
    # 同日既有真观测（带页码）又有 legacy 行：只有前者能撑起放行，且报告里只出现前者
    code, calls = _run(monkeypatch, 0, strict=False, refreshed=7, refreshed_no_page=12620)
    assert code == 0
    assert len(calls) == 1
    assert "刷新行数 = 14" in calls[0]  # fake 对 sale/rent 各返回 7
    assert "12620" not in calls[0] and "25240" not in calls[0]


def test_incomplete_census_tasks_lists_only_pairs_without_complete():
    """只点名 MAX(complete)=0 的 (城,型)；有任意一行 complete=1 的就算有证据。"""
    conn = _FakeConn(0, census_rows=[("gz", "sale", 0), ("gz", "fangyuan", 1), ("yf", "sale", 0)])
    assert cg.incomplete_census_tasks(conn, "2026-08-31") == (["gz:sale", "yf:sale"], True)


def test_incomplete_census_tasks_without_rows_means_unknown_not_complete():
    """当日无行（或表不存在）≠ 全都完备——ready=False 让调用方把「账本缺失」点出来。

    这是「缺省即未知」的落点：把「没账本」读成「都看了」会让 absent 判定凭空生出来。
    """
    assert cg.incomplete_census_tasks(_FakeConn(0, census_rows=None), "2026-08-31") == ([], False)
    assert cg.incomplete_census_tasks(_FakeConn(0, census_table_exists=False), "2026-08-31") == (
        [],
        False,
    )


def test_census_note_silent_when_all_complete():
    assert cg.census_note([], True) == ""


def test_main_names_cities_without_complete_observation(monkeypatch):
    """S2 验收：某 (城,型) 当日 complete=0 → 闸门必须点名它，且点名不改变放行判定。"""
    rows = [(c, t, 0 if c in ("gz", "sz") else 1) for c, t, _ in ALL_CENSUS_COMPLETE]
    code, calls = _run(monkeypatch, 300, strict=False, census_rows=rows)
    assert code == 0
    assert len(calls) == 1
    assert "4 个 (城,型) 当日无完备观测" in calls[0]
    assert "gz:sale" in calls[0] and "sz:fangyuan" in calls[0]
    assert "不得产生任何 absent 判定" in calls[0]


def test_main_census_missing_is_named_not_silent(monkeypatch):
    """账本当日无行 = 全城无证据：新增行数再健康也必须点名，不许静默放行。"""
    code, calls = _run(monkeypatch, 300, strict=False)
    assert code == 0
    assert len(calls) == 1
    assert "crawl_census 当日无账本行" in calls[0]
    assert "不得产生任何 absent 判定" in calls[0]


def test_main_census_table_absent_is_named(monkeypatch):
    code, calls = _run(monkeypatch, 300, strict=False, census_table_exists=False)
    assert code == 0
    assert "crawl_census 当日无账本行" in calls[0]


def test_main_zero_rows_block_also_names_census(monkeypatch):
    """拦停分支同样带 census 点名：一次告警把「没数据」与「没证据」都说清楚。"""
    code, calls = _run(monkeypatch, 0, strict=False, refreshed=0)
    assert code == 2
    assert "未产出任何数据" in calls[0]
    assert "crawl_census" in calls[0]


def test_missing_observe_columns_names_only_the_bad_tables():
    assert cg.missing_observe_columns(_FakeConn(0, has_page_column=True)) == []
    assert cg.missing_observe_columns(_FakeConn(0, has_page_column=False)) == [
        "crawl_housing_sale",
        "crawl_housing_rent",
    ]


def test_main_missing_observe_column_blocks_even_with_healthy_new_rows(monkeypatch):
    """观测列缺失 = refreshed 口径不可用 → 无条件拦停，新增行数再健康也不放行。

    这正是当前存量库的状态：列还没长出来（`CREATE TABLE IF NOT EXISTS` 不会补列），
    闸门必须报「ETL 未迁移」而不是抛 1054 或者干脆跳过这个检查。
    """
    for new_rows in (0, 300):
        code, calls = _run(monkeypatch, new_rows, strict=False, has_page_column=False)
        assert code == 2
        assert len(calls) == 1
        assert "last_seen_page" in calls[0]
        assert "_ensure_columns" in calls[0]
    # 严格模式与非严格同样拦停
    assert _run(monkeypatch, 300, strict=True, has_page_column=False)[0] == 2
