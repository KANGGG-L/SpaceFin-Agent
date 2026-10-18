"""`market_state`：纯函数 `decide_removal` + 调用层（计划 S3 · 状态机）。

核心不变式：**无法证明「今天扫全了」的日子，绝不推进缺席计数，也绝不清零**。
每条用例都是防「假绿」的一道闸门 —— 把「我没扫到」当成「它下架了」是本模块
唯一需要拼死防住的失败模式。因此凡是判不出来的输入，断言都指向 unknown / absent，
而不是 active / removed。

调用层不连真库：用 FakeConn 顶替 `_connect`。**Fake 对未知 SQL 一律 raise**
（照 test_crawl_quality_gate.py 的骨架）—— 返回「看起来合理」的假值会让测试自己造绿。

载入方式照 test_census.py：用 importlib 按文件路径加载，
避免 `pytest tools/` 联跑时多个 tests 目录的 conftest 串味。
"""

import datetime
import importlib.util
import os
import re
import sys

import pytest

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ORCH_DIR)  # market_state / census 的延迟 import 都要用到
SPEC = importlib.util.spec_from_file_location(
    "market_state_under_test", os.path.join(ORCH_DIR, "market_state.py")
)
ms = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ms
SPEC.loader.exec_module(ms)


DS = datetime.date(2026, 8, 31)
OLD = DS - datetime.timedelta(days=30)  # 挂牌已满 30 天，min_age 不再是变量


def _absent(prev_streak, first_seen_date=OLD, ds=DS, n=3, min_age_days=7, prev_removed_date=None):
    """合格观测下未出现。"""
    return ms.decide_removal(
        False, True, prev_streak, n, first_seen_date, ds, min_age_days, prev_removed_date
    )


def _replay(days, first_seen_date=OLD, n=3, min_age_days=7):
    """按 [(seen, eligible), ...] 逐日投影，返回最后一天的 (status, streak, removed_date)。

    days 里的日期从 `DS - len(days) + 1` 起算，与「逐日推进」的真实调用一致。
    上一日的 removed_date 会传给下一日（真实调用层就是这么持住首次判定日的）。
    """
    streak = 0
    removed_date = None
    start = DS - datetime.timedelta(days=len(days) - 1)
    for i, (seen, eligible) in enumerate(days):
        day = start + datetime.timedelta(days=i)
        status, streak, removed_date, _ = ms.decide_removal(
            seen, eligible, streak, n, first_seen_date, day, min_age_days, removed_date
        )
    return status, streak, removed_date


# ------------------------------------------------------------------ 三个基本分支
def test_seen_today_resets_streak_and_is_active():
    # 出现即清零：不管此前累积了多少天缺席
    out = ms.decide_removal(True, True, 5, 3, OLD, DS)
    assert out == ("active", 0, None, "seen")
    # 从 0 开始也是 active，不产生 removed_date
    assert ms.decide_removal(True, True, 0, 3, OLD, DS)[:2] == ("active", 0)


def test_eligible_absent_increments_streak():
    assert _absent(0) == ("absent", 1, None, "streak")
    assert _absent(1) == ("absent", 2, None, "streak")


def test_not_eligible_absent_keeps_streak_and_is_unknown():
    """本模块的核心：不推进、不清零，状态 unknown。"""
    out = ms.decide_removal(False, False, 2, 3, OLD, DS)
    assert out == ("unknown", 2, None, "not_eligible")
    # 零 streak 的非合格日也不能凭空生出缺席
    assert ms.decide_removal(False, False, 0, 3, OLD, DS) == (
        "unknown",
        0,
        None,
        "not_eligible",
    )


def test_seen_wins_over_not_eligible():
    """seen 优先于 eligible：「今天看到了」是直接证据，不需要任何缺席资格。

    反过来的顺序（eligible 压在 seen 前）会把已确认在架的房源报成 unknown，
    在架数系统性少算 → 流转率**分母**失真，比缺一个数据点更糟。
    cutover / 页洞 / 账本缺失拦的是「缺席推断」，不是「在架事实」。
    """
    assert ms.decide_removal(True, False, 4, 3, OLD, DS) == ("active", 0, None, "seen")
    # 首见日早于 cutover 也一样：今天在架就是 active
    assert ms.decide_removal(True, False, 9, 3, OLD, DS) == ("active", 0, None, "seen")


def test_not_eligible_and_unseen_keeps_streak_and_is_unknown():
    """没看到 + 没有缺席资格 → unknown：cutover 该拦的还是要拦住。"""
    assert ms.decide_removal(False, False, 4, 3, OLD, DS) == (
        "unknown",
        4,
        None,
        "not_eligible",
    )
    # 已判下架的 key 遇到不合格日：状态 unknown、streak 不动，但下架日带回
    assert ms.decide_removal(False, False, 3, 3, OLD, DS, 7, OLD) == (
        "unknown",
        3,
        OLD,
        "not_eligible",
    )


# -------------------------------------------------------------- unknown 不打断 streak
def test_unknown_day_in_the_middle_does_not_break_streak():
    # absent, unknown, absent, absent → 第三天 streak 继续累加到 3，而不是被 unknown 归零
    status, streak, removed_date = _replay(
        [(False, True), (False, False), (False, True), (False, True)]
    )
    assert (status, streak) == ("removed", 3)
    assert removed_date == DS

    # 只走前三天：unknown 夹在中间后，streak 是 2 而不是 1
    status, streak, _ = _replay([(False, True), (False, False), (False, True)])
    assert (status, streak) == ("absent", 2)


def test_unknown_days_never_accumulate():
    # 连续 5 天不合格观测 → streak 恒 0，绝不能攒出下架
    status, streak, removed_date = _replay([(False, False)] * 5)
    assert (status, streak, removed_date) == ("unknown", 0, None)


def test_unknown_days_keep_removed_date_and_streak():
    """已下架的房源遇到不合格日：状态 unknown、streak 不动，但下架日是历史事实。

    若这里返回 None，调用层照抄写回就会把下架日抹成 NULL，
    流转率里「何时下架」的时间序列会凭空出现空洞。
    """
    hit_day = DS - datetime.timedelta(days=2)  # 3 天缺席达标，再接 2 个不合格日
    status, streak, removed_date = _replay([(False, True)] * 3 + [(False, False)] * 2)
    assert status == "unknown"
    assert streak == 3  # 不推进也不清零
    assert removed_date == hit_day  # 仍是首次判定日，不是 None

    # 单日直接调用同理：传入历史下架日，unknown 分支原样带回
    out = ms.decide_removal(False, False, 3, 3, OLD, DS, 7, hit_day)
    assert out == ("unknown", 3, hit_day, "not_eligible")


# ---------------------------------------------------------------------- N 的边界
def test_streak_reaching_n_marks_removed():
    assert _absent(2) == ("removed", 3, DS, "confirmed_absent")


def test_streak_n_minus_1_stays_absent():
    assert _absent(1) == ("absent", 2, None, "streak")


def test_n_boundary_with_other_n():
    # N=1：一次合格缺席即下架
    assert _absent(0, n=1) == ("removed", 1, DS, "confirmed_absent")
    # N=5：第 4 天仍是 absent
    status, streak, _ = _replay([(False, True)] * 4, n=5)
    assert (status, streak) == ("absent", 4)


# ------------------------------------------------------- removed_date = 首次判定日
def test_removed_date_is_the_day_streak_hit_n():
    """removed_date 是 streak 恰好达到 N 的那天，不是「转正日」。"""
    # 第 1、2 天缺席，第 3 天达到 N=3 → removed_date 就是第 3 天
    days = [(False, True), (False, True), (False, True)]
    start = DS - datetime.timedelta(days=2)
    streak = 0
    hit_day = None
    for i, (seen, eligible) in enumerate(days):
        day = start + datetime.timedelta(days=i)
        status, streak, removed_date, _ = ms.decide_removal(seen, eligible, streak, 3, OLD, day)
        if status == "removed" and hit_day is None:
            hit_day = removed_date
    assert hit_day == DS
    assert streak == 3


def test_removed_date_stays_first_hit_day_as_streak_grows():
    """连续 3 天缺席判下架后，第 4、5 天继续缺席：removed_date 恒为**首次判定日**。

    下架日期一旦跟着当日走，所有「下架日期」与流转率时间序列都会整条漂移。
    """
    for extra in (0, 1, 2):
        # 窗口恒以 DS 收尾：第 3 天达标日 = DS - extra，逐日推进后必须仍停在那天
        hit_day = DS - datetime.timedelta(days=extra)
        status, streak, removed_date = _replay([(False, True)] * (3 + extra))
        assert status == "removed"
        assert streak == 3 + extra
        assert removed_date == hit_day  # 不随当日后移


def test_prev_removed_date_is_carried_over_verbatim():
    """传入 prev_removed_date 时原样沿用（历史已判定的下架日优先于当日）。"""
    first_hit = DS - datetime.timedelta(days=10)
    out = _absent(3, ds=DS, prev_removed_date=first_hit)
    assert out == ("removed", 4, first_hit, "confirmed_absent")
    # 字符串入参同样归一化成 date
    assert _absent(3, ds=DS, prev_removed_date=first_hit.isoformat())[2] == first_hit


def test_prev_removed_date_none_keeps_legacy_behavior():
    """不传 prev_removed_date 时与旧行为完全一致：首次判定落到当日。"""
    assert _absent(2) == ("removed", 3, DS, "confirmed_absent")
    assert _absent(3, ds=DS + datetime.timedelta(days=1))[2] == DS + datetime.timedelta(days=1)


# -------------------------------------------------------------- min_age 与重挂
def test_min_age_blocks_removal_and_relist_starts_fresh_streak():
    # 挂牌 6 天（< 7）：streak 已到 N 也不得判下架
    six_days_ago = DS - datetime.timedelta(days=6)
    assert _absent(2, first_seen_date=six_days_ago) == ("absent", 3, None, "streak")
    # 恰好 7 天：放行
    seven_days_ago = DS - datetime.timedelta(days=7)
    assert _absent(2, first_seen_date=seven_days_ago) == (
        "removed",
        3,
        DS,
        "confirmed_absent",
    )
    # 重挂（新 key）：first_seen_date = 当天、streak 从头开始，7 天内不可能下架
    assert _absent(0, first_seen_date=DS) == ("absent", 1, None, "streak")


def test_min_age_zero_and_negative_age_are_conservative():
    # min_age_days=0 放行当天新挂（调用方显式关闭门槛才有此行为）
    assert _absent(2, first_seen_date=DS, min_age_days=0)[0] == "removed"
    # 首次观测晚于 ds（脏数据 / 时区串日）→ 年龄为负 → 保守不判 removed
    assert _absent(2, first_seen_date=DS + datetime.timedelta(days=1))[0] == "absent"


def test_first_seen_date_none_never_removes():
    """年龄未知 → 永不判 removed（保守方向）。"""
    out = _absent(10, first_seen_date=None)
    assert out == ("absent", 11, None, "streak")
    assert out[2] is None


# ------------------------------------------------------------------ 日期入参归一化
def test_accepts_date_datetime_and_string_equivalently():
    base = ms.decide_removal(False, True, 2, 3, "2026-08-01", "2026-08-31")
    assert base == ("removed", 3, DS, "confirmed_absent")
    assert base == ms.decide_removal(False, True, 2, 3, OLD, DS)
    # datetime（DB 列可能带时分秒）与纯 date 等价
    assert base == ms.decide_removal(False, True, 2, 3, datetime.datetime(2026, 8, 1, 23, 59), DS)
    # removed_date 统一返回 datetime.date，便于直接落 DATE 列
    assert isinstance(base[2], datetime.date)


# -------------------------------------------------------------------- 非法输入
@pytest.mark.parametrize(
    "kwargs",
    [
        {"N": 0},  # N 至少 1
        {"N": -1},
        {"prev_streak": -1},
        {"min_age_days": -1},
        {"N": "3"},  # 类型不对同样是调用方 bug
        {"prev_streak": 1.5},
        {"N": True},  # bool 是 int 子类，不能蒙混过关
    ],
)
def test_invalid_arguments_raise_value_error(kwargs):
    base = dict(
        seen_today=False,
        eligible_today=True,
        prev_streak=0,
        N=3,
        first_seen_date=OLD,
        ds=DS,
        min_age_days=7,
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        ms.decide_removal(**base)


@pytest.mark.parametrize(
    "bad",
    ["2026/08/31", "2026-8-31", "not-a-date", "", "2026-08-31 00:00:00", None, 20260831],
)
def test_bad_dates_raise_value_error(bad):
    # ds 是判定日，错了整条链都会偏移 → 一律响亮失败
    with pytest.raises(ValueError):
        ms.decide_removal(False, True, 0, 3, OLD, bad)
    if bad is None:
        # first_seen_date=None 是**合法**入参（年龄未知 → 保守不判 removed），不是错误
        assert ms.decide_removal(False, True, 0, 3, None, DS) == ("absent", 1, None, "streak")
    else:
        with pytest.raises(ValueError):
            ms.decide_removal(False, True, 0, 3, bad, DS)


# ============================================================================
# 调用层：读库 → project_day（纯计算）→ 写 dwd_listing_market_state
# ============================================================================
class _FakeCursor:
    """按 SQL 里的表名分派；**认不出的 SQL 直接 raise**，绝不返回假真值。"""

    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.log.append((" ".join(sql.split()), params))
        flat = " ".join(sql.split())
        if "CREATE TABLE IF NOT EXISTS" in flat:
            self._conn.ddl_seen += 1
            self._rows = []
        elif flat.startswith("DELETE FROM dwd_listing_market_state WHERE ds=%s"):
            self._conn.deletes.append(params[0])
            for pk in [k for k in self._conn.table if k[0] == params[0]]:
                del self._conn.table[pk]
            self._rows = []
        elif flat.startswith("INSERT INTO dwd_listing_market_state"):
            # 真库主键是 (ds, url_key)：不做当日 DELETE 就重跑 = 主键冲突。
            # Fake 必须同样炸，否则「忘了 DELETE」这个 bug 在测试里是绿的。
            pk = (params[0], params[1])
            if pk in self._conn.table:
                raise AssertionError(f"主键冲突（重跑同一 ds 未先 DELETE 当日）: {pk}")
            self._conn.table[pk] = params
            self._conn.inserts.append(params)
            self._rows = []
        elif "FROM crawl_housing_sale WHERE last_seen_date=%s" in flat:
            self._rows = list(self._conn.sale_rows)
        elif "FROM crawl_housing_rent WHERE last_seen_date=%s" in flat:
            self._rows = list(self._conn.rent_rows)
        elif "FROM crawl_census WHERE ds=%s" in flat:
            self._rows = list(self._conn.census_rows)
        elif "SELECT MAX(ds) FROM dwd_listing_market_state" in flat:
            self._rows = [(self._conn.prev_ds,)]
        elif flat.startswith(
            "SELECT url_key, city, typ, market_status, absent_streak, removed_date,"
        ):
            self._rows = list(self._conn.prev_rows)
        elif "information_schema.columns" in flat:
            self._rows = list(self._conn.info_cols)
        else:
            raise AssertionError(f"Fake 不认识的 SQL（宁可炸也不造假）: {flat}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


def _ddl_columns():
    """从 STATE_DDL 派生 (列名, 类型)：测试里不再维护第三份手写清单。"""
    cols = []
    for line in ms.STATE_DDL.splitlines():
        m = re.match(r"^  ([a-z_]+)\s+([A-Za-z]+(?:\(\d+\))?)", line)
        if m:  # PRIMARY KEY / KEY 是大写开头，自动被排除
            c = re.search(r"COMMENT '([^']*)'", line)
            cols.append((m.group(1), m.group(2), c.group(1) if c else ""))
    return cols


class _FakeConn:
    def __init__(
        self,
        sale_rows=(),
        rent_rows=(),
        census_rows=(),
        prev_ds=None,
        prev_rows=(),
        info_cols=None,
    ):
        self.sale_rows = sale_rows  # (url_key, district, first_seen_date, last_seen_page)
        self.rent_rows = rent_rows
        self.census_rows = census_rows  # (city, typ, complete, first_page, contiguous_pages)
        self.prev_ds = prev_ds
        self.prev_rows = prev_rows  # 9 列，见 read_prev_state
        # 活库实际列；默认与 STATE_DDL 一致（information_schema 的返回形状）
        self.info_cols = _ddl_columns() if info_cols is None else list(info_cols)
        self.log = []
        self.deletes = []
        self.inserts = []
        self.table = {}  # (ds, url_key) → 该行，模拟主键约束
        self.commits = 0
        self.ddl_seen = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.committed = True
        self.log.append(("COMMIT", None))  # 事务边界进日志，便于断言「删+写一次提交」

    def close(self):
        self.closed = True


def _sale(key, city="gz", first_seen=OLD, page=1):
    return (key, city, first_seen, page)


def _census(city="gz", typ="sale", complete=1, first=1, contig=2):
    return (city, typ, complete, first, contig)


def _prev(
    key, streak=0, removed=None, city="gz", typ="sale", page=1, first_seen=OLD, last_obs=None
):
    # 与 read_prev_state 的 SELECT 顺序一致（9 列，含 last_observed_date）
    return (key, city, typ, "absent", streak, removed, first_seen, page, last_obs)


def _project(conn, ds=DS, cutover=None):
    dwd = ms.read_dwd_rows(conn, ds)
    certified = ms.read_census_pages(conn, ds)
    prev_ds, prev = ms.read_prev_state(conn, ds)
    rows = ms.project_day(
        dwd_rows=dwd,
        certified=certified,
        prev_state=prev,
        ds=ds,
        # 默认 cutover 放到很早：这些用例考的是缺席链，不是历史遗留行
        cutover_date=cutover or (ds - datetime.timedelta(days=60)),
    )
    return prev_ds, rows


def test_no_complete_row_makes_whole_cityday_unknown():
    """账本里该 (城,型) 全是 complete=0 → 当日一个 absent 都不许产生。

    用**今天没看到**的 key 来验（缺席候选由前一日状态带进来）：
    seen 优先之后，当天在架的 key 会直接 active，验不到缺席闸门。
    """
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census(complete=0)],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=2)],
    )
    _, rows = _project(conn)
    assert len(rows) == 1
    r = rows[0]
    assert (r["market_status"], r["absent_streak"], r["eligible"]) == ("unknown", 2, 0)
    assert r["eligible_reason"] == "no_certified_pages"


def test_census_without_rows_is_unknown_not_eligible():
    """账本当日无行（不是 complete=0，是压根没账）→ 同样不缺席。"""
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=1)],
    )
    _, rows = _project(conn)
    r = rows[0]
    assert (r["eligible"], r["market_status"], r["absent_streak"]) == (0, "unknown", 1)


def test_page_out_of_certified_range_is_not_eligible():
    """认证页只有 1..2，房源在第 5 页 → 那页今天没扫到，不能算缺席。"""
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census(contig=2)],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", page=5)],
    )
    _, rows = _project(conn)
    r = rows[0]
    assert (r["eligible"], r["eligible_reason"], r["market_status"]) == (
        0,
        "page_out_of_range",
        "unknown",
    )


def test_null_page_is_never_eligible():
    """legacy 行 `last_seen_page IS NULL` → 无页码证据，永久 unknown。"""
    conn = _FakeConn(sale_rows=[_sale("k1", page=None)], census_rows=[_census()])
    _, rows = _project(conn)
    assert (rows[0]["eligible"], rows[0]["eligible_reason"]) == (0, "no_page_evidence")


def test_first_seen_before_cutover_is_not_eligible():
    """今天没看到 + 首见日早于 cutover → 不许当作缺席证据。"""
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census()],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", first_seen=DS - datetime.timedelta(days=30))],
    )
    _, rows = _project(conn, cutover=DS - datetime.timedelta(days=5))
    assert rows[0]["eligible_reason"] == "before_cutover"
    assert rows[0]["market_status"] == "unknown"


def test_seen_before_cutover_is_active_but_still_ineligible():
    """今天在架但首见日早于 cutover → `active` + `eligible=0`（不是 unknown）。

    `eligible` 说的是「有没有资格作**缺席**证据」，是当日属性，与 active 并存不冲突。
    `market_status='active' 且 eligible=0` 是正确状态，不是 bug。
    """
    conn = _FakeConn(census_rows=[_census()])
    conn.sale_rows = [_sale("k1", first_seen=DS - datetime.timedelta(days=30))]
    _, rows = _project(conn, cutover=DS - datetime.timedelta(days=5))
    r = rows[0]
    assert r["market_status"] == "active"
    assert r["seen_today"] == 1
    assert (r["eligible"], r["eligible_reason"]) == (0, "before_cutover")
    assert r["absent_streak"] == 0


def test_seen_and_eligible_is_active_with_eligible_one():
    """今天在架且合格 → active + eligible=1。"""
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    _, rows = _project(conn)
    r = rows[0]
    assert (r["market_status"], r["eligible"], r["eligible_reason"]) == ("active", 1, "ok")


def test_unseen_before_cutover_stays_unknown_and_does_not_advance():
    """今天没看到 + before_cutover → 仍 unknown，streak 不推进（cutover 拦的是缺席）。"""
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census()],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=1, first_seen=DS - datetime.timedelta(days=30))],
    )
    _, rows = _project(conn, cutover=DS - datetime.timedelta(days=5))
    r = rows[0]
    assert r["market_status"] == "unknown"
    assert r["absent_streak"] == 1  # 不推进也不清零
    assert (r["eligible"], r["eligible_reason"]) == (0, "before_cutover")


def test_null_district_falls_back_to_unknown_and_is_not_eligible():
    """DWD.district 可 NULL（历史注释写「区/县」是错的）→ 落 'unknown'。

    'unknown' 对不上 crawl_census 的任何短码 → eligible=0 → unknown，
    正好是保守方向；绝不能让它插进 NOT NULL 的 city 列时报错。
    """
    conn = _FakeConn(sale_rows=[_sale("k1", city=None)], census_rows=[_census()])
    _, rows = _project(conn)
    assert rows[0]["city"] == "unknown"
    assert (rows[0]["eligible"], rows[0]["eligible_reason"]) == (0, "no_certified_pages")
    # seen 优先：今天在架就是 active；「没有缺席资格」不等于「不在架」
    assert rows[0]["market_status"] == "active"


def test_check_state_columns_passes_when_table_matches_ddl():
    conn = _FakeConn()
    assert ms.check_state_columns(conn) == []


def test_check_state_columns_reports_missing_column_and_prints_alter(capsys):
    """缺列必须报错，且输出的修复语句里带缺失列名（不能只报错不给药方）。"""
    conn = _FakeConn(info_cols=[c for c in _ddl_columns() if c[0] != "last_observed_date"])
    problems = ms.check_state_columns(conn)
    assert any("last_observed_date" in p for p in problems)
    out = capsys.readouterr().out
    assert "ALTER TABLE dwd_listing_market_state ADD COLUMN last_observed_date" in out


def test_check_state_columns_flags_narrow_city(capsys):
    """city 窄列要被点名：省外子域原文会被截断，与账本短码 join 必然错位。"""
    cols = [("city", "varchar(8)", c[2]) if c[0] == "city" else c for c in _ddl_columns()]
    problems = ms.check_state_columns(_FakeConn(info_cols=cols))
    assert any("city 宽度不足" in p for p in problems)
    assert "MODIFY city VARCHAR(32) NOT NULL" in capsys.readouterr().out


def test_check_state_columns_flags_missing_comment(capsys):
    """列缺注释也要被点名：MODIFY 漏带 COMMENT 会抹掉「这列装什么」的唯一线索。"""
    cols = [(n, t, "") if n == "city" else (n, t, c) for n, t, c in _ddl_columns()]
    problems = ms.check_state_columns(_FakeConn(info_cols=cols))
    assert any("city 缺注释" in p for p in problems)
    out = capsys.readouterr().out
    assert f"ALTER TABLE {ms.STATE_TABLE} MODIFY COLUMN city" in out
    assert "省外存子域原文" in out  # 修复语句要带上原文注释


def test_main_refuses_to_write_when_table_is_stale(monkeypatch):
    """结构不对就不许算、不许写：强制模式退出 2，影子模式退出 0 但不写一行。"""
    stale = [c for c in _ddl_columns() if c[0] != "seen_today"]
    monkeypatch.setattr(ms, "_connect", lambda dsn=None: _FakeConn(info_cols=stale))
    monkeypatch.setattr(sys, "argv", ["market_state", "--date", DS.isoformat(), "--enforce"])
    assert ms.main() == 2

    conn = _FakeConn(info_cols=stale)
    monkeypatch.setattr(ms, "_connect", lambda dsn=None: conn)
    monkeypatch.setattr(sys, "argv", ["market_state", "--date", DS.isoformat()])
    assert ms.main() == 0  # 影子模式不拦停 DAG
    assert conn.inserts == []


def test_new_key_seen_today_is_active_with_zero_streak():
    """首次出现的 key：无前一日状态 → streak 0、status active。"""
    conn = _FakeConn(sale_rows=[_sale("new1")], census_rows=[_census()], prev_ds=None)
    prev_ds, rows = _project(conn)
    assert prev_ds is None
    assert len(rows) == 1
    r = rows[0]
    assert (r["market_status"], r["absent_streak"], r["seen_today"], r["eligible"]) == (
        "active",
        0,
        1,
        1,
    )
    assert r["decision_reason"] == "seen"


def test_last_observed_date_is_set_when_seen_and_carried_when_absent():
    """`last_observed_date`：观测到在架 = 当日；缺席日沿用上次（不因没看到而清空）。"""
    seen_day = DS
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    _, rows = _project(conn, ds=seen_day)
    assert rows[0]["last_observed_date"] == seen_day

    conn2 = _FakeConn(
        sale_rows=[],
        census_rows=[_census()],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=1, last_obs=DS - datetime.timedelta(days=5))],
    )
    _, rows2 = _project(conn2)
    assert rows2[0]["last_observed_date"] == DS - datetime.timedelta(days=5)


def test_insert_columns_match_state_ddl_exactly():
    """STATE_DDL 与 _INSERT_SQL 必须逐列同名同序。

    两处一旦分叉（或将来有人只改一处），活库上 `CREATE TABLE IF NOT EXISTS`
    不会补列，INSERT 直接报 Unknown column —— 这是本次两份 DDL 打架的根因，钉死它。
    """
    ddl_cols = []
    for line in ms.STATE_DDL.splitlines():
        m = re.match(r"^  ([a-z_]+)\s", line)
        if m:  # 大写的 PRIMARY KEY / KEY 行不匹配 [a-z_]，自动被排除
            ddl_cols.append(m.group(1))
    assert len(ddl_cols) == 15, ddl_cols  # 14 业务列 + created_at
    assert "last_observed_date" in ddl_cols

    m = re.search(r"INSERT INTO \w+ \((.*?)\)\s*VALUES\s*\((.*?)\)", ms._INSERT_SQL, re.S)
    insert_cols = [c.strip() for c in m.group(1).split(",")]
    placeholders = [p.strip() for p in m.group(2).split(",")]
    assert insert_cols == ddl_cols[:-1]  # created_at 有默认值，不参与插入
    assert placeholders == ["%s"] * len(insert_cols)
    # city 必须装得下省外子域原文（guangzhou/zhongshan 都超过 8 字符）
    assert re.search(r"city\s+VARCHAR\(32\)", ms.STATE_DDL)


def test_key_missing_from_dwd_advances_streak_when_eligible():
    """今天 DWD 里没有它（缺席候选）→ 由前一日状态带进来，合格时才 +1。"""
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census()],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=1)],
    )
    prev_ds, rows = _project(conn)
    assert prev_ds == DS - datetime.timedelta(days=1)
    r = rows[0]
    assert (r["market_status"], r["absent_streak"], r["seen_today"]) == ("absent", 2, 0)
    assert r["eligible"] == 1  # 缺席日靠 prev 的 last_seen_page 验页


def test_prev_removed_date_carries_into_next_day():
    """已下架的 key 继续缺席：removed_date 仍是首次判定日，不是当日。"""
    first_hit = DS - datetime.timedelta(days=4)
    conn = _FakeConn(
        sale_rows=[],
        census_rows=[_census()],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=3, removed=first_hit)],
    )
    _, rows = _project(conn)
    r = rows[0]
    assert r["market_status"] == "removed"
    assert r["absent_streak"] == 4
    assert r["removed_date"] == first_hit

    # 遇到不合格日：状态 unknown、streak 不动，但下架日仍是历史事实
    conn2 = _FakeConn(
        sale_rows=[],
        census_rows=[_census(complete=0)],
        prev_ds=DS - datetime.timedelta(days=1),
        prev_rows=[_prev("k1", streak=3, removed=first_hit)],
    )
    _, rows2 = _project(conn2)
    assert (rows2[0]["market_status"], rows2[0]["absent_streak"]) == ("unknown", 3)
    assert rows2[0]["removed_date"] == first_hit


def test_rent_table_with_zero_rows_is_skipped_quietly():
    """rent 表 0 行是正常的（TASK_TYPES=sale），不许报错、不许混进 sale 桶。"""
    conn = _FakeConn(sale_rows=[_sale("k1")], rent_rows=[], census_rows=[_census()])
    _, rows = _project(conn)
    assert [r["url_key"] for r in rows] == ["k1"]
    assert all(r["typ"] == "sale" for r in rows)


def test_write_state_recomputes_whole_day_idempotently():
    """当日重算：每次写都先 DELETE 当日再全量 INSERT，跑两遍不留双份。"""
    conn = _FakeConn(sale_rows=[_sale("k1"), _sale("k2")], census_rows=[_census()])
    for _ in range(2):
        _, rows = _project(conn)
        ms.write_state(conn, DS, rows)
    assert len(rows) == 2
    assert conn.deletes == [DS, DS]  # 两次都先清当日
    assert len(conn.inserts) == 4  # 2 行 × 2 遍，不是累加
    assert conn.ddl_seen == 2  # CREATE TABLE IF NOT EXISTS 幂等
    assert len(conn.table) == 2  # 覆盖而非追加（Fake 对重复主键直接 raise）


def test_rerun_same_ds_leaves_second_result_only():
    """同一 ds 重跑：表里的结果**等于第二次**，第一次的残留不许留下。

    影子期每天都重跑同一 ds、补跑历史日也重跑 —— 漏了 DELETE 就会主键冲突，
    或用 IGNORE/REPLACE 兜底时静默留旧状态（报告照样绿）。
    """
    conn = _FakeConn(sale_rows=[_sale("k1"), _sale("k2")], census_rows=[_census()])
    _, rows1 = _project(conn)
    ms.write_state(conn, DS, rows1)
    assert sorted(pk[1] for pk in conn.table) == ["k1", "k2"]

    # 第二次：k2 今天没被扫到（只出现在前一日状态里）
    conn.prev_ds = DS - datetime.timedelta(days=1)
    conn.prev_rows = [
        (
            r["url_key"],
            r["city"],
            r["typ"],
            r["market_status"],
            r["absent_streak"],
            r["removed_date"],
            r["first_seen_date"],
            r["last_seen_page"],
            r["last_observed_date"],
        )
        for r in rows1
    ]
    conn.sale_rows = [_sale("k1")]
    _, rows2 = _project(conn)
    ms.write_state(conn, DS, rows2)
    assert sorted(pk[1] for pk in conn.table) == ["k1", "k2"]
    # INSERT 第 6 个占位符是 seen_today
    assert conn.table[(DS, "k1")][5] == 1  # k1 今天在架（第二次的结果）
    assert conn.table[(DS, "k2")][5] == 0  # k2 今天缺席，不是第一次在架的旧值


def test_delete_and_insert_share_one_transaction():
    """DELETE 与 INSERT 必须同一事务、最后一次性 commit。

    中途异常若留下「已删未写」的当日，比重复行更糟：下游会读到
    「今天没有任何房源有状态」。
    """
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    _, rows = _project(conn)
    ms.write_state(conn, DS, rows)

    assert conn.commits == 1  # 只提交一次
    kinds = [sql for sql, _ in conn.log]
    delete_at = next(i for i, s in enumerate(kinds) if s.startswith("DELETE FROM"))
    insert_at = next(i for i, s in enumerate(kinds) if s.startswith("INSERT INTO"))
    commit_at = next(i for i, s in enumerate(kinds) if s == "COMMIT")
    assert delete_at < insert_at < commit_at  # 先删、再写、最后提交
    assert kinds.count("COMMIT") == 1


def _run_main(monkeypatch, conn, argv_extra=(), enforce_env="0"):
    monkeypatch.setattr(ms, "_connect", lambda dsn=None: conn)
    monkeypatch.setenv("SPACEFIN_MARKET_STATE_ENFORCE", enforce_env)
    monkeypatch.setattr(sys, "argv", ["market_state", "--date", DS.isoformat(), *argv_extra])
    return ms.main()


def test_shadow_mode_returns_zero_and_writes_state(monkeypatch):
    """影子模式（默认）：写派生表 + 打印日报，退出码 0，不拦停任何东西。"""
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    assert _run_main(monkeypatch, conn) == 0
    assert conn.deletes == [DS]
    assert len(conn.inserts) == 1
    assert conn.closed is True


def test_shadow_mode_never_fails_pipeline_on_db_error(monkeypatch):
    """影子模式的契约：退出码恒 0。连不上库只能进日志，不能拦停 DAG。"""

    def boom(dsn=None):
        raise RuntimeError("MySQL 连不上")

    monkeypatch.setattr(ms, "_connect", boom)
    monkeypatch.setenv("SPACEFIN_MARKET_STATE_ENFORCE", "0")
    monkeypatch.setattr(sys, "argv", ["market_state", "--date", DS.isoformat()])
    assert ms.main() == 0
    # 强制模式才让环境失败冒泡成 2
    monkeypatch.setattr(sys, "argv", ["market_state", "--date", DS.isoformat(), "--enforce"])
    assert ms.main() == 2


def test_dry_run_writes_nothing(monkeypatch):
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    assert _run_main(monkeypatch, conn, ("--dry-run",)) == 0
    assert conn.inserts == [] and conn.deletes == []


def test_first_run_creates_table_before_reading_prev_state(monkeypatch):
    """首轮引导：先建表再读「上一日状态」，否则空库永远起不来（读 prev 会先炸）。"""
    conn = _FakeConn(sale_rows=[_sale("k1")], census_rows=[_census()])
    assert _run_main(monkeypatch, conn) == 0
    kinds = [sql for sql, _ in conn.log]
    ddl = next(i for i, s in enumerate(kinds) if s.startswith("CREATE TABLE IF NOT EXISTS"))
    prev = next(i for i, s in enumerate(kinds) if "SELECT MAX(ds)" in s)
    assert ddl < prev
    assert len(conn.inserts) == 1


def test_shadow_mode_does_not_touch_quality_gate(monkeypatch):
    """影子期不得牵动闸门：本模块不存在任何闸门判定入口。

    用源码级断言钉住 —— 一旦有人把 gate 逻辑引进来，这个用例会先红。
    """
    src = open(os.path.join(ORCH_DIR, "market_state.py"), encoding="utf-8").read()
    assert "crawl_quality_gate" not in src
    assert "post_slack" not in src
