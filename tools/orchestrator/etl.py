#!/usr/bin/env python3
"""
ETL 脚本：采集 raw JSONL → 清洗 → url_key 去重 → geocode → MySQL DWD 主表 + ODS 数据湖 Parquet。

运行形态（已确认决策，见 docs/tech/components/crawler-etl.md）：
- 宿主机 Python 环境直接跑（单一环境 conda `spark`，`tools/orchestrator/.venv` 是其别名），Airflow DAG 用 BashOperator 调本脚本（不容器化）
- 增量：只读 `raw/ds=<观测日>/*.jsonl` 一个目录。一个 ds 目录只会被那一天的采集写入，
  所以「本次读到什么」=「当天实际观测到什么」，`last_seen_date` 才可能是观测结果而不是运行日期
- backfill：--backfill 忽略 etl_processed.json 重跑目标 ds 目录（须配 --date）；
  重建历史 = 对每个 ds 目录各跑一次（见 docs/tech/components/crawler-etl.md）
- legacy：--legacy-flat-raw 才读 raw 顶层扁平 *.jsonl（迁移前的历史语料），
  且必须显式给 --observe-date——那批文件的真实抓取日只有人知道，不再默认成「今天」
- 去重键：url_key（URL 规范化房源 ID，见 url_key.py），MySQL 唯一主键 + COALESCE 只更新非空/非零字段
- 留存：first_seen_date 保留、last_seen_date 更新为本 ds、last_seen_page 记本次观测页码、
  days_on_market = last_seen - first_seen
- 输出：spacefin_crawler 库两张 DWD 表 + data_lake/housing/dt=.. 每日观测集 Parquet + etl_report.json
- 无 CSV 输出（单一数据出口）

用法：
    python etl.py --date 2026-08-04 --raw-dir output/guangdong/raw --lake-dir data_lake/housing
    python etl.py --date 2026-08-04 --backfill ...   # 重跑该 ds（忽略已处理状态）
    python etl.py --legacy-flat-raw --observe-date 2026-08-24 --backfill ...  # 一次性回补旧语料
    python etl.py --init-db --backfill --date 2026-08-04 ...  # 首次初始化（root）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pyarrow as pa
import pyarrow.parquet as pq
import pymysql
from anjuke_crawler.geocoder import DbGeocoder, LocalGeocoder
from anjuke_crawler.parse import RENT_HEADERS, SCHEMA_HEADERS
from url_city import resolve_city
from url_key import make_url_key

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql", "crawl_schema.sql")

CRAWL_DB = "spacefin_crawler"
APP_USER = "spacefin_crawler_app"

CITY_NAMES = {
    "gz": "广州",
    "sz": "深圳",
    "zh": "珠海",
    "st": "汕头",
    "fs": "佛山",
    "sg": "韶关",
    "zj": "湛江",
    "zq": "肇庆",
    "jm": "江门",
    "mm": "茂名",
    "hui": "惠州",
    "mz": "梅州",
    "sw": "汕尾",
    "hy": "河源",
    "yj": "阳江",
    "qy": "清远",
    "dg": "东莞",
    "zs": "中山",
    "cz": "潮州",
    "jy": "揭阳",
    "yf": "云浮",
}

# 数值字段：0 无意义 → 清洗置 None（upsert COALESCE 后保留旧值）
_ZERO_MEANINGLESS = {
    "area_sqm",
    "total_price_wan",
    "unit_price_yuan",
    "monthly_rent_yuan",
    "building_year",
    "latitude",
    "longitude",
}


def log(msg) -> None:
    """统一日志（对齐 master.py/worker.py 风格）。"""
    print(f"[etl {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 配置 / .env
# ----------------------------------------------------------------------------


def load_env() -> dict:
    """读仓库根 .env（仅取 MYSQL_* 相关键，不写回）。

    允许进程环境 os.environ 中的 MYSQL_* 覆盖 .env 的值——容器化 Airflow 通过
    compose env_file 注入 MYSQL_HOST=host.docker.internal，使 ETL 脚本在桥接网络容器内
    也能连到宿主 MySQL；宿主/采集栈（host 网络）进程无此变量，则回退 .env 的 127.0.0.1。
    """
    env = {}
    path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.startswith("MYSQL_"):
            env[k] = v
    return env


def _mysql_params(dsn: str | None, env: dict) -> dict:
    """解析连接参数：--mysql-dsn 优先，否则从 .env 读。返回 pymysql 参数 dict。"""
    if dsn:
        p = urlparse(dsn)
        host = p.hostname or "127.0.0.1"
        port = p.port or 3306
        db = (p.path or "").lstrip("/") or CRAWL_DB
        user = p.username or APP_USER
        password = p.password or ""
        return {"host": host, "port": port, "user": user, "password": password, "database": db}
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": env.get("MYSQL_APP_USER", APP_USER),
        "password": env.get("MYSQL_APP_PASSWORD", ""),
        "database": CRAWL_DB,
    }


def _root_params(env: dict) -> dict:
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": "root",
        "password": env.get("MYSQL_ROOT_PASSWORD", ""),
    }


_DWD_TABLES = ("crawl_housing_sale", "crawl_housing_rent")

# DWD 新增列的唯一登记处。必须同时改这里和 sql/crawl_schema.sql：
# `CREATE TABLE IF NOT EXISTS` 在活库上不会长出缺的列，所以只写 schema 文件 = 列不存在。
# 原 _ensure_geocode_status 只被 init_db 调用（即只有显式 --init-db 时才补列），
# 存量库因此长期缺列——本函数改为每次运行都跑，不再依赖 --init-db。
_DWD_COLUMNS = [
    (
        "geocode_status",
        "VARCHAR(16) NULL COMMENT '坐标补全状态 pending/hit/miss'",
    ),
    (
        "last_seen_page",
        "SMALLINT NULL COMMENT '最近一次观测到在架时的列表页码；NULL=该次观测没有页码证据'",
    ),
    # --- S3 房源生命周期：market_state.py 写入后的状态缓存 ---
    (
        "market_status",
        "VARCHAR(16) NOT NULL DEFAULT 'unknown' COMMENT "
        "'active|absent|unknown|removed；DWD 只是缓存，权威在 dwd_listing_market_state'",
    ),
    (
        "removed_date",
        "DATE NULL COMMENT 'decide_removal 确认下架的观测日'",
    ),
    (
        "unit_sig",
        "CHAR(12) NULL COMMENT '小区+户型+面积+楼层段+价格段哈希，识别重挂（不合并主键）'",
    ),
]

# 索引同样不会在活库自己长出来，故与列放在同一登记处、同一次幂等校验里补齐。
# market_status + last_seen_date 是「按状态捞当日/历史观测」的唯一查询形态。
_DWD_INDEXES = [
    ("idx_status_last_seen", "(market_status, last_seen_date)"),
]


def _ensure_columns(conn) -> None:
    """幂等守卫：两张 DWD 表缺失的列/索引当场 ALTER 补上。"""
    for tbl in _DWD_TABLES:
        with conn.cursor() as cur:
            for col, ddl in _DWD_COLUMNS:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                    (CRAWL_DB, tbl, col),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE {CRAWL_DB}.{tbl} ADD COLUMN {col} {ddl}")
            for name, ddl in _DWD_INDEXES:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema=%s AND table_name=%s AND index_name=%s",
                    (CRAWL_DB, tbl, name),
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(f"ALTER TABLE {CRAWL_DB}.{tbl} ADD INDEX {name} {ddl}")
    conn.commit()


def init_db(env: dict) -> None:
    """root 仅用于初始化：建库/建表/建专用账号（幂等）。密码占位符替换为 .env 的 app 密码。"""
    root = dict(_root_params(env))
    app_pwd = env.get("MYSQL_APP_PASSWORD", "")
    if not app_pwd:
        print("[etl] WARN: MYSQL_APP_PASSWORD 未设置，专用账号密码为空")
    conn = pymysql.connect(
        host=root["host"],
        port=root["port"],
        user=root["user"],
        password=root["password"],
        autocommit=True,
    )
    try:
        with open(SCHEMA_SQL, encoding="utf-8") as f:
            sql = f.read()
        sql = sql.replace("PLACEHOLDER_CHANGED_BY_ETL", app_pwd)
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                with conn.cursor() as cur:
                    cur.execute(stmt)
        print("[etl] init_db: schema/账号就绪")
        _ensure_columns(conn)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# 增量识别（etl_processed.json）
# ----------------------------------------------------------------------------

STATE_FILE = "etl_processed.json"
DS_DIR_PREFIX = "ds="  # raw 目录约定：raw/ds=<观测日>/<city>_<type>_<worker>.jsonl


def _state_path(out_dir: str) -> str:
    return os.path.join(out_dir, STATE_FILE)


def _load_state(out_dir: str) -> dict:
    p = _state_path(out_dir)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"WARN 状态文件读取失败，按空状态处理: {p} ({type(e).__name__}: {e})")
            return {}
    return {}


def _save_state(out_dir: str, state: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = _state_path(out_dir) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _state_path(out_dir))
    except OSError as e:
        # 状态文件写失败（如 output/ 由 root 创建、ETL 以非 root 运行致 PermissionError）
        # 不应让已成功入库的数据被误报为失败。降级为警告，ETL 正常结束（退出码 0）。
        log(
            f"WARN 状态文件写入失败，已跳过状态记录（数据已入库，不影响本次加载）: "
            f"{_state_path(out_dir)} ({type(e).__name__}: {e})"
        )


def _jsonl_in(dirpath: str) -> list:
    return sorted(f for f in os.listdir(dirpath) if f.endswith(".jsonl"))


def list_ds_dirs(raw_dir: str) -> list:
    """raw 下已有的观测日子目录名（升序，如 ['ds=2026-08-30', 'ds=2026-08-31']）。"""
    if not os.path.isdir(raw_dir):
        return []
    return sorted(
        d
        for d in os.listdir(raw_dir)
        if d.startswith(DS_DIR_PREFIX) and os.path.isdir(os.path.join(raw_dir, d))
    )


def collect_files(
    raw_dir: str, out_dir: str, backfill: bool, date: str, legacy_flat: bool = False
) -> tuple[list, int, dict]:
    """按 etl_processed.json 选出待处理文件，返回 (to_process, skipped, state)。

    `to_process` 是**相对 raw_dir 的路径**（如 `ds=2026-08-31/gz_sale_1.jsonl`），
    同时直接用作状态键：路径本身含观测日，跨日同名不会误判已处理，也不必再靠
    把日期拼进键里的写法（旧写法每晚都会把整批文件重新登记、于是重新处理一遍）。

    只扫 `raw/ds=<date>/` 一个目录 = 本次只观测那一天真正爬到的东西。旧版扫 raw
    顶层扁平 *.jsonl：那批跨日追加的文件里装着全部历史语料，所以 ETL 每晚把整个
    历史重读并重写一遍 DWD（实测 2026-08-30：inserted=0、updated=28,619、
    25,241 行的 last_seen_date 被刷成当天）—— last_seen_date 从此是「ETL 重跑日」
    的函数，与页面是否真被爬过无关。

    `legacy_flat=True` 才读顶层扁平文件，供迁移前的一次性回补：状态键是裸文件名，
    所以同一份内容只会被处理一次，不再每晚重来。
    """
    if not os.path.isdir(raw_dir):
        raise SystemExit(f"[etl] raw 目录不存在: {raw_dir}")
    state = _load_state(out_dir)
    if legacy_flat:
        targets = _jsonl_in(raw_dir)
    else:
        ds_dir = f"{DS_DIR_PREFIX}{date}"
        if not os.path.isdir(os.path.join(raw_dir, ds_dir)):
            have = list_ds_dirs(raw_dir)
            log(
                f"WARN 无本观测日的 raw 子目录: {raw_dir}/{ds_dir}"
                f" → 本次读到 0 个文件、0 行入库。"
                f" 现存 ds 目录 {len(have)} 个，最新 {have[-1] if have else '（无）'}"
                + (
                    "；若最新目录不是本观测日，先查 CRAWL_RUN_ID 有没有透传到 worker"
                    "（缺失时它会退化成默认的 ds=manual）"
                    if have
                    else ""
                )
            )
            return [], 0, state
        targets = [f"{ds_dir}/{f}" for f in _jsonl_in(os.path.join(raw_dir, ds_dir))]

    to_process = []
    skipped = 0
    for rel in targets:
        st = os.stat(os.path.join(raw_dir, rel))
        sig = (st.st_mtime, st.st_size)
        prev = state.get(rel)
        if not backfill and prev and abs(prev["mtime"] - sig[0]) < 1 and prev["size"] == sig[1]:
            skipped += 1
            continue
        to_process.append(rel)
    return to_process, skipped, state


def mark_processed(state: dict, rel: str, raw_dir: str) -> None:
    st = os.stat(os.path.join(raw_dir, rel))
    state[rel] = {"mtime": st.st_mtime, "size": st.st_size, "processed_at": time.time()}


# ----------------------------------------------------------------------------
# 读取 / 清洗 / 去重
# ----------------------------------------------------------------------------


def _clean_str(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _clean_num(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        if int(f) == f:
            return int(f)
        return f
    except (TypeError, ValueError):
        return None


def clean_row(row: dict, typ: str) -> dict:
    """轻量清洗：类型转换 + 空值/无意义 0 置 None。不做业务阈值清洗。"""
    out = {}
    for k, v in row.items():
        if k in ("latitude", "longitude", "area_sqm", "total_price_wan"):
            v = _clean_num(v)
            if v == 0:
                v = None
        elif k in (
            "bedrooms",
            "halls",
            "bathrooms",
            "building_year",
            "building_age",
            "parking_count",
            "unit_price_yuan",
            "monthly_rent_yuan",
        ):
            v = _clean_num(v)
            if k in _ZERO_MEANINGLESS and v == 0:
                v = None
        else:
            v = _clean_str(v)
        out[k] = v
    return out


def load_rows(rel_paths: list, raw_dir: str) -> tuple[dict, int]:
    """读待处理文件 → {(city,type): [cleaned rows]}，返回 (rows_by_task, raw_count)。

    `rel_paths` 是 collect_files 给的相对 raw_dir 路径，因此**文件名**的城市/类型只能从
    **basename** 解析——直接 split 整个相对路径会把 `ds=2026-08-31/gz` 当成城市码。

    行的城市归属不看文件名，按 **URL 子域**重算（方案 A，见 url_city.py）：实测 47.4%
    的房源 URL 子域不在广东 21 城（免费代理 IP 在省外、安居客按 IP 返回当地列表页），
    文件名只代表「本来想爬哪个城」。归属写进 `row["district"]`——DWD 没有 city 列，
    城市码就落在 district（worker.py 的 district_tag=city），改它 DWD 归属、geocode
    城市、write_lake 的 city 分区三处一起跟着对。

    分桶键的 city 因此可能是：21 城码（进按城指标）/ 省外子域原文（省外桶）/ `unknown`。
    文件名城市码仍要过 CITY_NAMES 白名单——它拦的是「派了什么活」，不是「爬到了什么」。
    """
    rows_by_task = {}
    raw_count = 0
    for rel in rel_paths:
        fname = os.path.basename(rel)
        # 文件名约定: {city}_{type}_{worker_id}.jsonl（type ∈ sale|fangyuan|rent）
        parts = fname.split("_")
        if len(parts) < 2:
            continue
        city, typ = parts[0], parts[1]
        if typ == "sale":
            key = (city, "sale")
        elif typ in ("fangyuan", "rent"):
            key = (city, "rent")
        else:
            log(f"跳过非法文件名（type 不在 sale/fangyuan/rent）: {rel}")
            continue
        # 城市码白名单（CITY_NAMES 与 anjuke_crawler.geocoder 同源）：防伪城市码入库/生成湖分区
        # 注意这校验的是**文件名**，不是内容——行的归属由下面 resolve_city 按 URL 重算，
        # 内容里的省外子域另有对账（crawl_reconcile.py）
        if city not in CITY_NAMES:
            log(f"跳过非法文件名（city 不在 CITY_NAMES 白名单）: {rel}")
            continue
        fpath = os.path.join(raw_dir, rel)
        with open(fpath, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError as e:
                    log(f"跳过坏 JSON 行: {rel}:{lineno} ({e})")
                    continue
                # _source/_wave/_crawled_at 是采集侧元数据，只在 raw 与对账里存活
                page = r.pop("_page", None)
                for meta in ("_source", "_wave", "_crawled_at"):
                    r.pop(meta, None)
                row = clean_row(r, key[1])
                row["last_seen_page"] = _clean_num(page)
                city_eff, _reason = resolve_city(r.get("url") or "", city)
                row["district"] = city_eff
                rows_by_task.setdefault((city_eff, key[1]), []).append(row)
                raw_count += 1
    return rows_by_task, raw_count


def dedup(rows: list) -> list[dict]:
    """按 url_key 去重（保留首次出现）。"""
    seen = set()
    out = []
    for r in rows:
        k = make_url_key(r.get("url") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ----------------------------------------------------------------------------
# MySQL DWD upsert
# ----------------------------------------------------------------------------

_TABLE = {"sale": "crawl_housing_sale", "rent": "crawl_housing_rent"}
_HEADERS = {"sale": SCHEMA_HEADERS, "rent": RENT_HEADERS}


def _upsert_sql(typ: str, cols: list) -> str:
    table = _TABLE[typ]
    coldef = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    update_cols = [
        c for c in cols if c not in ("url_key", "first_seen_date", "etl_ts", "geocode_status")
    ]
    # COALESCE 只更新非空/非零字段；first_seen_date 保留；last_seen_date 恒更新；days_on_market 重算
    updates = []
    for c in update_cols:
        if c == "last_seen_date":
            updates.append("last_seen_date = VALUES(last_seen_date)")
        elif c == "last_seen_page":
            # 与 last_seen_date 同样无条件覆盖：页码必须和本次观测严格配对。
            # 走 COALESCE 会留下「日期是今天、页码是上周」的错配，闸门就把陈旧证据
            # 当成今日刷新证据放行（refreshed 一栏的意义正是「有页面证据的在架」）。
            updates.append("last_seen_page = VALUES(last_seen_page)")
        elif c == "days_on_market":
            updates.append("days_on_market = DATEDIFF(VALUES(last_seen_date), first_seen_date)")
        else:
            updates.append(f"{c} = COALESCE(VALUES({c}), {c})")
    # 存量刷新（ON DUPLICATE UPDATE）也要推进 etl_ts：它是「最近一次被 ETL 触碰」的信号，
    # 驾驶舱 DWD 新鲜度用 MAX(etl_ts) 判定。若只在 INSERT 时写，重爬存量市场会因无新房源
    # 而让 etl_ts 永久停在旧值、误报「已滞后」——与 crawl_quality_gate 的「refreshed>0 正常稳态」
    # 双口径保持一致（见 2026-08-25 阻塞复盘）。cols 不含 etl_ts（靠 DB 默认 CURRENT_TIMESTAMP），
    # 故 UPDATE 分支需显式推进。
    updates.append("etl_ts = CURRENT_TIMESTAMP")
    return (
        f"INSERT INTO {table} ({coldef}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(updates)
    )


def _existing_first_seen(conn, typ: str, keys: list, batch=1000) -> dict:
    """返回 {已存在的 url_key: 库中真实 first_seen_date}（分批 IN 查询）。

    一次查询两用：键存在与否 = updated/inserted 判据；取回的 first_seen_date
    是落湖时唯一的真实来源（write_lake 不得再拿运行日硬当首次出现日）。
    """
    if not keys:
        return {}
    found = {}
    table = _TABLE[typ]
    with conn.cursor() as cur:
        for i in range(0, len(keys), batch):
            chunk = keys[i : i + batch]
            marks = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT url_key, first_seen_date FROM {table} WHERE url_key IN ({marks})", chunk
            )
            for url_key, first in cur.fetchall():
                found[url_key] = first
    return found


def upsert_dwd(conn, typ: str, rows: list, date: str, source: str) -> dict:
    """批量 upsert 去重后的房源行。返回 {inserted, updated, first_seen: {url_key: date}}。"""
    if not rows:
        return {"inserted": 0, "updated": 0, "first_seen": {}}
    headers = _HEADERS[typ]
    cols = (
        ["url_key", "url"]
        + [c for c in headers if c != "url"]
        + [
            "first_seen_date",
            "last_seen_date",
            "last_seen_page",
            "days_on_market",
            "geocode_status",
            "source",
        ]
    )
    sql = _upsert_sql(typ, cols)
    values = []
    keys = []
    for r in rows:
        url = r.get("url") or ""
        key = make_url_key(url)
        keys.append(key)
        # 补全状态：坐标已命中(hit) 或 待补全(pending)
        gs = (
            "hit"
            if (r.get("latitude") is not None and r.get("longitude") is not None)
            else "pending"
        )
        values.append(
            tuple(
                [key, url]
                + [r.get(c) for c in headers if c != "url"]
                + [date, date, r.get("last_seen_page"), 0, gs, source]
            )
        )

    first_seen = _existing_first_seen(conn, typ, keys)
    inserted = updated = 0
    batch = 5000
    with conn.cursor() as cur:
        for i in range(0, len(values), batch):
            cur.executemany(sql, values[i : i + batch])
        # ROW_COUNT 在 executemany 下不可靠，改按库前后差值无法精分；用 IN 结果区分
    for k in keys:
        if k in first_seen:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated, "first_seen": first_seen}


def dwd_counts(conn, typ: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {_TABLE[typ]}")
        return cur.fetchone()[0]


def dwd_retention_buckets(conn, typ: str) -> dict:
    """days_on_market 分桶分布。"""
    buckets = {"0-7": 0, "8-30": 0, "31-90": 0, "90+": 0}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT CASE WHEN days_on_market <= 7 THEN '0-7' WHEN days_on_market <= 30 THEN '8-30' "
            f"WHEN days_on_market <= 90 THEN '31-90' ELSE '90+' END AS b, COUNT(*) "
            f"FROM {_TABLE[typ]} GROUP BY b"
        )
        for b, c in cur.fetchall():
            buckets[b] = c
    return buckets


def dwd_city_counts(conn, typ: str) -> dict:
    """按 district（=城市代码）统计各表行数，返回 {city: n}。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT district, COUNT(*) FROM {_TABLE[typ]} GROUP BY district")
        return {row[0]: row[1] for row in cur.fetchall()}


def dwd_days_stats(conn, typ: str) -> dict:
    """days_on_market 均值与中位数。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT days_on_market FROM {_TABLE[typ]} "
            f"WHERE days_on_market IS NOT NULL ORDER BY days_on_market"
        )
        vals = [r[0] for r in cur.fetchall()]
    if not vals:
        return {"mean": 0, "median": 0}
    mean = round(sum(vals) / len(vals), 2)
    n = len(vals)
    mid = n // 2
    median = vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 2)
    return {"mean": mean, "median": median}


def _record_miss(miss: dict, city: str, community: str) -> None:
    key = (city, community)
    miss[key] = miss.get(key, 0) + 1


# ----------------------------------------------------------------------------
# ODS 湖 Parquet
# ----------------------------------------------------------------------------


def _lake_cols(typ: str) -> list:
    return (
        ["url_key", "url"]
        + [c for c in _HEADERS[typ] if c != "url"]
        + [
            "first_seen_date",
            "last_seen_date",
            "source",
        ]
    )


def _pa_type(v):
    if isinstance(v, bool):
        return pa.bool_()
    if isinstance(v, int):
        return pa.int64()
    if isinstance(v, float):
        return pa.float64()
    if isinstance(v, str):
        return pa.string()
    return None  # 未知类型 → string 兜底


def _lake_schema(recs: list) -> tuple:
    """按本批全部记录的 key 并集构造显式 schema。

    from_pylist 默认只按首行 keys 推 schema，sale/rent 混写时 rent 独有字段
    （monthly_rent_yuan/rent_type）会被静默丢弃。这里扫全批取并集 + 逐列推断类型，
    缺失值填 null；类型冲突（如 str/int 混排）统一 string 并返回需字符串化的列名。
    """
    cols = []
    for r in recs:
        for k in r:
            if k not in cols:
                cols.append(k)
    fields = []
    coerce = set()
    for c in cols:
        t = None
        for r in recs:
            v = r.get(c)
            if v is None:
                continue
            vt = _pa_type(v)
            if vt is None:
                t = pa.string()
                coerce.add(c)
                break
            if t is None:
                t = vt
            elif t != vt:
                if {t, vt} == {pa.int64(), pa.float64()}:
                    t = pa.float64()
                else:
                    t = pa.string()
                    coerce.add(c)
                    break
        fields.append(pa.field(c, t or pa.string()))
    return pa.schema(fields), coerce


def write_lake(lake_dir: str, date: str, rows_by_task: dict, source: str, first_seen: dict) -> int:
    """写当日观测集 Parquet 分区（dt/type/city）。重跑只覆盖本次涉及的子分区（幂等）。

    观测集按 url_key 全局唯一（与 DWD 主键语义一致），跨任务重复只保留首现。
    按 (type, city) 分组独立成表写：既避免 sale/rent 混写丢字段，也保证增量 run
    不会误删当日其他 type/city 的分区（否则当日全量观测集会被削成增量子集）。

    `first_seen` = {(type, url_key): 库里真实首见日}（upsert_dwd 读回）。旧版对每条记录
    硬写 first_seen_date=date，等于宣称「当天所有房源都是首次出现」，并原样经
    lake/sync.py 进 Doris/MinIO。本次新入库的键才取 date。

    跨组重复键落在哪个 city 分区 = `rows_by_task` 的遍历顺序（即文件名升序），
    是「先到先得」而不是业务判定；这一归属差异由 crawl_reconcile.py 显式对出来。
    """
    if not rows_by_task:
        return 0

    seen = set()
    groups = {}
    for (city, typ), rows in sorted(rows_by_task.items()):
        for r in rows:
            url = r.get("url") or ""
            key = make_url_key(url)
            if key in seen:
                continue
            seen.add(key)
            rec = {"url_key": key, "url": url}
            rec.update({c: r.get(c) for c in _HEADERS[typ] if c != "url"})
            rec.update(
                {
                    "first_seen_date": first_seen.get((typ, key)) or date,
                    "last_seen_date": date,
                    "source": source,
                }
            )
            rec["dt"] = date
            rec["type"] = typ
            rec["city"] = city
            groups.setdefault((typ, city), []).append(rec)

    total = 0
    for (typ, city), recs in sorted(groups.items()):
        sub = os.path.join(lake_dir, f"dt={date}", f"type={typ}", f"city={city}")
        if os.path.isdir(sub):
            shutil.rmtree(sub)
        schema, coerce = _lake_schema(recs)
        if coerce:
            for r in recs:
                for c in coerce:
                    if r.get(c) is not None:
                        r[c] = str(r[c])
        table = pa.Table.from_pylist(recs, schema=schema)
        pq.write_to_dataset(
            table,
            root_path=lake_dir,
            partition_cols=["dt", "type", "city"],
            existing_data_behavior="overwrite_or_ignore",
        )
        total += len(recs)
    log(f"写入湖 {total} 行 / {len(groups)} 个子分区 @ {lake_dir}/dt={date}")
    return total


# ----------------------------------------------------------------------------
# geocode（本次行）
# ----------------------------------------------------------------------------


def geocode_buckets(rows_by_task: dict, geocoder, miss: dict) -> tuple[int, int]:
    """按 (city,type) 桶补坐标。非 21 城桶（省外子域 / unknown）**整桶跳过**。

    省外房源不进按城指标，给它们补坐标没有收益；而 DbGeocoder 按 (city, community) 隔离
    词典（geocoder.py），拿省外城市码去查会往 community_coords 登记一批永远查不到的
    pending，污染跨城词典。
    """
    attempted = hit = 0
    for (city, _typ), rows in sorted(rows_by_task.items()):
        if city not in CITY_NAMES:
            continue
        a, h = geocode_rows(rows, geocoder, miss, city=city)
        attempted += a
        hit += h
    return attempted, hit


def geocode_rows(rows: list, geocoder, miss: dict, city: str = None) -> tuple[int, int]:
    """对 lat/lng 为 null 的行补 geocode（词典命中才更新）。返回 (attempted, hit)。

    走 DbGeocoder：带 city（城市代码）精确查词典；pending 自动登记待查。
    """
    attempted = hit = 0
    for r in rows:
        if r.get("latitude") is None or r.get("longitude") is None:
            attempted += 1
            lat, lng = geocoder.geocode(
                r.get("community") or "",
                r.get("title") or "",
                city=city or r.get("district") or None,
            )
            if lat is not None:
                r["latitude"], r["longitude"] = lat, lng
                hit += 1
            else:
                _record_miss(miss, r.get("district") or city or "", r.get("community") or "")
    return attempted, hit


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="output/guangdong/raw")
    ap.add_argument("--out-dir", default="output/guangdong")
    ap.add_argument("--lake-dir", default="data_lake/housing")
    ap.add_argument("--mysql-dsn", default=None, help="完整 DSN（默认从 .env 读 MYSQL_APP_*）")
    ap.add_argument("--geocoder-db", default=None, help="LocalGeocoder 本地坐标 JSON（可选）")
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="忽略 etl_processed.json，重跑目标观测日的整个 raw/ds=<date>/ 目录（须显式配 --date）",
    )
    ap.add_argument(
        "--date",
        default=None,
        help="观测日 YYYY-MM-DD（增量用 DAG 执行日 {{ ds }}）；决定扫描哪个 raw/ds=<date>/ 目录",
    )
    ap.add_argument(
        "--legacy-flat-raw",
        action="store_true",
        help="读 raw 顶层扁平 *.jsonl（S1 之前的历史语料布局）；须配 --observe-date",
    )
    ap.add_argument(
        "--observe-date",
        default=None,
        help="仅 --legacy-flat-raw 用：这批扁平文件的真实抓取日。旧版把它默认成运行日，"
        "于是历史语料每晚都被声称「今天在架」——真实日期只有人知道，故此处强制显式给出",
    )
    ap.add_argument(
        "--init-db",
        action="store_true",
        help="用 root 执行一次初始化（建库/建表/建专用账号，幂等）；日常/Airflow 运行不需要，避免注入 root 凭据",
    )
    args = ap.parse_args()

    if args.legacy_flat_raw and not args.observe_date:
        raise SystemExit(
            "[etl] --legacy-flat-raw 必须配 --observe-date <真实抓取日>："
            "扁平历史文件的观测日无法从文件本身推出（mtime 会被复制/挂载改写），"
            "默认成今天就是把 last_seen_date 重新变成 ETL 重跑日"
        )
    if args.observe_date and not args.legacy_flat_raw:
        raise SystemExit(
            "[etl] --observe-date 只对 --legacy-flat-raw 有意义（ds 目录名本身就是观测日）"
        )
    if args.backfill and not (args.date or args.observe_date):
        raise SystemExit(
            "[etl] --backfill 需要显式 --date（重跑哪个观测日就给它哪天；默认今天会重跑今天）"
        )
    date = args.observe_date or args.date or time.strftime("%Y-%m-%d")

    env = load_env()
    t0 = time.time()

    # ---- 初始化（仅显式 --init-db 时用 root；日常/Airflow 运行走 app 账号，不注入 root 凭据）----
    if args.init_db:
        init_db(env)
    params = _mysql_params(args.mysql_dsn, env)
    conn = pymysql.connect(**params, autocommit=False, charset="utf8mb4")
    # 列守卫必须在日常路径跑：schema 文件里的 CREATE TABLE IF NOT EXISTS 对已存在的表
    # 不会长出缺列，只靠 --init-db 补列 = 存量库永远没有 last_seen_page。
    _ensure_columns(conn)

    # geocoder：优先 DbGeocoder（community_coords 表词典，跨城隔离+单小区只查一次）；
    # 兼容旧文件模式 LocalGeocoder（--geocoder-db 传入 JSON 时回退）
    geocoder = None
    if args.geocoder_db and os.path.exists(args.geocoder_db):
        lg = LocalGeocoder()
        try:
            with open(args.geocoder_db, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if isinstance(v, list) and len(v) == 2:
                        lg.add(k, v[0], v[1])
            geocoder = lg
        except Exception as e:
            log(f"WARN geocoder 词典加载失败: {args.geocoder_db} ({type(e).__name__}: {e})")
    if geocoder is None:
        geocoder = DbGeocoder(conn, CITY_NAMES)
        geocoder.load_all()
        print(f"[etl] geocoder=DbGeocoder（community_coords 表，缓存 {len(geocoder._cache)} 条）")

    # ---- 增量识别 ----
    files, skipped, state = collect_files(
        args.raw_dir, args.out_dir, args.backfill, date, args.legacy_flat_raw
    )
    scanned = (
        args.raw_dir if args.legacy_flat_raw else os.path.join(args.raw_dir, DS_DIR_PREFIX + date)
    )
    log(
        f"处理文件 {len(files)}（跳过已处理 {skipped}）观测日={date} 扫描={scanned}"
        + (" backfill" if args.backfill else "")
        + (" legacy-flat" if args.legacy_flat_raw else "")
    )

    # ---- 读取 + 清洗 + 去重 ----
    rows_by_task, raw_count = load_rows(files, args.raw_dir)
    miss = {}
    unique_by_task = {}
    for key, rows in sorted(rows_by_task.items()):
        unique_by_task[key] = dedup(rows)
    geocode_attempted, geocode_hit = geocode_buckets(unique_by_task, geocoder, miss)
    unique_total = sum(len(v) for v in unique_by_task.values())
    log(
        f"raw {raw_count} 行 → 去重后 {unique_total}（重复率 "
        f"{round((raw_count - unique_total) / raw_count, 4) if raw_count else 0}）"
    )

    # ---- 分任务 upsert DWD ----
    t_upsert0 = time.time()
    source = "backfill" if args.backfill else "daily"
    inc = {"sale": {"inserted": 0, "updated": 0}, "rent": {"inserted": 0, "updated": 0}}
    first_seen: dict = {}
    for (_city, typ), rows in unique_by_task.items():
        res = upsert_dwd(conn, typ, rows, date, source)
        inc[typ]["inserted"] += res["inserted"]
        inc[typ]["updated"] += res["updated"]
        # 复合键：sale/rent 是两张表，同一 url_key 可能两边都有行且首见日不同
        first_seen.update({(typ, k): v for k, v in res["first_seen"].items()})
    t_upsert1 = time.time()

    # ---- DWD 补全已从 ETL 解耦（见 docs/tech/components/crawler-etl.md）----
    # 补全由独立 geocode_backfill.py 负责（读 community_coords 词典 → UPDATE DWD null 行），
    # ETL 只保证数据落库 + 记录 geocode_status 状态。此处不再内联 geocode_dwd_nulls。

    # ---- 批量提交 geocoder 累积的 pending 登记 / hit 计数 ----
    if hasattr(geocoder, "flush"):
        geocoder.flush()

    # ---- ODS 湖 ----
    t_lake0 = time.time()
    lake_rows = write_lake(args.lake_dir, date, unique_by_task, source, first_seen)
    t_lake1 = time.time()

    # ---- 标记已处理 ----
    for rel in files:
        mark_processed(state, rel, args.raw_dir)
    _save_state(args.out_dir, state)

    # ---- 累计/留存/报告 ----
    counts = {t: dwd_counts(conn, t) for t in ("sale", "rent")}
    retention = {t: dwd_retention_buckets(conn, t) for t in ("sale", "rent")}
    city_dist = {t: dwd_city_counts(conn, t) for t in ("sale", "rent")}
    days_stats = {t: dwd_days_stats(conn, t) for t in ("sale", "rent")}

    elapsed = time.time() - t0
    # 没有页码证据的行 = 无法证明「今天真的扫到过它」，闸门据此把 refreshed 降级为未知
    rows_missing_page = sum(
        1 for rows in unique_by_task.values() for r in rows if not r.get("last_seen_page")
    )
    report = {
        "date": date,
        "mode": (
            "legacy-flat"
            if args.legacy_flat_raw
            else ("backfill" if args.backfill else "incremental")
        ),
        "input": {
            "scanned_dir": scanned,
            "raw_count": raw_count,
            "files_processed": len(files),
            "files_skipped": skipped,
            "unique_total": unique_total,
            "rows_missing_page": rows_missing_page,
            "dup_rate": round((raw_count - unique_total) / raw_count, 4) if raw_count else 0,
        },
        "incremental": {
            "sale": inc["sale"],
            "rent": inc["rent"],
            "total_inserted": inc["sale"]["inserted"] + inc["rent"]["inserted"],
            "total_updated": inc["sale"]["updated"] + inc["rent"]["updated"],
        },
        "stock": {
            "sale_total": counts["sale"],
            "rent_total": counts["rent"],
            "city_distribution": city_dist,
        },
        "retention": retention,
        "days_on_market_stats": days_stats,
        "geocode": {
            "attempted": geocode_attempted,
            "hit": geocode_hit,
            "hit_rate": round(geocode_hit / geocode_attempted, 4) if geocode_attempted else 0,
            "miss_communities": len(miss),
        },
        "lake": {"dt": date, "rows": lake_rows, "dir": args.lake_dir},
        "performance": {
            "elapsed_sec": round(elapsed, 2),
            "throughput_rows_per_sec": round(unique_total / elapsed, 2) if elapsed else 0,
            "upsert_sec": round(t_upsert1 - t_upsert0, 2),
            "geocode_dwd_nulls_sec": round(t_lake0 - t_upsert1, 2),
            "lake_sec": round(t_lake1 - t_lake0, 2),
        },
    }

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "etl_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    conn.close()
    print("=" * 60)
    print("[etl] 完成")
    print(f"  扫描 {scanned}（观测日 {date}，mode={report['mode']}）")
    print(f"  sale 库存: {counts['sale']} / rent 库存: {counts['rent']}")
    print(
        f"  新增 {report['incremental']['total_inserted']} / 更新 {report['incremental']['total_updated']}"
    )
    print(f"  无页码证据的行: {rows_missing_page} / {unique_total}（这些行的 last_seen_page=NULL）")
    if unique_total and rows_missing_page == unique_total:
        print(
            "  WARN 本批全部行都没有页码证据 → 只能证明 ETL 读到了它们，"
            "不能证明今天在架；闸门按未知处理（见 crawl_quality_gate.count_refreshed_rows）"
        )
    print(f"  geocode 命中率: {report['geocode']['hit_rate']} (miss {len(miss)})")
    print(f"  湖分区行: {lake_rows} @ {args.lake_dir}/dt={date}")
    print(f"  耗时 {elapsed:.1f}s，报告: {os.path.join(args.out_dir, 'etl_report.json')}")


if __name__ == "__main__":
    main()
