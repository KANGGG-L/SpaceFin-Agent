#!/usr/bin/env python3
"""
master 容器（主备高可用版）：任务分配 + 资源（代理）管理 + 主备选举。

职责：
1. 主备选举：Redis 抢锁（SET NX + EX 60s）决定 leader；leader 续期心跳，
   standby 检测心跳超时自动接管（避免 master 单点）。
2. 任务分配（leader）：维护 42 任务状态 Hash `spacefin:task:{city}:{type}`
   （21 城 × sale/fangyuan），pending 任务推入队列 `spacefin:tasks`；
   worker 心跳超时 + 锁过期的任务重新入队；任务按轮次循环派单。
3. 代理巡查（leader）：周期拉代理源 -> 并发验证 HTTPS CONNECT 到安居客
   -> 注入 Redis 双池 `spacefin:proxy_pool:qg`/`:free`；定期复查剔除失效；
   为每个注册 worker 同步 `spacefin:proxy_list:{worker}`。
4. HTTP API（:5100）：
      GET /health               存活
      GET /role                 当前角色 leader/standby
      GET /pool_count           代理池规模（qg/free/total）
      GET /tasks                任务状态总览（round/new/dup/source）
      GET /proxy/qg|/free|/random  原子弹出一个可用代理（worker 抓取用）
      GET /proxies?worker=x     该 worker 当前 proxy list
"""

import concurrent.futures
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import redis

REDIS_HOST = os.getenv("REDIS_HOST", "spacefin-redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
MY_ID = os.getenv("HOSTNAME", "master-unknown")

# 区分两个代理池：青果优先，免费源兜底
POOL_QG = "spacefin:proxy_pool:qg"  # 青果短效代理池（优先使用）
POOL_FREE = "spacefin:proxy_pool:free"  # 免费代理池（兜底）
WORKERS_KEY = "spacefin:workers"  # set of worker names
TASK_QUEUE = "spacefin:tasks"  # list of pending tasks
TASK_PREFIX = "spacefin:task:"  # hash per city:type
LEADER_KEY = "spacefin:master:leader"  # current leader
HB_KEY = "spacefin:master:heartbeat"  # leader heartbeat ts
WORKER_HB_PREFIX = "spacefin:worker_hb:"  # worker 心跳 key 前缀
QG_CONSUMED_KEY = "spacefin:qg_consumed"  # 青果提取侧计数（停止条件）
LOCK_PREFIX = "spacefin:task_lock:"  # 任务独占锁前缀
PHASE_KEY = "spacefin:phase"  # 阶段：sale(先出售) / fangyuan(后出租)
STOP_KEY = "spacefin:stop"  # 全局终止信号（fangyuan 提前结束/资源耗尽置位）
EMPTY_CYCLES_KEY = "spacefin:empty_cycles"  # 双池连续空转轮数计数（终止判定用）

POOL_SIZE_MIN = int(os.getenv("POOL_SIZE_MIN", 60))
QG_TARGET = int(os.getenv("QG_TARGET", 5))  # 青果池目标保有量
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", 90))  # 代理巡查周期(秒)
LEADER_TTL = int(os.getenv("LEADER_TTL", 60))  # 锁/心跳 TTL(秒)
WORKER_TTL = int(os.getenv("WORKER_TTL", 120))  # worker 心跳超时(秒)
WORKER_HB_TTL = int(os.getenv("WORKER_HB_TTL", 120))  # worker 心跳 key TTL
MASTER_PORT = int(os.getenv("MASTER_PORT", 5100))
QG_BUDGET = int(os.getenv("QG_BUDGET", 1000))  # 青果 IP 总预算（跑满 1000）
QG_SALE_BUDGET = int(os.getenv("QG_SALE_BUDGET", 600))  # sale 阶段青果配额（前 600）
EMPTY_STALL_CYCLES = int(os.getenv("EMPTY_STALL_CYCLES", 3))  # 双池连续空转 N 轮 → 终止
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", 3))  # 每任务循环轮次上限

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]
TARGET_HOST = os.getenv("TARGET_HOST", "guangzhou.anjuke.com")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------- 青果短效代理源 ----------------
# 青果：总配额 1000 个 IP、存活 1 分钟、提取即消耗、需 Basic Proxy-Authorization。
# 按需拉取：num=deficit（缺多少拉多少），池满即停，避免浪费配额。
# 凭据通过环境变量注入（docker-compose 从 .env 读取，.env 已 gitignore），不写入源码。
QG_API = os.getenv(
    "QG_API",
    "https://share.proxy.qg.net/get?num=5&area=&isp=0&format=json&seq=&distinct=false",
)
QG_USER = os.getenv("QG_USER", "")
QG_PWD = os.getenv("QG_PWD", "")
QG_ENABLED = os.getenv("QG_ENABLED", "0") == "1"

# 任务定义：广东 21 城 × sale(出售)/fangyuan(出租)。
# pages 分开：sale=100, fangyuan=300（出租翻得更深，空页早停仍会提前收敛）。
# target 极大，靠空页早停或 pages 上限收敛。各任务独立 round。
CITIES = [
    "gz",
    "sz",
    "zh",
    "st",
    "fs",
    "sg",
    "zj",
    "zq",
    "jm",
    "mm",
    "hui",
    "mz",
    "sw",
    "hy",
    "yj",
    "qy",
    "dg",
    "zs",
    "cz",
    "jy",
    "yf",
]
PAGES_SALE = int(os.getenv("PAGES_SALE", 100))
PAGES_RENT = int(os.getenv("PAGES_RENT", 300))
TARGET = int(os.getenv("TARGET", 999999))


def _build_default_tasks():
    """生成 42 任务（21 城 × sale/fangyuan）。"""
    tasks = []
    for c in CITIES:
        tasks.append({"city": c, "type": "sale", "pages": PAGES_SALE, "target": TARGET})
        tasks.append({"city": c, "type": "fangyuan", "pages": PAGES_RENT, "target": TARGET})
    return tasks


DEFAULT_TASKS = _build_default_tasks()


def log(msg):
    print(f"[master:{MY_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- 代理巡查 ----------------
def _connect_ok(proxy, timeout=6):
    host, port = proxy.split(":")
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout + 3)
        req = f"CONNECT {TARGET_HOST}:443 HTTP/1.1\r\nHost: {TARGET_HOST}:443\r\n\r\n"
        s.sendall(req.encode())
        resp = s.recv(4096).decode("utf-8", "ignore")
        if "200" not in resp.split("\r\n")[0]:
            s.close()
            return None
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(s, server_hostname=TARGET_HOST)
        tls.close()
        return proxy
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def _download_source(url, retries=3, timeout=20):
    """拉代理源列表。源站（raw.githubusercontent.com 等）对容器出口经常瞬时超时，
    一次失败就放弃会导致补池轮空 → 双池皆空 → 误判资源耗尽终止全局。必须重试。
    """
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            log(f"source download fail {url} (try {attempt}/{retries}): {e}")
            time.sleep(attempt * 2)  # 2s/4s/6s 退避
    return ""


def fetch_qg_proxies(num=5):
    """从青果按需提取短效代理（需认证）。num=实际需求量（配额宝贵，勿多拉）。

    返回 (server, deadline) 列表；num 由调用方按 deficit 传入，避免固定 num 浪费配额。
    """
    if not QG_ENABLED or num <= 0:
        return []
    try:
        # 动态设置 num，且不超过单次上限 1000
        import urllib.parse

        from curl_cffi import requests as creq

        n = min(num, 1000)
        url = (
            QG_API.split("?")[0]
            + "?"
            + urllib.parse.urlencode(
                {
                    **{
                        k: v
                        for k, v in [
                            p.split("=", 1) for p in QG_API.split("?")[1].split("&") if "=" in p
                        ]
                    },
                    "num": str(n),
                }
            )
        )
        r = creq.get(url, impersonate="chrome", timeout=20, headers={"User-Agent": UA})
        data = r.json()
        if data.get("code") != "SUCCESS":
            log(f"qg api error: {data}")
            return []
        items = data.get("data", [])
        out = [(x["server"], x.get("deadline", "")) for x in items if x.get("server")]
        log(
            f"qg extracted {len(out)} proxies (requested={n}, deadline ~{items[0].get('deadline') if items else '?'})"
        )
        return out
    except Exception as e:
        log(f"qg fetch error: {e}")
        return []


def _qg_connect_ok(proxy, timeout=6):
    """青果代理带认证验证 HTTPS CONNECT。"""
    import base64

    host, port = proxy.split(":")
    token = base64.b64encode(f"{QG_USER}:{QG_PWD}".encode()).decode()
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout + 3)
        req = (
            f"CONNECT {TARGET_HOST}:443 HTTP/1.1\r\nHost: {TARGET_HOST}:443\r\n"
            f"Proxy-Authorization: Basic {token}\r\n\r\n"
        )
        s.sendall(req.encode())
        resp = s.recv(4096).decode("utf-8", "ignore")
        if "200" not in resp.split("\r\n")[0]:
            s.close()
            return None
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(s, server_hostname=TARGET_HOST)
        tls.close()
        return proxy
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def refill_qg(rdb):
    """青果池补拉（独立闸门，与免费池规模无关）。

    青果：存活1分钟、配额1000个、提取即消耗 → 池中青果代理不足 QG_TARGET 时按缺口补拉
    （num=deficit，缺多少拉多少，不浪费配额）；提取侧计数 `qg_consumed` 累计，
    达 QG_BUDGET 后停止补拉（跑满配额），worker 转免费池。
    """
    if not QG_ENABLED:
        return 0
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    if consumed >= QG_BUDGET:
        log(f"qg budget exhausted ({consumed}/{QG_BUDGET}), stop qg extraction")
        return 0
    qg_count = rdb.hlen(POOL_QG)
    deficit = max(0, QG_TARGET - qg_count)
    if deficit <= 0:
        log(f"qg pool sufficient ({qg_count}/{QG_TARGET}), skip extraction to save quota")
        return 0
    qg = fetch_qg_proxies(num=deficit)
    if not qg:
        return 0
    # 提取侧计数：API 按 num 扣配额
    rdb.incrby(QG_CONSUMED_KEY, len(qg))
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    usable = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        for r in ex.map(lambda x: _qg_connect_ok(x[0]), qg):
            if r:
                usable.append(r)
    pipe = rdb.pipeline()
    for p in usable:
        val = json.dumps(
            {
                "proxy": p,
                "https": True,
                "fail_count": 0,
                "region": None,
                "anonymous": "",
                "source": "qg",
                "check_count": 0,
                "last_status": True,
                "last_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        pipe.hset(POOL_QG, p, val)
    pipe.execute()
    injected = len(usable)
    log(
        f"qg on-demand injected {injected} (deficit={deficit}), "
        f"consumed={consumed}/{QG_BUDGET}, qg_pool={rdb.hlen(POOL_QG)}"
    )
    return injected


def refill_free(rdb):
    """免费池兜底（独立闸门）：仅当青果池为空时填充（青果可用则优先，不拉免费）。"""
    if rdb.hlen(POOL_QG) != 0:
        return 0
    seen = set()
    for url in PROXY_SOURCES:
        for line in _download_source(url).splitlines():
            line = line.strip()
            if line and ":" in line and len(line) < 40:
                seen.add(line)
    candidates = list(seen)
    log(f"free fallback candidates={len(candidates)}, verifying HTTPS CONNECT...")
    usable = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=80) as ex:
        for r in ex.map(_connect_ok, candidates):
            if r:
                usable.append(r)
    if not usable:
        return 0
    pipe = rdb.pipeline()
    for p in usable:
        val = json.dumps(
            {
                "proxy": p,
                "https": True,
                "fail_count": 0,
                "region": None,
                "anonymous": "",
                "source": "free",
                "check_count": 0,
                "last_status": True,
                "last_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        pipe.hset(POOL_FREE, p, val)
    pipe.execute()
    log(f"free injected {len(usable)} proxies, free_pool={rdb.hlen(POOL_FREE)}")
    return len(usable)


def prune_pool(rdb):
    """双池清理：青果池按 1 分钟过期即删（不重验，省配额）；免费池 CONNECT 验证剔除。"""
    dead = []

    # 青果池：按注入时间（≈deadline）过期即删
    for key in list(rdb.hkeys(POOL_QG)):
        try:
            val = rdb.hget(POOL_QG, key)
            d = json.loads(val) if isinstance(val, str) else json.loads(val.decode())
            ts = d.get("last_time", "")
            if _elapsed_seconds(ts) > 55:  # 短效 1 分钟，55s 视为过期
                dead.append((POOL_QG, key))
        except Exception:
            dead.append((POOL_QG, key))

    # 免费池：CONNECT 验证
    free_keys = [x.decode() if isinstance(x, bytes) else x for x in rdb.hkeys(POOL_FREE)]
    if free_keys:
        with concurrent.futures.ThreadPoolExecutor(max_workers=60) as ex:
            for p, ok in zip(free_keys, ex.map(_connect_ok, free_keys), strict=True):
                if not ok:
                    dead.append((POOL_FREE, p))

    for pool, key in dead:
        rdb.hdel(pool, key)
    log(
        f"pruned {len(dead)} expired/dead proxies, "
        f"qg={rdb.hlen(POOL_QG)} free={rdb.hlen(POOL_FREE)}"
    )
    return len(dead)


def _source_of(rdb, proxy):
    try:
        val = rdb.hget(POOL_FREE, proxy) or rdb.hget(POOL_QG, proxy)
        d = json.loads(val) if isinstance(val, str) else json.loads(val.decode())
        return d.get("source", "")
    except Exception:
        return ""


def _elapsed_seconds(ts_str):
    """计算 'YYYY-MM-DD HH:MM:SS' 距今秒数；解析失败返回大数（视为过期）。"""
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        t = time.mktime(time.strptime(ts_str, fmt))
        return time.time() - t
    except Exception:
        return 1 << 30


# 原子弹出：HKEYS + HDEL（消费即删，不归还）；池空返回 nil
POP_PROXY_LUA = """
local keys = redis.call('HKEYS', KEYS[1])
if #keys == 0 then return false end
redis.call('HDEL', KEYS[1], keys[1])
return keys[1]
"""


def pop_proxy(rdb, pool):
    """原子弹出一个可用代理；池空返回 None。青果/免费皆适用（一次性消费）。"""
    try:
        return rdb.eval(POP_PROXY_LUA, 1, pool)
    except Exception as e:
        log(f"pop proxy error: {e}")
        return None


def _remove_from_worker_lists(rdb, proxy):
    """弹出分配后，把该代理从所有 worker 本地回退列表移除，封堵回退缝隙。"""
    try:
        for w in rdb.smembers(WORKERS_KEY):
            rdb.srem(f"spacefin:proxy_list:{w}", proxy)
    except Exception:
        pass


def prune_stale_workers(rdb):
    """清除心跳超时的 worker 注册（防 force-recreate 残留累积 + 旧版本遗留）。"""
    stale = 0
    for w in list(rdb.smembers(WORKERS_KEY)):
        w = w.decode() if isinstance(w, bytes) else w
        if not rdb.exists(WORKER_HB_PREFIX + w):
            rdb.srem(WORKERS_KEY, w)
            rdb.delete(f"spacefin:proxy_list:{w}")
            stale += 1
            log(f"worker {w} heartbeat gone, removed registration")
    if stale:
        log(f"pruned {stale} stale worker registrations, remaining={rdb.scard(WORKERS_KEY)}")
    return stale


def sync_worker_lists(rdb):
    """同步每 worker 的 proxy list（青果在前、免费在后，供 worker 本地回退用）。"""
    workers = [w.decode() if isinstance(w, bytes) else w for w in rdb.smembers(WORKERS_KEY)]
    if not workers:
        return
    qg = [p.decode() if isinstance(p, bytes) else p for p in rdb.hkeys(POOL_QG)]
    free = [p.decode() if isinstance(p, bytes) else p for p in rdb.hkeys(POOL_FREE)]
    for w in workers:
        key = f"spacefin:proxy_list:{w}"
        pipe = rdb.pipeline()
        pipe.delete(key)
        for p in qg:  # 青果优先
            pipe.sadd(key, p)
        for p in free:  # 免费兜底
            pipe.sadd(key, p)
        pipe.execute()
    log(f"synced qg={len(qg)} free={len(free)} -> {len(workers)} workers")


# ---------------- 任务分配 ----------------
def _task_key(city, typ):
    return f"{TASK_PREFIX}{city}:{typ}"


def _get_phase(rdb):
    """当前阶段：sale(先跑出售，最快消耗青果配额) / fangyuan(后跑出租，不耗 IP)。缺省 sale。"""
    return rdb.get(PHASE_KEY) or "sale"


def _init_phase_tasks(rdb, typ):
    """初始化某阶段任务（状态缺失才创建 pending round=0 并入队，已有状态保持断点）。"""
    for t in DEFAULT_TASKS:
        if t["type"] != typ:
            continue
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            state = {
                "city": t["city"],
                "type": t["type"],
                "pages": t["pages"],
                "target": t["target"],
                "round": 0,
                "status": "pending",
                "worker": "",
                "count": 0,
                "worker_hb": 0,
                "ts": time.time(),
            }
            rdb.hset(key, mapping=state)
            rdb.rpush(TASK_QUEUE, json.dumps(t))


def init_tasks(rdb):
    """按当前阶段初始化任务：仅当前阶段（sale/fangyuan）任务创建并入队。"""
    _init_phase_tasks(rdb, _get_phase(rdb))
    log(f"tasks initialized, queue len={rdb.llen(TASK_QUEUE)}")


def _all_type_done(rdb, typ):
    """某类型全部任务已达 MAX_ROUNDS（用于阶段推进判断）。"""
    for t in DEFAULT_TASKS:
        if t["type"] != typ:
            continue
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            return False
        if not _is_finished(rdb, key):
            return False
    return True


def _check_phase_transition(rdb):
    """sale -> fangyuan：sale 全部完成 或 青果配额耗尽 时切换，并初始化 fangyuan 任务。"""
    if _get_phase(rdb) != "sale":
        return
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    if consumed >= QG_SALE_BUDGET:
        reason = f"sale qg quota consumed ({consumed}/{QG_SALE_BUDGET})"
    elif _all_type_done(rdb, "sale"):
        reason = "all sale tasks reached MAX_ROUNDS"
    else:
        return
    log(f"phase transition sale -> fangyuan: {reason}")
    rdb.set(PHASE_KEY, "fangyuan")
    _init_phase_tasks(rdb, "fangyuan")


def _check_termination(rdb):
    """fangyuan 阶段终止判定（达成任一条件即置 STOP_KEY，worker 暂停、等待统计）：
    - 全部 fangyuan 任务达 MAX_ROUNDS（提前结束）；
    - qg 配额跑满且双池皆空（资源真正耗尽）；
    - 双池连续 EMPTY_STALL_CYCLES 轮皆空（qg 提取失败/免费源失效，避免无限空转）。
    """
    if _get_phase(rdb) != "fangyuan" or rdb.exists(STOP_KEY):
        return
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    qg_empty = rdb.hlen(POOL_QG) == 0
    free_empty = rdb.hlen(POOL_FREE) == 0
    reason = ""
    if _all_type_done(rdb, "fangyuan"):
        reason = f"all fangyuan tasks reached MAX_ROUNDS (consumed={consumed})"
    elif consumed >= QG_BUDGET and qg_empty and free_empty:
        reason = f"qg budget exhausted ({consumed}/{QG_BUDGET}) and both pools empty"
    elif qg_empty and free_empty:
        n = int(rdb.incr(EMPTY_CYCLES_KEY))
        if n >= EMPTY_STALL_CYCLES:
            reason = f"both pools empty for {n} cycles (consumed={consumed}/{QG_BUDGET})"
    else:
        rdb.delete(EMPTY_CYCLES_KEY)
    if reason:
        log(f"TERMINATION: {reason}")
        rdb.set(STOP_KEY, reason)


def _is_finished(rdb, key):
    """任务已达 MAX_ROUNDS（所有轮次完成）。"""
    try:
        round_n = int(rdb.hget(key, "round") or 0)
        return round_n >= MAX_ROUNDS
    except Exception:
        return False


def _purge_queue(rdb, typ):
    """清理队列中非当前阶段的任务（防阶段切换残留/重启残留被 worker 误领）。"""
    removed = 0
    for item in rdb.lrange(TASK_QUEUE, 0, -1):
        try:
            d = json.loads(item) if isinstance(item, str) else json.loads(item.decode())
            if d.get("type", "sale") != typ:
                rdb.lrem(TASK_QUEUE, 0, item)
                removed += 1
        except Exception:
            pass
    if removed:
        log(f"purged {removed} stale phase-queue items")
    return removed


def requeue_stale_tasks(rdb):
    """管理任务队列（仅当前阶段任务入队，非当前阶段不可被领）：
    - running 但锁已过期且 worker 心跳超时 → 重新入队（本轮重跑，round 不消耗）；
    - 所有任务达 MAX_ROUNDS 且无 running/pending → 系统收敛结束。
    """
    now = time.time()
    phase = _get_phase(rdb)
    queued = set()
    for item in rdb.lrange(TASK_QUEUE, 0, -1):
        try:
            d = json.loads(item) if isinstance(item, str) else json.loads(item.decode())
            queued.add((d["city"], d.get("type", "sale")))
        except Exception:
            pass
    for t in DEFAULT_TASKS:
        if t["type"] != phase:
            continue
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            continue
        state = rdb.hgetall(key)
        status = state.get("status", "pending")
        lock_key = LOCK_PREFIX + f"{t['city']}:{t['type']}"
        if status == "running":
            lock_alive = rdb.exists(lock_key)
            worker_hb = float(state.get("worker_hb", 0) or 0)
            if (not lock_alive) and (now - worker_hb > WORKER_TTL):
                log(f"task {t['city']}:{t['type']} lock expired + heartbeat stale, requeue")
                rdb.hset(key, mapping={"status": "pending", "worker": "", "worker_hb": 0})
                rdb.rpush(TASK_QUEUE, json.dumps(t))
        elif status == "done":
            round_n = int(state.get("round", 0) or 0)
            if round_n < MAX_ROUNDS and (t["city"], t["type"]) not in queued:
                log(
                    f"task {t['city']}:{t['type']} done round {round_n} "
                    f"< MAX_ROUNDS({MAX_ROUNDS}), requeue round {round_n + 1}"
                )
                rdb.hset(key, mapping={"status": "pending", "worker": "", "worker_hb": 0})
                rdb.rpush(TASK_QUEUE, json.dumps(t))
        elif status == "pending" and (t["city"], t["type"]) not in queued:
            rdb.rpush(TASK_QUEUE, json.dumps(t))


# ---------------- 主备选举 ----------------
_leader_state = {"is_leader": False}


def try_become_leader(rdb):
    """抢锁成为 leader。返回是否成功。"""
    ok = rdb.set(LEADER_KEY, MY_ID, nx=True, ex=LEADER_TTL)
    if ok:
        rdb.set(HB_KEY, time.time(), ex=LEADER_TTL)
        _leader_state["is_leader"] = True
        log("BECOME LEADER")
        return True
    return False


def renew_leadership(rdb):
    """leader 续期：校验自己仍是 leader 才续期。key 丢失时让位（避免双主振荡）。"""
    cur = rdb.get(LEADER_KEY)
    if cur == MY_ID:
        rdb.set(LEADER_KEY, MY_ID, xx=True, ex=LEADER_TTL)
        rdb.set(HB_KEY, time.time(), ex=LEADER_TTL)
        return True
    if cur is None:
        # 锁意外丢失：不主动抢回，转 standby，由 standby_loop 统一处理接管，
        # 避免两个 master 互相抢锁形成振荡。
        log("lock lost, going standby")
        _leader_state["is_leader"] = False
        return False
    log(f"renew: leader key is '{cur}', not mine")
    _leader_state["is_leader"] = False
    return False


def heartbeat_thread(rdb):
    """独立心跳线程：持续续期锁，防止巡查等长任务期间锁过期被 standby 接管。"""
    while _leader_state["is_leader"]:
        try:
            if not renew_leadership(rdb):
                log("lost leadership, back to standby")
                return
        except Exception as e:
            log(f"heartbeat error: {e}")
        time.sleep(max(1, LEADER_TTL // 3))


def watch_leader(rdb):
    """standby：监控 leader 心跳，超时则尝试接管。"""
    hb = rdb.get(HB_KEY)
    if hb is None:
        return try_become_leader(rdb)
    try:
        elapsed = time.time() - float(hb)
    except Exception:
        return try_become_leader(rdb)
    if elapsed > LEADER_TTL * 2:
        log(f"leader heartbeat stale ({elapsed:.0f}s), trying takeover")
        return try_become_leader(rdb)
    return False


def leader_loop(rdb):
    """leader 主循环：每 3 秒续期锁 + 快速调度；长任务交给 maintenance 线程。"""

    def maintenance():
        while _leader_state["is_leader"]:
            try:
                # 已终止：保持心跳/API，跳过调度与补池（统计数据阶段）
                if rdb.exists(STOP_KEY):
                    time.sleep(REFRESH_INTERVAL)
                    continue
                init_tasks(rdb)
                requeue_stale_tasks(rdb)
                _check_phase_transition(rdb)
                # 确保队列只含当前阶段任务（阶段切换/重启残留防误领）
                _purge_queue(rdb, _get_phase(rdb))
                prune_stale_workers(rdb)
                # 双池独立闸门：青果按 QG_TARGET 补（sale/fangyuan 两阶段都真实耗 IP）、
                # 免费按 POOL_SIZE_MIN 补（青果池空时才拉，兜底）。
                if rdb.hlen(POOL_QG) < QG_TARGET:
                    refill_qg(rdb)
                if rdb.hlen(POOL_FREE) < POOL_SIZE_MIN:
                    refill_free(rdb)
                prune_pool(rdb)
                sync_worker_lists(rdb)
                _check_termination(rdb)
            except Exception as e:
                log(f"maintenance error: {e}")
            time.sleep(REFRESH_INTERVAL)

    threading.Thread(target=maintenance, daemon=True).start()

    while _leader_state["is_leader"]:
        try:
            if not renew_leadership(rdb):
                log("lost leadership, back to standby")
                return
        except Exception as e:
            log(f"leader loop error: {e}")
        time.sleep(max(1, LEADER_TTL // 3))


def standby_loop(rdb):
    while True:
        try:
            if watch_leader(rdb):
                log("takeover success, entering leader loop")
                return True
        except Exception as e:
            log(f"standby loop error: {e}")
        time.sleep(5)
    return False


# ---------------- HTTP API ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            for kv in self.path.split("?")[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    query[k] = v
        rdb = self.server.rdb

        if path == "/health":
            self._json({"status": "ok", "id": MY_ID, "role": self.server.role})
        elif path == "/role":
            self._json({"id": MY_ID, "role": self.server.role, "leader": rdb.get(LEADER_KEY)})
        elif path == "/pool_count":
            self._json(
                {
                    "qg": rdb.hlen(POOL_QG),
                    "free": rdb.hlen(POOL_FREE),
                    "total": rdb.hlen(POOL_QG) + rdb.hlen(POOL_FREE),
                }
            )
        elif path == "/tasks":
            out = {}
            total_req = 0
            total_new = 0
            total_dup = 0
            for t in DEFAULT_TASKS:
                key = _task_key(t["city"], t["type"])
                st = rdb.hgetall(key) if rdb.exists(key) else {}
                new_c = int(st.get("new_count", 0) or 0)
                dup_c = int(st.get("dup_count", 0) or 0)
                total_req += new_c + dup_c
                total_new += new_c
                total_dup += dup_c
                out[f"{t['city']}:{t['type']}"] = {
                    "round": st.get("round", 0),
                    "max_round": MAX_ROUNDS,
                    "status": st.get("status", "?"),
                    "count": st.get("count", 0),
                    "new": new_c,
                    "dup": dup_c,
                    "pages_done": st.get("pages_done", 0),
                    "source": st.get("source", ""),
                    "worker": st.get("worker", ""),
                }
            self._json(
                {
                    "tasks": out,
                    "queue_len": rdb.llen(TASK_QUEUE),
                    "phase": _get_phase(rdb),
                    "stop": rdb.get(STOP_KEY),
                    "qg_consumed": int(rdb.get(QG_CONSUMED_KEY) or 0),
                    "qg_budget": QG_BUDGET,
                    "qg_sale_budget": QG_SALE_BUDGET,
                    "total_request": total_req,
                    "total_new": total_new,
                    "total_dup": total_dup,
                }
            )
        elif path == "/proxy/qg":
            p = pop_proxy(rdb, POOL_QG)
            if not p:
                self._json({"proxy": None, "error": "qg pool empty"})
                return
            _remove_from_worker_lists(rdb, p)
            self._json({"proxy": p, "source": "qg"})
        elif path == "/proxy/free":
            p = pop_proxy(rdb, POOL_FREE)
            if not p:
                self._json({"proxy": None, "error": "free pool empty"})
                return
            _remove_from_worker_lists(rdb, p)
            self._json({"proxy": p, "source": "free"})
        elif path == "/proxy/random":
            # 青果优先，青果空则免费（分次 pop，精确记录 source）
            p = pop_proxy(rdb, POOL_QG)
            if p:
                _remove_from_worker_lists(rdb, p)
                self._json({"proxy": p, "source": "qg"})
                return
            p = pop_proxy(rdb, POOL_FREE)
            if p:
                _remove_from_worker_lists(rdb, p)
                self._json({"proxy": p, "source": "free"})
                return
            self._json({"proxy": None, "error": "both pools empty"})
        elif path == "/proxies":
            worker = query.get("worker", "")
            key = f"spacefin:proxy_list:{worker}"
            proxies = [x.decode() if isinstance(x, bytes) else x for x in rdb.smembers(key)]
            self._json({"worker": worker, "count": len(proxies), "proxies": proxies})
        else:
            self._json({"error": "not found"}, 404)


class MasterServer(HTTPServer):
    def __init__(self, addr, rdb):
        self.rdb = rdb
        self.role = "starting"
        super().__init__(addr, Handler)


def main():
    rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    rdb.ping()
    log(
        f"master up, redis={REDIS_HOST}:{REDIS_PORT}, "
        f"qg_pool={rdb.hlen(POOL_QG)} free_pool={rdb.hlen(POOL_FREE)}"
    )

    srv = MasterServer(("0.0.0.0", MASTER_PORT), rdb)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"API listening on :{MASTER_PORT}")

    # 先尝试成为 leader；否则进入 standby 监控
    is_leader = try_become_leader(rdb)
    srv.role = "leader" if is_leader else "standby"

    while True:
        if srv.role == "leader":
            leader_loop(rdb)  # 失锁时返回，转为 standby
            _leader_state["is_leader"] = False
            srv.role = "standby"
        else:
            if standby_loop(rdb):  # 接管成功
                srv.role = "leader"
        time.sleep(2)


if __name__ == "__main__":
    main()
