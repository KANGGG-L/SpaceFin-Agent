"""S1 观测语义：ETL 只观测「本 ds 目录」，观测日/页码/首见日三件事必须是真话。

回归的三条实测缺陷（2026-08-30 对账）：
- collect_files 扫 raw 顶层扁平文件 → 每晚重读整批历史语料并重写 DWD
  （inserted=0 / updated=28,619 / 25,241 行 last_seen_date 被刷成当天）；
- load_rows 丢掉了采集侧的页码证据 → 闸门无法区分「今天在架」与「ETL 恰好又读了它」；
- write_lake 对每条记录硬写 first_seen_date=运行日 → 湖分区宣称当天所有房源首次出现。

不连真实库：用 _Conn 顶替 pymysql 连接。
"""

import datetime
import importlib.util
import json
import os
import sys

import pyarrow.parquet as pq
import pytest

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/orchestrator
SPEC = importlib.util.spec_from_file_location(
    "etl_under_observe_test", os.path.join(ORCH_DIR, "etl.py")
)
etl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = etl
sys.path.insert(0, ORCH_DIR)
SPEC.loader.exec_module(etl)

# 真实形态的房源 ID：make_url_key 的出售正则是 [A-Za-z0-9]+，遇到 `_` 就截断，
# 所以形如 P123_1 / P123_2 的两条 URL 会归并成同一个键——测试样本必须用不同 ID。
URL_A = "https://gz.anjuke.com/prop/view/S4656241945109512?soj_info=abc"
URL_B = "https://gz.anjuke.com/prop/view/S4656241945109513"
URL_RENT = "https://gz.zu.anjuke.com/fangyuan/4688937808519174"
KEY_A = etl.make_url_key(URL_A)
KEY_B = etl.make_url_key(URL_B)
KEY_RENT = etl.make_url_key(URL_RENT)


def test_fixture_keys_are_distinct_and_type_namespaces_do_not_overlap():
    """write_lake 的跨任务去重是全局 seen：只有 sale/rent 键空间互不相交才是安全的。"""
    assert KEY_A != KEY_B
    assert KEY_A.startswith("sale:") and KEY_RENT.startswith("rent:")


def _write_jsonl(raw_dir, rel, rows):
    path = os.path.join(raw_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _row(url=URL_A, **extra):
    r = {
        "url": url,
        "title": "天河三居",
        "community": "天河北苑小区",
        "district": "gz",
        "area_sqm": 96.5,
        "total_price_wan": 520,
    }
    r.update(extra)
    return r


# ----------------------------------------------------------------------------
# collect_files：观测范围 = 本 ds 目录
# ----------------------------------------------------------------------------


def test_collect_scans_only_the_target_ds_dir(tmp_path):
    """核心修复：只读 raw/ds=<date>/，其他观测日的历史文件一个都不许进本次观测。"""
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-01/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "ds=2026-09-02/sz_sale_1.jsonl", [_row()])

    files, skipped, state = etl.collect_files(raw, str(tmp_path / "out"), False, "2026-09-02")

    assert files == ["ds=2026-09-02/gz_sale_1.jsonl", "ds=2026-09-02/sz_sale_1.jsonl"]
    assert skipped == 0
    assert state == {}


def test_collect_flat_legacy_files_are_not_read_by_default(tmp_path):
    """迁移前的扁平语料留在 raw 顶层：日常增量绝不能再读它们，否则日期语义当场作废。"""
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "gz_sale_worker-1.jsonl", [_row()])  # 历史扁平布局

    files, _, _ = etl.collect_files(raw, str(tmp_path / "out"), False, "2026-09-02")

    assert files == ["ds=2026-09-02/gz_sale_1.jsonl"]


def test_collect_legacy_flat_requires_the_explicit_flag(tmp_path):
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "gz_sale_worker-1.jsonl", [_row()])
    _write_jsonl(raw, "sz_sale_worker-1.jsonl", [_row()])

    files, _, _ = etl.collect_files(
        raw, str(tmp_path / "out"), False, "2026-08-24", legacy_flat=True
    )

    # 状态键是裸文件名（不含 ds 目录），所以同一份内容只会再被处理一次，不会每晚重来
    assert files == ["gz_sale_worker-1.jsonl", "sz_sale_worker-1.jsonl"]
    _, _, state = etl.collect_files(
        raw, str(tmp_path / "out"), False, "2026-08-24", legacy_flat=True
    )
    assert state == {}


def test_collect_missing_target_ds_dir_yields_zero_rows_not_an_error(tmp_path):
    """没有本观测日的目录 = 今天什么都没爬到，必须走「0 行 → 闸门 exit 2」而不是崩掉。"""
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-01/gz_sale_1.jsonl", [_row()])
    out = str(tmp_path / "out")

    files, skipped, state = etl.collect_files(raw, out, False, "2026-09-02")

    assert files == [] and skipped == 0
    assert isinstance(state, dict)


def test_collect_raises_when_raw_dir_absent(tmp_path):
    with pytest.raises(SystemExit):
        etl.collect_files(str(tmp_path / "nowhere"), str(tmp_path / "out"), False, "2026-09-02")


def test_same_day_rerun_skips_processed_files(tmp_path):
    raw = str(tmp_path / "raw")
    out = str(tmp_path / "out")
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "ds=2026-09-02/sz_sale_1.jsonl", [_row()])

    files, skipped, state = etl.collect_files(raw, out, False, "2026-09-02")
    for rel in files:
        etl.mark_processed(state, rel, raw)
    etl._save_state(out, state)

    files2, skipped2, _ = etl.collect_files(raw, out, False, "2026-09-02")
    assert files2 == []
    assert skipped2 == 2


def test_backfill_reprocesses_target_ds_only(tmp_path):
    """--backfill 的语义是「重跑本观测日」，不是「把历史上所有 ds 都读一遍」。"""
    raw = str(tmp_path / "raw")
    out = str(tmp_path / "out")
    _write_jsonl(raw, "ds=2026-09-01/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_1.jsonl", [_row()])

    files, _, state = etl.collect_files(raw, out, False, "2026-09-02")
    for rel in files:
        etl.mark_processed(state, rel, raw)
    etl._save_state(out, state)

    files2, skipped2, _ = etl.collect_files(raw, out, True, "2026-09-02")
    assert files2 == ["ds=2026-09-02/gz_sale_1.jsonl"]
    assert skipped2 == 0


def test_state_key_is_relpath_so_next_day_same_filename_is_not_skipped(tmp_path):
    """worker-1 每晚都写同名文件：状态键必须含 ds 目录，否则第二天的它会被当成已处理。"""
    raw = str(tmp_path / "raw")
    out = str(tmp_path / "out")
    _write_jsonl(raw, "ds=2026-09-01/gz_sale_worker-1.jsonl", [_row()])

    files, _, state = etl.collect_files(raw, out, False, "2026-09-01")
    for rel in files:
        etl.mark_processed(state, rel, raw)
    etl._save_state(out, state)

    _write_jsonl(raw, "ds=2026-09-02/gz_sale_worker-1.jsonl", [_row()])
    files2, skipped2, _ = etl.collect_files(raw, out, False, "2026-09-02")
    assert files2 == ["ds=2026-09-02/gz_sale_worker-1.jsonl"]
    assert skipped2 == 0


def test_appended_file_is_reprocessed(tmp_path):
    """mtime+size 指纹是幂等判据的一部分：追加写入的文件必须重新处理。"""
    raw = str(tmp_path / "raw")
    out = str(tmp_path / "out")
    rel = "ds=2026-09-02/gz_sale_1.jsonl"
    _write_jsonl(raw, rel, [_row()])

    files, _, state = etl.collect_files(raw, out, False, "2026-09-02")
    for r in files:
        etl.mark_processed(state, r, raw)
    etl._save_state(out, state)

    with open(os.path.join(raw, rel), "a", encoding="utf-8") as f:
        f.write(json.dumps(_row(url=URL_B)) + "\n")

    files2, skipped2, _ = etl.collect_files(raw, out, False, "2026-09-02")
    assert files2 == [rel]
    assert skipped2 == 0


def test_list_ds_dirs(tmp_path):
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "ds=2026-09-01/gz_sale_1.jsonl", [_row()])
    _write_jsonl(raw, "legacy_gz/gz_sale_1.jsonl", [_row()])
    (tmp_path / "raw" / "not_a_dir.txt").write_text("x")

    assert etl.list_ds_dirs(raw) == ["ds=2026-09-01", "ds=2026-09-02"]
    assert etl.list_ds_dirs(str(tmp_path / "nowhere")) == []


# ----------------------------------------------------------------------------
# load_rows：页码证据 + 文件名解析
# ----------------------------------------------------------------------------


def test_load_rows_parses_city_type_from_basename_not_ds_path(tmp_path):
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-02/gz_sale_worker-1.jsonl", [_row()])

    rows_by_task, raw_count = etl.load_rows(["ds=2026-09-02/gz_sale_worker-1.jsonl"], raw)

    assert raw_count == 1
    assert list(rows_by_task) == [("gz", "sale")]


def test_load_rows_turns_page_meta_into_last_seen_page(tmp_path):
    raw = str(tmp_path / "raw")
    _write_jsonl(
        raw,
        "ds=2026-09-02/gz_sale_worker-1.jsonl",
        [
            _row(
                url=URL_A, _page=7, _source="qg", _wave="depth", _crawled_at="2026-09-02T01:00:00"
            ),
            _row(url=URL_B),
        ],
    )

    rows_by_task, _ = etl.load_rows(["ds=2026-09-02/gz_sale_worker-1.jsonl"], raw)
    rows = rows_by_task[("gz", "sale")]

    assert [r["last_seen_page"] for r in rows] == [7, None]
    # 采集侧元数据不许渗进 DWD 行：表里没有这些列
    for meta in ("_page", "_source", "_wave", "_crawled_at"):
        assert all(meta not in r for r in rows)


def test_load_rows_fangyuan_maps_to_rent_table(tmp_path):
    raw = str(tmp_path / "raw")
    _write_jsonl(raw, "ds=2026-09-02/gz_fangyuan_worker-1.jsonl", [_row()])

    rows_by_task, _ = etl.load_rows(["ds=2026-09-02/gz_fangyuan_worker-1.jsonl"], raw)

    assert list(rows_by_task) == [("gz", "rent")]


# ----------------------------------------------------------------------------
# upsert：first_seen 保留 / last_seen_* 恒更新
# ----------------------------------------------------------------------------


def test_upsert_sql_keeps_first_seen_and_overwrites_observation_pair():
    cols = [
        "url_key",
        "url",
        "title",
        "first_seen_date",
        "last_seen_date",
        "last_seen_page",
        "days_on_market",
        "geocode_status",
        "source",
    ]
    sql = etl._upsert_sql("sale", cols)
    update_part = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]

    # first_seen_date 不作为更新目标出现（days_on_market 重算时读它是合法的）
    assert "first_seen_date =" not in update_part
    assert "last_seen_date = VALUES(last_seen_date)" in update_part
    # COALESCE 会留下「日期是今天、页码是上周」的错配 → 页码必须无条件覆盖
    assert "last_seen_page = VALUES(last_seen_page)" in update_part
    assert "last_seen_page = COALESCE" not in update_part
    assert "days_on_market = DATEDIFF(VALUES(last_seen_date), first_seen_date)" in update_part
    assert "etl_ts = CURRENT_TIMESTAMP" in update_part


class _FakeConn:
    """只回答三类语句：information_schema 列/索引存在性、url_key 的 first_seen_date、INSERT。

    在 execute 内就把答案备好（与真库的游标语义一致：fetch* 取的是最近一次 execute
    的结果），这样每个用例不必再为「怎么作答」写一份子类。
    """

    def __init__(self, existing_columns=None, known_first_seen=None, existing_indexes=None):
        self.existing_columns = set(existing_columns or [])
        self.existing_indexes = set(existing_indexes or [])
        self.known_first_seen = dict(known_first_seen or {})
        self.executed = []
        self.commits = 0
        self._one = None
        self._all = []
        # 按表记录本会话里 ALTER 出来的列/索引：真库上 ALTER 是持久的，fake 必须同样持久，
        # 否则「重复运行不重复 ALTER」这类幂等断言会退化成空转（实现怎么改都能绿）。
        # 必须按表分账——两张 DWD 表各自长出自己的列，补完 sale 不代表 rent 也有。
        self._added = {}

    def _added_set(self, tbl, kind):
        return self._added.setdefault(tbl, {"cols": set(), "idx": set()})[kind]

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def _answer(self, sql, params):
        flat = " ".join(sql.split())
        if flat.startswith("ALTER TABLE") and " ADD COLUMN " in flat:
            tbl = flat.split()[2].split(".")[-1]
            self._added_set(tbl, "cols").add(flat.split(" ADD COLUMN ", 1)[1].split()[0])
        elif flat.startswith("ALTER TABLE") and " ADD INDEX " in flat:
            tbl = flat.split()[2].split(".")[-1]
            self._added_set(tbl, "idx").add(flat.split(" ADD INDEX ", 1)[1].split()[0])
        elif "information_schema.columns" in flat:
            tbl, col = params[1], params[2]
            self._one = (1 if col in (self.existing_columns | self._added_set(tbl, "cols")) else 0,)
        elif "information_schema.statistics" in flat:
            tbl, idx = params[1], params[2]
            self._one = (1 if idx in (self.existing_indexes | self._added_set(tbl, "idx")) else 0,)
        elif flat.startswith("SELECT url_key, first_seen_date"):
            self._all = [
                (k, self.known_first_seen[k]) for k in params if k in self.known_first_seen
            ]


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        self._conn._answer(sql, params)

    def executemany(self, sql, seq):
        self._conn.executed.append((sql, list(seq)))

    def fetchone(self):
        return self._conn._one

    def fetchall(self):
        return self._conn._all

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _executed_alters(conn):
    return [" ".join(s.split()) for s, _ in conn.executed if s.strip().startswith("ALTER TABLE")]


def test_ensure_columns_alters_only_missing_columns():
    """CREATE TABLE IF NOT EXISTS 对存量表不会长出缺列，只靠 --init-db 补 = 活库永远没新列。"""
    conn = _FakeConn(existing_columns={"geocode_status"})
    etl._ensure_columns(conn)

    alters = _executed_alters(conn)
    missing_cols = [c for c, _ in etl._DWD_COLUMNS if c != "geocode_status"]
    # 每表补全部缺列 + 全部缺索引
    assert len(alters) == len(etl._DWD_TABLES) * (len(missing_cols) + len(etl._DWD_INDEXES))
    assert sum("ADD COLUMN last_seen_page" in a for a in alters) == len(etl._DWD_TABLES)
    assert not any("geocode_status" in a for a in alters)
    assert conn.commits == 1


def test_ensure_columns_is_noop_when_every_column_exists():
    conn = _FakeConn(
        existing_columns={c for c, _ in etl._DWD_COLUMNS},
        existing_indexes={n for n, _ in etl._DWD_INDEXES},
    )
    etl._ensure_columns(conn)
    assert _executed_alters(conn) == []


def test_ensure_columns_adds_missing_index_even_when_all_columns_exist():
    """列齐了不代表索引在：索引和列一样不会在活库自己长出来，必须单独查 statistics 补。"""
    conn = _FakeConn(existing_columns={c for c, _ in etl._DWD_COLUMNS})
    etl._ensure_columns(conn)

    alters = _executed_alters(conn)
    assert len(alters) == len(etl._DWD_TABLES)
    for name, ddl in etl._DWD_INDEXES:
        assert sum(f"ADD INDEX {name} {ddl}" in a for a in alters) == len(etl._DWD_TABLES)
    assert not any("ADD COLUMN" in a for a in alters)


def test_ensure_columns_is_idempotent_across_repeated_runs():
    """每次 main 都跑一遍：第二次运行不得再发任何 ALTER（列与索引都只补一次）。"""
    conn = _FakeConn(existing_columns={"geocode_status"})
    etl._ensure_columns(conn)
    first_run = _executed_alters(conn)
    assert first_run  # 前置：首次运行确实补了东西，否则本用例是空转

    conn.executed.clear()
    etl._ensure_columns(conn)
    assert _executed_alters(conn) == []


def test_s3_lifecycle_columns_are_registered_for_both_dwd_tables():
    """market_status/removed_date/unit_sig 三列在活库只会由 _ensure_columns 补出。"""
    names = {c for c, _ in etl._DWD_COLUMNS}
    assert {"market_status", "removed_date", "unit_sig"} <= names
    idx_names = {n for n, _ in etl._DWD_INDEXES}
    assert "idx_status_last_seen" in idx_names


def test_existing_first_seen_returns_only_keys_already_in_db():
    conn = _FakeConn(known_first_seen={KEY_A: datetime.date(2026, 8, 1)})
    found = etl._existing_first_seen(conn, "sale", [KEY_A, KEY_B])
    assert found == {KEY_A: datetime.date(2026, 8, 1)}


def test_upsert_dwd_reports_first_seen_and_page_value():
    """inserted/updated 判据与落湖真实首见日同源：一次 IN 查询两用。"""
    first = datetime.date(2026, 8, 1)
    conn = _FakeConn(known_first_seen={KEY_A: first})
    rows = [
        {"url": URL_A, "last_seen_page": 7},
        {"url": URL_B, "last_seen_page": None},
    ]
    res = etl.upsert_dwd(conn, "sale", rows, "2026-09-02", "daily")

    assert res["updated"] == 1 and res["inserted"] == 1
    assert res["first_seen"] == {KEY_A: first}

    insert_sql, seq = [e for e in conn.executed if e[0].strip().startswith("INSERT")][0]
    cols = insert_sql.split("(", 1)[1].split(")")[0].split(", ")
    page_idx = cols.index("last_seen_page")
    key_idx = cols.index("url_key")
    by_key = {v[key_idx]: v for v in seq}
    assert by_key[KEY_A][page_idx] == 7
    assert by_key[KEY_B][page_idx] is None


# ----------------------------------------------------------------------------
# write_lake：湖分区的 first_seen_date 必须是真实首见日
# ----------------------------------------------------------------------------


def test_write_lake_uses_read_back_first_seen_not_run_date(tmp_path):
    lake = str(tmp_path / "lake")
    rows_by_task = {
        ("gz", "sale"): [{"url": URL_A, "last_seen_page": 7}, {"url": URL_B, "last_seen_page": 3}]
    }
    first_seen = {("sale", KEY_A): datetime.date(2026, 8, 1)}

    total = etl.write_lake(lake, "2026-09-02", rows_by_task, "daily", first_seen)

    assert total == 2
    sub = os.path.join(lake, "dt=2026-09-02", "type=sale", "city=gz")
    tbl = pq.read_table(sub).to_pylist()
    by_key = {r["url_key"]: r for r in tbl}
    # 老房源保留库里真实的首见日；本次新入库的键才等于观测日
    assert str(by_key[KEY_A]["first_seen_date"]) == "2026-08-01"
    assert str(by_key[KEY_B]["first_seen_date"]) == "2026-09-02"
    assert all(str(r["last_seen_date"]) == "2026-09-02" for r in tbl)


def test_write_lake_keeps_sale_and_rent_partitions_independent(tmp_path):
    """同城同日既有出售又有出租时，两个 type 分区各自取自己表的真实首见日。"""
    lake = str(tmp_path / "lake")
    rows_by_task = {("gz", "sale"): [{"url": URL_A}], ("gz", "rent"): [{"url": URL_RENT}]}
    first_seen = {
        ("sale", KEY_A): datetime.date(2026, 7, 1),
        ("rent", KEY_RENT): datetime.date(2026, 8, 15),
    }

    total = etl.write_lake(lake, "2026-09-02", rows_by_task, "daily", first_seen)

    assert total == 2
    sale = pq.read_table(os.path.join(lake, "dt=2026-09-02", "type=sale", "city=gz")).to_pylist()
    rent = pq.read_table(os.path.join(lake, "dt=2026-09-02", "type=rent", "city=gz")).to_pylist()
    assert str(sale[0]["first_seen_date"]) == "2026-07-01"
    assert str(rent[0]["first_seen_date"]) == "2026-08-15"
