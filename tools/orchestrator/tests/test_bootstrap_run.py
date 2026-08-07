"""新 run 引导（§3.8）：换 CRAWL_RUN_ID 重置本 run 状态，但断点与去重集合必须保留。

crawl_progress:* / crawled_urls:* 一旦被误删 → 次日全量重爬 + 跨日去重失效，
这是整个增量方案的命脉，故写成显式断言。
"""

import json

import pytest
from orchmods import seed_tasks

PREV = "2026-08-03"
NEW = "2026-08-04"


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=NEW)


def seed_prev_run(master, rdb):
    """构造「上一 run 跑完」的现场。"""
    seed_tasks(
        master,
        rdb,
        finished=True,
        status="done",
        round=3,
        finish_reason="pages_exhausted",
        count=500,
        new_count=500,
        dup_count=20,
        blocked_count=7,
        pages_done=53,
        worker="worker-3",
        worker_hb=1234567890.0,
    )
    rdb.set(master.RUN_CURRENT_KEY, PREV)
    rdb.set(master.STOP_KEY, "free_pool_0pct")
    rdb.set(master.EMPTY_CYCLES_KEY, 8)
    rdb.set(master.PHASE_KEY, "fangyuan")
    rdb.set(master.QG_CONSUMED_KEY, 981)
    rdb.rpush(master.TASK_QUEUE, json.dumps({"city": "gz", "type": "fangyuan"}))
    for city, typ in (("gz", "sale"), ("sz", "fangyuan")):
        rdb.set(f"{master.IP_USED_PREFIX}{city}:{typ}", 91)
    # 必须存活的两类 key
    rdb.set("spacefin:crawl_progress:gz:sale", 57)
    rdb.set("spacefin:crawl_progress:sz:fangyuan", 12)
    rdb.sadd("spacefin:crawled_urls:gz:sale", "u1", "u2", "u3")
    rdb.sadd("spacefin:crawled_urls:sz:fangyuan", "r1")


def test_bootstrap_resets_run_state(master, rdb):
    seed_prev_run(master, rdb)
    assert master._bootstrap_run(rdb) is True

    assert rdb.get(master.STOP_KEY) is None
    assert rdb.get(master.EMPTY_CYCLES_KEY) is None
    assert rdb.get(master.PHASE_KEY) == "city-interleave"
    assert rdb.get(master.QG_CONSUMED_KEY) is None
    assert rdb.llen(master.TASK_QUEUE) == 0
    assert rdb.get(master.RUN_CURRENT_KEY) == NEW

    for t in master.DEFAULT_TASKS:
        st = rdb.hgetall(master._task_key(t["city"], t["type"]))
        assert st["status"] == "pending"
        assert st["round"] == "0"
        assert st["finished"] == "0"
        assert st["finish_reason"] == ""
        assert st["count"] == "0" and st["new_count"] == "0" and st["dup_count"] == "0"
        assert st["blocked_count"] == "0" and st["pages_done"] == "0"
        assert st["worker"] == "" and st["worker_hb"] == "0"
        assert rdb.get(f"{master.IP_USED_PREFIX}{t['city']}:{t['type']}") is None
        assert int(
            rdb.get(f"{master.IP_BUDGET_PREFIX}{t['city']}:{t['type']}")
        ) == master._budget_of(t["city"], t["type"])
    assert len(master.DEFAULT_TASKS) == 42


def test_bootstrap_preserves_progress_and_url_sets(master, rdb):
    """最关键：断点续爬与跨日去重的 key 绝不能被新 run 清掉。"""
    seed_prev_run(master, rdb)
    assert rdb.get("spacefin:crawl_progress:gz:sale") == "57"  # 前置条件：确实存在
    assert rdb.scard("spacefin:crawled_urls:gz:sale") == 3
    master._bootstrap_run(rdb)

    assert rdb.get("spacefin:crawl_progress:gz:sale") == "57"
    assert rdb.get("spacefin:crawl_progress:sz:fangyuan") == "12"
    assert rdb.smembers("spacefin:crawled_urls:gz:sale") == {"u1", "u2", "u3"}
    assert rdb.scard("spacefin:crawled_urls:sz:fangyuan") == 1
    assert sorted(rdb.keys("spacefin:crawl_progress:*")) == [
        "spacefin:crawl_progress:gz:sale",
        "spacefin:crawl_progress:sz:fangyuan",
    ]


def test_same_run_id_is_idempotent(master, rdb):
    seed_prev_run(master, rdb)
    assert master._bootstrap_run(rdb) is True

    # 模拟本 run 已跑出一些进展（Airflow 重试场景）
    key = master._task_key("gz", "sale")
    rdb.hset(
        key,
        mapping={
            "status": "done",
            "finished": "1",
            "finish_reason": "empty_pages",
            "new_count": 42,
        },
    )
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 30)

    assert master._bootstrap_run(rdb) is False  # 同 run_id 不重置
    st = rdb.hgetall(key)
    assert st["finished"] == "1"
    assert st["finish_reason"] == "empty_pages"
    assert st["new_count"] == "42"
    assert rdb.get(f"{master.IP_USED_PREFIX}gz:sale") == "30"


def test_first_boot_with_empty_redis(master, rdb):
    """首次启动（无任何状态）：也算新 run，不得因 task hash 不存在而报错。"""
    assert master._bootstrap_run(rdb) is True
    assert rdb.get(master.RUN_CURRENT_KEY) == NEW
    assert rdb.get(master.PHASE_KEY) == "city-interleave"
    # task hash 不存在的由 init_tasks 创建，此处只需保证预算表已落盘
    assert int(rdb.get(f"{master.IP_BUDGET_PREFIX}gz:sale")) == 60
    assert int(rdb.get(f"{master.IP_BUDGET_PREFIX}yf:fangyuan")) == 20
