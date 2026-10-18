"""爬虫任务领取竞态修复（C 类）：RPOPLPUSH 可靠队列 + 崩溃回收。

定位结论：claim_task 真实代码在 tools/orchestrator/worker.py:329（非 anjuke_crawler），
原实现 LPOP 出队后再 SET NX 抢锁，崩溃于二者之间会丢任务。本测试覆盖：
- 正常领取：返回任务、processing 列表清空、任务锁已握；
- 崩溃遗留（任务在 processing 列表）可由 recover_processing_tasks 回收重派，不丢；
- 任务状态哈希已不存在时安全丢弃，不卡死；
- 抢锁竞争下任务不丢（锁过期后仍能被领走）。
"""

import pytest


@pytest.fixture
def worker(load_worker):
    def _loader(**env):
        env.setdefault("WORKER_ID", "worker-test")
        return load_worker(**env)

    return _loader


def _seed_task(mod, rdb, city="gz", typ="sale"):
    rdb.hset(
        mod._task_key(city, typ),
        mapping={
            "city": city,
            "type": typ,
            "pages": 10,
            "target": 999999,
            "round": 0,
            "status": "pending",
            "finished": "0",
            "finish_reason": "",
        },
    )


def test_claim_task_returns_task_and_clears_processing(worker, rdb):
    mod = worker()
    _seed_task(mod, rdb)
    rdb.rpush(mod.TASK_QUEUE, '{"city": "gz", "type": "sale"}')

    task = mod.claim_task(rdb)

    assert task["city"] == "gz"
    # 主队列与 processing 列表都已清空，任务锁已握
    assert rdb.llen(mod.TASK_QUEUE) == 0
    assert rdb.llen(mod.TASK_PROCESSING_QUEUE) == 0
    lock_key = mod.LOCK_PREFIX + "gz:sale"
    assert rdb.get(lock_key) == "worker-test"
    assert rdb.hget(mod._task_key("gz", "sale"), "status") == "running"


def test_claim_task_recovers_crashed_processing_task(worker, rdb):
    """模拟崩溃：任务已 RPOPLPUSH 进 processing 列表但未来得及领走 → 回收重派。"""
    mod = worker()
    _seed_task(mod, rdb)
    rdb.rpush(mod.TASK_QUEUE, '{"city": "gz", "type": "sale"}')

    # 模拟崩溃瞬间：任务被原子移动到 processing，主队列空
    rdb.rpoplpush(mod.TASK_QUEUE, mod.TASK_PROCESSING_QUEUE)
    assert rdb.llen(mod.TASK_QUEUE) == 0
    assert rdb.llen(mod.TASK_PROCESSING_QUEUE) == 1

    moved = mod.recover_processing_tasks(rdb)

    assert moved == 1
    assert rdb.llen(mod.TASK_QUEUE) == 1
    assert rdb.llen(mod.TASK_PROCESSING_QUEUE) == 0
    # 回收后可被正常领取
    task = mod.claim_task(rdb)
    assert task["city"] == "gz"


def test_claim_task_drops_missing_task_state(worker, rdb):
    """任务哈希已不存在（master 清理过）→ 安全丢弃，返回 None，不卡死、不丢其它任务。"""
    mod = worker()
    rdb.rpush(mod.TASK_QUEUE, '{"city": "gz", "type": "sale"}')

    task = mod.claim_task(rdb)

    assert task is None
    assert rdb.llen(mod.TASK_QUEUE) == 0
    assert rdb.llen(mod.TASK_PROCESSING_QUEUE) == 0


def test_claim_task_lock_contention_eventually_reclaimed(worker, rdb):
    """抢锁被其它 worker 持有时不丢任务：锁过期后仍能被本 worker 领走。"""
    mod = worker()
    _seed_task(mod, rdb)
    rdb.rpush(mod.TASK_QUEUE, '{"city": "gz", "type": "sale"}')

    # 模拟其它 worker 持有锁（短 TTL，1s 后过期）
    rdb.set(mod.LOCK_PREFIX + "gz:sale", "worker-other", nx=True, ex=1)

    task = mod.claim_task(rdb)  # 锁过期后重新领走

    assert task is not None
    assert task["city"] == "gz"
    assert rdb.get(mod.LOCK_PREFIX + "gz:sale") == "worker-test"


def test_two_tasks_both_claimable_with_distinct_locks(worker, rdb):
    mod = worker()
    _seed_task(mod, rdb, "gz", "sale")
    _seed_task(mod, rdb, "sz", "fangyuan")
    rdb.rpush(mod.TASK_QUEUE, '{"city": "gz", "type": "sale"}')
    rdb.rpush(mod.TASK_QUEUE, '{"city": "sz", "type": "fangyuan"}')

    t1 = mod.claim_task(rdb)
    t2 = mod.claim_task(rdb)

    assert {t1["city"], t2["city"]} == {"gz", "sz"}
    assert rdb.llen(mod.TASK_PROCESSING_QUEUE) == 0
    assert rdb.get(mod.LOCK_PREFIX + "gz:sale") == "worker-test"
    assert rdb.get(mod.LOCK_PREFIX + "sz:fangyuan") == "worker-test"
