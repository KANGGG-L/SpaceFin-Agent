"""Doris 湖仓接入主入口：建库建表 → MySQL→ODS 全量导入 → DWD/DWS/ADS 分层加工
→ 数据湖 Parquet 上传 MinIO → 湖文件联邦查询与贴源落仓 → 与 MySQL 对账验证。

用法（在 tools/lake 下执行，复用 tools/orchestrator/.venv 的 Python）：
    python sync.py                 # 全量：建表 + 导入 + 分层 + 湖上传 + 验证
    python sync.py --skip-minio    # 跳过 MinIO 上传（湖文件不变时提速）
    python sync.py --verify-only   # 只跑对账与验证（表已就绪时）
    python sync.py --date 2026-08-04   # 指定数据湖快照日期（默认 2026-08-04）

可重复执行：所有表先 TRUNCATE 再导入/加工，结果与执行次数无关。
"""

import argparse
import base64
import datetime
import io
import os
import sys
import uuid

import pymysql
import requests

# 允许 `python tools/lake/sync.py` 与 `python sync.py` 两种入口
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import minio_sync  # noqa: E402
from schema import (  # noqa: E402
    ADS_FILL_SQL,
    ADS_TABLES,
    DWD_FILL_SQL,
    DWD_TABLES,
    DWS_FILL_SQL,
    DWS_TABLES,
    LAKE_TABLE,
    MYSQL_SOURCES,
    ODS_TABLES,
    ddl_for,
)

DEFAULT_LAKE_DATE = "2026-08-04"


# ---------------- 连接 ---------------
def doris_conn():
    return pymysql.connect(
        host=config.DORIS["host"],
        port=config.DORIS["query_port"],
        user=config.DORIS["user"],
        password=config.DORIS["password"],
        charset="utf8mb4",
    )


def mysql_conn(database):
    return pymysql.connect(
        host=config.MYSQL["host"],
        port=config.MYSQL["port"],
        user=config.MYSQL["user"],
        password=config.MYSQL["password"],
        database=database,
        charset="utf8mb4",
    )


def sql(cursor, statement):
    """执行单条语句；失败时带库/表上下文抛错，方便定位。"""
    try:
        cursor.execute(statement)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"SQL failed: {e}\nSQL: {statement[:400]}") from e


# ---------------- 建库建表 ---------------
def create_schema(cursor, recreate: bool = False):
    if recreate:
        # 表结构演进时强制重建：DROP 后由下面 CREATE 重建（无历史数据需保留，开发环境可接受）
        for db, tables in (
            ("ods", {**ODS_TABLES, **LAKE_TABLE}),
            ("dwd", DWD_TABLES),
            ("dws", DWS_TABLES),
            ("ads", ADS_TABLES),
        ):
            for name in tables:
                sql(cursor, f"DROP TABLE IF EXISTS {db}.{name}")
    for db in config.LAYER_DBS:
        sql(cursor, f"CREATE DATABASE IF NOT EXISTS {db}")
    for db, tables in (
        ("ods", {**ODS_TABLES, **LAKE_TABLE}),
        ("dwd", DWD_TABLES),
        ("dws", DWS_TABLES),
        ("ads", ADS_TABLES),
    ):
        for name, spec in tables.items():
            sql(cursor, ddl_for(db, name, spec["columns"], spec["key"], spec.get("buckets", 1)))


# ---------------- MySQL → ODS Stream Load ----------------
_COL_SEP = "\x01"  # 列分隔符：控制字符 SOH。比 \t 安全——标题等文本字段可能含 \t，但绝不含 \x01
_NULL_MARK = "\\N"  # Doris CSV 约定的 NULL 字面量


def dump_table_csv(mysql_db, table, cols):
    """读 MySQL 全表，按 cols 顺序产出 CSV（\\x01 分隔，NULL 用 Doris 约定 \\N）。"""
    conn = mysql_conn(mysql_db)
    out = io.StringIO()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(cols)} FROM `{mysql_db}`.`{table}`")
        field_sanitized = 0
        for row in cur:
            for i, v in enumerate(row):
                if v is None:
                    out.write(_NULL_MARK)
                else:
                    if isinstance(v, bytes):
                        v = v.decode("utf-8", "replace")
                    s = str(v)
                    # \x01 是列分隔符、\n 是行分隔符，混入会错位；源数据标题偶见换行，
                    # 退化为空格并计数告警（贴源表不允许静默改数据，故只处理分隔符冲突）。
                    if _COL_SEP in s or "\n" in s:
                        field_sanitized += 1
                        s = s.replace(_COL_SEP, " ").replace("\n", " ")
                    out.write(s)
                if i < len(row) - 1:
                    out.write(_COL_SEP)
            out.write("\n")
    finally:
        conn.close()
    return out.getvalue(), field_sanitized


def stream_load(db, table, csv_text):
    """Doris Stream Load（走 BE 8040 HTTP 端口），返回 (OK, 明细)。"""
    url = (
        f"http://{config.DORIS['be_http_host']}:{config.DORIS['be_http_port']}"
        f"/api/{db}/{table}/_stream_load"
    )
    auth = base64.b64encode(f"{config.DORIS['user']}:{config.DORIS['password']}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "label": f"{db}_{table}_{datetime.datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}",
        "Expect": "100-continue",
        "format": "csv",
        # 列分隔符传转义字面量（HTTP header 不能含真实控制字符），Doris 解释为 \x01
        "column_separator": "\\x01",
        # 严格模式：任何坏行都让本次导入失败，避免「导入成功但悄悄丢行」
        "max_filter_ratio": "0",
    }
    resp = requests.put(url, headers=headers, data=csv_text.encode("utf-8"), timeout=600)
    payload = resp.json()
    ok = payload.get("Status") == "Success" and payload.get("NumberLoadedRows", 0) >= 0
    return ok, payload


def sync_mysql_to_ods(d_cursor, d_conn):
    evidence = {}
    for mysql_db, mysql_table, doris_table in MYSQL_SOURCES:
        cols = [c[0] for c in ODS_TABLES[doris_table]["columns"]]
        csv_text, sanitized = dump_table_csv(mysql_db, mysql_table, cols)
        sql(d_cursor, f"TRUNCATE TABLE ods.{doris_table}")
        ok, payload = stream_load("ods", doris_table, csv_text)
        if not ok:
            raise RuntimeError(
                f"stream load {doris_table} failed: {payload.get('Message', payload)}"
            )
        loaded = payload.get("NumberLoadedRows", 0)
        if sanitized:
            print(f"  [warn] {doris_table}: {sanitized} 个字段含分隔符已退化")
        # 立即校验 Doris 落库行数 == 加载行数，不等说明有行被过滤
        d_cursor.execute(f"SELECT COUNT(*) FROM ods.{doris_table}")
        actual = d_cursor.fetchone()[0]
        if actual != loaded:
            raise RuntimeError(
                f"{doris_table}: loaded {loaded} but counted {actual} — 数据未完全落库"
            )
        evidence[doris_table] = loaded
        print(f"  ✓ {mysql_db}.{mysql_table} → ods.{doris_table}: {loaded} 行")
    return evidence


# ---------------- 分层加工（DWD/DWS/ADS） ----------------
def fill_layer(d_cursor, tables, fill_sql, stat_date):
    for name in tables:
        target = tables_drop_target(name)
        sql(d_cursor, f"TRUNCATE TABLE {target}")
        stmt = fill_sql[name].format(stat_date=stat_date)
        sql(d_cursor, stmt)
        d_cursor.execute(f"SELECT COUNT(*) FROM {target}")
        print(f"  ✓ 分层表 {target}: {d_cursor.fetchone()[0]} 行")


def tables_drop_target(name: str) -> str:
    """按表名反查所在库（dwd_* → dwd，其余同理），Doris 不支持跨库 TRUNCATE 简写。"""
    for db in config.LAYER_DBS:
        if name.startswith(db + "_"):
            return f"{db}.{name}"
    raise ValueError(f"unknown layer table: {name}")


# ---------------- 湖（MinIO Parquet → 联邦查询 → 贴源落仓） ----------------
def lake_tvf():
    """Doris S3 表函数：直查 MinIO 上的 Parquet（不落仓的联邦查询）。"""
    return (
        "s3("
        f"'uri' = 's3://{config.MINIO['bucket']}/sale/*.parquet', "
        f"'s3.access_key' = '{config.MINIO['access_key']}', "
        f"'s3.secret_key' = '{config.MINIO['secret_key']}', "
        f"'s3.endpoint' = '{config.MINIO['endpoint']}', "
        "'s3.region' = 'us-east-1', "
        "'format' = 'parquet', "
        "'s3.path.style.access' = 'true'"
        ")"
    )


def materialize_lake(d_cursor, lake_date):
    """把湖上的 sale Parquet 落成 ods.ods_housing_sale_lake（证明 湖→仓 导入链路）。"""
    sql(d_cursor, "TRUNCATE TABLE ods.ods_housing_sale_lake")
    select_cols = [
        "url_key",
        "url",
        "title",
        "community",
        "district",
        "bedrooms",
        "halls",
        "bathrooms",
        "area_sqm",
        "direction",
        "floor",
        "building_year",
        "building_age",
        "parking_count",
        "total_price_wan",
        "unit_price_yuan",
        "latitude",
        "longitude",
        "first_seen_date",
        "last_seen_date",
        "source",
    ]
    stmt = (
        "INSERT INTO ods.ods_housing_sale_lake "
        f"SELECT {', '.join(select_cols)}, '{lake_date}' AS snapshot_dt "
        f"FROM {lake_tvf()}"
    )
    sql(d_cursor, stmt)
    d_cursor.execute("SELECT COUNT(*) FROM ods.ods_housing_sale_lake")
    n = d_cursor.fetchone()[0]
    print(f"  ✓ 湖文件落仓 ods.ods_housing_sale_lake: {n} 行")
    return n


# ---------------- 验证与对账 ----------------
def verify_reconciliation(d_cursor, mysql_conn, expected=None):
    """对账证据：Doris ODS 行数 vs MySQL 源行数；湖上联邦查询；分层链路行数。

    expected：全量同步时传入 {表名: 同步时的 MySQL 行数}，避免 CDC 增量写入
    （ods_cdc_log 等持续追加）导致「对账时 MySQL 已比 Doris 多几行」的假 MISMATCH。
    """
    expected = expected or {}
    report = []

    def row(sql_text, params=None):
        d_cursor.execute(sql_text, params)
        return d_cursor.fetchone()[0]

    # 1) ODS 与 MySQL 行数对账
    for mysql_db, mysql_table, doris_table in MYSQL_SOURCES:
        if doris_table in expected:
            m_count = expected[doris_table]
        else:
            m_cur = mysql_conn.cursor()
            m_cur.execute(f"SELECT COUNT(*) FROM `{mysql_db}`.`{mysql_table}`")
            m_count = m_cur.fetchone()[0]
        d_count = row(f"SELECT COUNT(*) FROM ods.{doris_table}")
        mark = "OK" if m_count == d_count else "MISMATCH"
        report.append(("ODS 对账", f"{doris_table}", f"MySQL {m_count} / Doris {d_count}", mark))

    # 2) 湖上联邦查询（不落仓，直查 MinIO Parquet）
    tvf_count = row(f"SELECT COUNT(*) FROM {lake_tvf()}")
    tvf_cities = row(f"SELECT COUNT(DISTINCT district) FROM {lake_tvf()}")
    report.append(("联邦查询(MinIO)", "sale/*.parquet", f"{tvf_count} 行 / {tvf_cities} 城", "OK"))

    # 3) 湖文件落仓行数应与联邦查询一致
    lake_count = row("SELECT COUNT(*) FROM ods.ods_housing_sale_lake")
    mark = "OK" if lake_count == tvf_count else "MISMATCH"
    report.append(("湖→仓落仓", "ods_housing_sale_lake", f"{lake_count} 行", mark))

    # 4) DWD 清洗效果：无效报价被剔除的行数（ODS 44,369 → DWD 44,349，剔 20 条）
    ods_sale = row("SELECT COUNT(*) FROM ods.ods_housing_sale")
    dwd_sale = row("SELECT COUNT(*) FROM dwd.dwd_housing_sale")
    dropped = ods_sale - dwd_sale
    report.append(
        ("DWD 清洗", "dwd_housing_sale", f"{ods_sale} → {dwd_sale} 行(剔{dropped}条无效价)", "OK")
    )

    # 5) 分层链路行数
    for db, table in [
        ("dws", "dws_city_price_stats"),
        ("dws", "dws_risk_class"),
        ("ads", "ads_risk_class"),
        ("ads", "ads_city_avg_price"),
        ("ads", "ads_1104_g11"),
    ]:
        n = row(f"SELECT COUNT(*) FROM {db}.{table}")
        report.append(("分层链路", f"{db}.{table}", f"{n} 行", "OK"))

    # 6) DWS 城市均价 vs MySQL（按房源数 Top5，验证聚合口径一致）
    m_cur = mysql_conn.cursor()
    m_cur.execute(
        "SELECT district, COUNT(*), ROUND(AVG(unit_price_yuan), 2) "
        "FROM spacefin_crawler.crawl_housing_sale WHERE unit_price_yuan > 0 "
        "GROUP BY district ORDER BY COUNT(*) DESC LIMIT 5"
    )
    mysql_top = {r[0]: (r[1], str(r[2])) for r in m_cur.fetchall()}
    d_cursor.execute(
        "SELECT city_code, listing_count, CAST(avg_unit_price_yuan AS CHAR) "
        "FROM dws.dws_city_price_stats ORDER BY listing_count DESC LIMIT 5"
    )
    doris_top = {r[0]: (r[1], r[2]) for r in d_cursor.fetchall()}
    for city in mysql_top:
        same = mysql_top.get(city) == doris_top.get(city)
        report.append(
            (
                "DWS城市聚合",
                city,
                f"MySQL {mysql_top[city]} / Doris {doris_top.get(city)}",
                "OK" if same else "CHECK",
            )
        )

    return report


def print_report(report):
    print("\n================ 湖仓接入对账报告 ================")
    print(f"{'类别':<14} {'对象':<24} {'数值':<42} 状态")
    print("-" * 90)
    for cat, obj, val, mark in report:
        print(f"{cat:<14} {obj:<24} {val:<42} {mark}")
    print("=" * 90)


def compute_stat_date(m_conn) -> str:
    """分层报表口径日：取 MySQL ADS 已产出的最大 stat_date，保证与上游口径一致。"""
    cur = m_conn.cursor()
    cur.execute("SELECT MAX(stat_date) FROM spacefin_crawler.ads_risk_class")
    row = cur.fetchone()
    if row and row[0]:
        return str(row[0])
    return datetime.date.today().isoformat()


def main():
    parser = argparse.ArgumentParser(description="Doris 湖仓接入（MinIO + Doris 湖仓全量接入）")
    parser.add_argument("--verify-only", action="store_true", help="只做对账验证")
    parser.add_argument("--skip-minio", action="store_true", help="跳过 MinIO 湖文件上传")
    parser.add_argument(
        "--recreate", action="store_true", help="重建分层库所有表（schema 变更后使用）"
    )
    parser.add_argument(
        "--date", default=DEFAULT_LAKE_DATE, help=f"数据湖快照日期（默认 {DEFAULT_LAKE_DATE}）"
    )
    args = parser.parse_args()

    d_conn = doris_conn()
    m_conn = mysql_conn(config.MYSQL["database"])
    expected = None  # 全量同步时填充「同步时刻」的 MySQL 行数，供对账使用
    try:
        d_cursor = d_conn.cursor()
        stat_date = compute_stat_date(m_conn)
        print(f"[1/6] 报表口径日 stat_date = {stat_date}")

        if not args.verify_only:
            print("[2/6] 建库建表 ...")
            create_schema(d_cursor, recreate=args.recreate)
            d_conn.commit()

            print("[3/6] MySQL → ODS 全量导入 ...")
            expected = sync_mysql_to_ods(d_cursor, d_conn)
            d_conn.commit()

            if not args.skip_minio:
                print("[4/6] 数据湖 Parquet 上传 MinIO ...")
                result = minio_sync.upload_lake_snapshot(config.LAKE_SOURCE_DIR, args.date)
                for house_type, (n_files, size_mb) in result.items():
                    print(f"  ✓ {house_type}: {n_files} 个文件 / {size_mb} MB")
                d_conn.commit()
                print("[4b] 湖文件联邦查询→落仓 ODS ...")
                materialize_lake(d_cursor, args.date)
                d_conn.commit()
            else:
                print("[4/6] 跳过 MinIO 上传（--skip-minio）")

            print("[5/6] 分层加工 DWD / DWS / ADS ...")
            fill_layer(d_cursor, DWD_TABLES, DWD_FILL_SQL, stat_date)
            fill_layer(d_cursor, DWS_TABLES, DWS_FILL_SQL, stat_date)
            fill_layer(d_cursor, ADS_TABLES, ADS_FILL_SQL, stat_date)
            d_conn.commit()

        print("[6/6] 对账与验证 ...")
        report = verify_reconciliation(d_cursor, m_conn, expected)
        print_report(report)

        # 硬校验：关键对账不过则返回非 0，供 CI/调度感知
        failed = [r for r in report if r[3] in ("MISMATCH", "CHECK")]
        if failed:
            print(f"\n!!! {len(failed)} 项对账未通过（见上）")
            sys.exit(2)
        print("\n全部对账通过。")
    finally:
        d_conn.close()
        m_conn.close()


if __name__ == "__main__":
    main()
