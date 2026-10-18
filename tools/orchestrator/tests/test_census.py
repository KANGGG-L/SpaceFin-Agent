"""census：页覆盖/完备性纯函数 + 三源读数（tmp 目录，不连库不连 redis）。

核心不变式：complete=0 的行不向 S3 提供任何页码证据。
所有「判不出来」的输入（无终态、无波次、有页洞、比计划浅）都必须落到 0，
因此下面每个负例都是「防止把没扫到当下架」的一道闸门。
"""

import hashlib
import importlib.util
import json
import os
import sys

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/orchestrator
SPEC = importlib.util.spec_from_file_location(
    "census_under_test", os.path.join(ORCH_DIR, "census.py")
)
cs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cs
sys.path.insert(0, ORCH_DIR)
SPEC.loader.exec_module(cs)


# ------------------------------------------------------------------ page_interval
def test_page_interval_empty_and_single():
    assert cs.page_interval([]) == (0, 0, 0)
    assert cs.page_interval([7]) == (7, 7, 7)
    assert cs.page_interval([0, -3, None, "x"]) == (0, 0, 0)  # 脏值不进区间


def test_page_interval_contiguous():
    assert cs.page_interval([1, 2, 3, 4, 5]) == (1, 5, 5)


def test_page_interval_stops_at_hole():
    # 3 缺失 → 区间止于 2，但 max_seen=5 暴露页洞存在
    assert cs.page_interval([1, 2, 4, 5]) == (1, 2, 5)


def test_page_interval_deep_resume_has_no_head_coverage():
    assert cs.page_interval([46, 47, 48]) == (46, 48, 48)


# ------------------------------------------------------------------------ judge
def _judge(**kw):
    base = dict(
        pages_ok=5,
        first_page=1,
        contiguous=5,
        max_seen=5,
        finish_reason="pages_exhausted",
        planned=5,
        head_rewind=2,
        has_wave=True,
    )
    base.update(kw)
    return cs.judge(**base)


def test_judge_head_wave_complete():
    assert _judge() == (1, "ok")


def test_judge_accepts_empty_pages_as_terminal():
    # 列表到底（ZERO_CARD_RETRY 个 IP 三连确认 0 卡片）是最强的完备证据
    assert _judge(contiguous=3, max_seen=3, finish_reason="empty_pages") == (1, "ok")


def test_judge_rejects_starved_reasons():
    for reason in ("no_proxy", "budget_exhausted", "render_unreachable"):
        assert _judge(finish_reason=reason) == (0, f"reason:{reason}")


def test_judge_rejects_missing_finish_reason():
    # redis 被清 / run 中途被杀 → 无法认证，宁可全部判未知
    assert _judge(finish_reason="") == (0, "no_finish_reason")


def test_judge_rejects_hole():
    assert _judge(pages_ok=4, contiguous=2, max_seen=5) == (0, "gap@3")


def test_judge_requires_head_depth():
    assert _judge(contiguous=1, max_seen=1, pages_ok=1) == (0, "shallow1<2")
    # planned 更小时以 planned 为准：计划本就只到第 1 页
    assert _judge(planned=1, contiguous=1, max_seen=1, pages_ok=1) == (1, "ok")


def test_judge_deep_row_needs_no_head():
    assert _judge(first_page=46, contiguous=100, max_seen=100, pages_ok=55) == (1, "ok")


def test_judge_no_ok_pages_and_no_wave():
    assert _judge(pages_ok=0) == (0, "no_ok_page")
    assert _judge(has_wave=False) == (0, "no_wave")


# ------------------------------------------------- 批次城市一致性（city_mismatch）
def test_judge_flags_whole_city_pollution():
    """页扫全、终态正常，但整批是外地房源 → 必须判 0，否则整城缺席被当成下架。"""
    complete, reason = _judge(city_match_ratio=0.0)
    assert complete == 0
    assert reason.startswith("city_mismatch")
    assert "0.0" in reason and "0.5" in reason


def test_judge_city_mismatch_boundary_equals_min_passes():
    """恰好等于阈值算通过（判定是严格小于）；略低一点即拦下。"""
    assert _judge(city_match_ratio=0.5) == (1, "ok")
    assert _judge(city_match_ratio=0.49)[0] == 0


def test_judge_city_mismatch_respects_custom_min():
    assert _judge(city_match_ratio=0.3, city_match_min=0.9)[0] == 0
    assert _judge(city_match_ratio=0.3, city_match_min=0.2) == (1, "ok")


def test_judge_without_raw_rows_never_flags_city_mismatch():
    """无 raw 行 → ratio=None → 无从判断，不许当成内容错误（向后兼容）。"""
    assert _judge(city_match_ratio=None) == (1, "ok")
    complete, reason = _judge(city_match_ratio=None, pages_ok=0)
    assert complete == 0 and reason == "no_ok_page"


def test_judge_form_checks_precede_city_mismatch():
    """城市错配是内容级，形式故障更基础：页洞/终态先报，不能被 city_mismatch 掩盖。"""
    assert _judge(pages_ok=4, contiguous=2, max_seen=5, city_match_ratio=0.0) == (0, "gap@3")
    assert _judge(finish_reason="no_proxy", city_match_ratio=0.0) == (0, "reason:no_proxy")


def test_city_mismatch_reason_fits_the_column():
    _, reason = _judge(city_match_ratio=1 / 3, city_match_min=0.5)
    assert len(reason) <= 32


def test_read_raw_counts_city_hits_against_the_dispatched_city(tmp_path):
    raw = str(tmp_path / "raw")
    fn = os.path.join(raw, "zh_sale_worker-1.jsonl")
    _append(fn, {"url": "https://zhuhai.anjuke.com/prop/view/A1", "_wave": "floor"})
    _append(fn, {"url": "https://zh.anjuke.com/prop/view/A2", "_wave": "floor"})  # 短码也算本城
    _append(fn, {"url": "https://beijing.anjuke.com/prop/view/A3", "_wave": "floor"})  # 污染
    _append(fn, {"url": "https://gz.anjuke.com/prop/view/A4", "_wave": "floor"})  # 串到别城
    _append(fn, {"url": "", "_wave": "floor"})  # 无 URL：计入 total，不算 hit（保守）

    b = cs.read_raw(raw)[("zh", "sale", "floor", "worker-1")]
    assert (b["city_hit"], b["city_total"]) == (2, 5)
    assert b["rows"] == 5


def test_build_rows_flags_polluted_batch_and_keeps_clean_one():
    """整城污染批次判 city_mismatch；同形态但城市一致的批次照常 complete=1。"""
    stats = {("zh", "sale", "floor", "w1"): {1, 2}, ("gz", "sale", "floor", "w1"): {1, 2}}
    raw = {
        ("zh", "sale", "floor", "w1"): {
            "rows": 10,
            "keys": {"a"},
            "city_hit": 1,
            "city_total": 10,
        },
        ("gz", "sale", "floor", "w1"): {
            "rows": 10,
            "keys": {"b"},
            "city_hit": 9,
            "city_total": 10,
        },
    }
    waveinfo = {
        ("zh", "sale", "floor"): {"reason": "pages_exhausted", "planned": 5},
        ("gz", "sale", "floor"): {"reason": "pages_exhausted", "planned": 5},
    }

    rows = {r["city"]: r for r in cs.build_rows(stats, raw, waveinfo, 2)}

    assert rows["zh"]["complete"] == 0
    assert rows["zh"]["complete_reason"].startswith("city_mismatch")
    assert rows["zh"]["city_match"] == 0.1
    assert rows["gz"]["complete"] == 1 and rows["gz"]["complete_reason"] == "ok"


def test_build_rows_no_raw_bucket_is_untouched_by_city_gate():
    """只扫到页没落行（raw 桶不存在）：ratio=None，判定与加闸门之前逐字相同。"""
    stats = {("gz", "sale", "floor", "w1"): {1, 2}}
    waveinfo = {("gz", "sale", "floor"): {"reason": "target_reached", "planned": 5}}
    r = cs.build_rows(stats, {}, waveinfo, 2)[0]
    assert r["city_match"] is None and (r["city_hit"], r["city_total"]) == (0, 0)
    assert r["complete"] == 1 and r["complete_reason"] == "ok"


def test_build_rows_accepts_raw_bucket_without_city_counters():
    """旧调用方/单测传的 raw 桶没有 city_hit/city_total → 不得崩、也不得判错。"""
    stats = {("gz", "sale", "floor", "w1"): {1, 2}}
    raw = {("gz", "sale", "floor", "w1"): {"rows": 3, "keys": {"a"}}}
    waveinfo = {("gz", "sale", "floor"): {"reason": "target_reached", "planned": 5}}
    r = cs.build_rows(stats, raw, waveinfo, 2)[0]
    assert r["city_match"] is None and r["complete"] == 1


# ------------------------------------------------------------------ 三源读数合并
def _append(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def test_read_stats_keeps_only_ok_pages_from_record_fields(tmp_path):
    """页覆盖只认 status=ok；wave/worker 以记录内字段为准（不是文件名）。"""
    stats = str(tmp_path / "stats")
    fn = os.path.join(stats, "requests_gz_sale_worker-1.jsonl")
    _append(
        fn,
        {
            "city": "gz",
            "type": "sale",
            "page": 1,
            "status": "ok",
            "wave": "floor",
            "worker": "worker-1",
        },
    )
    _append(
        fn,
        {
            "city": "gz",
            "type": "sale",
            "page": 2,
            "status": "blocked",
            "wave": "floor",
            "worker": "worker-1",
        },
    )
    _append(
        fn,
        {
            "city": "gz",
            "type": "sale",
            "page": 3,
            "status": "ok",
            "wave": "floor",
            "worker": "worker-9",
        },
    )
    _append(fn, {"city": "gz", "type": "sale", "page": 4})  # 无 status → 丢弃
    _append(fn, {"city": "sz", "page": 1, "status": "ok"})  # 无 type → 丢弃
    assert cs.read_stats(stats) == {
        ("gz", "sale", "floor", "worker-1"): {1},
        ("gz", "sale", "floor", "worker-9"): {3},
    }


def test_read_raw_buckets_by_filename_worker_and_row_wave(tmp_path):
    raw = str(tmp_path / "raw")
    _append(
        os.path.join(raw, "gz_sale_worker-1.jsonl"),
        {"url": "https://gz.anjuke.com/sale/123", "_wave": "floor", "_page": 1},
    )
    _append(
        os.path.join(raw, "gz_sale_worker-1.jsonl"),
        {"url": "https://gz.anjuke.com/sale/123", "_wave": "rescue", "_page": 1},
    )
    rw = cs.read_raw(raw)
    # 同一 URL 落到不同波次的两个桶，各算各的（digest 用于比「同日是否同一批」）
    assert rw[("gz", "sale", "floor", "worker-1")]["rows"] == 1
    assert rw[("gz", "sale", "rescue", "worker-1")]["rows"] == 1
    assert (
        rw[("gz", "sale", "floor", "worker-1")]["keys"]
        == (rw[("gz", "sale", "rescue", "worker-1")]["keys"])
    )


def test_build_rows_marks_head_shallow_incomplete(tmp_path):
    """blocked 页不算 ok → 该桶只剩 1 页，够不上 head_rewind=2，行必须不完备。"""
    stats = {("gz", "sale", "floor", "worker-1"): {1}}
    raw = {("gz", "sale", "floor", "worker-1"): {"rows": 1, "keys": {"a"}}}
    waveinfo = {("gz", "sale", "floor"): {"reason": "pages_exhausted", "planned": 5}}
    rows = cs.build_rows(stats, raw, waveinfo, 2)
    assert len(rows) == 1
    r = rows[0]
    assert r["complete"] == 0
    assert r["complete_reason"] == "shallow1<2"
    assert (r["pages_ok"], r["first_page"], r["contiguous_pages"], r["scope"]) == (
        1,
        1,
        1,
        "head",
    )
    assert r["rows_emitted"] == 1 and r["distinct_keys"] == 1


def test_build_rows_surfaces_real_page_hole(tmp_path):
    """ok 页 1,2,3,5（4 blocked）→ 区间止于 3 且 max_seen=5，认证只能到 3。"""
    stats = {("gz", "sale", "floor", "w1"): {1, 2, 3, 5}}
    waveinfo = {("gz", "sale", "floor"): {"reason": "target_reached", "planned": 5}}
    rows = cs.build_rows(stats, {}, waveinfo, 2)
    assert rows[0]["complete"] == 0
    assert rows[0]["complete_reason"] == "gap@4"
    assert rows[0]["contiguous_pages"] == 3


def test_read_raw_skips_illegal_names(tmp_path):
    raw = str(tmp_path / "raw")
    _append(os.path.join(raw, "README.jsonl"), {"url": "x"})
    _append(os.path.join(raw, "xx_sale_worker-1.jsonl"), {"url": "https://xx.anjuke.com/s/1"})
    _append(os.path.join(raw, "gz_notype_worker-1.jsonl"), {"url": "https://gz.anjuke.com/s/1"})
    assert cs.read_raw(raw) == {}


def test_read_stats_missing_dir_is_empty(tmp_path):
    assert cs.read_stats(str(tmp_path / "nope")) == {}
    assert cs.read_raw(str(tmp_path / "nope")) == {}


# ------------------------------------------------------------------- 占位与认证集
def test_fill_missing_tasks_covers_every_city_type():
    rows = cs.fill_missing_tasks([])
    assert len(rows) == len(cs.CITY_NAMES) * len(cs.CRAWL_TYPES)
    assert all(r["complete"] == 0 and r["complete_reason"] == "no_observation" for r in rows)
    assert all(r["wave"] == cs.NO_WAVE and r["worker"] == cs.NO_WORKER for r in rows)


def test_fill_missing_tasks_keeps_real_rows():
    real = [
        {
            "city": "gz",
            "typ": "sale",
            "wave": "floor",
            "worker": "w1",
            "scope": "head",
            "pages_planned": "",
            "pages_ok": 1,
            "first_page": 1,
            "contiguous_pages": 1,
            "finish_reason": "ok",
            "rows_emitted": 0,
            "distinct_keys": 0,
            "key_digest": "",
            "complete": 1,
            "complete_reason": "ok",
        }
    ]
    rows = cs.fill_missing_tasks(real)
    assert sum(1 for r in rows if r["city"] == "gz" and r["typ"] == "sale") == 1
    assert len(rows) == len(cs.CITY_NAMES) * len(cs.CRAWL_TYPES)


def test_certified_pages_only_from_complete_rows():
    def row(city, typ, wave, first, contig, complete):
        return {
            "city": city,
            "typ": typ,
            "wave": wave,
            "first_page": first,
            "contiguous_pages": contig,
            "complete": complete,
        }

    rows = [
        row("gz", "sale", "floor", 1, 5, 1),
        row("gz", "sale", "depth", 6, 9, 0),  # 不完备 → 不认证
        row("sz", "sale", "depth", 46, 100, 1),
    ]
    out = cs.certified_pages(rows)
    assert out[("gz", "sale")] == set(range(1, 6))
    # 不完备的 depth 行若被误信，6..9 就会混进来 → 那 4 页上的缺席会被当成成立
    assert not out[("gz", "sale")] & set(range(6, 10))
    assert out[("sz", "sale")] == set(range(46, 101))


def test_key_digest_stable_and_absent_when_no_rows():
    raw = {("gz", "sale", "floor", "w1"): {"rows": 2, "keys": {"b", "a"}}}
    rows = cs.build_rows({}, raw, {}, 2)
    assert rows[0]["key_digest"] == hashlib.sha1(b"a,b").hexdigest()
    assert rows[0]["distinct_keys"] == 2
    assert rows[0]["complete_reason"] == "no_ok_page"

    # 只扫到页、一行没落（新城/空列表）→ digest 为空，不能伪造「比对通过」
    rows = cs.build_rows({("gz", "sale", "floor", "w1"): {1, 2}}, {}, {}, 2)
    assert rows[0]["key_digest"] == ""
    assert rows[0]["distinct_keys"] == 0
