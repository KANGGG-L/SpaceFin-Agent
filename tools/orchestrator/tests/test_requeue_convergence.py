"""收敛性保证（P1 修复 A 的验收）：**每个任务在有限步内必然 finished=1**。

这是整套 Airflow 编排能否成立的核心命题。命题一旦不成立：42 个任务的分母永远凑不齐
→ /crawl_status 的 all_done 只能靠 STOP 置位 → Airflow Sensor 烧完 6h 超时失败
→ etl_finalize / geocode_backfill_finalize 整条下游永不执行，当天数据不入库。

覆盖 6 组（契约 §3）：
1. stale_abandoned 收敛（当前阶段，requeue 超限）
2. 未超限仍重排
3. 孤儿收尾扫描 seal_orphan_tasks（修复 A 核心）+ 「阶段还没到」不得误盖的回归护栏
4. _check_phase_transition 切换瞬间盖章
5. 收敛终局：41 finished + 1 超限孤儿 → all_done=true + done 键写入（最关键）
6. worker 侧 fail_budget 收敛（requeue_count 递增 → 超限写终态、不再入队）

只连真实 Redis 的 **db 15**（conftest 的 rdb 夹具强制校验并前后各清一次），绝不触碰 db 0。
"""

import json
import time

import pytest
from conftest import get_json, running_master, seed_tasks

RUN = "2026-08-04"
STALE_HB = 1.0  # 远古心跳 → now - hb 必然 > WORKER_TTL


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=RUN)


# ---------------- 现场构造 ----------------
def mark_run_ready(master, rdb):
    """让 crawl_status 认可本 run（否则 all_done 恒为 False）。"""
    rdb.set(master.RUN_CURRENT_KEY, RUN)


def set_phase(master, rdb, phase):
    rdb.set(master.PHASE_KEY, phase)


def put_task(master, rdb, city, typ, **fields):
    """覆写单个任务 hash 的若干字段。"""
    rdb.hset(master._task_key(city, typ), mapping={k: str(v) for k, v in fields.items()})


def state(master, rdb, city, typ):
    return rdb.hgetall(master._task_key(city, typ))


def make_dead_running(master, rdb, city, typ, requeue_count=0):
    """判死现场：status=running、任务锁不存在、worker_hb 远古。"""
    rdb.delete(master.LOCK_PREFIX + f"{city}:{typ}")
    put_task(
        master,
        rdb,
        city,
        typ,
        status="running",
        finished="0",
        finish_reason="",
        worker="worker-9",
        worker_hb=STALE_HB,
        requeue_count=requeue_count,
    )


def make_alive_running(master, rdb, city, typ, lock=False):
    """存活现场：status=running，心跳新鲜（或持有锁）。"""
    put_task(
        master,
        rdb,
        city,
        typ,
        status="running",
        finished="0",
        finish_reason="",
        worker="worker-1",
        worker_hb=STALE_HB if lock else time.time(),
    )
    if lock:
        rdb.set(master.LOCK_PREFIX + f"{city}:{typ}", "worker-1", ex=60)
    else:
        rdb.delete(master.LOCK_PREFIX + f"{city}:{typ}")


def queued(master, rdb):
    """当前队列中的 (city, type) 集合。"""
    out = set()
    for item in rdb.lrange(master.TASK_QUEUE, 0, -1):
        d = json.loads(item)
        out.add((d["city"], d.get("type", "sale")))
    return out


# ================= 第 1 组：stale_abandoned 收敛（当前阶段） =================
def test_requeue_over_limit_writes_stale_abandoned_and_does_not_enqueue(master, rdb):
    """running + 锁过期 + 心跳超时 + requeue_count 已达 MAX_REQUEUE → 终态，不再入队。"""
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    make_dead_running(master, rdb, "gz", "sale", requeue_count=master.MAX_REQUEUE)
    rdb.delete(master.TASK_QUEUE)

    master.requeue_stale_tasks(rdb)

    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "1"
    assert st["finish_reason"] == "stale_abandoned"
    assert st["status"] == "done"
    assert st["worker"] == "" and st["worker_hb"] == "0"
    assert int(st["requeue_count"]) == master.MAX_REQUEUE + 1
    assert ("gz", "sale") not in queued(master, rdb)  # 终态任务绝不能回队

    # 幂等：再跑一轮不改写终态、不入队
    master.requeue_stale_tasks(rdb)
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "stale_abandoned"
    assert ("gz", "sale") not in queued(master, rdb)


def test_requeue_count_climbs_to_terminal_within_max_requeue_rounds(master, rdb):
    """收敛步数有界：连续判死最多 MAX_REQUEUE+1 轮必进终态（不会无限乒乓）。"""
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    make_dead_running(master, rdb, "sz", "sale")

    seen = []
    for _ in range(master.MAX_REQUEUE + 1):
        make_dead_running(  # 模拟每轮重排后 worker 又领走并再次卡死
            master,
            rdb,
            "sz",
            "sale",
            requeue_count=int(state(master, rdb, "sz", "sale").get("requeue_count", 0) or 0),
        )
        master.requeue_stale_tasks(rdb)
        seen.append(state(master, rdb, "sz", "sale")["finished"])

    assert seen[-1] == "1", f"MAX_REQUEUE+1 轮后仍未终态: {seen}"
    assert state(master, rdb, "sz", "sale")["finish_reason"] == "stale_abandoned"


# ================= 第 2 组：未超限仍重排 =================
def test_under_limit_requeues_and_keeps_finished_zero(master, rdb):
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    make_dead_running(master, rdb, "gz", "sale", requeue_count=1)
    rdb.delete(master.TASK_QUEUE)

    master.requeue_stale_tasks(rdb)

    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "0"  # 还有重试机会，不得提前盖终态
    assert st["finish_reason"] == ""
    assert st["status"] == "pending"
    assert st["worker"] == "" and st["worker_hb"] == "0"
    assert int(st["requeue_count"]) == 2
    assert ("gz", "sale") in queued(master, rdb)


def test_alive_running_task_is_left_alone(master, rdb):
    """心跳新鲜 / 锁仍在 → master 不得抢走正在跑的任务。"""
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    make_alive_running(master, rdb, "gz", "sale")
    make_alive_running(master, rdb, "sz", "sale", lock=True)
    rdb.delete(master.TASK_QUEUE)

    master.requeue_stale_tasks(rdb)

    for city in ("gz", "sz"):
        st = state(master, rdb, city, "sale")
        assert st["status"] == "running"
        assert st["finished"] == "0"
        assert st.get("requeue_count", "0") in ("0", "")
        assert (city, "sale") not in queued(master, rdb)


# ================= 第 3 组：孤儿收尾扫描（修复 A 核心） =================
def test_orphan_pending_sale_task_is_sealed_phase_ended(master, rdb):
    """phase 已是 fangyuan，残留 sale pending 任务必须被盖 phase_ended（否则 42 分母永缺）。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb)
    put_task(master, rdb, "gz", "sale", status="pending", finished="0", finish_reason="")
    rdb.delete(master.TASK_QUEUE)

    n = master.seal_orphan_tasks(rdb)

    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "1"
    assert st["finish_reason"] == "phase_ended"
    assert st["status"] == "done"
    assert ("gz", "sale") not in queued(master, rdb)  # 盖章不等于重排
    assert n >= 1


def test_orphan_running_but_alive_is_not_sealed(master, rdb):
    """阶段虽已过去，但 worker 还活着 → 留给 worker 自己写终态，别贴假标签。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb)
    make_alive_running(master, rdb, "gz", "sale")  # 心跳新鲜
    make_alive_running(master, rdb, "sz", "sale", lock=True)  # 锁还在

    master.seal_orphan_tasks(rdb)

    for city in ("gz", "sz"):
        st = state(master, rdb, city, "sale")
        assert st["finished"] == "0", f"{city}:sale 被误盖，worker 仍在跑"
        assert st["status"] == "running"
        assert st["finish_reason"] == ""


def test_orphan_running_and_dead_is_sealed_stale_abandoned_without_requeue(master, rdb):
    """阶段已过去 + 判死 → stale_abandoned；不论 requeue_count 是否超限都不重排。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb)
    make_dead_running(master, rdb, "gz", "sale", requeue_count=0)
    rdb.delete(master.TASK_QUEUE)

    master.seal_orphan_tasks(rdb)

    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "1"
    assert st["finish_reason"] == "stale_abandoned"
    assert st["worker"] == "" and st["worker_hb"] == "0"
    assert int(st["requeue_count"]) == 0  # 阶段已过去，不消耗重排额度
    assert ("gz", "sale") not in queued(master, rdb)


def test_seal_never_overwrites_existing_finish_reason(master, rdb):
    """幂等：已终态任务的 finish_reason 是审计依据，绝不能被盖章覆写。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    put_task(master, rdb, "gz", "sale", status="done", finished="1", finish_reason="target_reached")
    put_task(master, rdb, "sz", "sale", status="pending", finished="0", finish_reason="")

    assert master.seal_orphan_tasks(rdb) == 1  # 只盖 sz
    before = state(master, rdb, "sz", "sale")
    assert master.seal_orphan_tasks(rdb) == 0  # 二次执行零改动
    assert state(master, rdb, "sz", "sale") == before
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "target_reached"


def test_seal_does_not_touch_future_phase_tasks(master, rdb):
    """回归护栏：phase=sale 时，21 个 fangyuan 任务是「还没轮到」，绝不能被盖章。

    若按「typ != phase」判定，_bootstrap_run 重置后的第一个 maintenance 周期就会把
    全部 fangyuan 任务盖成 finished=1；而 _init_phase_tasks 对已存在的 key 不重建，
    出租阶段整轮不跑、all_done 反而秒真 —— ETL 只拿到 sale 数据的静默缺失。
    """
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)

    assert master.seal_orphan_tasks(rdb) == 0
    for t in master.DEFAULT_TASKS:
        if t["type"] == "fangyuan":
            st = state(master, rdb, t["city"], "fangyuan")
            assert st["finished"] == "0", f"{t['city']}:fangyuan 在 sale 阶段被误盖"
            assert st["finish_reason"] == ""


def test_day2_bootstrap_then_maintenance_keeps_fangyuan_runnable(master, rdb):
    """第 2 天 run 的端到端护栏：引导 → 巡查 → 切阶段后 fangyuan 仍能被派单。"""
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    rdb.set(master.RUN_CURRENT_KEY, "2026-08-03")
    rdb.set(master.PHASE_KEY, "fangyuan")

    assert master._bootstrap_run(rdb) is True
    assert rdb.get(master.PHASE_KEY) == "sale"
    master.init_tasks(rdb)
    master.requeue_stale_tasks(rdb)  # 含收尾扫描

    fy = [
        state(master, rdb, t["city"], "fangyuan")
        for t in master.DEFAULT_TASKS
        if t["type"] == "fangyuan"
    ]
    assert all(s["finished"] == "0" for s in fy), "新 run 一开始就把 fangyuan 盖完了"

    # sale 全部跑完 → 切阶段 → 下一轮巡查必须把 fangyuan 派出去
    for t in master.DEFAULT_TASKS:
        if t["type"] == "sale":
            put_task(
                master,
                rdb,
                t["city"],
                "sale",
                status="done",
                finished="1",
                finish_reason="pages_exhausted",
            )
    master._check_phase_transition(rdb)
    assert rdb.get(master.PHASE_KEY) == "fangyuan"
    master.requeue_stale_tasks(rdb)
    master._purge_queue(rdb, "fangyuan")
    assert len({c for c, t in queued(master, rdb) if t == "fangyuan"}) == 21


def test_seal_preserves_progress_and_url_sets(master, rdb):
    """收尾扫描不得碰断点与去重集合（增量方案的命脉）。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb)
    rdb.set("spacefin:crawl_progress:gz:sale", 57)
    rdb.sadd("spacefin:crawled_urls:gz:sale", "u1", "u2")

    master.seal_orphan_tasks(rdb)

    assert rdb.get("spacefin:crawl_progress:gz:sale") == "57"
    assert rdb.smembers("spacefin:crawled_urls:gz:sale") == {"u1", "u2"}


def test_requeue_stale_tasks_runs_the_orphan_sweep(master, rdb):
    """maintenance 只调 requeue_stale_tasks，收尾扫描必须挂在它上面才会被执行。"""
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb)
    put_task(master, rdb, "gz", "sale", status="pending", finished="0")

    master.requeue_stale_tasks(rdb)

    assert state(master, rdb, "gz", "sale")["finished"] == "1"
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "phase_ended"


# ================= 第 4 组：切换瞬间盖章 =================
def test_phase_transition_seals_pending_skips_running_keeps_reason(master, rdb):
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    put_task(
        master, rdb, "gz", "sale", status="done", finished="1", finish_reason="pages_exhausted"
    )
    put_task(master, rdb, "sz", "sale", status="pending", finished="0", finish_reason="")
    make_alive_running(master, rdb, "zh", "sale")
    rdb.set(master.QG_CONSUMED_KEY, master.QG_SALE_BUDGET)  # 触发条件：sale 配额耗尽

    master._check_phase_transition(rdb)

    assert rdb.get(master.PHASE_KEY) == "fangyuan"
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "pages_exhausted"  # 不覆写
    sz = state(master, rdb, "sz", "sale")
    assert sz["finished"] == "1" and sz["finish_reason"] == "phase_ended"
    zh = state(master, rdb, "zh", "sale")
    assert zh["finished"] == "0" and zh["status"] == "running"  # 交给 worker


def test_running_task_missed_by_transition_is_caught_by_orphan_sweep(master, rdb):
    """P1-A 的完整复现链：切阶段时 running 被跳过 → worker 死掉 → 收尾扫描兜住。"""
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    make_alive_running(master, rdb, "gz", "sale")
    rdb.set(master.QG_CONSUMED_KEY, master.QG_SALE_BUDGET)

    master._check_phase_transition(rdb)
    assert state(master, rdb, "gz", "sale")["finished"] == "0"  # 切换瞬间确实漏掉了

    # worker 撞 fail budget → requeue_task 写回 pending（不写 finished），队列项被 purge 清掉
    put_task(master, rdb, "gz", "sale", status="pending", worker="", worker_hb=0)
    master._purge_queue(rdb, "fangyuan")

    master.requeue_stale_tasks(rdb)
    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "1", "P1-A 未修复：孤儿 sale 任务永远凑不齐 42 分母"
    assert st["finish_reason"] == "phase_ended"


def test_phase_transition_by_all_sale_done(master, rdb):
    set_phase(master, rdb, "sale")
    seed_tasks(master, rdb)
    for t in master.DEFAULT_TASKS:
        if t["type"] == "sale":
            put_task(
                master,
                rdb,
                t["city"],
                "sale",
                status="done",
                finished="1",
                finish_reason="empty_pages",
            )

    master._check_phase_transition(rdb)
    assert rdb.get(master.PHASE_KEY) == "fangyuan"
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "empty_pages"


# ================= 第 5 组：收敛终局（最关键） =================
def _seed_41_finished_plus_one_orphan(master, rdb):
    """41 个 finished + 1 个「阶段已过去 + 判死 + requeue 超限」的 running 孤儿。"""
    mark_run_ready(master, rdb)
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    make_dead_running(master, rdb, "gz", "sale", requeue_count=master.MAX_REQUEUE)
    rdb.delete(master.TASK_QUEUE)


def test_convergence_endgame_all_done_and_done_key(master, rdb):
    """41 finished + 1 超限孤儿 → 收尾扫描后 all_done 为真且 done 键写入。

    这是 Airflow Sensor 唯一的放行依据；不成立则 6h 超时、ETL 永不执行。
    """
    _seed_41_finished_plus_one_orphan(master, rdb)

    before = master.crawl_status(rdb)
    assert before["all_done"] is False and before["finished_tasks"] == 41
    assert rdb.get(master._run_done_key(RUN)) is None

    master.requeue_stale_tasks(rdb)  # maintenance 每周期都会调它

    st = master.crawl_status(rdb)
    assert st["all_done"] is True
    assert st["finished_tasks"] == 42
    assert st["done_reason"] == "all_finished"  # 靠真收敛，不是靠 STOP 兜底
    assert st["stop"] is None
    assert rdb.get(master.STOP_KEY) is None
    ts = rdb.get(master._run_done_key(RUN))
    assert ts and ts.startswith("20")
    assert rdb.get(master._run_done_reason_key(RUN)) == "all_finished"

    orphan = state(master, rdb, "gz", "sale")
    assert orphan["finished"] == "1" and orphan["finish_reason"] == "stale_abandoned"
    assert ("gz", "sale") not in queued(master, rdb)


def test_convergence_endgame_over_http_sensor_view(master, rdb):
    """Airflow Sensor 视角：真起 HTTP server 打 /crawl_status，字段与 run_id 都要对。"""
    _seed_41_finished_plus_one_orphan(master, rdb)

    with running_master(master, rdb) as base:
        assert get_json(base, "/crawl_status")["all_done"] is False
        master.requeue_stale_tasks(rdb)
        st = get_json(base, "/crawl_status")

    assert st["all_done"] is True
    assert st["run_id"] == RUN  # Sensor 还要校验 run_id 匹配 {{ ds }}
    assert st["finished_tasks"] == 42
    assert rdb.get(master._run_done_key(RUN))
    gz_sale = next(c for c in st["cities"] if c["city"] == "gz" and c["type"] == "sale")
    assert gz_sale["finished"] is True
    assert gz_sale["reason"] == "stale_abandoned"  # 可与真跑完的任务区分


def test_convergence_endgame_multiple_orphans(master, rdb):
    """混合终局：pending 孤儿 + 判死孤儿 + 存活 running 各一，最后一个交回后才 all_done。"""
    mark_run_ready(master, rdb)
    set_phase(master, rdb, "fangyuan")
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    put_task(master, rdb, "gz", "sale", status="pending", finished="0", finish_reason="")
    make_dead_running(master, rdb, "sz", "sale", requeue_count=master.MAX_REQUEUE)
    make_alive_running(master, rdb, "zh", "sale")
    rdb.delete(master.TASK_QUEUE)

    master.requeue_stale_tasks(rdb)
    st = master.crawl_status(rdb)
    assert st["finished_tasks"] == 41  # zh 还活着，不该被盖
    assert st["all_done"] is False

    # worker 自己写终态（finish_task 的等价写入）
    put_task(
        master, rdb, "zh", "sale", status="done", finished="1", finish_reason="pages_exhausted"
    )
    st = master.crawl_status(rdb)
    assert st["all_done"] is True and st["finished_tasks"] == 42
    assert rdb.get(master._run_done_key(RUN))


# ================= 第 6 组：worker 侧 fail_budget 收敛 =================
CITY, TYP = "gz", "sale"


@pytest.fixture
def worker(load_worker):
    def _loader(**env):
        env.setdefault("WORKER_ID", "worker-test")
        env.setdefault("FAIL_BUDGET", "2")
        env.setdefault("FAIL_BACKOFF", "0")
        env.setdefault("MAX_REQUEUE", "2")
        return load_worker(**env)

    return _loader


def _wire_all_fail(mod, monkeypatch):
    monkeypatch.setattr(mod, "fetch_page", lambda *a, **k: None)  # 每页都被拦
    monkeypatch.setattr(mod, "get_proxy", lambda *a, **k: ("1.2.3.4:8000", "qg", False))
    monkeypatch.setattr(mod, "get_proxy_with_source", lambda *a, **k: ("1.2.3.4:8000", "qg", False))
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)


def _claim(mod, rdb):
    """模拟 worker 领到任务：写 running + 持锁。"""
    rdb.hset(
        mod._task_key(CITY, TYP),
        mapping={
            "city": CITY,
            "type": TYP,
            "pages": 5,
            "target": 999999,
            "round": 0,
            "status": "running",
            "finished": "0",
            "finish_reason": "",
            "worker": mod.MY_ID,
            "worker_hb": time.time(),
        },
    )
    rdb.set(mod.LOCK_PREFIX + f"{CITY}:{TYP}", mod.MY_ID, ex=120)


def test_worker_fail_budget_requeues_then_finishes(worker, rdb, monkeypatch, tmp_path):
    """连续失败超 fail_budget：requeue_count 递增，超 MAX_REQUEUE 后写 fail_budget 终态。"""
    mod = worker()
    _wire_all_fail(mod, monkeypatch)

    for expected in range(1, mod.MAX_REQUEUE + 1):  # 1..2 → 未超限，回队重试
        _claim(mod, rdb)
        mod.crawl(rdb, CITY, TYP, 5, 999999, 0, str(tmp_path))
        st = rdb.hgetall(mod._task_key(CITY, TYP))
        assert int(st["requeue_count"]) == expected
        assert st["finished"] == "0", "还有重试额度，不该提前终态"
        assert st["status"] == "pending"
        assert rdb.llen(mod.TASK_QUEUE) == expected
        assert rdb.exists(mod.LOCK_PREFIX + f"{CITY}:{TYP}") == 0  # 锁必须释放

    queue_len = rdb.llen(mod.TASK_QUEUE)
    _claim(mod, rdb)
    mod.crawl(rdb, CITY, TYP, 5, 999999, 0, str(tmp_path))

    st = rdb.hgetall(mod._task_key(CITY, TYP))
    assert int(st["requeue_count"]) == mod.MAX_REQUEUE + 1
    assert st["finished"] == "1"
    assert st["finish_reason"] == "fail_budget"
    assert st["status"] == "done"
    assert rdb.llen(mod.TASK_QUEUE) == queue_len, "超限后绝不能再入队（否则无限乒乓）"
    assert rdb.exists(mod.LOCK_PREFIX + f"{CITY}:{TYP}") == 0


def test_worker_and_master_share_requeue_counter(worker, master, rdb, monkeypatch, tmp_path):
    """worker 与 master 共享同一 requeue_count：两侧交替也只会更早收敛，不会互相续命。"""
    mod = worker(MAX_REQUEUE="3")
    _wire_all_fail(mod, monkeypatch)
    set_phase(master, rdb, "sale")

    _claim(mod, rdb)
    mod.crawl(rdb, CITY, TYP, 5, 999999, 0, str(tmp_path))
    assert int(rdb.hget(mod._task_key(CITY, TYP), "requeue_count")) == 1

    make_dead_running(master, rdb, CITY, TYP, requeue_count=1)
    master.requeue_stale_tasks(rdb)  # master 判死 → 2
    assert int(rdb.hget(mod._task_key(CITY, TYP), "requeue_count")) == 2

    _claim(mod, rdb)
    mod.crawl(rdb, CITY, TYP, 5, 999999, 0, str(tmp_path))  # worker → 3
    assert int(rdb.hget(mod._task_key(CITY, TYP), "requeue_count")) == 3

    _claim(mod, rdb)
    mod.crawl(rdb, CITY, TYP, 5, 999999, 0, str(tmp_path))  # → 4 > 3 → 终态
    st = rdb.hgetall(mod._task_key(CITY, TYP))
    assert st["finished"] == "1" and st["finish_reason"] == "fail_budget"
