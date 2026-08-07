"""收敛性保证（P1 修复 A 的验收 + 2026-08-07 按城交错调度适配）。

核心命题不变：**每个任务在有限步内必然 finished=1**。
命题一旦不成立：42 个任务的分母永远凑不齐 → /crawl_status 的 all_done 只能靠 STOP 置位
→ Airflow Sensor 烧完 6h 超时失败 → etl_finalize / geocode_backfill_finalize 整条下游永不执行。

2026-08-07 调度重构：不再有全局 sale→fangyuan 阶段（PHASE）。改为「按城交错 + fangyuan 先执行」——
全 42 任务（21 城 × sale/fangyuan）自 bootstrap 起同时有效并一次性入队，队列顺序即
DEFAULT_TASKS（先全 21 城 fangyuan、再全 21 城 sale），worker 从队首领取，
fangyuan 波次优先消耗代理。因此本文件移除了所有 phase / seal_orphan_tasks 相关断言，改为：

覆盖 6 组（契约 §3）：
1. stale_abandoned 收敛（requeue 超限放弃 / 未超限仍重排 / 存活 running 不动）
2. 按城交错调度：init_tasks 全量入队且城序交错；终止判定对全 42 任务统一生效
3. 收敛终局：41 finished + 1 running 判死孤儿 → all_done=true + done 键写入（最关键）
4. worker 侧 fail_budget 收敛（requeue_count 递增 → 超限写终态、不再入队）

只连真实 Redis 的 **db 15**（conftest 的 rdb 夹具强制校验并前后各清一次），绝不触碰 db 0。
"""

import json
import time

import pytest
from orchmods import get_json, running_master, seed_tasks

RUN = "2026-08-04"
STALE_HB = 1.0  # 远古心跳 → now - hb 必然 > WORKER_TTL


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=RUN)


# ---------------- 现场构造 ----------------
def mark_run_ready(master, rdb):
    """让 crawl_status 认可本 run（否则 all_done 恒为 False）。"""
    rdb.set(master.RUN_CURRENT_KEY, RUN)


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


def queue_order(master, rdb):
    """当前队列中的 (city, type) 顺序列表。"""
    out = []
    for item in rdb.lrange(master.TASK_QUEUE, 0, -1):
        d = json.loads(item)
        out.append((d["city"], d.get("type", "sale")))
    return out


# ================= 第 1 组：stale_abandoned 收敛（requeue 驱动） =================
def test_requeue_over_limit_writes_stale_abandoned_and_does_not_enqueue(master, rdb):
    """running + 锁过期 + 心跳超时 + requeue_count 已达 MAX_REQUEUE → 超限放弃，不再入队。"""
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


def test_requeue_under_limit_retries_and_keeps_finished_zero(master, rdb):
    """running 判死但 requeue_count 未达上限 → 重置 pending 重排（重试瞬死 worker）。"""
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


def test_requeue_leaves_alive_running_untouched(master, rdb):
    """心跳新鲜 / 锁仍在 → master 不得抢走正在跑的任务。"""
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


# ================= 第 2 组：按城交错调度（替代原 phase 切换） =================
def test_init_tasks_enqueues_all_42_city_interleaved(master, rdb):
    """bootstrap 起点（空 redis）：init_tasks 必须把全 42 任务一次性入队，
    且顺序为 fangyuan 先执行 [gz_fangyuan, sz_fangyuan, ..., yf_fangyuan, gz_sale, sz_sale, ..., yf_sale]。"""
    master.init_tasks(rdb)
    order = queue_order(master, rdb)
    assert order == [(t["city"], t["type"]) for t in master.DEFAULT_TASKS]
    assert len(order) == 42


def test_termination_all_tasks_done_sets_stop(master, rdb):
    """全 42 finished → _check_termination 置 STOP，crawl_status 以 all_finished 收口。"""
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")

    master._check_termination(rdb)
    assert rdb.get(master.STOP_KEY)

    st = master.crawl_status(rdb)
    assert st["all_done"] is True
    assert st["finished_tasks"] == 42
    assert st["done_reason"] == "all_finished"  # 靠真收敛，不是靠 STOP 兜底


def test_termination_empty_stall_sets_stop(master, rdb):
    """双池连续 EMPTY_STALL_CYCLES 轮皆空 → 置 STOP，all_done 经 stop 路径收口。"""
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb)  # 全 pending（未 finished）

    for _ in range(master.EMPTY_STALL_CYCLES):
        master._check_termination(rdb)

    assert rdb.get(master.STOP_KEY)
    st = master.crawl_status(rdb)
    assert st["all_done"] is True
    assert st["done_reason"].startswith("stop:")  # 经 STOP 兜底，非 all_finished


def test_requeue_enqueues_all_pending_on_day2(master, rdb):
    """第 2 天 run 的端到端护栏：引导 → 巡查 → 全 42 任务（含 fangyuan）同时入队且未误终态。"""
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    rdb.set(master.RUN_CURRENT_KEY, "2026-08-03")  # 上一 run，触发新 run 引导

    assert master._bootstrap_run(rdb) is True
    assert rdb.get(master.PHASE_KEY) == "city-interleave"  # 调度模式标记
    master.init_tasks(rdb)  # 任务 hash 已存在（被重置为 pending）→ 不重建、不重复入队
    master.requeue_stale_tasks(rdb)  # pending 补队 + running 判死处理

    q = queued(master, rdb)
    assert len(q) == 42, f"全 42 任务应入队，实际 {len(q)}"
    assert len({c for c, t in q if t == "fangyuan"}) == 21
    # 不得因「阶段没到」把任何任务盖成 finished
    for t in master.DEFAULT_TASKS:
        assert state(master, rdb, t["city"], t["type"])["finished"] == "0"


def test_requeue_enqueues_pending_but_does_not_finish(master, rdb):
    """回归护栏：全部 pending（非 running）→ requeue 只补队，绝误盖任何终态。"""
    seed_tasks(master, rdb)  # 全部 pending

    master.requeue_stale_tasks(rdb)
    q = queued(master, rdb)
    assert len(q) == 42  # 全补队
    for t in master.DEFAULT_TASKS:
        st = state(master, rdb, t["city"], t["type"])
        assert st["finished"] == "0", f"{t['city']}:{t['type']} 被误盖"
        assert st["finish_reason"] == ""


def test_requeue_idempotent_on_finished_reason(master, rdb):
    """幂等：已终态任务的 finish_reason 是审计依据，绝不能被覆写。"""
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    put_task(master, rdb, "gz", "sale", status="done", finished="1", finish_reason="target_reached")
    make_dead_running(master, rdb, "sz", "sale", requeue_count=master.MAX_REQUEUE)  # 仅 sz 判死超限

    master.requeue_stale_tasks(rdb)
    assert state(master, rdb, "sz", "sale")["finish_reason"] == "stale_abandoned"
    before = state(master, rdb, "sz", "sale")
    master.requeue_stale_tasks(rdb)  # 二次：sz 已 finished，不再改动
    assert state(master, rdb, "sz", "sale") == before
    assert state(master, rdb, "gz", "sale")["finish_reason"] == "target_reached"


def test_requeue_preserves_progress_and_url_sets(master, rdb):
    """requeue 不得碰断点与去重集合（增量方案的命脉）。"""
    seed_tasks(master, rdb)
    rdb.set("spacefin:crawl_progress:gz:sale", 57)
    rdb.sadd("spacefin:crawled_urls:gz:sale", "u1", "u2")

    master.requeue_stale_tasks(rdb)

    assert rdb.get("spacefin:crawl_progress:gz:sale") == "57"
    assert rdb.smembers("spacefin:crawled_urls:gz:sale") == {"u1", "u2"}


def test_requeue_abandons_running_dead(master, rdb):
    """maintenance 调 requeue_stale_tasks：running 判死超限 → 盖 stale_abandoned。"""
    seed_tasks(master, rdb)
    make_dead_running(master, rdb, "gz", "sale", requeue_count=master.MAX_REQUEUE)

    master.requeue_stale_tasks(rdb)

    st = state(master, rdb, "gz", "sale")
    assert st["finished"] == "1"
    assert st["finish_reason"] == "stale_abandoned"
    assert int(st["requeue_count"]) == master.MAX_REQUEUE + 1


# ================= 第 3 组：收敛终局（最关键） =================
def _seed_41_finished_plus_one_orphan(master, rdb):
    """41 个 finished + 1 个「running 判死 + requeue 超限」的孤儿。"""
    mark_run_ready(master, rdb)
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    make_dead_running(master, rdb, "gz", "sale", requeue_count=master.MAX_REQUEUE)
    rdb.delete(master.TASK_QUEUE)


def test_convergence_endgame_all_done_and_done_key(master, rdb):
    """41 finished + 1 超限孤儿 → requeue 后 all_done 为真且 done 键写入。

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
    seed_tasks(master, rdb, finished=True, status="done", finish_reason="pages_exhausted")
    put_task(master, rdb, "gz", "sale", status="pending", finished="0", finish_reason="")
    make_dead_running(master, rdb, "sz", "sale", requeue_count=master.MAX_REQUEUE)
    make_alive_running(master, rdb, "zh", "sale")
    rdb.delete(master.TASK_QUEUE)

    master.requeue_stale_tasks(rdb)
    st = master.crawl_status(rdb)
    # 41 原始 finished + sz 被 stale_abandoned = 42 中 40 完成；gz pending / zh 存活 仍未完成
    assert st["finished_tasks"] == 40
    assert st["all_done"] is False

    # worker 自己写终态（finish_task 的等价写入）
    put_task(
        master, rdb, "zh", "sale", status="done", finished="1", finish_reason="pages_exhausted"
    )
    # gz 仍为 pending（被补队等待 worker 领取）；模拟 worker 最终把它跑完
    put_task(
        master, rdb, "gz", "sale", status="done", finished="1", finish_reason="pages_exhausted"
    )
    st = master.crawl_status(rdb)
    assert st["all_done"] is True and st["finished_tasks"] == 42
    assert rdb.get(master._run_done_key(RUN))


# ================= 第 4 组：worker 侧 fail_budget 收敛 =================
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
