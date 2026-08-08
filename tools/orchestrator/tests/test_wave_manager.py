"""波次状态机（Wave State Machine: floor -> rescue -> depth -> done）单元测试。"""

import json

import pytest
from orchmods import get_json, running_master, seed_pool, seed_tasks

RUN = "2026-08-04"


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=RUN)


def test_wave_tick_initializes_floor_wave(master, rdb):
    rdb.delete(master.WAVE_KEY)
    master._wave_tick(rdb)

    assert rdb.get(master.WAVE_KEY) == "floor"
    assert rdb.llen(master.TASK_QUEUE) == 42
    gz_task = rdb.hgetall(master._task_key("gz", "sale"))
    assert int(gz_task["pages"]) == master.WAVE_FLOOR_PAGES
    assert gz_task["wave"] == "floor"


def test_floor_wave_targets_42_tasks_pages_5(master, rdb):
    targets = master._get_wave_targets(rdb, "floor")
    assert len(targets) == 42
    for t in targets:
        assert t["pages"] == master.WAVE_FLOOR_PAGES
        assert t["type"] in ("fangyuan", "sale")


def test_floor_wave_pop_order_fifo(master, rdb):
    """floor 波次正序 LPUSH，worker 从队尾 RPOP 时严格按 targets 顺序弹出（FIFO，gz 优先）。"""
    master._open_wave(rdb, "floor")
    expected_targets = master._get_wave_targets(rdb, "floor")
    popped = []
    while True:
        raw = rdb.rpop(master.TASK_QUEUE)
        if not raw:
            break
        d = json.loads(raw)
        popped.append((d["city"], d["type"]))

    expected_order = [(t["city"], t["type"]) for t in expected_targets]
    assert popped == expected_order
    assert popped[0] == ("gz", "fangyuan")
    assert popped[1] == ("gz", "sale")


def test_rescue_wave_filters_only_failed_tasks(master, rdb):
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")
    rdb.hset(master._task_key("gz", "sale"), "finish_reason", "fail_budget")
    rdb.hset(master._task_key("sz", "fangyuan"), "finish_reason", "no_proxy")

    targets = master._get_wave_targets(rdb, "rescue")
    target_pairs = {(t["city"], t["type"]) for t in targets}
    assert target_pairs == {("gz", "sale"), ("sz", "fangyuan")}
    for t in targets:
        assert t["pages"] == master.WAVE_FLOOR_PAGES


def test_rescue_wave_pop_order_fifo(master, rdb):
    """rescue 波次正序 LPUSH，worker 从队尾 RPOP 时严格按筛选任务的 targets 顺序弹出。"""
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")
    rdb.hset(master._task_key("gz", "sale"), "finish_reason", "fail_budget")
    rdb.hset(master._task_key("sz", "fangyuan"), "finish_reason", "no_proxy")
    rdb.hset(master._task_key("dg", "sale"), "finish_reason", "fail_budget")

    expected_targets = master._get_wave_targets(rdb, "rescue")
    master._open_wave(rdb, "rescue")

    popped = []
    while True:
        raw = rdb.rpop(master.TASK_QUEUE)
        if not raw:
            break
        d = json.loads(raw)
        popped.append((d["city"], d["type"]))

    expected_order = [(t["city"], t["type"]) for t in expected_targets]
    assert popped == expected_order


def test_rescue_wave_skips_when_no_failed_tasks(master, rdb):
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")
    rdb.set(master.WAVE_KEY, "floor")

    master._wave_tick(rdb)
    assert rdb.get(master.WAVE_KEY) == "depth"


def test_depth_wave_targets_prioritizes_top_cities_and_checks_budget(master, rdb):
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 10)
    rdb.set(f"{master.IP_USED_PREFIX}sz:fangyuan", 20)
    rdb.set(f"{master.IP_USED_PREFIX}zh:sale", 20)
    rdb.hset(master._task_key("fs", "fangyuan"), "finish_reason", "not_found")

    targets = master._get_wave_targets(rdb, "depth")
    target_pairs = [(t["city"], t["type"]) for t in targets]

    assert ("zh", "sale") not in target_pairs
    assert ("fs", "fangyuan") not in target_pairs

    top_targets = target_pairs[:4]
    for c, _ in top_targets:
        assert c in master.BUDGET_TOP_CITIES

    for t in targets:
        if t["type"] == "fangyuan":
            assert t["pages"] == master.PAGES_RENT
        else:
            assert t["pages"] == master.PAGES_SALE


def test_depth_wave_pop_order_prioritizes_top_cities(master, rdb):
    """depth 波次正序 LPUSH，worker 从队尾 RPOP 时广深头部优先弹出。"""
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")
    for t in master.DEFAULT_TASKS:
        rdb.set(f"{master.IP_USED_PREFIX}{t['city']}:{t['type']}", 0)

    master._open_wave(rdb, "depth")
    popped = []
    while True:
        raw = rdb.rpop(master.TASK_QUEUE)
        if not raw:
            break
        d = json.loads(raw)
        popped.append((d["city"], d["type"]))

    assert len(popped) == 42
    assert popped[:4] == [
        ("gz", "fangyuan"),
        ("gz", "sale"),
        ("sz", "fangyuan"),
        ("sz", "sale"),
    ]


def test_depth_wave_advances_to_done(master, rdb):
    seed_tasks(master, rdb, finished=True, finish_reason="target_reached")
    rdb.set(master.WAVE_KEY, "depth")

    master._wave_tick(rdb)
    assert rdb.get(master.WAVE_KEY) == "done"


def test_all_done_gated_during_active_waves_and_stop_overrides(master, rdb):
    """波次运行期间（wave!=done）即使全任务 finished，all_done 仍为 False；但 STOP 信号可强制覆盖。"""
    rdb.set(master.RUN_CURRENT_KEY, RUN)
    rdb.set(master.WAVE_KEY, "floor")
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")

    # 1. wave 未到 done 时，all_done 恒为 False
    st1 = master.crawl_status(rdb)
    assert st1["all_done"] is False
    assert st1["waves_active"] is True

    # 2. _check_termination 不会提前误盖 STOP
    master._check_termination(rdb)
    assert rdb.get(master.STOP_KEY) is None

    # 3. 外部 STOP 信号（如资源耗尽）出现时，立即放行 all_done
    rdb.set(master.STOP_KEY, "manual_stop_test")
    st2 = master.crawl_status(rdb)
    assert st2["all_done"] is True
    assert "stop" in st2["done_reason"]


def test_wave_next_recovery_on_interrupted_transition(master, rdb):
    rdb.set(master.WAVE_KEY, "floor")
    rdb.set(master.WAVE_NEXT_KEY, "rescue")
    seed_tasks(master, rdb, finished=True, finish_reason="fail_budget")

    master._wave_tick(rdb)
    assert rdb.get(master.WAVE_KEY) == "rescue"
    assert rdb.get(master.WAVE_NEXT_KEY) is None


def test_requeue_count_reset_per_wave(master, rdb):
    """波次切换时，任务 hash 的 requeue_count 必须重置为 0。"""
    seed_tasks(master, rdb, finished=True, finish_reason="fail_budget")
    rdb.hset(master._task_key("gz", "sale"), "requeue_count", "2")

    master._open_wave(rdb, "rescue")
    st = rdb.hgetall(master._task_key("gz", "sale"))
    assert st["requeue_count"] == "0"


def test_bootstrap_resets_floor_pages_to_5_on_day_2(load_master, rdb):
    """次日新 run 启动时，bootstrap 必须将各城 pages 重置为地板页数 5。"""
    m_day1 = load_master(CRAWL_RUN_ID="2026-08-04")
    m_day1._bootstrap_run(rdb)
    # 模拟 day 1 进入 depth 深度波次并将 pages 设为 100
    rdb.hset(
        m_day1._task_key("gz", "sale"),
        mapping={"pages": "100", "wave": "depth", "finished": "1"},
    )

    # day 2 启动
    m_day2 = load_master(CRAWL_RUN_ID="2026-08-05")
    assert m_day2._bootstrap_run(rdb) is True

    gz = rdb.hgetall(m_day2._task_key("gz", "sale"))
    assert gz["wave"] == "floor"
    assert int(gz["pages"]) == 5
    assert gz["finished"] == "0"


def test_wave_log_snapshot_idempotency(master, rdb):
    """重试波次快照时按 wave 覆盖写，不发生行数重复翻倍。"""
    rdb.set(master.RUN_CURRENT_KEY, RUN)
    rdb.set(master.WAVE_KEY, "rescue")
    seed_tasks(master, rdb, finished=True, new_count=10, dup_count=2, pages_done=5)

    # 连续调用两次快照
    master._snapshot_wave(rdb, "floor")
    master._snapshot_wave(rdb, "floor")

    # 当前任务产生增量 5
    rdb.hset(master._task_key("gz", "sale"), mapping={"new_count": "5", "dup_count": "1"})

    st = master.crawl_status(rdb)
    gz = next(c for c in st["cities"] if c["city"] == "gz" and c["type"] == "sale")
    # 历史快照 10 + 当前增量 5 = 15（而非 10 + 10 + 5 = 25）
    assert gz["rows"] == 15


def test_requeue_stale_tasks_preserves_task_hash_pages(master, rdb):
    seed_tasks(master, rdb, finished=True, status="done")
    rdb.hset(
        master._task_key("gz", "sale"),
        mapping={
            "status": "running",
            "finished": "0",
            "worker": "worker-1",
            "worker_hb": 1.0,
            "pages": 5,
            "wave": "floor",
            "requeue_count": 0,
        },
    )
    rdb.delete(master.TASK_QUEUE)
    rdb.delete(master.LOCK_PREFIX + "gz:sale")

    master.requeue_stale_tasks(rdb)

    raw = rdb.rpop(master.TASK_QUEUE)
    assert raw is not None
    data = json.loads(raw)
    assert data["city"] == "gz"
    assert data["type"] == "sale"
    assert data["pages"] == 5
    assert data["wave"] == "floor"


def test_fangyuan_free_rescue_fallback(master, rdb, monkeypatch):
    monkeypatch.setattr(master, "FANGYUAN_FREE_RESCUE", True)
    seed_pool(rdb, master.POOL_FREE, ["10.0.0.1:8080"], source="free")

    rdb.hset(master._task_key("gz", "fangyuan"), "wave", "floor")
    with running_master(master, rdb) as base:
        res = get_json(base, "/proxy/free?city=gz&type=fangyuan")
        assert res["proxy"] is None
        assert "not available for fangyuan" in res.get("error", "")

    rdb.hset(master._task_key("gz", "fangyuan"), "wave", "rescue")
    with running_master(master, rdb) as base:
        res = get_json(base, "/proxy/free?city=gz&type=fangyuan")
        assert res["proxy"] == "10.0.0.1:8080"
        assert res["source"] == "free"


def test_crawl_status_aggregates_wave_logs(master, rdb):
    rdb.set(master.RUN_CURRENT_KEY, RUN)
    rdb.set(master.WAVE_KEY, "rescue")
    seed_tasks(master, rdb, finished=False)

    snap_dict = {"floor": {"wave": "floor", "new": 50, "dup": 10, "pages_done": 5}}
    rdb.hset(master.WAVE_LOG_KEY, "gz:sale", json.dumps(snap_dict))

    rdb.hset(
        master._task_key("gz", "sale"),
        mapping={"new_count": 20, "dup_count": 5, "pages_done": 2},
    )

    st = master.crawl_status(rdb)
    assert st["wave"] == "rescue"
    assert st["waves_active"] is True
    assert st["rows"]["new"] == 70
    assert st["rows"]["dup"] == 15
    gz = next(c for c in st["cities"] if c["city"] == "gz" and c["type"] == "sale")
    assert gz["rows"] == 70


def test_legacy_mode_when_wave_disabled(master, rdb, monkeypatch):
    monkeypatch.setattr(master, "WAVE_ENABLED", False)
    rdb.delete(master.TASK_QUEUE)
    for t in master.DEFAULT_TASKS:
        rdb.delete(master._task_key(t["city"], t["type"]))

    master._wave_tick(rdb)
    assert rdb.llen(master.TASK_QUEUE) == 42
    st = master.crawl_status(rdb)
    assert st["wave"] == "legacy"
    assert st["waves_active"] is False
