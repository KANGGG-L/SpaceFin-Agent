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
      GET /crawl_status         本 run 采集完成状态（Airflow Sensor 依据）
      GET /proxy/qg|/free|/random?city=&type=  原子弹出一个可用代理（按城预算约束）
      GET /proxy/report?city=&type=&src=qg&page=&proxy=&attempt=  抓取失败退款上报
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
TASK_PROCESSING_QUEUE = "spacefin:tasks:processing"  # processing task queue
TASK_PREFIX = "spacefin:task:"  # hash per city:type
LEADER_KEY = "spacefin:master:leader"  # current leader
HB_KEY = "spacefin:master:heartbeat"  # leader heartbeat ts
WORKER_HB_PREFIX = "spacefin:worker_hb:"  # worker 心跳 key 前缀
QG_CONSUMED_KEY = "spacefin:qg_consumed"  # 青果提取侧计数（停止条件）
QG_LAST_POP_KEY = "spacefin:qg_last_pop"  # 最近一次成功发放青果的时间戳（需求闸门）
QG_REFILL_KEY = "spacefin:qg_last_refill"  # 最近一次青果提取时间戳（提取限流窗口）
FREE_USED_KEY = "spacefin:free_used"  # 本轮已发放的免费代理计数（免费池总上限）
LOCK_PREFIX = "spacefin:task_lock:"  # 任务独占锁前缀
PHASE_KEY = "spacefin:phase"  # 调度模式观测标记（2026-08-07 起恒置 "city-interleave"，仅作观测）
STOP_KEY = "spacefin:stop"  # 全局终止信号（资源耗尽/物理终态置位）
EMPTY_CYCLES_KEY = "spacefin:empty_cycles"  # 双池连续空转轮数计数（终止判定用）
IP_USED_PREFIX = "spacefin:ip_used:"  # 每城每类型已发放的青果代理次数
IP_REFUNDED_PREFIX = "spacefin:ip_refunded:"  # 每城每类型失败退还次数
IP_BUDGET_PREFIX = "spacefin:ip_budget:"  # 每城每类型青果预算（便于外部查看）
REFUND_SEEN_PREFIX = "spacefin:refund_seen:"  # 退款 nonce 防重
RUN_CURRENT_KEY = "spacefin:crawl_run:current"  # 当前 run id

# 波次状态机 Redis 键与配置
WAVE_KEY = "spacefin:crawl_wave"  # 当前波次：floor/rescue/depth/done
WAVE_NEXT_KEY = "spacefin:crawl_wave:next"  # 瞬态崩溃安全转移标记
WAVE_LOG_KEY = "spacefin:crawl_wave_log"  # 每波次快照 hash
WAVE_ENABLED = os.getenv("WAVE_ENABLED", "1") == "1"
WAVE_FLOOR_PAGES = int(os.getenv("WAVE_FLOOR_PAGES", 5))
WAVE_DEPTH_ENABLED = os.getenv("WAVE_DEPTH_ENABLED", "1") == "1"
FANGYUAN_FREE_RESCUE = os.getenv("FANGYUAN_FREE_RESCUE", "1") == "1"
ACTIVE_WAVES = ("floor", "rescue", "depth")
FLOOR_MET = {"pages_exhausted", "empty_pages", "not_found", "target_reached"}

POOL_SIZE_MIN = int(os.getenv("POOL_SIZE_MIN", 60))
QG_TARGET = int(os.getenv("QG_TARGET", 5))  # 青果池目标保有量
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", 90))  # 代理巡查周期(秒)
LEADER_TTL = int(os.getenv("LEADER_TTL", 60))  # 锁/心跳 TTL(秒)
WORKER_TTL = int(os.getenv("WORKER_TTL", 120))  # worker 心跳超时(秒)
WORKER_HB_TTL = int(os.getenv("WORKER_HB_TTL", 120))  # worker 心跳 key TTL
MASTER_PORT = int(os.getenv("MASTER_PORT", 5100))
QG_BUDGET = int(os.getenv("QG_BUDGET", 1000))  # 青果 IP 总预算（跑满 1000）
QG_SALE_BUDGET = int(
    os.getenv("QG_SALE_BUDGET", 500)
)  # 历史观测字段（/tasks 回显）；2026-08-07 起不再作为发放闸门，每城配额改由 try_consume_ip 保障
# 补池节流（秒）：避免高并发时频繁触发青果 API（青果提取有频控，默认 60s 经验值）
QG_REFILL_INTERVAL = int(os.getenv("QG_REFILL_INTERVAL", 60))
FREE_BUDGET = int(
    os.getenv("FREE_BUDGET", 1000)
)  # 本轮免费代理发放总上限（到限后本轮不再用免费池）
EMPTY_STALL_CYCLES = int(os.getenv("EMPTY_STALL_CYCLES", 3))  # 双池连续空转 N 轮 → 终止
MAX_REQUEUE = int(
    os.getenv("MAX_REQUEUE", 3)
)  # 单任务一次 run 内重排次数上限（收敛保证，非重试调优）

# ---------------- 每城 IP 预算 ----------------
# 青果 1000 IP 按城市配额：头部城（gz/sz）各占 60，其余 19 城各 20（总额 500/500）。
# 失败退款后语义为"成功页数上限"。
IP_BUDGET_ENABLED = os.getenv("IP_BUDGET_ENABLED", "1") == "1"
BUDGET_TOP_CITIES = [
    c.strip() for c in os.getenv("BUDGET_TOP_CITIES", "gz,sz").split(",") if c.strip()
]
IP_BUDGET_SALE_TOP = int(os.getenv("IP_BUDGET_SALE_TOP", 60))
IP_BUDGET_SALE_OTHER = int(os.getenv("IP_BUDGET_SALE_OTHER", 20))
IP_BUDGET_FY_TOP = int(os.getenv("IP_BUDGET_FY_TOP", 60))
IP_BUDGET_FY_OTHER = int(os.getenv("IP_BUDGET_FY_OTHER", 20))
IP_BUDGET_JSON = os.getenv("IP_BUDGET_JSON", "")
CRAWL_RUN_ID = os.getenv("CRAWL_RUN_ID", "manual")  # 本次 run 标识（Airflow 传 {{ ds }}）

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
    """生成 42 任务（21 城 × sale/fangyuan）。

    2026-08-07 改（fangyuan 先执行）：全 21 城 fangyuan 排在前、sale 排在后，
    呼应「fangyuan 优先 + sale 用免费池兜底」的预算分配——fangyuan 波次先消耗其 500 qg，
    再进入 sale 波次（qg 500 + 免费池）。每城每类型配额仍由 try_consume_ip 单独保障。
    """
    tasks = []
    for c in CITIES:
        tasks.append({"city": c, "type": "fangyuan", "pages": PAGES_RENT, "target": TARGET})
    for c in CITIES:
        tasks.append({"city": c, "type": "sale", "pages": PAGES_SALE, "target": TARGET})
    return tasks


DEFAULT_TASKS = _build_default_tasks()


def log(msg):
    print(f"[master:{MY_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_ip_budget():
    """生成每城每类型的青果预算表 {city: {"sale": n, "fangyuan": n}}。

    默认按 BUDGET_TOP_CITIES 区分头部/其余城；IP_BUDGET_JSON 可整体覆盖
    （形如 {"gz": {"sale": 91, "fangyuan": 67}}），解析失败仅告警并忽略。
    """
    table = {}
    for c in CITIES:
        top = c in BUDGET_TOP_CITIES
        table[c] = {
            "sale": IP_BUDGET_SALE_TOP if top else IP_BUDGET_SALE_OTHER,
            "fangyuan": IP_BUDGET_FY_TOP if top else IP_BUDGET_FY_OTHER,
        }
    if IP_BUDGET_JSON.strip():
        try:
            override = json.loads(IP_BUDGET_JSON)
            for city, item in override.items():
                if city not in table or not isinstance(item, dict):
                    continue
                for typ in ("sale", "fangyuan"):
                    if typ in item:
                        table[city][typ] = int(item[typ])
        except Exception as e:
            log(f"IP_BUDGET_JSON parse failed, ignored: {e}")
    return table


CITY_SET = set(CITIES)
IP_BUDGET = build_ip_budget()
_scope_warned = set()  # 未知 city/type 只告警一次


def _resolve_scope(city, typ):
    """归属判断：city 须在 CITIES 白名单、type 须是 sale/fangyuan，否则视为未提供
    （不计预算、不拒绝），并对每种非法组合只 log 一次。
    """
    if not city and not typ:
        return None, None
    if city in CITY_SET and typ in ("sale", "fangyuan"):
        return city, typ
    sig = f"{city}|{typ}"
    if sig not in _scope_warned:
        _scope_warned.add(sig)
        log(f"proxy request with unknown scope city='{city}' type='{typ}', budget ignored")
    return None, None


def _budget_of(city, typ):
    return int(IP_BUDGET.get(city, {}).get(typ, 0))


def _used_of(rdb, city, typ):
    try:
        return int(rdb.get(f"{IP_USED_PREFIX}{city}:{typ}") or 0)
    except Exception:
        return 0


def _budget_state(rdb, city, typ):
    """返回 (used, budget, exhausted)；预算关闭或未提供归属时为 (0, 0, False)。"""
    if not IP_BUDGET_ENABLED or not city or not typ:
        return 0, 0, False
    budget = _budget_of(city, typ)
    used = _used_of(rdb, city, typ)
    return used, budget, used >= budget


def try_consume_ip(rdb, city, typ):
    """预扣一次青果发放额度。用 Redis 原子 INCR 先扣再比较（超额 DECR 回滚），
    避免 check-then-act 竞态（maintenance 线程与 HTTP 线程并发）。

    返回 (allowed, used, budget)。预算关闭或未提供归属时恒放行且不计数。
    """
    if not IP_BUDGET_ENABLED or not city or not typ:
        return True, 0, 0
    budget = _budget_of(city, typ)
    key = f"{IP_USED_PREFIX}{city}:{typ}"
    try:
        used = int(rdb.incr(key))
    except Exception as e:
        log(f"ip budget incr error {city}:{typ}: {e}")
        return True, 0, budget
    if used > budget:
        release_ip(rdb, city, typ)
        return False, max(used - 1, 0), budget
    return True, used, budget


def release_ip(rdb, city, typ):
    """回滚一次预扣（预扣后未真正发放代理时调用）。"""
    if not IP_BUDGET_ENABLED or not city or not typ:
        return
    try:
        rdb.decr(f"{IP_USED_PREFIX}{city}:{typ}")
    except Exception as e:
        log(f"ip budget decr error {city}:{typ}: {e}")


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


def refill_qg(rdb, on_demand=False):
    """青果池补拉（独立闸门，与免费池规模无关）。

    青果：存活1分钟、配额1000个、提取即消耗 → 池中青果代理不足 QG_TARGET 时按缺口补拉
    （num=deficit，缺多少拉多少，不浪费配额）；提取侧计数 `qg_consumed` 累计，
    达 QG_BUDGET 后停止补拉（跑满配额），worker 转免费池。

    配额守恒（2026-08-05 加）：时间窗口限流 QG_REFILL_INTERVAL 秒内最多提取一次，
    on_demand 同样受限（防止 5 worker 并发把「池空即补」压成密集提取循环，
    实测曾 5 分钟烧掉 980/1000 配额）。窗口用 Redis SET NX EX 原子占位，
    并发请求只有一个能通过，其余本轮跳过（池空时 worker 转免费池兜底，慢点没关系）。

    on_demand=True（HTTP 请求路径，存在即时需求）忽略需求闸门强制补拉，
    但仍受提取限流窗口约束；维护线程补拉（on_demand=False）仅当近期有实际青果发放时
    才提取，避免 worker 空闲/掉进 free 失败循环时「提取→55s 过期」白白烧配额。
    """
    if not QG_ENABLED:
        return 0
    # 提取限流窗口：距上次提取不足 QG_REFILL_INTERVAL 秒则跳过（配额守恒优先）
    now = time.time()
    refill_key = QG_REFILL_KEY
    acquired = rdb.set(refill_key, now, nx=True, ex=QG_REFILL_INTERVAL)
    if not acquired:
        last = rdb.get(refill_key)
        log(
            f"qg refill throttled (last={float(last or 0):.0f}, "
            f"interval={QG_REFILL_INTERVAL}s), skip extraction"
        )
        return 0
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    if consumed >= QG_BUDGET:
        log(f"qg budget exhausted ({consumed}/{QG_BUDGET}), stop qg extraction")
        return 0
    if not on_demand:
        last_pop = float(rdb.get(QG_LAST_POP_KEY) or 0)
        if time.time() - last_pop > 120:
            log("no recent qg demand, skip maintenance refill to avoid quota waste")
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
    if int(rdb.get(FREE_USED_KEY) or 0) >= FREE_BUDGET:
        return 0  # 本轮免费配额已用尽，不再补池（避免白跑验证）
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


def _disable_free_pool(rdb):
    """本轮免费配额耗尽：清空免费池与 worker 代理列表，使本轮不再有免费代理可发。

    不删免费池则 worker 仍可能经 /proxies 回退列表拿到免费代理，绕开 FREE_BUDGET 上限。
    """
    try:
        rdb.delete(POOL_FREE)
        for k in rdb.scan_iter("spacefin:proxy_list:*"):
            rdb.delete(k)
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


# 调度顺序（2026-08-09 LIFO 修复 + 波次状态机）：
# 全 42 任务（21 城 × sale/fangyuan）自 bootstrap 起同时有效。
# master 采用 LPUSH 入队，worker 从队尾 RPOPLPUSH 领取（FIFO），
# 领取顺序与 DEFAULT_TASKS 顺序一致：全 21 城 fangyuan（gz→yf）优先执行，
# 随后全 21 城 sale（gz→yf）执行。
# 每城每类型配额由 try_consume_ip 单独保障（各 500，合计 QG_BUDGET=1000）。
SCHED_MODE = "city-interleave"


def _init_task_state(city, typ, wave, pages, target, **overrides):
    """统一生成任务 Hash 初始字典，杜绝多处重复定义。"""
    st = {
        "city": city,
        "type": typ,
        "pages": str(pages),
        "target": str(target),
        "round": "0",
        "status": "pending",
        "finished": "0",
        "finish_reason": "",
        "requeue_count": "0",
        "count": "0",
        "new_count": "0",
        "dup_count": "0",
        "blocked_count": "0",
        "pages_done": "0",
        "worker": "",
        "worker_hb": "0",
        "wave": str(wave),
        "ts": str(time.time()),
    }
    st.update({k: str(v) for k, v in overrides.items()})
    return st


def _is_fangyuan_free_rescue_allowed(rdb, city, typ):
    """rescue 波次且开启 FANGYUAN_FREE_RESCUE 时，允许 fangyuan 回落免费池。"""
    if typ != "fangyuan":
        return True
    if not FANGYUAN_FREE_RESCUE:
        return False
    task_wave = (rdb.hget(_task_key(city, typ), "wave") or "") if (city and typ) else ""
    return task_wave == "rescue"


def _init_all_tasks(rdb):
    """初始化全部 42 任务（21 城 × sale/fangyuan），LPUSH 入队保证 FIFO。

    DEFAULT_TASKS 顺序为 [gz_fangyuan, sz_fangyuan, ..., gz_sale, sz_sale, ...]。
    master 经 LPUSH 依次压入队首，worker 从队尾 RPOPLPUSH 领取，保证领取顺序与
    DEFAULT_TASKS 顺序完全一致（FIFO）。
    仅当任务 hash 缺失（首次创建）才入队，避免每轮 maintenance 重复入队堆积。
    """
    for t in DEFAULT_TASKS:
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            st = _init_task_state(
                t["city"],
                t["type"],
                "floor" if WAVE_ENABLED else "",
                t["pages"],
                t["target"],
            )
            rdb.hset(key, mapping=st)
            rdb.set(
                f"{IP_BUDGET_PREFIX}{t['city']}:{t['type']}",
                _budget_of(t["city"], t["type"]),
            )
            rdb.lpush(TASK_QUEUE, json.dumps(t))


def init_tasks(rdb):
    """初始化全部任务（按城交错：全 42 同时有效并入队）。"""
    _init_all_tasks(rdb)
    log(f"tasks initialized, queue len={rdb.llen(TASK_QUEUE)}")


def _ensure_task_hashes(rdb):
    """确保全部 42 任务 Hash 与预算镜像存在（不入队）。"""
    for t in DEFAULT_TASKS:
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            st = _init_task_state(
                t["city"],
                t["type"],
                "floor" if WAVE_ENABLED else "",
                WAVE_FLOOR_PAGES if WAVE_ENABLED else t["pages"],
                t["target"],
            )
            rdb.hset(key, mapping=st)
        rdb.set(
            f"{IP_BUDGET_PREFIX}{t['city']}:{t['type']}",
            _budget_of(t["city"], t["type"]),
        )


def _snapshot_wave(rdb, current_wave):
    """记录当前波次历史快照到 spacefin:crawl_wave_log（按波次名幂等写入，防崩溃重试重复累加）。"""
    for t in DEFAULT_TASKS:
        city, typ = t["city"], t["type"]
        key = _task_key(city, typ)
        if not rdb.exists(key):
            continue
        st = rdb.hgetall(key)
        snap = {
            "wave": current_wave,
            "reason": st.get("finish_reason", ""),
            "new": int(st.get("new_count", 0) or 0),
            "dup": int(st.get("dup_count", 0) or 0),
            "blocked": int(st.get("blocked_count", 0) or 0),
            "pages_done": int(st.get("pages_done", 0) or 0),
            "ip_used": _used_of(rdb, city, typ),
            "ts": time.time(),
        }
        field = f"{city}:{typ}"
        raw = rdb.hget(WAVE_LOG_KEY, field)
        try:
            history = json.loads(raw) if raw else {}
            if isinstance(history, list):
                history = {x.get("wave", "unknown"): x for x in history}
        except Exception:
            history = {}
        history[current_wave] = snap
        rdb.hset(WAVE_LOG_KEY, field, json.dumps(history))


def _get_wave_targets(rdb, wave_name):
    """根据状态推导当前波次的目标任务列表（按执行顺序）。"""
    if wave_name == "floor":
        # 领取顺序：每城先 fangyuan 后 sale，CITIES 序（gz/sz 先）
        targets = []
        for c in CITIES:
            targets.append(
                {"city": c, "type": "fangyuan", "pages": WAVE_FLOOR_PAGES, "target": TARGET}
            )
            targets.append({"city": c, "type": "sale", "pages": WAVE_FLOOR_PAGES, "target": TARGET})
        return targets
    elif wave_name == "rescue":
        # rescue: 上一波 finish_reason ∉ FLOOR_MET
        targets = []
        for c in CITIES:
            for typ in ("fangyuan", "sale"):
                key = _task_key(c, typ)
                fin_reason = (rdb.hget(key, "finish_reason") or "") if rdb.exists(key) else ""
                if fin_reason not in FLOOR_MET:
                    targets.append(
                        {"city": c, "type": typ, "pages": WAVE_FLOOR_PAGES, "target": TARGET}
                    )
        return targets
    elif wave_name == "depth":
        if not WAVE_DEPTH_ENABLED:
            return []
        # depth: ip_used < budget 且 finish_reason != "not_found"
        top_cities = [c for c in BUDGET_TOP_CITIES if c in CITY_SET]
        other_cities = [c for c in CITIES if c not in top_cities]
        ordered_cities = top_cities + other_cities
        targets = []
        for c in ordered_cities:
            for typ in ("fangyuan", "sale"):
                key = _task_key(c, typ)
                fin_reason = (rdb.hget(key, "finish_reason") or "") if rdb.exists(key) else ""
                used = _used_of(rdb, c, typ)
                budget = _budget_of(c, typ)
                if fin_reason != "not_found" and used < budget and budget > 0:
                    pages = PAGES_RENT if typ == "fangyuan" else PAGES_SALE
                    targets.append({"city": c, "type": typ, "pages": pages, "target": TARGET})
        return targets
    return []


def _open_wave(rdb, w):
    """打开指定波次：重置目标任务状态并 LPUSH 入队。全部完成后 SET wave=w, DEL wave:next。"""
    targets = _get_wave_targets(rdb, w)
    if not targets:
        rdb.set(WAVE_KEY, w)
        rdb.delete(WAVE_NEXT_KEY)
        log(f"wave '{w}' has 0 target tasks, marked as {w}")
        return

    queued_set = set()
    for q_key in (TASK_QUEUE, TASK_PROCESSING_QUEUE):
        for item in rdb.lrange(q_key, 0, -1):
            try:
                d = json.loads(item) if isinstance(item, str) else json.loads(item.decode())
                queued_set.add((d["city"], d.get("type", "sale")))
            except Exception:
                pass

    # 正序 LPUSH：targets 依次 lpush 压入队首，worker 从队尾 RPOPLPUSH 领取，
    # 弹出顺序严格与 targets 列表正序完全一致（FIFO）
    for t in targets:
        city, typ = t["city"], t["type"]
        key = _task_key(city, typ)
        wave_pages = t["pages"]
        rdb.hset(
            key,
            mapping=_init_task_state(city, typ, w, wave_pages, t["target"], requeue_count="0"),
        )
        if (city, typ) not in queued_set:
            payload = {
                "city": city,
                "type": typ,
                "pages": wave_pages,
                "target": t["target"],
                "wave": w,
            }
            rdb.lpush(TASK_QUEUE, json.dumps(payload))
            queued_set.add((city, typ))

    rdb.set(WAVE_KEY, w)
    rdb.delete(WAVE_NEXT_KEY)
    log(f"wave '{w}' opened with {len(targets)} tasks, queue len={rdb.llen(TASK_QUEUE)}")


def _wave_tick(rdb):
    """波次状态机调度逻辑（每 maintenance 周期由 leader 调用）。"""
    if not WAVE_ENABLED:
        init_tasks(rdb)
        return

    _ensure_task_hashes(rdb)

    next_wave = rdb.get(WAVE_NEXT_KEY)
    if next_wave:
        log(f"resuming interrupted wave transition to '{next_wave}'")
        _open_wave(rdb, next_wave)
        return

    wave = rdb.get(WAVE_KEY)
    if wave is None:
        _open_wave(rdb, "floor")
        return

    if wave == "done":
        return

    if not _all_tasks_done(rdb):
        return

    # 当前波次全部 42 任务已完成 -> 快照并推进后继链
    _snapshot_wave(rdb, wave)

    chain = ["floor", "rescue", "depth", "done"]
    try:
        curr_idx = chain.index(wave)
    except ValueError:
        curr_idx = 0

    nxt = "done"
    for cand in chain[curr_idx + 1 :]:
        if cand == "done":
            nxt = "done"
            break
        targets = _get_wave_targets(rdb, cand)
        if targets:
            nxt = cand
            break

    if nxt == "done":
        rdb.set(WAVE_KEY, "done")
        log("all waves completed, wave=done")
        return

    rdb.set(WAVE_NEXT_KEY, nxt)
    _open_wave(rdb, nxt)


def _all_tasks_done(rdb):
    """全部 42 任务已终态 finished=1（用于终止判定）。"""
    for t in DEFAULT_TASKS:
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            return False
        if not _is_finished(rdb, key):
            return False
    return True


def _check_termination(rdb):
    """终止判定（达成任一条件即置 STOP_KEY，worker 暂停、等待统计）：
    - 全部 42 任务 finished（波次开启且未到 done 时抑制该分支）；
    - qg 配额跑满且双池皆空（资源真正耗尽）；
    - 双池连续 EMPTY_STALL_CYCLES 轮皆空（qg 提取失败/免费源失效，避免无限空转）。
    """
    if rdb.exists(STOP_KEY):
        return
    consumed = int(rdb.get(QG_CONSUMED_KEY) or 0)
    qg_empty = rdb.hlen(POOL_QG) == 0
    free_empty = rdb.hlen(POOL_FREE) == 0
    reason = ""
    waves_active = bool(WAVE_ENABLED and rdb.get(WAVE_KEY) != "done")
    if _all_tasks_done(rdb):
        if not waves_active:
            reason = f"all 42 tasks finished (consumed={consumed})"
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
    """任务已终态：worker 在任务结束时写 finished=1（含预算耗尽即完成）。"""
    try:
        return (rdb.hget(key, "finished") or "0") == "1"
    except Exception:
        return False


def _purge_queue(rdb):
    """清理队列中已终态（finished=1）或非法（不在 42 任务表）的项，避免 worker 误领已完成任务。"""
    valid = {(t["city"], t["type"]) for t in DEFAULT_TASKS}
    removed = 0
    for item in rdb.lrange(TASK_QUEUE, 0, -1):
        try:
            d = json.loads(item) if isinstance(item, str) else json.loads(item.decode())
            c, t = d.get("city"), d.get("type", "sale")
            if (c, t) not in valid:
                rdb.lrem(TASK_QUEUE, 0, item)
                removed += 1
                continue
            if rdb.hget(_task_key(c, t), "finished") == "1":
                rdb.lrem(TASK_QUEUE, 0, item)
                removed += 1
        except Exception:
            pass
    if removed:
        log(f"purged {removed} stale/terminal queue items")
    return removed


def requeue_stale_tasks(rdb):
    """管理任务队列（按城交错：全部 42 任务同时有效）：
    - running 但锁已过期且 worker 心跳超时 → 判死：
        * requeue_count 未达 MAX_REQUEUE → 重置为 pending 重新入队（保留 hash pages）；
        * 已达 MAX_REQUEUE → 盖 finished=1 + stale_abandoned（放弃，避免无限乒乓）。
    - pending 但不在队列里 → 补入队（从任务 Hash 读取当前波次 pages，不得用全量 DEFAULT_TASKS）。
    """
    now = time.time()
    queued = set()
    for item in rdb.lrange(TASK_QUEUE, 0, -1):
        try:
            d = json.loads(item) if isinstance(item, str) else json.loads(item.decode())
            queued.add((d["city"], d.get("type", "sale")))
        except Exception:
            pass
    for t in DEFAULT_TASKS:
        key = _task_key(t["city"], t["type"])
        if not rdb.exists(key):
            continue
        state = rdb.hgetall(key)
        status = state.get("status", "pending")
        lock_key = LOCK_PREFIX + f"{t['city']}:{t['type']}"
        hash_pages = int(state.get("pages", 0) or t["pages"])
        hash_target = int(state.get("target", 0) or t["target"])
        hash_wave = state.get("wave", "")
        task_payload = {
            "city": t["city"],
            "type": t["type"],
            "pages": hash_pages,
            "target": hash_target,
        }
        if hash_wave:
            task_payload["wave"] = hash_wave

        if status == "running":
            lock_alive = rdb.exists(lock_key)
            worker_hb = float(state.get("worker_hb", 0) or 0)
            if (not lock_alive) and (now - worker_hb > WORKER_TTL):
                n = int(rdb.hincrby(key, "requeue_count", 1))
                if n <= MAX_REQUEUE:
                    log(
                        f"task {t['city']}:{t['type']} lock expired + heartbeat stale, "
                        f"requeue ({n}/{MAX_REQUEUE})"
                    )
                    rdb.hset(key, mapping={"status": "pending", "worker": "", "worker_hb": 0})
                    rdb.rpush(TASK_QUEUE, json.dumps(task_payload))
                else:
                    log(
                        f"task {t['city']}:{t['type']} requeued {n} times (max {MAX_REQUEUE}), "
                        f"abandon as stale_abandoned"
                    )
                    rdb.hset(
                        key,
                        mapping={
                            "status": "done",
                            "finished": "1",
                            "finish_reason": "stale_abandoned",
                            "worker": "",
                            "worker_hb": 0,
                        },
                    )
        elif status == "pending" and (t["city"], t["type"]) not in queued:
            rdb.rpush(TASK_QUEUE, json.dumps(task_payload))


# ---------------- run 引导与完成信号 ----------------
def _run_done_key(run_id):
    return f"spacefin:crawl_run:{run_id}:done"


def _run_done_reason_key(run_id):
    return f"spacefin:crawl_run:{run_id}:done_reason"


def _bootstrap_run(rdb):
    """新 run 引导（Airflow 每日触发）：`spacefin:crawl_run:current` 与 CRAWL_RUN_ID
    不同（含首次为空）时做一次「新 run 重置」；同一 run_id 重复启动不重置（幂等续跑）。

    必须在 maintenance 的 STOP 早退之前调用，否则上一 run 的 stop 会让新 run 永远起不来。
    保留 `spacefin:crawl_progress:*`（断点续爬）与 `spacefin:crawled_urls:*`（跨日去重）。
    """
    cur = rdb.get(RUN_CURRENT_KEY)
    if cur == CRAWL_RUN_ID:
        return False
    rdb.delete(STOP_KEY)
    rdb.delete(EMPTY_CYCLES_KEY)
    rdb.set(PHASE_KEY, "city-interleave")
    rdb.delete(QG_CONSUMED_KEY)
    rdb.delete(QG_REFILL_KEY)
    rdb.delete(FREE_USED_KEY)
    rdb.delete(TASK_QUEUE)
    rdb.delete(TASK_PROCESSING_QUEUE)
    rdb.delete(WAVE_KEY)
    rdb.delete(WAVE_NEXT_KEY)
    rdb.delete(WAVE_LOG_KEY)
    for k in rdb.keys(f"{REFUND_SEEN_PREFIX}*"):
        rdb.delete(k)
    for t in DEFAULT_TASKS:
        city, typ = t["city"], t["type"]
        key = _task_key(city, typ)
        if rdb.exists(key):
            st = _init_task_state(
                city,
                typ,
                "floor" if WAVE_ENABLED else "",
                WAVE_FLOOR_PAGES if WAVE_ENABLED else t["pages"],
                t["target"],
            )
            rdb.hset(key, mapping=st)
        rdb.delete(f"{IP_USED_PREFIX}{city}:{typ}")
        rdb.delete(f"{IP_REFUNDED_PREFIX}{city}:{typ}")
        rdb.set(f"{IP_BUDGET_PREFIX}{city}:{typ}", _budget_of(city, typ))
    rdb.set(RUN_CURRENT_KEY, CRAWL_RUN_ID)
    log(
        f"[run] bootstrap new run {CRAWL_RUN_ID}, reset {len(DEFAULT_TASKS)} tasks "
        f"(prev={cur or 'none'})"
    )
    return True


def crawl_status(rdb):
    """本 run 采集完成状态（Airflow Sensor 依据）。all_done 首次为真时幂等写完成信号。

    run_ready 门控：spacefin:crawl_run:current == CRAWL_RUN_ID，防止跨 run 误读。
    """
    run_ready = rdb.get(RUN_CURRENT_KEY) == CRAWL_RUN_ID
    stop = rdb.get(STOP_KEY)
    wave = rdb.get(WAVE_KEY)
    waves_active = bool(WAVE_ENABLED and wave != "done")
    cities = []
    finished_tasks = 0
    finished_by_type = {"sale": 0, "fangyuan": 0}
    rows_new = 0
    rows_dup = 0
    total_refunded = 0

    snapshot_rows_by_city = {}
    if rdb.exists(WAVE_LOG_KEY):
        for field, raw in rdb.hgetall(WAVE_LOG_KEY).items():
            field_str = field.decode() if isinstance(field, bytes) else field
            raw_str = raw.decode() if isinstance(raw, bytes) else raw
            try:
                history = json.loads(raw_str)
                if isinstance(history, dict):
                    entries = history.values()
                elif isinstance(history, list):
                    entries = history
                else:
                    entries = []
                snap_new = sum(int(x.get("new", 0) or 0) for x in entries)
                snap_dup = sum(int(x.get("dup", 0) or 0) for x in entries)
                snapshot_rows_by_city[field_str] = {"new": snap_new, "dup": snap_dup}
            except Exception:
                pass

    for t in DEFAULT_TASKS:
        city, typ = t["city"], t["type"]
        key = _task_key(city, typ)
        st = rdb.hgetall(key) if rdb.exists(key) else {}
        fin = st.get("finished", "0") == "1"
        if fin:
            finished_tasks += 1
            finished_by_type[typ] = finished_by_type.get(typ, 0) + 1
        new_c = int(st.get("new_count", 0) or 0)
        dup_c = int(st.get("dup_count", 0) or 0)

        field = f"{city}:{typ}"
        snap_data = snapshot_rows_by_city.get(field, {"new": 0, "dup": 0})
        task_total_new = new_c + snap_data["new"]
        task_total_dup = dup_c + snap_data["dup"]

        rows_new += task_total_new
        rows_dup += task_total_dup

        refunded_c = int(rdb.get(f"{IP_REFUNDED_PREFIX}{city}:{typ}") or 0)
        total_refunded += refunded_c

        cities.append(
            {
                "city": city,
                "type": typ,
                "budget": _budget_of(city, typ),
                "used": _used_of(rdb, city, typ),
                "finished": fin,
                "reason": st.get("finish_reason", "") or None,
                "rows": task_total_new,
                "wave": st.get("wave", wave or ""),
            }
        )
    total_tasks = len(DEFAULT_TASKS)
    all_finished = finished_tasks >= total_tasks
    all_done = run_ready and (bool(stop) or (not waves_active and all_finished))
    done_reason = None
    if all_done:
        if not waves_active and all_finished:
            done_reason = "all_finished"
        elif stop:
            done_reason = f"stop:{stop}"
        else:
            done_reason = "all_finished" if all_finished else "unknown"
        done_key = _run_done_key(CRAWL_RUN_ID)
        if rdb.set(done_key, time.strftime("%Y-%m-%dT%H:%M:%S"), nx=True):
            rdb.set(_run_done_reason_key(CRAWL_RUN_ID), done_reason)
            log(f"[run] {CRAWL_RUN_ID} done: {done_reason}")
        done_reason = rdb.get(_run_done_reason_key(CRAWL_RUN_ID)) or done_reason
    return {
        "run_id": CRAWL_RUN_ID,
        "all_done": all_done,
        "done_reason": done_reason,
        "phase": SCHED_MODE,
        "wave": wave or ("floor" if WAVE_ENABLED else "legacy"),
        "waves_active": waves_active,
        "stop": stop,
        "total_tasks": total_tasks,
        "finished_tasks": finished_tasks,
        "finished_by_type": finished_by_type,
        "qg_consumed": int(rdb.get(QG_CONSUMED_KEY) or 0),
        "refunded": total_refunded,
        "rows": {"total": rows_new + rows_dup, "new": rows_new, "dup": rows_dup},
        "cities": cities,
    }


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
                # 必须先于 STOP 早退：新 run 引导会清掉上一 run 的 stop，
                # 否则上一 run 的终止信号会让新 run 永远起不来。
                _bootstrap_run(rdb)
                # 已终止：保持心跳/API，跳过调度与补池（统计数据阶段）
                if rdb.exists(STOP_KEY):
                    time.sleep(REFRESH_INTERVAL)
                    continue
                _wave_tick(rdb)
                requeue_stale_tasks(rdb)
                # 按城交错：全 42 任务同时有效，无需阶段切换
                _purge_queue(rdb)
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

    def _proxy_json(self, proxy, source, city, typ, used, budget, exhausted, error=None):
        """代理发放统一响应（恒 HTTP 200，字段见契约 §3.3）。"""
        obj = {
            "proxy": proxy,
            "source": source,
            "budget_exhausted": bool(exhausted),
            "city": city,
            "type": typ,
            "used": used,
            "budget": budget,
        }
        if error:
            obj["error"] = error
        self._json(obj)

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
                    "finished": st.get("finished", "0"),
                    "finish_reason": st.get("finish_reason", ""),
                    "requeue_count": st.get("requeue_count", "0"),
                    "status": st.get("status", "?"),
                    "count": st.get("count", 0),
                    "new": new_c,
                    "dup": dup_c,
                    "pages_done": st.get("pages_done", 0),
                    "source": st.get("source", ""),
                    "worker": st.get("worker", ""),
                    "wave": st.get("wave", ""),
                }
            self._json(
                {
                    "tasks": out,
                    "queue_len": rdb.llen(TASK_QUEUE),
                    "phase": SCHED_MODE,
                    "wave": rdb.get(WAVE_KEY),
                    "stop": rdb.get(STOP_KEY),
                    "qg_consumed": int(rdb.get(QG_CONSUMED_KEY) or 0),
                    "qg_budget": QG_BUDGET,
                    "qg_sale_budget": QG_SALE_BUDGET,
                    "total_request": total_req,
                    "total_new": total_new,
                    "total_dup": total_dup,
                }
            )
        elif path == "/crawl_status":
            self._json(crawl_status(rdb))
        elif path == "/proxy/report":
            city, typ = _resolve_scope(query.get("city", ""), query.get("type", ""))
            src = query.get("src", "")
            attempt = query.get("attempt", "")
            used, budget, _ = _budget_state(rdb, city, typ)
            if not attempt:
                self._json(
                    {
                        "refunded": False,
                        "used": used,
                        "budget": budget,
                        "error": "attempt is required",
                    }
                )
                return
            if src != "qg" or not city or not typ:
                self._json({"refunded": False, "used": used, "budget": budget})
                return
            seen_key = f"{REFUND_SEEN_PREFIX}{attempt}"
            if not rdb.set(seen_key, "1", nx=True, ex=7200):
                self._json({"refunded": False, "used": used, "budget": budget})
                return
            lua = """
            local v = redis.call('GET', KEYS[1])
            if v and tonumber(v) > 0 then return redis.call('DECR', KEYS[1]) end
            return tonumber(v) or 0
            """
            try:
                new_used = int(rdb.eval(lua, 1, f"{IP_USED_PREFIX}{city}:{typ}") or 0)
            except Exception as e:
                log(f"report eval error: {e}")
                new_used = used
            try:
                rdb.incr(f"{IP_REFUNDED_PREFIX}{city}:{typ}")
            except Exception:
                pass
            self._json({"refunded": True, "used": new_used, "budget": budget})
        elif path == "/proxy/qg":
            city, typ = _resolve_scope(query.get("city", ""), query.get("type", ""))
            # 按城交错调度（2026-08-07）：不再有全局 sale 配额闸门。
            # 每城每类型配额由 try_consume_ip 单独保障（sale 500 / fangyuan 500，合计 1000）。
            allowed, used, budget = try_consume_ip(rdb, city, typ)
            if not allowed:
                self._proxy_json(None, None, city, typ, used, budget, True, "ip budget exhausted")
                return
            p = pop_proxy(rdb, POOL_QG)
            if not p:
                # 按需补拉：worker 正在请求就是需求，当场提取（受 QG_BUDGET 上限）
                refill_qg(rdb, on_demand=True)
                p = pop_proxy(rdb, POOL_QG)
            if not p:
                release_ip(rdb, city, typ)  # 未真正发放，回滚预扣
                used, budget, exhausted = _budget_state(rdb, city, typ)
                self._proxy_json(None, None, city, typ, used, budget, exhausted, "qg pool empty")
                return
            rdb.set(QG_LAST_POP_KEY, time.time())
            _remove_from_worker_lists(rdb, p)
            self._proxy_json(p, "qg", city, typ, used, budget, budget > 0 and used >= budget)
        elif path == "/proxy/free":
            # 免费池=碰运气兜底：本轮发放总量受 FREE_BUDGET 限制，到限后本轮不再发免费代理。
            # fangyuan 仅用青果（qg only），免费池默认不对其发放（rescue 波次开启 FANGYUAN_FREE_RESCUE 时除外）。
            city, typ = _resolve_scope(query.get("city", ""), query.get("type", ""))
            if typ == "fangyuan" and not _is_fangyuan_free_rescue_allowed(rdb, city, typ):
                used, budget, exhausted = _budget_state(rdb, city, typ)
                self._proxy_json(
                    None,
                    None,
                    city,
                    typ,
                    used,
                    budget,
                    exhausted,
                    "free pool not available for fangyuan (qg only)",
                )
                return
            p = pop_proxy(rdb, POOL_FREE)
            used, budget, exhausted = _budget_state(rdb, city, typ)
            if p:
                n = rdb.incr(FREE_USED_KEY)
                if n > FREE_BUDGET:
                    rdb.decr(FREE_USED_KEY)
                    _disable_free_pool(rdb)  # 清空免费池与 worker 列表，本轮不再用免费池
                    self._proxy_json(
                        None, None, city, typ, used, budget, exhausted, "free budget exhausted"
                    )
                    return
                _remove_from_worker_lists(rdb, p)
                self._proxy_json(p, "free", city, typ, used, budget, exhausted)
                return
            self._proxy_json(None, None, city, typ, used, budget, exhausted, "free pool empty")
        elif path == "/proxy/random":
            # 青果优先（受预算约束），青果不可用（池空或预算耗尽）则回落免费池；
            # fangyuan 为 qg only，默认不允许回落免费池（rescue 波次开启 FANGYUAN_FREE_RESCUE 时除外）。
            city, typ = _resolve_scope(query.get("city", ""), query.get("type", ""))
            allowed, used, budget = try_consume_ip(rdb, city, typ)
            if allowed:
                p = pop_proxy(rdb, POOL_QG)
                if not p:
                    refill_qg(rdb, on_demand=True)  # 按需补拉，同 /proxy/qg
                    p = pop_proxy(rdb, POOL_QG)
                if p:
                    rdb.set(QG_LAST_POP_KEY, time.time())
                    _remove_from_worker_lists(rdb, p)
                    self._proxy_json(
                        p, "qg", city, typ, used, budget, budget > 0 and used >= budget
                    )
                    return
                release_ip(rdb, city, typ)  # 未真正发放，回滚预扣
            if typ == "fangyuan" and not _is_fangyuan_free_rescue_allowed(rdb, city, typ):
                used2, budget2, exhausted2 = _budget_state(rdb, city, typ)
                self._proxy_json(
                    None,
                    None,
                    city,
                    typ,
                    used2,
                    budget2,
                    exhausted2,
                    "qg pool empty for fangyuan (qg only, no free fallback)",
                )
                return
            p = pop_proxy(rdb, POOL_FREE)
            used, budget, exhausted = _budget_state(rdb, city, typ)
            if p:
                n = rdb.incr(FREE_USED_KEY)
                if n > FREE_BUDGET:
                    rdb.decr(FREE_USED_KEY)
                    _disable_free_pool(rdb)  # 本轮免费配额耗尽
                    self._proxy_json(
                        None, None, city, typ, used, budget, exhausted, "free budget exhausted"
                    )
                    return
                _remove_from_worker_lists(rdb, p)
                self._proxy_json(p, "free", city, typ, used, budget, exhausted)
                return
            self._proxy_json(None, None, city, typ, used, budget, exhausted, "both pools empty")
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
