#!/usr/bin/env python3
"""观测完备性账本 crawl_census（计划 S2）。

一句话：把「今天到底扫到了哪几页」写成一张可查的表，让「这条房源今天没出现」
这句话只在**有证据**时才成立。缺了这张表，缺席 = 下架就是拿「我没看」当「它不在」。

取数三源，各自的权威不同（任一源缺失都不会让判定变得乐观，只会变保守）：
  stats JSONL `stats/ds=<ds>/requests_<city>_<typ>_<worker>.jsonl` → 页覆盖（哪些页真被看到）
  raw   JSONL `raw/ds=<ds>/<city>_<typ>_<worker>.jsonl`           → 行数 / distinct key / key 指纹
                                                                    + **批次城市一致性**（见下）
  Redis 波次快照 `spacefin:crawl_wave_log:<run>` + 任务 Hash        → finish_reason / 计划页数

`complete` 是白名单判定（只认 4 个正常终态 + 无洞的页区间 + 批次城市一致），未知原因
一律 0；不完整的行**不向 S3 提供任何页码证据**——这是「缺省即未知」的落地点。

**批次城市一致性**（`city_mismatch` 闸门）：页扫全、终态正常，只证明「这一页区间正常
看完了」，不证明「看到的是派单那个城」。实测污染是**整批**的——worker 派去爬 `zh`、
代理 IP 在北京，整批爬回的全是北京房源（某轮 zh 新增 131 行 100% 为 beijing）。若放行，
珠海真实房源当天一条都没被扫到却被判「合格观测下未出现」，连续 3 天即误判下架。
故对每批 raw 行用 `url_city.resolve_city` 比对「URL 归属 == 派单城市」，比例低于
`--city-match-min`（默认 0.5）即判不完备。这条是**内容级**判定，故意排在形式级判定
（页洞/终态/深度）之后——形式故障更基础，不该被内容问题掩盖。

用法：
  python tools/orchestrator/census.py --date 2026-08-31
  python tools/orchestrator/census.py --date 2026-08-31 --dry-run --json
退出码：0 = 账本已写（含「本日无观测」这种全 0 的账）；2 = 工具/环境失败或对账异常。
`--fail-on-incomplete` 让「存在 complete=0 的任务」也返回 2（S3 强制阶段再打开）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))

from etl import CITY_NAMES, _mysql_params, load_env  # noqa: E402  复用生产侧口径，防第二套漂移
from url_city import resolve_city  # noqa: E402  城市归属只认这一套口径
from url_key import make_url_key  # noqa: E402

# 采集侧的任务类型（文件名口径），不是 DWD 表名口径：fangyuan 落 crawl_housing_rent。
CRAWL_TYPES = ("sale", "fangyuan")
# 只有这 4 个终态证明「这一页区间我是正常看完的」。
# empty_pages 计入：它是 ZERO_CARD_RETRY 个不同 IP 都返回 0 卡片后的三连确认，
# 比 pages_exhausted（撞到计划页数上限）更接近「列表到此为止」，缺席证据只会更强。
OK_FINISH_REASONS = ("pages_exhausted", "target_reached", "not_found", "empty_pages")
NO_WAVE = "none"
NO_WORKER = "none"
# 批次城市一致性阈值：低于它即判 city_mismatch。0.5 = 「过半不是本城的」就整批作废，
# 宁可少一天数据也不放一整批外地房进缺席证据（误判下架是不可逆的对外结论）。
CITY_MATCH_MIN = 0.5

CENSUS_DDL = """
CREATE TABLE IF NOT EXISTS crawl_census (
  ds               DATE          NOT NULL COMMENT '观测日（= raw/stats 的 ds 目录名，非 ETL 重跑日）',
  city             VARCHAR(8)    NOT NULL COMMENT '城市码',
  typ              VARCHAR(8)    NOT NULL COMMENT '采集任务类型 sale|fangyuan（非 DWD 表名口径）',
  wave             VARCHAR(16)   NOT NULL COMMENT '波次 floor|rescue|depth|unknown；无观测行用 none',
  worker           VARCHAR(16)   NOT NULL COMMENT '产出该行的容器 WORKER_ID；无观测行用 none',
  scope            VARCHAR(8)    NOT NULL DEFAULT '' COMMENT 'head=从第1页起 / deep=断点续爬的深部',
  pages_planned    VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '该波计划页数（来自 redis 波次快照/任务 Hash）',
  pages_ok         SMALLINT      NOT NULL DEFAULT 0 COMMENT '状态 ok 的去重页数',
  first_page       SMALLINT      NOT NULL DEFAULT 0 COMMENT '本页区间起点（head 行=1）',
  contiguous_pages SMALLINT      NOT NULL DEFAULT 0 COMMENT '从 first_page 起连续 ok 的最深页；也是区间终点',
  finish_reason    VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '任务终态（redis 权威；空=查无终态）',
  rows_emitted     INT           NOT NULL DEFAULT 0 COMMENT '该行 raw 落盘行数（含重复，供 ETL 去重对账）',
  distinct_keys    INT           NOT NULL DEFAULT 0 COMMENT '该行内去重 url_key 数',
  key_digest       CHAR(40)      NOT NULL DEFAULT '' COMMENT 'sha1(sorted url_key)：不存全量键也能比出同日是否同一批',
  complete         TINYINT       NOT NULL DEFAULT 0 COMMENT '1=该 (城,型,波,容器) 观测完备，可作缺席证据',
  complete_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'ok / no_observation / gap@N / shallow / reason:X ...',
  created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ds, city, typ, wave, worker),
  KEY idx_ds_city_complete (ds, city, complete)
) ENGINE = InnoDB COMMENT = '采集观测完备性账本 · 每观测日每(城,型,波,容器)一行，缺席判定的唯一证据源'
"""

UPSERT_SQL = (
    "INSERT INTO crawl_census (ds, city, typ, wave, worker, scope, pages_planned, pages_ok,"
    " first_page, contiguous_pages, finish_reason, rows_emitted, distinct_keys, key_digest,"
    " complete, complete_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    " ON DUPLICATE KEY UPDATE scope=VALUES(scope), pages_planned=VALUES(pages_planned),"
    " pages_ok=VALUES(pages_ok), first_page=VALUES(first_page),"
    " contiguous_pages=VALUES(contiguous_pages), finish_reason=VALUES(finish_reason),"
    " rows_emitted=VALUES(rows_emitted), distinct_keys=VALUES(distinct_keys),"
    " key_digest=VALUES(key_digest), complete=VALUES(complete),"
    " complete_reason=VALUES(complete_reason)"
)


def log(msg: str) -> None:
    print(f"[census {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 纯函数：页覆盖 + 完备性判定（单元测试只打这两个）
# ----------------------------------------------------------------------------


def page_interval(pages) -> tuple:
    """返回 (first, contiguous_end, max_seen)：从最小页起连续 ok 的最深页。

    不连续时 contiguous_end < max_seen，差值处即页洞——页洞必须来自「这一页始终拿不到
    有效响应」，而 worker 遇阻是换 IP 重试到成功或写 blocked 终态，所以真出现页洞
    就意味着该城该型当天的观测断了一截，后面所有页都不再是证据。
    """
    s = {p for p in pages if isinstance(p, int) and p > 0}
    if not s:
        return 0, 0, 0
    first = min(s)
    end = first
    while end + 1 in s:
        end += 1
    return first, end, max(s)


def judge(
    *,
    pages_ok: int,
    first_page: int,
    contiguous: int,
    max_seen: int,
    finish_reason: str,
    planned: int,
    head_rewind: int,
    has_wave: bool,
    city_match_ratio: float = None,
    city_match_min: float = CITY_MATCH_MIN,
) -> tuple:
    """返回 (complete 0/1, complete_reason)。任何不确定的输入都判 0（保守）。

    `city_match_ratio=None` 表示「这批没有 raw 行，无从判断」（如只扫到页没落行），
    此时**不**判 city_mismatch——无内容不能反证内容错，行为与加这条闸门之前完全一致。
    """
    if pages_ok == 0:
        return 0, "no_ok_page"
    if not has_wave:
        # 落盘时无 wave（旧镜像）：归因不到波次，就不算这一波的观测。
        return 0, "no_wave"
    if max_seen > contiguous:
        return 0, f"gap@{contiguous + 1}"
    if first_page == 1:
        need = min(head_rewind, planned) if planned else head_rewind
        if contiguous < need:
            return 0, f"shallow{contiguous}<{need}"
    if not finish_reason:
        return 0, "no_finish_reason"
    if finish_reason not in OK_FINISH_REASONS:
        return 0, f"reason:{finish_reason}"[:32]
    # 最后一道闸：形式级都过了，再看内容是不是这个城的（整批爬错城的情形）
    if city_match_ratio is not None and city_match_ratio < city_match_min:
        return 0, f"city_mismatch{round(city_match_ratio, 2)}<{city_match_min}"[:32]
    return 1, "ok"


# ----------------------------------------------------------------------------
# 取数
# ----------------------------------------------------------------------------


def read_stats(stats_dir: str) -> dict:
    """stats/ds=<ds>/requests_*.jsonl → {(city,typ,wave,worker): set(ok 页码)}。

    只收 `status=="ok"` 的页：认证的是「这一页确实看到了有效响应」，
    blocked/not_found/zero_card_recheck 都只是「试过」，不构成页覆盖。
    页记录里的 `worker`/`wave` 是权威（不靠文件名），旧镜像写的文件没有这两个
    字段，用 `unknown` 落到可判定的桶里，再由 judge 判成不完备。
    """
    buckets: dict = {}
    if not os.path.isdir(stats_dir):
        return buckets
    for fn in sorted(os.listdir(stats_dir)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(stats_dir, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                city, typ = rec.get("city"), rec.get("type")
                page = rec.get("page")
                if not city or not typ or not isinstance(page, int):
                    continue
                if rec.get("status") != "ok":
                    continue
                wave = (rec.get("wave") or "unknown").strip() or "unknown"
                worker = (rec.get("worker") or "unknown").strip() or "unknown"
                buckets.setdefault((city, typ, wave, worker), set()).add(page)
    return buckets


def read_raw(raw_dir: str) -> dict:
    """raw/ds=<ds>/*.jsonl → {(city,typ,wave,worker): {"rows":n,"keys":set,city_hit,city_total}}。

    worker 取文件名尾段（`<city>_<typ>_<worker>.jsonl`），wave 取行内 `_wave`；
    两者都是落盘时写死的证据，比 redis 更贴近「这批行究竟由谁产出」。

    `city_hit/city_total`：批次城市一致性。桶的 city 是**派单城市**（文件名），
    逐行用 `resolve_city` 判 URL 归属，只有**等于派单城市**才算 hit——省外桶（子域原文）
    与 unknown 都不算。没有 URL 的行计入 total 但**不**算 hit（保守：爬不到 URL 就无法
    自证是本城的）。这两个计数之比即 `city_match_ratio`，由 judge 拦整城污染。
    """
    buckets: dict = {}
    if not os.path.isdir(raw_dir):
        return buckets
    for fn in sorted(os.listdir(raw_dir)):
        if not fn.endswith(".jsonl"):
            continue
        stem = fn[: -len(".jsonl")]
        parts = stem.split("_", 2)
        if len(parts) != 3:
            continue
        city, typ, worker = parts
        if city not in CITY_NAMES or typ not in CRAWL_TYPES:
            continue
        with open(os.path.join(raw_dir, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                wave = (rec.get("_wave") or "unknown").strip() or "unknown"
                b = buckets.setdefault(
                    (city, typ, wave, worker),
                    {"rows": 0, "keys": set(), "city_hit": 0, "city_total": 0},
                )
                b["rows"] += 1
                url = rec.get("url") or ""
                b["keys"].add(make_url_key(url))
                b["city_total"] += 1
                if resolve_city(url, city)[0] == city:
                    b["city_hit"] += 1
    return buckets


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def read_redis(run_id: str, host: str, port: int) -> dict:
    """redis → {(city,typ,wave): {"reason":str,"planned":int,"pages_done":int}}；连不上返回 {}。

    波次快照在**每次波次收尾**时写，因此只有跑到波次边界的 run 才有；
    run 被中途停掉（例如 Airflow 失败）时回落到任务 Hash 的当前 wave/finish_reason。
    """
    out: dict = {}
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        log(f"WARN 无 redis 模块，finish_reason 只能留空: {exc}")
        return out
    try:
        rdb = redis.Redis(host=host, port=port, decode_responses=True)
        rdb.ping()
    except Exception as exc:  # noqa: BLE001 - redis 不可达只降级，不拦停记账
        log(f"WARN redis {host}:{port} 不可达（{type(exc).__name__}: {exc}），降级为无终态")
        return out
    try:
        log_key = f"spacefin:crawl_wave_log:{run_id}"
        for field, raw in rdb.hgetall(log_key).items():
            city, _, typ = field.partition(":")
            if not typ:
                continue
            try:
                hist = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(hist, list):
                hist = {x.get("wave", "unknown"): x for x in hist}
            for wave, snap in hist.items():
                out[(city, typ, wave)] = {
                    "reason": str(snap.get("reason") or ""),
                    "planned": _int(snap.get("pages")),
                    "pages_done": _int(snap.get("pages_done")),
                }
        for key in rdb.scan_iter("spacefin:task:*", count=1000):
            tail = key[len("spacefin:task:") :].split(":")
            if len(tail) != 2:
                continue
            city, typ = tail
            st = rdb.hgetall(key)
            wave = st.get("wave") or "unknown"
            slot = out.setdefault((city, typ, wave), {"reason": "", "planned": 0, "pages_done": 0})
            slot["reason"] = slot["reason"] or (st.get("finish_reason") or "")
            slot["planned"] = slot["planned"] or _int(st.get("pages"))
            slot["pages_done"] = slot["pages_done"] or _int(st.get("pages_done"))
    except Exception as exc:  # noqa: BLE001
        log(f"WARN redis 读取中断（{type(exc).__name__}: {exc}），已取到的部分仍生效")
    return out


# ----------------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------------


def build_rows(
    stats: dict, raw: dict, waveinfo: dict, head_rewind: int, city_match_min: float = CITY_MATCH_MIN
) -> list:
    """合并三源 → crawl_census 行。键集 = stats ∪ raw 出现过的 (城,型,波,容器)。"""
    keys = sorted(set(stats) | set(raw))
    rows = []
    for city, typ, wave, worker in keys:
        s = stats.get((city, typ, wave, worker), set())
        r = raw.get((city, typ, wave, worker), {})
        info = waveinfo.get((city, typ, wave), {})
        first, contig, max_seen = page_interval(s)
        planned = _int(info.get("planned"))
        reason = str(info.get("reason") or "")
        # 无 raw 行 → ratio=None → 不判 city_mismatch（无从判断不等于判断为错）
        city_hit, city_total = _int(r.get("city_hit")), _int(r.get("city_total"))
        ratio = city_hit / city_total if city_total else None
        complete, cr = judge(
            pages_ok=len(s),
            first_page=first,
            contiguous=contig,
            max_seen=max_seen,
            finish_reason=reason,
            planned=planned,
            head_rewind=head_rewind,
            has_wave=wave != "unknown",
            city_match_ratio=ratio,
            city_match_min=city_match_min,
        )
        keys_sorted = sorted(r.get("keys", set()))
        rows.append(
            {
                "city": city,
                "typ": typ,
                "wave": wave,
                "worker": worker,
                "scope": "head" if first == 1 else ("deep" if first else ""),
                "pages_planned": str(planned) if planned else "",
                "pages_ok": len(s),
                "first_page": first,
                "contiguous_pages": contig,
                "finish_reason": reason[:32],
                "rows_emitted": r.get("rows", 0),
                "distinct_keys": len(keys_sorted),
                "key_digest": (
                    hashlib.sha1(",".join(keys_sorted).encode()).hexdigest() if keys_sorted else ""
                ),
                "city_hit": city_hit,
                "city_total": city_total,
                "city_match": None if ratio is None else round(ratio, 4),
                "complete": complete,
                "complete_reason": cr,
            }
        )
    return rows


def fill_missing_tasks(rows: list) -> list:
    """每个 (城,型) 至少留一行：没被派到/没落盘也要记成 no_observation。

    这不是补数据，是**补负证据**。S3 只查「该城今日有没有 complete=1 的行」，
    若允许整城无行，「无行」和「账本没跑」就分不开，缺省即未知会退化成缺省即放行。
    """
    seen = {(r["city"], r["typ"]) for r in rows}
    for city in CITY_NAMES:
        for typ in CRAWL_TYPES:
            if (city, typ) in seen:
                continue
            rows.append(
                {
                    "city": city,
                    "typ": typ,
                    "wave": NO_WAVE,
                    "worker": NO_WORKER,
                    "scope": "",
                    "pages_planned": "",
                    "pages_ok": 0,
                    "first_page": 0,
                    "contiguous_pages": 0,
                    "finish_reason": "",
                    "rows_emitted": 0,
                    "distinct_keys": 0,
                    "key_digest": "",
                    "complete": 0,
                    "complete_reason": "no_observation",
                }
            )
    return sorted(rows, key=lambda r: (r["city"], r["typ"], r["wave"], r["worker"]))


def certified_pages(rows: list) -> dict:
    """(city,typ) → 已认证页码集合（只取 complete=1 的行，区间 [first, contiguous]）。"""
    out: dict = {}
    for r in rows:
        if not r["complete"]:
            continue
        s = out.setdefault((r["city"], r["typ"]), set())
        s.update(range(r["first_page"], r["contiguous_pages"] + 1))
    return out


def _fmt(rows: list) -> str:
    lines = []
    for r in rows:
        if r["complete"] or r["pages_ok"] or r["rows_emitted"]:
            hit, total = r.get("city_hit", 0), r.get("city_total", 0)
            city_part = f"{hit}/{total}" + (f"={round(100 * hit / total)}%" if total else "")
            lines.append(
                f"  {r['city']:<4} {r['typ']:<9} wave={r['wave']:<8} worker={r['worker']:<11}"
                f" pages_ok={r['pages_ok']:<3} [{r['first_page']}..{r['contiguous_pages']}]"
                f" rows={r['rows_emitted']:<6} keys={r['distinct_keys']:<6}"
                f" city={city_part:<12} reason={r['finish_reason'] or '-':<16}"
                f" -> {r['complete_reason']}"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="观测日 ds，YYYY-MM-DD")
    ap.add_argument("--run-id", default=None, help="redis 波次快照的 run 前缀，默认 = --date")
    ap.add_argument("--out-dir", default="output/guangdong")
    ap.add_argument("--raw-dir", default=None, help="默认 <out-dir>/raw")
    ap.add_argument("--stats-dir", default=None, help="默认 <out-dir>/stats")
    ap.add_argument("--head-rewind", type=int, default=int(os.getenv("HEAD_REWIND", 2)))
    ap.add_argument(
        "--city-match-min",
        type=float,
        default=CITY_MATCH_MIN,
        help="批次城市一致性阈值：raw 行里 URL 归属==派单城市的比例低于此值即判 "
        "city_mismatch（整批爬错城），默认 0.5",
    )
    ap.add_argument("--redis-host", default=os.getenv("REDIS_HOST", "127.0.0.1"))
    ap.add_argument("--redis-port", type=int, default=int(os.getenv("REDIS_PORT", 6379)))
    ap.add_argument("--no-redis", action="store_true")
    ap.add_argument("--mysql-dsn", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-incomplete", action="store_true")
    args = ap.parse_args()

    ds = args.date
    raw_dir = args.raw_dir or os.path.join(args.out_dir, "raw")
    stats_dir = args.stats_dir or os.path.join(args.out_dir, "stats")
    raw_ds = os.path.join(raw_dir, f"ds={ds}")
    stats_ds = os.path.join(stats_dir, f"ds={ds}")

    stats = read_stats(stats_ds)
    raw = read_raw(raw_ds)
    if args.no_redis:
        waveinfo = {}
    else:
        waveinfo = read_redis(args.run_id or ds, args.redis_host, args.redis_port)
    rows = fill_missing_tasks(
        build_rows(stats, raw, waveinfo, args.head_rewind, args.city_match_min)
    )

    real = [r for r in rows if r["wave"] != NO_WAVE]
    depth = certified_pages(rows)
    incomplete_tasks = sorted(
        {
            (r["city"], r["typ"])
            for r in rows
            if not r["complete"] and (r["city"], r["typ"]) not in depth
        }
    )
    n_tasks = len(CITY_NAMES) * len(CRAWL_TYPES)

    if args.json:
        print(
            json.dumps(
                {
                    "ds": ds,
                    "rows": rows,
                    "certified": {f"{c}:{t}": sorted(p) for (c, t), p in depth.items()},
                    "incomplete_tasks": [list(x) for x in incomplete_tasks],
                    "tasks_total": n_tasks,
                    "tasks_complete": len(depth),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"=== crawl_census ds={ds} 目录 raw={raw_ds} stats={stats_ds} ===")
        print(_fmt(rows))
        print("--- 汇总 ---")
        print(f"账本行数 {len(rows)}（真实观测 {len(real)} + 无观测占位 {len(rows) - len(real)}）")
        print(
            f"完备任务 {len(depth)}/{n_tasks}；不完备 {len(incomplete_tasks)}"
            + (
                ": "
                + ", ".join(f"{c}:{t}" for c, t in incomplete_tasks[:12])
                + ("…" if len(incomplete_tasks) > 12 else "")
                if incomplete_tasks
                else ""
            )
        )
        for (c, t), pages in sorted(depth.items()):
            if pages:
                hole = pages != set(range(min(pages), max(pages) + 1))
                print(
                    f"  {c}/{t}: 认证页 {min(pages)}..{max(pages)}"
                    f"（{len(pages)} 页{'，内有页洞' if hole else ''}）"
                )

    if not real:
        log(
            f"本日无任何观测证据（既无 stats/ds={ds} 也无 raw/ds={ds}）——已记为"
            f" {len(rows)} 行 no_observation：S3 当日全城不得产生任何 absent 判定"
        )

    if not args.dry_run:
        import pymysql

        params = _mysql_params(args.mysql_dsn, load_env())
        try:
            conn = pymysql.connect(**params, autocommit=False, charset="utf8mb4")
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR 连不上 MySQL: {type(exc).__name__}: {exc}")
            return 2
        try:
            with conn.cursor() as cur:
                cur.execute(CENSUS_DDL)
                # 账本严格等于本次计算结果：先清当日旧行再全量写入。
                # 只 upsert 不清会留陈旧行——no_observation 占位的主键是 (…,'none','none')，
                # 而真实观测行是 (…,'unknown','worker-N')，主键不重叠、upsert 覆盖不到；
                # 于是「先跑一次全 no_observation、数据到齐后再跑」会让同一 (城,型)
                # 同时留着「没观测过」和「观测了但不完备」两行。当日范围重算是安全的：
                # 少算了只会让账本更保守（complete 更少），不会凭空造出缺席证据。
                cur.execute("DELETE FROM crawl_census WHERE ds=%s", (ds,))
                for r in rows:
                    cur.execute(
                        UPSERT_SQL,
                        (
                            ds,
                            r["city"],
                            r["typ"],
                            r["wave"],
                            r["worker"],
                            r["scope"],
                            r["pages_planned"],
                            r["pages_ok"],
                            r["first_page"],
                            r["contiguous_pages"],
                            r["finish_reason"],
                            r["rows_emitted"],
                            r["distinct_keys"],
                            r["key_digest"],
                            r["complete"],
                            r["complete_reason"],
                        ),
                    )
            conn.commit()
            log(f"已写 crawl_census {len(rows)} 行（ds={ds}，当日重算，重复运行结果不变）")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log(
                f"ERROR 写 crawl_census 失败: {type(exc).__name__}: {exc}；"
                f"若报 CREATE/ALTER 权限不足，用 root 跑一次 python3 tools/orchestrator/etl.py --init-db"
            )
            return 2
        finally:
            conn.close()
    else:
        log("--dry-run：未写库")

    if args.fail_on_incomplete and incomplete_tasks:
        log(f"--fail-on-incomplete：{len(incomplete_tasks)} 个任务当日不完备，退出 2")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
