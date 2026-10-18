"""S1.2 采集侧观测证据：raw 按观测日分目录落盘，每行带页码/波次/时间戳。

真跑 `crawl()`（同 test_worker_resume 的做法：只换掉取代理/抓页/解析/sleep），
断言的是**磁盘上实际长出的目录结构和行内容**；再和 S1.1 的 ETL 读取端做一次往返
（worker 写的文件必须正好被 `collect_files(观测日)` 选中、`_page` 必须落到
`last_seen_page`）。写读两端的约定只有跑一遍才知道对上了。
"""

import importlib.util
import json
import os
import re
import sys
import zlib

import pytest

ORCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location("etl_for_worker_observe_test", ORCH_DIR + "/etl.py")
etl = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = etl
sys.path.insert(0, ORCH_DIR)
_SPEC.loader.exec_module(etl)

CITY, TYP = "gz", "sale"
TARGET = 999999
RUN_A = "2026-08-31"
RUN_B = "2026-09-01"
FNAME = "gz_sale_worker-test.jsonl"
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
def worker(load_worker):
    def _loader(**env):
        env.setdefault("WORKER_ID", "worker-test")
        return load_worker(**env)

    return _loader


def _raw_file(out_dir, ds):
    return os.path.join(out_dir, "raw", f"ds={ds}", FNAME)


def _wire(mod, monkeypatch, rdb, on_page=None):
    """换掉外部依赖：每页 2 条真实形态 URL，同页稳定、跨页不同、跨观测日也不同。"""

    def fake_fetch(city, typ, page, proxy_str, proxy_src=""):
        if on_page:
            on_page(page)
        return f'<div class="property">PAGE:{page}</div>'

    def fake_parse(html, city, typ):
        page = html.split("PAGE:")[1].split("<")[0]
        day_salt = zlib.crc32(mod.RUN_DS.encode()) % 10000
        base = 4656241945109500 + day_salt * 1000 + int(page) * 10
        return [
            {"url": f"https://{city}.anjuke.com/prop/view/S{base}"},
            {"url": f"https://{city}.anjuke.com/prop/view/S{base + 1}"},
        ]

    monkeypatch.setattr(mod, "fetch_page", fake_fetch)
    monkeypatch.setattr(mod, "parse_rows", fake_parse)
    proxy = ("1.2.3.4:8000", "qg", False)
    monkeypatch.setattr(mod, "get_proxy", lambda *a, **k: proxy)
    monkeypatch.setattr(mod, "get_proxy_with_source", lambda *a, **k: proxy)
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)


def _run(mod, rdb, out_dir, pages):
    rdb.hset(
        mod._task_key(CITY, TYP),
        mapping={
            "city": CITY,
            "type": TYP,
            "pages": pages,
            "target": TARGET,
            "round": 0,
            "status": "running",
            "finished": "0",
            "finish_reason": "",
        },
    )
    return mod.crawl(rdb, CITY, TYP, pages, TARGET, 0, out_dir)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _flat_jsonls(raw_dir):
    if not os.path.isdir(raw_dir):
        return []
    return sorted(f for f in os.listdir(raw_dir) if f.endswith(".jsonl"))


def test_raw_lands_in_observation_day_dir_not_flat(worker, rdb, monkeypatch, tmp_path):
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb)
    out_dir = str(tmp_path)

    _run(mod, rdb, out_dir, 3)

    raw = os.path.join(out_dir, "raw")
    assert _flat_jsonls(raw) == []  # 扁平文件一个都不许再长出来
    assert os.listdir(os.path.join(raw, f"ds={RUN_A}")) == [FNAME]


def test_missing_run_id_does_not_become_empty_ds(worker, rdb, monkeypatch, tmp_path):
    """漏传 CRAWL_RUN_ID 必须退到 ds=manual（看得见、可被对账点名），而不是 ds= 或当天。"""
    mod = worker(CRAWL_RUN_ID="")
    _wire(mod, monkeypatch, rdb)
    out_dir = str(tmp_path)

    _run(mod, rdb, out_dir, 1)

    assert mod.RUN_DS == "manual"
    assert os.path.isfile(_raw_file(out_dir, "manual"))


def test_every_row_carries_page_wave_and_utc_timestamp(worker, rdb, monkeypatch, tmp_path):
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb)
    rdb.set("spacefin:crawl_wave", "floor")
    out_dir = str(tmp_path)

    _run(mod, rdb, out_dir, 3)

    rows = _read_jsonl(_raw_file(out_dir, RUN_A))
    assert [r["_page"] for r in rows] == [1, 1, 2, 2, 3, 3]
    assert {r["_wave"] for r in rows} == {"floor"}
    assert all(ISO_UTC.match(r["_crawled_at"]) for r in rows)
    assert {r["_source"] for r in rows} == {"qg"}  # 原有元数据不能被挤掉


def test_wave_is_snapshot_at_task_claim_not_per_page(worker, rdb, monkeypatch, tmp_path):
    """波次在领取任务时读一次：中途 master 推进到 depth，本任务已写出的行仍归 floor。

    行级 _wave 只是辅助归因，权威时间线是 master 的 crawl_wave_log 快照；每页重读
    会把同一个任务的行拆到两个波次，事后无法还原任务边界。
    """
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb, on_page=lambda p: rdb.set("spacefin:crawl_wave", "depth"))
    rdb.set("spacefin:crawl_wave", "floor")
    out_dir = str(tmp_path)

    _run(mod, rdb, out_dir, 3)

    rows = _read_jsonl(_raw_file(out_dir, RUN_A))
    assert {r["_wave"] for r in rows} == {"floor"}


def test_two_observation_days_never_share_a_file(worker, rdb, monkeypatch, tmp_path):
    """跨观测日必须各写各的目录：否则 ETL 无法区分「今天爬到」与「昨天爬到的还在」。"""
    out_dir = str(tmp_path)
    mod_a = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod_a, monkeypatch, rdb)
    _run(mod_a, rdb, out_dir, 2)

    mod_b = worker(CRAWL_RUN_ID=RUN_B)
    _wire(mod_b, monkeypatch, rdb)
    _run(mod_b, rdb, out_dir, 2)

    assert sorted(os.listdir(os.path.join(out_dir, "raw"))) == [f"ds={RUN_A}", f"ds={RUN_B}"]
    urls_a = {r["url"] for r in _read_jsonl(_raw_file(out_dir, RUN_A))}
    urls_b = {r["url"] for r in _read_jsonl(_raw_file(out_dir, RUN_B))}
    assert urls_a and urls_b and not (urls_a & urls_b)


def test_same_day_retry_appends_instead_of_overwriting(worker, rdb, monkeypatch, tmp_path):
    """同一观测日内重试只能追加：换名或覆盖都会让当天已经爬到的一半证据消失。"""
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb)
    out_dir = str(tmp_path)

    _run(mod, rdb, out_dir, 2)
    first = len(_read_jsonl(_raw_file(out_dir, RUN_A)))
    rdb.delete(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}")
    _run(mod, rdb, out_dir, 2)

    assert len(_read_jsonl(_raw_file(out_dir, RUN_A))) == first * 2
    assert _flat_jsonls(os.path.join(out_dir, "raw")) == []


# ---------------- 与 S1.1 读取端的往返 ----------------
def test_etl_selects_exactly_the_day_the_worker_wrote(worker, rdb, monkeypatch, tmp_path):
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb)
    out_dir = str(tmp_path)
    _run(mod, rdb, out_dir, 2)
    raw = os.path.join(out_dir, "raw")
    dwd = os.path.join(out_dir, "dwd")

    picked, skipped, _state = etl.collect_files(raw, dwd, False, RUN_A)
    assert picked == [f"ds={RUN_A}/{FNAME}"]
    assert skipped == 0
    # 换一个观测日就必须什么都读不到（旧版会在这里把全部历史重读一遍）
    assert etl.collect_files(raw, dwd, False, RUN_B)[0] == []


def test_etl_loads_worker_rows_with_page_evidence(worker, rdb, monkeypatch, tmp_path):
    mod = worker(CRAWL_RUN_ID=RUN_A)
    _wire(mod, monkeypatch, rdb)
    out_dir = str(tmp_path)
    _run(mod, rdb, out_dir, 3)
    raw = os.path.join(out_dir, "raw")
    picked = etl.collect_files(raw, os.path.join(out_dir, "dwd"), False, RUN_A)[0]

    rows_by_task, raw_count = etl.load_rows(picked, raw)

    assert raw_count == 6
    rows = rows_by_task[(CITY, "sale")]
    assert [r["last_seen_page"] for r in rows] == [1, 1, 2, 2, 3, 3]
    for r in rows:  # 采集侧元数据不得漏进 DWD
        assert not {"_page", "_source", "_wave", "_crawled_at"} & set(r)
