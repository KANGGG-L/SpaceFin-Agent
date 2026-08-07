#!/usr/bin/env python3
"""
worker 容器（泛化版）：不绑定城市/类型，由 master 分配任务和资源。

流程：
1. 启动时注册到 Redis workers 集合（稳定 WORKER_ID，供 master 同步 proxy list）；
   后台心跳线程每 15s 写 `spacefin:worker_hb:{id}`（EX 120 自动过期）。
2. 从任务队列 `spacefin:tasks` LPOP 领取任务 {city, type, pages, target, round}；
   抢任务锁 `spacefin:task_lock:{city}:{type}`（SET NX EX 120），防同城双跑。
3. 抓取：每页从 master /proxy/qg 或 /proxy/free 取代理（弹出式分配），
   curl_cffi 伪装抓取；代理复用（一个代理连抓多页直到被拦才换）。
4. 轻量解析（跳过 geocode，ETL 后置）：sale→numeric schema，fangyuan→rent schema；
   按 URL set 去重（跨轮权威），每页写入 raw JSONL（含重复）+ stats JSONL（每页一行）。
5. 完成判定：空页早停（连续 EMPTY_LIMIT 页无卡片）、达 pages 上限、达 target、站点 404、
   该城 qg 预算耗尽、长期取不到代理，先到先完成；终态统一经 finish_task 写
   finished=1 + finish_reason（Airflow 依据 master /crawl_status 判断本 run 是否跑完）。
6. 增量续爬：任务开始时读 `spacefin:crawl_progress:{city}:{type}`，先回扫头部 p1..HEAD_REWIND
   （新增/置顶房源在列表头部），再从断点续爬深部页，页内幂等靠 URL set；每城每 run 只跑一轮。

泛化：worker 无城市/类型概念，可横向扩展，master 负责派单。

用法：
    python worker.py [--out-dir /output]
环境变量：
    MASTER_URL   master 地址，默认 http://master:5100
    REDIS_HOST   默认 spacefin-redis
    WORKER_ID    稳定 ID（compose 注入 worker-N），否则用 HOSTNAME
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from urllib.parse import quote, urlencode

import redis
from curl_cffi import requests as creq

MASTER_URL = os.getenv("MASTER_URL", "http://master:5100")
REDIS_HOST = os.getenv("REDIS_HOST", "spacefin-redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
WORKERS_KEY = "spacefin:workers"
TASK_QUEUE = "spacefin:tasks"
TASK_PREFIX = "spacefin:task:"
LOCK_PREFIX = "spacefin:task_lock:"
PROGRESS_PREFIX = "spacefin:crawl_progress:"
URLSET_PREFIX = "spacefin:crawled_urls:"
WORKER_HB_PREFIX = "spacefin:worker_hb:"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 5))
EMPTY_LIMIT = int(os.getenv("EMPTY_LIMIT", 3))
WORKER_HB_INTERVAL = int(os.getenv("WORKER_HB_INTERVAL", 15))
WORKER_HB_TTL = int(os.getenv("WORKER_HB_TTL", 120))
LOCK_TTL = int(os.getenv("LOCK_TTL", 120))
# 抓取失败退避与预算：连续失败达 FAIL_BUDGET 即中止当前任务并回队重试，
# 防止宿主渲染服务挂掉/代理池整体失效时在同一任务上死循环占死 worker。
FAIL_BACKOFF = int(os.getenv("FAIL_BACKOFF", 5))  # 每次 fetch 失败后的退避秒数
FAIL_BUDGET = int(os.getenv("FAIL_BUDGET", 5))  # 连续失败上限（约 FAIL_BACKOFF*FAIL_BUDGET=25s）
# fangyuan 单独放宽：安居客对 zu 列表页按出口 IP 随机弹验证码，实测通过率约 1/3，
# 单页平均需换 3 个 IP 才成功。沿用 sale 的 5 次预算会把正常的反爬波动误判成
# 「代理池整体失效」而反复中止任务；退避也无需 5s（IP 已换，不是同 IP 限流）。
FAIL_BACKOFF_RENDER = int(os.getenv("FAIL_BACKOFF_RENDER", 1))
FAIL_BUDGET_RENDER = int(os.getenv("FAIL_BUDGET_RENDER", 15))
# 真实安居客页面但 0 卡片时，同页换 IP 重试的次数。站点 404 能直接判「到底了」，
# 但「页码没越界、本城确实没房源」与「出口 IP 被降级返回无结果页」两者无法从
# HTML 上区分，只能靠换 IP 复核：连续 N 个不同 IP 都 0 卡片才认定真空页。
ZERO_CARD_RETRY = int(os.getenv("ZERO_CARD_RETRY", 3))
# 断点续爬：从上次完成页向前回扫 HEAD_REWIND 页重扫（抓新插入/置顶房源），
# 页内幂等靠 crawled_urls set。RESUME_ENABLED=0 则每次从第 1 页开始。
HEAD_REWIND = int(os.getenv("HEAD_REWIND", 2))
RESUME_ENABLED = os.getenv("RESUME_ENABLED", "1") != "0"
# 连续多少个「取不到代理」周期后放弃该任务：防止代理池整体枯竭时 worker 无限 pause，
# 任务永远不写终态、Airflow Sensor 永远等不到本 run 完成。
NO_PROXY_MAX_CYCLES = int(os.getenv("NO_PROXY_MAX_CYCLES", 5))
# 单个任务在一次 run 内允许被重新入队的次数上限（收敛保证，与 master 侧同名同默认）。
# 连续失败超 FAIL_BUDGET 中止时 HINCRBY requeue_count；超限即写终态 fail_budget 不再
# 入队——否则任务在 pending/running/中止间无限乒乓，/crawl_status.all_done 永远凑不齐
# 42 个 finished，Airflow Sensor 会烧完 6h 超时、ETL 永不执行。计数存在 task Hash、
# 不随队列 JSON 传递（队列元素仍只含 {city,type,pages,target,round}）。
MAX_REQUEUE = int(os.getenv("MAX_REQUEUE", 3))
# 青果短效代理认证（可选，经 .env 注入；代理池中青果代理需带认证使用）
QG_USER = os.getenv("QG_USER", "")
QG_PWD = os.getenv("QG_PWD", "")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CITY_SUBDOMAIN = {
    "gz": "guangzhou",
    "sz": "shenzhen",
    "zh": "zhuhai",
    "st": "shantou",
    "fs": "foshan",
    "sg": "shaoguan",
    "zj": "zhanjiang",
    "zq": "zhaoqing",
    "jm": "jiangmen",
    "mm": "maoming",
    "hui": "huizhou",
    "mz": "meizhou",
    "sw": "shanwei",
    "hy": "heyuan",
    "yj": "yangjiang",
    "qy": "qingyuan",
    "dg": "dongguan",
    "zs": "zhongshan",
    "cz": "chaozhou",
    "jy": "jieyang",
    "yf": "yunfu",
}

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MY_ID = os.getenv("WORKER_ID") or os.getenv("HOSTNAME") or "worker-unknown"


def log(city, msg):
    print(f"[worker:{MY_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- 注册与心跳 ----------------
def register(rdb):
    rdb.sadd(WORKERS_KEY, MY_ID)
    rdb.set(WORKER_HB_PREFIX + MY_ID, time.time(), ex=WORKER_HB_TTL)
    log("-", "registered to master")


def _hb_loop(rdb):
    while True:
        try:
            rdb.set(WORKER_HB_PREFIX + MY_ID, time.time(), ex=WORKER_HB_TTL)
        except Exception:
            pass
        time.sleep(WORKER_HB_INTERVAL)


# ---------------- 代理获取 ----------------
def get_proxy_from_master(source=None, city=None, typ=None):
    """从 master 分配代理；source 可选 'qg'/'free'，默认随机（master 内部青果优先）。

    带 city/type 时 master 按该城该类型的 qg 预算计数/拒绝（缺省则不计预算，向后兼容）。
    master 恒返回 HTTP 200：池空或预算耗尽时 proxy=null 且带 error/budget_exhausted。
    返回 (proxy, source, budget_exhausted)——budget_exhausted 必须透传给调用方，
    否则无法区分「暂时没代理」与「该城青果预算已用尽」，任务就无法优雅收尾。
    """
    path = "/proxy/random" if source is None else f"/proxy/{source}"
    params = {}
    if city:
        params["city"] = city
    if typ:
        params["type"] = typ
    url = f"{MASTER_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = json.load(r)
            proxy = data.get("proxy")
            src = data.get("source") or (source or "")
            return proxy, (src if proxy else ""), bool(data.get("budget_exhausted"))
    except Exception:
        return None, "", False


def get_proxy_from_redis(rdb):
    key = f"spacefin:proxy_list:{MY_ID}"
    try:
        p = rdb.spop(key)
        return p.decode() if isinstance(p, bytes) else p
    except Exception:
        return None


def get_proxy(rdb, city=None, typ=None):
    """池优先级：青果池 → 免费池 → Redis proxy list，返回 (proxy, source, budget_exhausted)。"""
    p, src, exhausted = get_proxy_from_master("qg", city, typ)
    if p:
        return p, src or "qg", exhausted
    p, src, ex_free = get_proxy_from_master("free", city, typ)
    exhausted = exhausted or ex_free
    if p:
        return p, src or "free", exhausted
    p = get_proxy_from_redis(rdb)
    return p, ("redis" if p else ""), exhausted


def get_proxy_with_source(rdb, city=None, typ=None):
    """取代理并返回 (proxy, source, budget_exhausted)：/proxy/random 青果优先、免费兜底。

    fangyuan 渲染需要知道 source，以决定是否向渲染服务传 auth=1（青果需注入
    Basic 认证，免费代理不需要）；budget_exhausted 用于预算耗尽时优雅收尾。
    """
    return get_proxy_from_master(None, city, typ)


# fangyuan 渲染：调宿主机渲染服务（宿主 Chrome 150 已验证能拿 zu-itemmod；
# 容器内 Chrome 151 会被站点反爬按指纹软拦截、只回空心壳）
_RENDER_TIMEOUT = int(os.getenv("RENDER_TIMEOUT", 90))
HOST_RENDER_URL = os.getenv("HOST_RENDER_URL", "http://host.docker.internal:8899/render")


def _render_with_timeout(city, page, proxy=None, auth=0, timeout=_RENDER_TIMEOUT):
    """调用宿主机渲染服务获取 fangyuan 页 HTML。

    proxy 提供时渲染服务经本地转发代理走青果/免费池（真实消耗青果配额）；
    auth=1 表示青果代理（转发时注入 Basic 认证），auth=0 为免费代理。
    超时/异常返回 None，调用方走 html is None 分支（记 failed/blocked 后 continue），不会崩。
    """
    url = f"{HOST_RENDER_URL}?city={city}&page={page}"
    if proxy:
        url += f"&proxy={quote(proxy, safe='')}&auth={auth}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        log(city, f"p{page} fangyuan render error: {repr(e)[:200]}")
        return None


def _is_blocked_html(html):
    """渲染结果是否「不是安居客的真实页面」——是则判失败、换 IP 重试同一页。

    实测 fangyuan 渲染回来的响应有五类，只有 A/E 是安居客真的给的页面：
      A 正常列表页  460K-745K，含 zu-itemmod 卡片
      B 验证码页    6-10K，title「请输入验证码 ws:<出口IP>」（按出口 IP 随机弹）
      C Chrome 内部错误页——**根本不是安居客的页面**：免费代理接了 CONNECT 却不回
        数据（ERR_EMPTY_RESPONSE，185K）或经 MITM 设备证书不受信（TLS 拦截页，134K）。
        DrissionPage 的 page.get() 失败时只返回 False 不抛异常，渲染服务照样把
        Chrome 自己生成的错误页当 200 回给 worker，于是伪装成「成功但 0 卡片」。
      E 站点 404    40K，title「404-安居客」，页码超出该城真实页深
    判别用正向标记 anjukestatic（安居客静态资源域）：A/E 必有，B/C 全无。
    不用关键词黑名单，因为 C 类的特征串位置很分散（interstitial-wrapper@5369、
    main-frame-error@12230），截 head 匹配会漏。
    这类响应若不识别，就会被当成「真空页」计入 empty_pages 触发 EMPTY_LIMIT 早停，
    把反爬/代理故障误判成「这城抓完了」。
    """
    if not html:
        return True
    return not ("zu-itemmod" in html or "anjukestatic" in html)


def _is_page_not_found(html):
    """站点 404：页码已超出该城真实页深，本轮到此为止（区别于反爬导致的空页）。"""
    return "404-安居客" in html or "抱歉,您要查看的页面丢失了" in html


def fetch_page(city, typ, page, proxy_str, proxy_src=""):
    """抓取一页。sale 用 curl_cffi；fangyuan 用宿主渲染（zu.anjuke.com 子域，走转发代理）。"""
    if typ == "fangyuan":
        # 渲染路径：宿主 Chrome 渲染列表页，经本地转发代理真实走青果/免费代理；
        # 子进程超时包裹，防 Chrome 异常时静默挂死
        html = _render_with_timeout(city, page, proxy_str, auth=1 if proxy_src == "qg" else 0)
        if _is_blocked_html(html):
            return None
        return html
    sub = CITY_SUBDOMAIN[city]
    home = f"https://{sub}.anjuke.com/"
    listing = f"https://{sub}.anjuke.com/sale/p{page}/"
    # 青果代理带 Basic 认证；普通代理直接 ip:port
    proxy_url = f"http://{QG_USER}:{QG_PWD}@{proxy_str}" if QG_USER else f"http://{proxy_str}"
    proxies = {"http": proxy_url, "https": proxy_url}
    s = creq.Session(impersonate="chrome")
    try:
        s.get(
            home,
            headers={"Referer": "https://www.baidu.com/", "User-Agent": UA},
            timeout=15,
            proxies=proxies,
        )
        r = s.get(
            listing,
            headers={"Referer": home, "User-Agent": UA},
            timeout=20,
            proxies=proxies,
            allow_redirects=True,
        )
        low = r.url.lower()
        if (
            "deny.do" in low
            or "antibot" in low
            or "captcha" in low
            or "xxzlGatewayUrl" in r.text[:2000]
        ):
            return None
        return r.text
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------- 轻量解析（跳过 geocode，ETL 后置） ----------------
def parse_rows(html, city, typ):
    if typ == "sale":
        from anjuke_crawler.parse import NOOP_GEOCODER, parse_numeric_schema_housing

        return parse_numeric_schema_housing(html, district_tag=city, geocoder=NOOP_GEOCODER)
    from anjuke_crawler.parse import NOOP_GEOCODER, parse_rent_schema_housing

    return parse_rent_schema_housing(html, district_tag=city, geocoder=NOOP_GEOCODER)


def _has_cards(html):
    """该页是否含房源卡片（区分真空页 vs 反爬空页）。sale 用 property，fangyuan 用 zu-itemmod。"""
    try:
        return 'class="property' in html or "property-content-title" in html or "zu-itemmod" in html
    except Exception:
        return False


# ---------------- 落盘 ----------------
def _append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------- 任务认领 ----------------
def _task_key(city, typ):
    return f"{TASK_PREFIX}{city}:{typ}"


def claim_task(rdb):
    """LPOP 队列任务；抢任务锁成功才返回（防同城双跑）。"""
    while True:
        raw = rdb.lpop(TASK_QUEUE)
        if raw is None:
            return None
        task = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        city, typ = task["city"], task.get("type", "sale")
        key = _task_key(city, typ)
        if not rdb.exists(key):
            continue
        # 抢任务锁：SET NX EX，成功才认领
        lock_key = LOCK_PREFIX + f"{city}:{typ}"
        if not rdb.set(lock_key, MY_ID, nx=True, ex=LOCK_TTL):
            continue  # 已被其他 worker 持有
        # 标记 running（保留原 round）
        rdb.hset(key, mapping={"status": "running", "worker": MY_ID, "worker_hb": time.time()})
        task["round"] = int(rdb.hget(key, "round") or 0)
        return task


def release_lock(rdb, city, typ):
    """完成/异常时释放任务锁（仅当是自己持有）。"""
    lock_key = LOCK_PREFIX + f"{city}:{typ}"
    try:
        cur = rdb.get(lock_key)
        if cur == MY_ID:
            rdb.delete(lock_key)
    except Exception:
        pass


def requeue_task(rdb, city, typ, pages, target, round_n):
    """中止任务时把任务放回队列（不丢弃），供后续 worker 重试。

    状态置回 pending + 清 worker，避免 master 按 "running + 锁过期" 逻辑重复入队；
    redis 写失败也不致命——任务仍留在 hash 里，master 的 requeue_stale_tasks 会兜底补回。
    """
    task = json.dumps(
        {"city": city, "type": typ, "pages": pages, "target": target, "round": round_n}
    )
    try:
        rdb.hset(
            _task_key(city, typ),
            mapping={
                "status": "pending",
                "worker": "",
                "worker_hb": 0,
            },
        )
        rdb.rpush(TASK_QUEUE, task)
        log(city, f"[{typ}] requeued to {TASK_QUEUE} (round {round_n})")
    except Exception as e:  # noqa: BLE001
        log(city, f"[{typ}] requeue error: {e} (master will re-add from state)")


def finish_task(rdb, city, typ, reason, new_total, dup_total, blocked_total, pages_done):
    """任务终态统一出口：写 status=done + finished=1 + finish_reason，释放任务锁。

    所有终态（pages_exhausted / empty_pages / target_reached / not_found /
    budget_exhausted / no_proxy）都经此函数，master 据 finished 判定本 run 是否跑完；
    中止重排路径（fail budget 超限 → requeue_task）**不写 finished**，保持可被重排。
    """
    key = _task_key(city, typ)
    try:
        cur_round = int(rdb.hget(key, "round") or 0)
        rdb.hset(
            key,
            mapping={
                "status": "done",
                "round": cur_round + 1,
                "finished": "1",
                "finish_reason": reason,
                "count": new_total,
                "new_count": new_total,
                "dup_count": dup_total,
                "blocked_count": blocked_total,
                "pages_done": pages_done,
                "worker": MY_ID,
                "worker_hb": time.time(),
            },
        )
    except Exception as e:  # noqa: BLE001
        log(city, f"[{typ}] finish_task redis error: {repr(e)[:200]}")
    release_lock(rdb, city, typ)
    log(
        city,
        f"[{typ}] DONE reason={reason}: new={new_total} dup={dup_total} "
        f"blocked={blocked_total} pages={pages_done}",
    )


# ---------------- 抓取主循环 ----------------
def crawl(rdb, city, typ, pages, target, round_n, out_dir):
    """抓取一个任务（每 run 每城一轮）。代理复用：一个代理连抓多页直到被拦才换。"""
    from anjuke_crawler.parse import save_numeric_schema_csv  # noqa: F401 (etl 用)

    raw_path = os.path.join(out_dir, "raw", f"{city}_{typ}_{MY_ID}.jsonl")
    stats_path = os.path.join(out_dir, "stats", f"requests_{city}_{typ}.jsonl")

    key = _task_key(city, typ)
    urls_key = URLSET_PREFIX + f"{city}:{typ}"
    progress_key = PROGRESS_PREFIX + f"{city}:{typ}"
    # 增量续爬 = 头部回扫 + 断点续深：新房源出现在列表**头部**，所以每 run 先重扫
    # p1..HEAD_REWIND 抓置顶/新增，再从断点 ckpt+1 往深处翻；页内幂等靠 crawled_urls set。
    # 断点已到底（ckpt >= pages）时深部为空，但头部回扫仍要跑——否则跑到底的城市
    # 此后每个 run 都零工作量，all_done 秒真、ETL 空转。
    page_limit = pages
    ckpt = 0
    if RESUME_ENABLED:
        try:
            ckpt = int(rdb.get(progress_key) or 0)
        except Exception:
            ckpt = 0
    if ckpt > 0:
        head_end = min(HEAD_REWIND, page_limit)
        plan = list(range(1, head_end + 1)) + list(
            range(max(ckpt + 1, head_end + 1), page_limit + 1)
        )
        log(
            city,
            f"[{typ}] resume ckpt p{ckpt}: head p1-p{head_end} + "
            f"deep {len(plan) - head_end} pages (limit p{page_limit})",
        )
    else:
        plan = list(range(1, page_limit + 1))
    plan_idx = 0
    pages_done = 0
    # 头部回扫不得把断点写回小页号，否则下个 run 会从头全量重爬（浪费 IP 配额）
    progress_max = ckpt
    fail_budget = FAIL_BUDGET_RENDER if typ == "fangyuan" else FAIL_BUDGET
    fail_backoff = FAIL_BACKOFF_RENDER if typ == "fangyuan" else FAIL_BACKOFF
    proxy_str = None
    proxy_src = ""
    consecutive_fail = 0
    empty_pages = 0
    zero_card_retry = 0
    new_total, dup_total = 0, 0
    blocked_total = 0
    # 本任务是否见过 master 回报的 budget_exhausted（该城该类型青果预算已用尽）
    budget_exhausted = False
    no_proxy_cycles = 0
    finish_reason = None

    while plan_idx < len(plan) and new_total < target:
        page = plan[plan_idx]
        # 每轮续期任务锁 + 心跳（防止被 master 判定 stale）
        rdb.expire(LOCK_PREFIX + f"{city}:{typ}", LOCK_TTL)

        # 无可用代理：取一个（fangyuan 也走代理——渲染经本地转发代理真实消耗青果配额）
        if proxy_str is None:
            proxy_src = ""
            for _ in range(6):
                if typ == "fangyuan":
                    proxy_str, proxy_src, ex = get_proxy_with_source(rdb, city, typ)
                else:
                    proxy_str, proxy_src, ex = get_proxy(rdb, city, typ)
                budget_exhausted = budget_exhausted or ex
                if proxy_str:
                    break
                log(city, "no proxy, wait 15s")
                time.sleep(15)
            if not proxy_str:
                # 拿不到代理 → 有限次容错后写终态（不再无限 pause，否则任务永不写
                # 终态、Airflow Sensor 等不到本 run 完成）。budget_exhausted 只决定
                # 终态原因、不缩短容错：qg 预算耗尽是 run 中期常态，此后 /proxy/free
                # 仍如实回显 budget_exhausted=true，免费池一次空窗不应把该城腰斩。
                no_proxy_cycles += 1
                reason = "budget_exhausted" if budget_exhausted else "no_proxy"
                log(
                    city,
                    f"[{typ}] no proxy ({reason}), pause {no_proxy_cycles}/{NO_PROXY_MAX_CYCLES}",
                )
                if no_proxy_cycles >= NO_PROXY_MAX_CYCLES:
                    finish_reason = reason
                    break
                time.sleep(20)
                continue
            no_proxy_cycles = 0
            log(city, f"using new proxy {proxy_str} ({proxy_src})")
            # 注意：这里不重置 consecutive_fail，让连续失败预算跨代理累积，
            # 否则代理池整体失效时 worker 会无限换代理而永不中止。

        html = fetch_page(city, typ, page, proxy_str, proxy_src)
        if html is None:
            consecutive_fail += 1
            blocked_total += 1
            # 失败页也写 stats：否则被反爬吃掉的请求在统计里完全不可见，
            # 事后无法区分「这城没数据」和「这城全被拦了」。
            _append_jsonl(
                stats_path,
                {
                    "city": city,
                    "type": typ,
                    "page": page,
                    "proxy": proxy_str,
                    "proxy_src": proxy_src,
                    "new_count": 0,
                    "dup_count": 0,
                    "status": "blocked",
                    "ts": time.time(),
                },
            )
            log(
                city,
                f"p{page} proxy={proxy_str} failed/blocked (fail#{consecutive_fail}), switch proxy",
            )
            time.sleep(fail_backoff)  # 退避：宿主渲染服务挂掉时防止同页死循环
            if consecutive_fail >= fail_budget:
                # 连续失败超预算 → 中止当前任务。先 HINCRBY requeue_count（存在 task
                # Hash，不进队列 JSON）；n <= MAX_REQUEUE 照旧回队重试（不写 finished，
                # 保持可被重排）；n > MAX_REQUEUE 走终态 fail_budget，保证收敛。
                log(
                    city,
                    f"[{typ}] {consecutive_fail} consecutive fetch failures "
                    f">= FAIL_BUDGET({fail_budget}), abort task & requeue",
                )
                try:
                    n = int(rdb.hincrby(key, "requeue_count", 1) or 0)
                except Exception:
                    n = MAX_REQUEUE + 1  # redis 抖动时保守走终态，避免无限乒乓
                if n > MAX_REQUEUE:
                    log(
                        city,
                        f"[{typ}] requeue_count {n} > MAX_REQUEUE({MAX_REQUEUE}), "
                        f"finish fail_budget (no more requeue)",
                    )
                    finish_task(
                        rdb,
                        city,
                        typ,
                        "fail_budget",
                        new_total,
                        dup_total,
                        blocked_total,
                        pages_done,
                    )
                    return new_total
                requeue_task(rdb, city, typ, pages, target, round_n)
                release_lock(rdb, city, typ)
                return new_total
            proxy_str = None  # 被拦/失败 → 换代理（fangyuan 本就 None，退避后重试）
            continue

        # 站点 404：页码已超出该城真实页深，继续往后翻只会更空 → 本轮到此为止
        if typ == "fangyuan" and _is_page_not_found(html):
            log(city, f"p{page} [{typ}] site 404 (beyond real page depth), round done")
            _append_jsonl(
                stats_path,
                {
                    "city": city,
                    "type": typ,
                    "page": page,
                    "proxy": proxy_str,
                    "proxy_src": proxy_src,
                    "new_count": 0,
                    "dup_count": 0,
                    "status": "not_found",
                    "ts": time.time(),
                },
            )
            finish_reason = "not_found"
            break

        # 真实安居客页面但 0 卡片：换 IP 复核，连续 N 个不同 IP 都 0 卡片才认真空页
        if typ == "fangyuan" and not _has_cards(html):
            if zero_card_retry < ZERO_CARD_RETRY:
                zero_card_retry += 1
                log(
                    city,
                    f"p{page} [{typ}] real page but 0 cards, "
                    f"recheck#{zero_card_retry} with new proxy",
                )
                _append_jsonl(
                    stats_path,
                    {
                        "city": city,
                        "type": typ,
                        "page": page,
                        "proxy": proxy_str,
                        "proxy_src": proxy_src,
                        "new_count": 0,
                        "dup_count": 0,
                        "status": "zero_card_recheck",
                        "ts": time.time(),
                    },
                )
                proxy_str = None
                continue
        zero_card_retry = 0

        # 轻量解析（跳过 geocode）
        rows = parse_rows(html, city, typ)
        # URL set 去重：只对新增 URL 计 new
        new_rows, dup_rows = [], []
        if rows:
            pipe = rdb.pipeline()
            for r in rows:
                pipe.sadd(urls_key, r["url"])
            added = pipe.execute()  # 每个 URL 的 SADD 返回 0/1
            for r, is_new in zip(rows, added, strict=True):
                if is_new:
                    new_rows.append(r)
                else:
                    dup_rows.append(r)
            empty_pages = 0  # 出数页打断空页连击，避免零星空页累积触发早停
        else:
            # 无卡片才算空页；有卡片但全重复继续翻（第2/3轮行为）
            if not _has_cards(html):
                empty_pages += 1
            else:
                empty_pages = 0
        new_total += len(new_rows)
        dup_total += len(dup_rows)

        # raw JSONL：全部写入（含重复，供 ETL 去重/吞吐统计）
        for r in rows:
            r["_source"] = "render" if typ == "fangyuan" else ("qg" if proxy_str else "unknown")
            _append_jsonl(raw_path, r)
        # stats JSONL：每页一行
        _append_jsonl(
            stats_path,
            {
                "city": city,
                "type": typ,
                "page": page,
                "proxy": proxy_str,
                "proxy_src": proxy_src,
                "new_count": len(new_rows),
                "dup_count": len(dup_rows),
                "status": "ok",
                "ts": time.time(),
            },
        )

        # 每页一次 pipeline：进度 + 任务状态
        try:
            pipe = rdb.pipeline()
            progress_max = max(progress_max, page)
            pipe.set(progress_key, progress_max)
            pipe.hset(
                key,
                mapping={
                    "count": new_total,
                    "new_count": new_total,
                    "dup_count": dup_total,
                    "pages_done": pages_done + 1,
                    "status": "running",
                    "worker": MY_ID,
                    "worker_hb": time.time(),
                },
            )
            pipe.execute()
        except Exception:
            pass

        log(city, f"p{page} [{typ}] new={len(new_rows)} dup={len(dup_rows)} cum_new={new_total}")
        consecutive_fail = 0
        plan_idx += 1
        pages_done += 1
        time.sleep(1.0)

        # fangyuan 每页轮换代理：渲染经本地转发代理逐页消耗青果/免费池配额
        if typ == "fangyuan":
            proxy_str = None

        # 空页早停：连续 EMPTY_LIMIT 页无卡片 → 该任务本轮抓完
        if empty_pages >= EMPTY_LIMIT:
            log(city, f"[{typ}] no cards for {empty_pages} pages, round done")
            finish_reason = "empty_pages"
            break

    # 终态：未在循环内定下原因，则按退出条件判定（达 target / 跑到 pages 上限）
    if finish_reason is None:
        finish_reason = "target_reached" if new_total >= target else "pages_exhausted"
    finish_task(rdb, city, typ, finish_reason, new_total, dup_total, blocked_total, pages_done)
    return new_total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/output")
    args = ap.parse_args()

    rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    try:
        rdb.ping()
        register(rdb)
    except Exception as e:
        log("-", f"redis unavailable ({e}); exit")
        sys.exit(1)

    threading.Thread(target=_hb_loop, args=(rdb,), daemon=True).start()
    log("-", f"generic worker up, master={MASTER_URL}")

    while True:
        # 全局停止信号：fangyuan 提前结束/资源耗尽后 master 置位 → worker 暂停等待统计
        try:
            if rdb.get("spacefin:stop"):
                log("-", "STOP signal set, pause (stats phase)")
                time.sleep(POLL_INTERVAL * 6)
                continue
        except Exception:
            pass
        # Redis 抖动（如 1GB VM 停顿）不应杀死进程：claim 段 redis 报错 → 退避重试
        try:
            task = claim_task(rdb)
        except Exception as e:  # noqa: BLE001
            log("-", f"redis error in claim_task: {repr(e)[:200]}; retry in 5s")
            time.sleep(5)
            continue
        if task is None:
            log("-", "no pending task, wait")
            time.sleep(POLL_INTERVAL)
            continue
        city, typ = task["city"], task.get("type", "sale")
        log(
            city,
            f"claimed {typ} round {task['round']}: pages={task['pages']} target={task['target']}",
        )
        try:
            crawl(rdb, city, typ, task["pages"], task["target"], task["round"], args.out_dir)
        except Exception as e:
            log(city, f"[{typ}] crawl error: {e}")
            release_lock(rdb, city, typ)
        time.sleep(2)


if __name__ == "__main__":
    main()
