"""完成信号（§3.4）：/crawl_status 的 all_done 判定与 crawl_run:{id}:done 写入。

Airflow Sensor 只认这个接口，误判会让「一页没跑」直接走到 ETL。
"""

import pytest
from conftest import get_json, running_master, seed_tasks

RUN = "2026-08-04"


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=RUN)


def mark_run_ready(master, rdb):
    rdb.set(master.RUN_CURRENT_KEY, RUN)


def test_not_all_finished_is_false(master, rdb):
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=False)
    rdb.hset(
        master._task_key("gz", "sale"), mapping={"finished": "1", "finish_reason": "empty_pages"}
    )
    rdb.hset(
        master._task_key("sz", "fangyuan"), mapping={"finished": "1", "finish_reason": "no_proxy"}
    )

    st = master.crawl_status(rdb)
    assert st["all_done"] is False
    assert st["done_reason"] is None
    assert st["total_tasks"] == 42
    assert st["finished_tasks"] == 2
    assert st["finished_by_type"] == {"sale": 1, "fangyuan": 1}
    assert rdb.get(master._run_done_key(RUN)) is None  # 未完成不得写 done 信号


def test_all_42_finished_sets_all_done_and_done_key(master, rdb):
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=True, finish_reason="pages_exhausted")

    st = master.crawl_status(rdb)
    assert st["all_done"] is True
    assert st["done_reason"] == "all_finished"
    assert st["finished_tasks"] == 42
    assert st["finished_by_type"] == {"sale": 21, "fangyuan": 21}
    assert st["run_id"] == RUN
    ts = rdb.get(master._run_done_key(RUN))
    assert ts and ts.startswith("20")  # ISO 时间戳
    assert rdb.get(master._run_done_reason_key(RUN)) == "all_finished"

    # 幂等：再查一次不覆盖已有时间戳
    master.crawl_status(rdb)
    assert rdb.get(master._run_done_key(RUN)) == ts


def test_stop_flag_also_completes_the_run(master, rdb):
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=False)
    rdb.set(master.STOP_KEY, "manual:captcha_wall")

    st = master.crawl_status(rdb)
    assert st["all_done"] is True
    assert st["done_reason"] == "stop:manual:captcha_wall"
    assert st["stop"] == "manual:captcha_wall"
    assert rdb.get(master._run_done_key(RUN))


def test_stale_state_before_bootstrap_is_not_done(master, rdb):
    """上一 run 的残留（任务全 finished + stop 置位）不得让本 run 秒判完成。"""
    seed_tasks(master, rdb, finished=True)
    rdb.set(master.STOP_KEY, "prev-run-stop")
    rdb.set(master.RUN_CURRENT_KEY, "2026-08-03")  # 尚未被本 run 引导

    st = master.crawl_status(rdb)
    assert st["all_done"] is False
    assert rdb.get(master._run_done_key(RUN)) is None


def test_crawl_status_over_http(master, rdb):
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=False)
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 91)
    rdb.hset(
        master._task_key("gz", "sale"),
        mapping={"finished": "1", "finish_reason": "budget_exhausted", "new_count": 123},
    )
    with running_master(master, rdb) as base:
        st = get_json(base, "/crawl_status")
    assert st["all_done"] is False
    assert st["finished_tasks"] == 1
    assert st["rows"]["new"] == 123
    gz_sale = next(c for c in st["cities"] if c["city"] == "gz" and c["type"] == "sale")
    assert gz_sale == {
        "city": "gz",
        "type": "sale",
        "budget": 91,
        "used": 91,
        "finished": True,
        "reason": "budget_exhausted",
        "rows": 123,
    }
