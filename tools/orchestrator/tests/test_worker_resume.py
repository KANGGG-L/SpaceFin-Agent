"""worker 断点续爬页计划 + 无代理收尾（§3.6 / §3.2）。

页计划逻辑内联在 crawl() 里（worker.py:430-448，非本 agent 的文件、不做可测性重构），
所以这里**真跑 crawl()**：真连 Redis(db 15)，只把「取代理 / 抓页 / 解析 / sleep」四个
外部依赖替换掉，断言实际访问的页序列、断点写回值和终态字段。
"""

import pytest

CITY, TYP = "gz", "sale"
TARGET = 999999


@pytest.fixture
def worker(load_worker):
    def _loader(**env):
        env.setdefault("WORKER_ID", "worker-test")
        return load_worker(**env)

    return _loader


def _wire(mod, monkeypatch, rdb, proxy=("1.2.3.4:8000", "qg", False)):
    """替换外部依赖，返回被访问页码的记录列表。"""
    visited = []

    def fake_fetch(city, typ, page, proxy_str, proxy_src=""):
        visited.append(page)
        return f'<div class="property">PAGE:{page}</div>'

    def fake_parse(html, city, typ):
        page = html.split("PAGE:")[1].split("<")[0]
        return [{"url": f"https://{city}.example/{typ}/p{page}/x1"}]

    monkeypatch.setattr(mod, "fetch_page", fake_fetch)
    monkeypatch.setattr(mod, "parse_rows", fake_parse)
    monkeypatch.setattr(mod, "get_proxy", lambda *a, **k: proxy)
    monkeypatch.setattr(mod, "get_proxy_with_source", lambda *a, **k: proxy)
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
    return visited


def _prepare_task(mod, rdb, pages, ckpt=None):
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
    if ckpt is not None:
        rdb.set(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}", ckpt)


def _run(mod, rdb, tmp_path, pages):
    return mod.crawl(rdb, CITY, TYP, pages, TARGET, 0, str(tmp_path))


def test_resume_head_rewind_plus_deep_pages(worker, rdb, monkeypatch, tmp_path):
    """ckpt=10, HEAD_REWIND=2, pages=15 → 先回扫 p1-p2（抓置顶新增），再从 p11 续深。"""
    mod = worker(HEAD_REWIND="2", RESUME_ENABLED="1")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=15, ckpt=10)

    _run(mod, rdb, tmp_path, 15)

    assert visited == [1, 2, 11, 12, 13, 14, 15]
    assert min(visited) >= 1  # 绝不出现 0 或负数页
    # 头部回扫不得把断点写回小页号
    assert rdb.get(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}") == "15"
    st = rdb.hgetall(mod._task_key(CITY, TYP))
    assert st["finished"] == "1"
    assert st["finish_reason"] == "pages_exhausted"
    assert st["pages_done"] == "7"


def test_head_rewind_3(worker, rdb, monkeypatch, tmp_path):
    mod = worker(HEAD_REWIND="3")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=12, ckpt=10)
    _run(mod, rdb, tmp_path, 12)
    assert visited == [1, 2, 3, 11, 12]


def test_ckpt_1_never_yields_zero_or_negative_page(worker, rdb, monkeypatch, tmp_path):
    mod = worker(HEAD_REWIND="2")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=5, ckpt=1)
    _run(mod, rdb, tmp_path, 5)
    assert visited == [1, 2, 3, 4, 5]
    assert min(visited) == 1
    assert len(visited) == len(set(visited))  # 不重复抓同一页


def test_no_checkpoint_starts_from_page_1(worker, rdb, monkeypatch, tmp_path):
    mod = worker(HEAD_REWIND="2")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=4)
    _run(mod, rdb, tmp_path, 4)
    assert visited == [1, 2, 3, 4]
    assert rdb.get(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}") == "4"


def test_ckpt_beyond_pages_only_rescans_head(worker, rdb, monkeypatch, tmp_path):
    """ckpt >= pages：深部已到底，只回扫头部，且断点不得被回退。"""
    mod = worker(HEAD_REWIND="2")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=5, ckpt=15)
    _run(mod, rdb, tmp_path, 5)
    assert visited == [1, 2]
    assert rdb.get(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}") == "15"
    assert rdb.hget(mod._task_key(CITY, TYP), "finished") == "1"


def test_resume_disabled_ignores_checkpoint(worker, rdb, monkeypatch, tmp_path):
    mod = worker(RESUME_ENABLED="0", HEAD_REWIND="2")
    visited = _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=4, ckpt=10)
    _run(mod, rdb, tmp_path, 4)
    assert visited == [1, 2, 3, 4]


def test_dedupe_across_runs_via_url_set(worker, rdb, monkeypatch, tmp_path):
    """第二次跑同样的页：URL set 命中 → 计 dup 而非 new（跨 run 去重）。"""
    mod = worker(HEAD_REWIND="2")
    _wire(mod, monkeypatch, rdb)
    _prepare_task(mod, rdb, pages=3)
    assert _run(mod, rdb, tmp_path, 3) == 3

    rdb.delete(mod.PROGRESS_PREFIX + f"{CITY}:{TYP}")
    _prepare_task(mod, rdb, pages=3)
    assert _run(mod, rdb, tmp_path, 3) == 0
    st = rdb.hgetall(mod._task_key(CITY, TYP))
    assert st["dup_count"] == "3" and st["new_count"] == "0"


@pytest.mark.parametrize("exhausted,reason", [(False, "no_proxy"), (True, "budget_exhausted")])
def test_no_proxy_terminates_with_finish_reason(
    worker, rdb, monkeypatch, tmp_path, exhausted, reason
):
    """取不到代理时必须在有限周期内写终态，否则 Airflow Sensor 永远等不到完成。"""
    mod = worker(NO_PROXY_MAX_CYCLES="2")
    calls = []

    def no_proxy(*a, **k):
        calls.append(1)
        return None, "", exhausted

    monkeypatch.setattr(mod, "get_proxy", no_proxy)
    monkeypatch.setattr(mod, "get_proxy_with_source", no_proxy)
    monkeypatch.setattr(mod, "fetch_page", lambda *a, **k: pytest.fail("must not fetch"))
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
    _prepare_task(mod, rdb, pages=10)

    _run(mod, rdb, tmp_path, 10)

    st = rdb.hgetall(mod._task_key(CITY, TYP))
    assert st["finished"] == "1"
    assert st["finish_reason"] == reason
    assert st["status"] == "done"
    assert len(calls) <= 6 * 2  # 有限次重试，未陷入无限 pause
