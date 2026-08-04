#!/usr/bin/env python
"""SpaceFin CDC（I-01）：MySQL 业务库 binlog → ODS 贴源层（1 分钟级可见）。

- 来源：spacefin 库 loan/collateral/customer（业务信贷源）
- 输出：
  - 数据湖 ODS：data_lake/cdc/ods/{table}/dt={date}/{seq}.jsonl（逐事件追加，before/after 全量）
  - MySQL 日志表：spacefin_crawler.ods_cdc_log（可 SQL 查询，1 分钟内可见）
- 断点续跑：resume_stream=True（位置持久化在 ~/.mysql_replication，重启续读）
- 用法：python tools/cdc/main.py --once / python tools/cdc/main.py（阻塞常驻）
"""

import argparse
import json
import os
import time
from datetime import date, datetime
from decimal import Decimal

import pymysql
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ODS_ROOT = os.path.join(REPO_ROOT, "data_lake", "cdc", "ods")
TABLES = ["loan", "collateral", "customer"]
LOG_TABLE = "ods_cdc_log"


def load_env():
    env = {}
    p = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _fix_str(v):
    """修复库按 latin-1 解码 utf8mb4 字节导致的乱码。

    安全规则：纯 ASCII 反转后不变；已正确解码的非 ASCII（如中文）encode('latin-1') 会抛错 → 原样返回；
    只有真正被 latin-1 错解码的字符串才能反转恢复为 UTF-8。
    """
    try:
        return v.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v


def _sanitize(o, _seen=None):
    """把复刻库行值递归转换为可 JSON 序列化的原始类型；遇循环引用打标而非抛错。"""
    if _seen is None:
        _seen = set()
    if id(o) in _seen:
        return f"<circular:{type(o).__name__}>"
    if isinstance(o, dict):
        _seen.add(id(o))
        out = {str(k): _sanitize(v, _seen) for k, v in o.items()}
        _seen.discard(id(o))
        return out
    if isinstance(o, (list, tuple)):
        _seen.add(id(o))
        out = [_sanitize(x, _seen) for x in o]
        _seen.discard(id(o))
        return out
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "ignore")
    if isinstance(o, str):
        return _fix_str(o)
    return o


def _column_names(conn, schema, table):
    """按 ORDINAL_POSITION 取表列名，用于把库回退的 UNKNOWN_COL* 映射为真实列名。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


def _rename_cols(row: dict, names: list) -> dict:
    """把 UNKNOWN_COL<i> 键映射为真实列名（库未提供元数据时的兜底）。"""
    if not row:
        return row
    out = {}
    for k, v in row.items():
        if isinstance(k, str) and k.startswith("UNKNOWN_COL"):
            idx = int(k.replace("UNKNOWN_COL", ""))
            out[names[idx] if idx < len(names) else k] = v
        else:
            out[k] = v
    return out


def _ensure_log_table(conn):
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            table_name VARCHAR(32), event_type VARCHAR(16),
            before_json TEXT NULL, after_json TEXT NULL,
            cdc_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_table_ts (table_name, cdc_ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def _write_ods_lake(table, event_type, before, after):
    d = datetime.now().strftime("%Y-%m-%d")
    seq = int(time.time() * 1000)
    dpath = os.path.join(ODS_ROOT, table, f"dt={d}")
    os.makedirs(dpath, exist_ok=True)
    fpath = os.path.join(dpath, f"{seq}.jsonl")
    rec = {
        "event_type": event_type,
        "before": before,
        "after": after,
        "ts": datetime.now().isoformat(),
    }
    with open(fpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return fpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="读到当前 binlog 位置后退出（用于校验）")
    args = ap.parse_args()

    env = load_env()
    host = env.get("MYSQL_HOST", "127.0.0.1")
    port = int(env.get("MYSQL_PORT", "3306"))
    user = env.get("MYSQL_CDC_USER", "spacefin_cdc")
    pwd = env.get("MYSQL_CDC_PASSWORD", "spacefin_cdc_dev")
    out_dbname = "spacefin_crawler"

    # 日志表（root 建表，避免 app 无 DDL）
    root = {
        "host": host,
        "port": port,
        "user": "root",
        "password": env.get("MYSQL_ROOT_PASSWORD", ""),
        "database": out_dbname,
    }
    wconn = pymysql.connect(**root, charset="utf8mb4")
    _ensure_log_table(wconn)

    stream = BinLogStreamReader(
        connection_settings={
            "host": host,
            "port": port,
            "user": user,
            "passwd": pwd,
            "charset": "utf8mb4",
        },
        server_id=1002,
        blocking=True,
        only_schemas=["spacefin"],
        only_tables=TABLES,
        resume_stream=True,
    )
    # 勿用 only_events 过滤行事件（TableMapEvent 会被跳过，列名解析失效）。
    # 列名：库在本环境下回退为 UNKNOWN_COL*，由 _column_names/_rename_cols 用 INFORMATION_SCHEMA 兜底映射。
    print(
        f"[cdc] streaming spacefin.{','.join(TABLES)} -> ODS lake + {out_dbname}.{LOG_TABLE}",
        flush=True,
    )

    col_names = {t: _column_names(wconn, "spacefin", t) for t in TABLES}

    count = 0
    try:
        for event in stream:
            if not isinstance(event, (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent)):
                continue  # RotateEvent/FormatDescriptionEvent 等非行事件跳过
            table = event.table
            if table not in TABLES:
                continue
            if isinstance(event, WriteRowsEvent):
                for r in event.rows:
                    after = _sanitize(_rename_cols(r.get("values"), col_names[table]))
                    _write_ods_lake(table, "INSERT", None, after)
                    _insert_log(wconn, table, "INSERT", None, after)
                    count += 1
            elif isinstance(event, UpdateRowsEvent):
                for r in event.rows:
                    before = _sanitize(_rename_cols(r.get("before_values"), col_names[table]))
                    after = _sanitize(_rename_cols(r.get("after_values"), col_names[table]))
                    _write_ods_lake(table, "UPDATE", before, after)
                    _insert_log(wconn, table, "UPDATE", before, after)
                    count += 1
            elif isinstance(event, DeleteRowsEvent):
                for r in event.rows:
                    before = _sanitize(_rename_cols(r.get("values"), col_names[table]))
                    _write_ods_lake(table, "DELETE", before, None)
                    _insert_log(wconn, table, "DELETE", before, None)
                    count += 1
            if count % 100 == 0:
                print(f"[cdc] processed {count} events", flush=True)
            if args.once:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        wconn.close()
    print(f"[cdc] done, {count} events", flush=True)


def _insert_log(conn, table, event_type, before, after):
    cur = conn.cursor()
    try:
        cur.execute(
            f"INSERT INTO {LOG_TABLE} (table_name, event_type, before_json, after_json) VALUES (%s,%s,%s,%s)",
            (
                table,
                event_type,
                json.dumps(before, ensure_ascii=False, default=str) if before else None,
                json.dumps(after, ensure_ascii=False, default=str) if after else None,
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[cdc] log insert error: {e}", flush=True)
    finally:
        cur.close()


if __name__ == "__main__":
    main()
