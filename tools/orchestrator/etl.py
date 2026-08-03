#!/usr/bin/env python3
"""
ETL 脚本：采集 raw JSONL → 清洗 → url_key 去重 → geocode → MySQL DWD 主表 + ODS 数据湖 Parquet。

运行形态（已确认决策，见 docs/tech/components/crawler-etl.md）：
- 宿主机 venv 直接跑，Airflow DAG 用 BashOperator 调本脚本（不容器化）
- 增量：只处理"未处理过 或 mtime+size 变化"的 raw 文件，靠 etl_processed.json 记录
- backfill：--backfill 全量扫（与 --date 互斥），first_seen=last_seen=运行日，同样写 etl_processed.json
- 去重键：url_key（URL 规范化房源 ID，见 url_key.py），MySQL 唯一主键 + COALESCE 只更新非空/非零字段
- 留存：first_seen_date 保留、last_seen_date 更新、days_on_market = last_seen - first_seen
- 输出：spacefin_crawler 库两张 DWD 表 + data_lake/housing/dt=.. 每日观测集 Parquet + etl_report.json
- 无 CSV 输出（单一数据出口）

用法：
    python etl.py --date 2026-08-04 --raw-dir output/guangdong/raw --lake-dir data_lake/housing
    python etl.py --backfill --raw-dir output/guangdong/raw --lake-dir data_lake/housing
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
    """读仓库根 .env（仅取 MYSQL_* 相关键，不写回）。"""
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


def _ensure_geocode_status(conn) -> None:
    """幂等守卫：若两 DWD 表缺 geocode_status 列则 ALTER 补（存量/部分恢复环境收敛）。"""
    for tbl in ("crawl_housing_sale", "crawl_housing_rent"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name='geocode_status'",
                (CRAWL_DB, tbl),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    f"ALTER TABLE {CRAWL_DB}.{tbl} ADD COLUMN geocode_status VARCHAR(16) NULL "
                    f"COMMENT '坐标补全状态 pending/hit/miss'"
                )
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
        _ensure_geocode_status(conn)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# 增量识别（etl_processed.json）
# ----------------------------------------------------------------------------

STATE_FILE = "etl_processed.json"


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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _state_path(out_dir))


def collect_files(raw_dir: str, out_dir: str, backfill: bool):
    """按 etl_processed.json 选出待处理文件。backfill 忽略状态全量返回。

    返回 (to_process, skipped_count, new_state)。
    """
    state = _load_state(out_dir)
    to_process = []
    skipped = 0
    if not os.path.isdir(raw_dir):
        raise SystemExit(f"[etl] raw 目录不存在: {raw_dir}")
    for fname in sorted(os.listdir(raw_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(raw_dir, fname)
        st = os.stat(fpath)
        sig = (st.st_mtime, st.st_size)
        prev = state.get(fname)
        if not backfill and prev and abs(prev["mtime"] - sig[0]) < 1 and prev["size"] == sig[1]:
            skipped += 1
            continue
        to_process.append(fname)
    return to_process, skipped, state


def mark_processed(state: dict, fname: str, fpath: str) -> None:
    st = os.stat(fpath)
    state[fname] = {"mtime": st.st_mtime, "size": st.st_size, "processed_at": time.time()}


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


def load_rows(fnames: list, raw_dir: str) -> tuple[dict, int]:
    """读待处理文件 → {(city,type): [cleaned rows]}，返回 (rows_by_task, raw_count)。"""
    rows_by_task = {}
    raw_count = 0
    for fname in fnames:
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
            log(f"跳过非法文件名（type 不在 sale/fangyuan/rent）: {fname}")
            continue
        # 城市码白名单（CITY_NAMES 与 anjuke_crawler.geocoder 同源）：防伪城市码入库/生成湖分区
        if city not in CITY_NAMES:
            log(f"跳过非法文件名（city 不在 CITY_NAMES 白名单）: {fname}")
            continue
        fpath = os.path.join(raw_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError as e:
                    log(f"跳过坏 JSON 行: {fname}:{lineno} ({e})")
                    continue
                r.pop("_source", None)
                rows_by_task.setdefault(key, []).append(clean_row(r, key[1]))
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
        elif c == "days_on_market":
            updates.append("days_on_market = DATEDIFF(VALUES(last_seen_date), first_seen_date)")
        else:
            updates.append(f"{c} = COALESCE(VALUES({c}), {c})")
    return (
        f"INSERT INTO {table} ({coldef}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(updates)
    )


def _existing_keys(conn, typ: str, keys: list, batch=1000) -> set:
    """返回库中已存在的 url_key 集合（分批 IN 查询）。"""
    if not keys:
        return set()
    found = set()
    table = _TABLE[typ]
    with conn.cursor() as cur:
        for i in range(0, len(keys), batch):
            chunk = keys[i : i + batch]
            marks = ",".join(["%s"] * len(chunk))
            cur.execute(f"SELECT url_key FROM {table} WHERE url_key IN ({marks})", chunk)
            found.update(r[0] for r in cur.fetchall())
    return found


def upsert_dwd(conn, typ: str, rows: list, date: str, source: str) -> dict:
    """批量 upsert 去重后的房源行。返回 {inserted, updated}。"""
    if not rows:
        return {"inserted": 0, "updated": 0}
    headers = _HEADERS[typ]
    cols = (
        ["url_key", "url"]
        + [c for c in headers if c != "url"]
        + [
            "first_seen_date",
            "last_seen_date",
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
                [key, url] + [r.get(c) for c in headers if c != "url"] + [date, date, 0, gs, source]
            )
        )

    existing = _existing_keys(conn, typ, keys)
    inserted = updated = 0
    batch = 5000
    with conn.cursor() as cur:
        for i in range(0, len(values), batch):
            cur.executemany(sql, values[i : i + batch])
        # ROW_COUNT 在 executemany 下不可靠，改按库前后差值无法精分；用 IN 结果区分
    for k in keys:
        if k in existing:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated}


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


def write_lake(lake_dir: str, date: str, rows_by_task: dict, source: str) -> int:
    """写当日观测集 Parquet 分区（dt/type/city）。重跑只覆盖本次涉及的子分区（幂等）。

    观测集按 url_key 全局唯一（与 DWD 主键语义一致），跨任务重复只保留首现。
    按 (type, city) 分组独立成表写：既避免 sale/rent 混写丢字段，也保证增量 run
    不会误删当日其他 type/city 的分区（否则当日全量观测集会被削成增量子集）。
    """
    if not rows_by_task:
        return 0

    seen = set()
    groups = {}
    for (city, typ), rows in rows_by_task.items():
        for r in rows:
            url = r.get("url") or ""
            key = make_url_key(url)
            if key in seen:
                continue
            seen.add(key)
            rec = {"url_key": key, "url": url}
            rec.update({c: r.get(c) for c in _HEADERS[typ] if c != "url"})
            rec.update({"first_seen_date": date, "last_seen_date": date, "source": source})
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
    ap.add_argument("--backfill", action="store_true", help="首次全量处理（与 --date 互斥）")
    ap.add_argument("--date", default=None, help="落盘日期 YYYY-MM-DD（增量用 DAG 执行日）")
    args = ap.parse_args()

    if args.backfill and args.date:
        raise SystemExit("[etl] --backfill 与 --date 互斥")
    date = args.date or time.strftime("%Y-%m-%d")

    env = load_env()
    t0 = time.time()

    # ---- 初始化（root 幂等，仅初始化用）----
    init_db(env)
    params = _mysql_params(args.mysql_dsn, env)
    conn = pymysql.connect(**params, autocommit=False, charset="utf8mb4")

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
    fnames, skipped, state = collect_files(args.raw_dir, args.out_dir, args.backfill)
    log(
        f"处理文件 {len(fnames)}（跳过已处理 {skipped}）date={date}"
        + (" backfill" if args.backfill else "")
    )

    # ---- 读取 + 清洗 + 去重 ----
    rows_by_task, raw_count = load_rows(fnames, args.raw_dir)
    miss = {}
    unique_by_task = {}
    geocode_attempted = geocode_hit = 0
    for key, rows in sorted(rows_by_task.items()):
        unique = dedup(rows)
        unique_by_task[key] = unique
        a, h = geocode_rows(unique, geocoder, miss, city=key[0])
        geocode_attempted += a
        geocode_hit += h
    unique_total = sum(len(v) for v in unique_by_task.values())
    log(
        f"raw {raw_count} 行 → 去重后 {unique_total}（重复率 "
        f"{round((raw_count - unique_total) / raw_count, 4) if raw_count else 0}）"
    )

    # ---- 分任务 upsert DWD ----
    t_upsert0 = time.time()
    source = "backfill" if args.backfill else "daily"
    inc = {"sale": {"inserted": 0, "updated": 0}, "rent": {"inserted": 0, "updated": 0}}
    for (_city, typ), rows in unique_by_task.items():
        res = upsert_dwd(conn, typ, rows, date, source)
        inc[typ]["inserted"] += res["inserted"]
        inc[typ]["updated"] += res["updated"]
    t_upsert1 = time.time()

    # ---- DWD 补全已从 ETL 解耦（见 docs/tech/components/crawler-etl.md）----
    # 补全由独立 geocode_backfill.py 负责（读 community_coords 词典 → UPDATE DWD null 行），
    # ETL 只保证数据落库 + 记录 geocode_status 状态。此处不再内联 geocode_dwd_nulls。

    # ---- 批量提交 geocoder 累积的 pending 登记 / hit 计数 ----
    if hasattr(geocoder, "flush"):
        geocoder.flush()

    # ---- ODS 湖 ----
    t_lake0 = time.time()
    lake_rows = write_lake(args.lake_dir, date, unique_by_task, source)
    t_lake1 = time.time()

    # ---- 标记已处理 ----
    for fname in fnames:
        mark_processed(state, fname, os.path.join(args.raw_dir, fname))
    _save_state(args.out_dir, state)

    # ---- 累计/留存/报告 ----
    counts = {t: dwd_counts(conn, t) for t in ("sale", "rent")}
    retention = {t: dwd_retention_buckets(conn, t) for t in ("sale", "rent")}
    city_dist = {t: dwd_city_counts(conn, t) for t in ("sale", "rent")}
    days_stats = {t: dwd_days_stats(conn, t) for t in ("sale", "rent")}

    elapsed = time.time() - t0
    report = {
        "date": date,
        "mode": "backfill" if args.backfill else "incremental",
        "input": {
            "raw_count": raw_count,
            "files_processed": len(fnames),
            "files_skipped": skipped,
            "unique_total": unique_total,
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
    print(f"  sale 库存: {counts['sale']} / rent 库存: {counts['rent']}")
    print(
        f"  新增 {report['incremental']['total_inserted']} / 更新 {report['incremental']['total_updated']}"
    )
    print(f"  geocode 命中率: {report['geocode']['hit_rate']} (miss {len(miss)})")
    print(f"  湖分区行: {lake_rows} @ {args.lake_dir}/dt={date}")
    print(f"  耗时 {elapsed:.1f}s，报告: {os.path.join(args.out_dir, 'etl_report.json')}")


if __name__ == "__main__":
    main()
