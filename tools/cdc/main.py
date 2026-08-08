#!/usr/bin/env python
"""SpaceFin CDC（I-01）：MySQL 业务库 binlog → ODS 贴源层（1 分钟级可见）。

- 来源：spacefin 库 loan/collateral/customer（业务信贷源）
- 输出：
  - 数据湖 ODS：data_lake/cdc/ods/{table}/dt={date}/{seq}.jsonl（逐事件追加，before/after 全量）
  - MySQL 日志表：spacefin_crawler.ods_cdc_log（可 SQL 查询，1 分钟内可见）
- 断点续跑：位点持久化到 spacefin_crawler.ods_cdc_position（本库实现，不用 ~/.mysql_replication）：
  每处理一个事件后把 stream.log_file/log_pos 落表，启动时读回并传给 BinLogStreamReader 续读，
  中断重启不丢事件（停机期间产生的新事件会在重启后补读）。为什么不用 pymysqlreplication 自带的
  resume_stream 文件：本版本库只把 resume_stream 当 COM_BINLOG_DUMP 的 flags 用，并不读写
  ~/.mysql_replication，故原代码从未真正持久化过位点（重启恒从 SHOW MASTER STATUS 当前头开始）。
- CDC 告警：常驻进程内监控线程每 60s 检测——(a) binlog 位点落后于主库且停滞 >30min（业务库有
  变更但 CDC 没跟上）；(b) consumer 消费水位落后 >1000 条。触发即写 ads_cdc_alert（root 幂等建表，
  同类型 30min 节流去重）。
- 用法：python tools/cdc/main.py --once / python tools/cdc/main.py（阻塞常驻）
"""

import argparse
import json
import os
import threading
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
POSITION_TABLE = "ods_cdc_position"
ALERT_TABLE = "ads_cdc_alert"
CONSUMER_NAME = "risk_downstream"

# 监控参数：60s 一轮；位点/消费滞后超过阈值才告警，同类型告警 30min 内去重，避免刷屏。
MONITOR_INTERVAL_SECONDS = 60
LAG_ALERT_WINDOW_SECONDS = 30 * 60
ALERT_THROTTLE_SECONDS = 30 * 60
CONSUMER_LAG_THRESHOLD = 1000


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


def _ensure_position_table(conn):
    """幂等建位点表（root DDL）。单行（repl_key='binlog'），记录已消费到的最新 binlog 位点。"""
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {POSITION_TABLE} (
            repl_key VARCHAR(32) PRIMARY KEY,
            log_file VARCHAR(64) NOT NULL,
            log_pos BIGINT NOT NULL DEFAULT 4,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def _ensure_alert_table(conn):
    """幂等建 CDC 告警表（root DDL）。与业务告警链路无关，只记录 CDC 自身的健康问题。"""
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ALERT_TABLE} (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            alert_type VARCHAR(32) NOT NULL,
            detail TEXT,
            alert_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            KEY idx_type_ts (alert_type, alert_ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    conn.commit()
    cur.close()


def read_position(conn):
    """读持久化位点；无记录返回 None（首启，从主库当前头开始）。"""
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT log_file, log_pos, updated_at FROM {POSITION_TABLE} WHERE repl_key='binlog'"
        )
        return cur.fetchone()
    finally:
        cur.close()


def write_position(conn, log_file, log_pos):
    """落位点（幂等 upsert）。必须在事件写完 ODS 湖/日志表之后再调用：
    先写数据后进位点，崩溃时最多重放最后一个事件（下游按 id 消费、重算幂等），不会丢事件。"""
    cur = conn.cursor()
    try:
        cur.execute(
            f"INSERT INTO {POSITION_TABLE} (repl_key, log_file, log_pos) VALUES ('binlog', %s, %s) "
            f"ON DUPLICATE KEY UPDATE log_file=VALUES(log_file), log_pos=VALUES(log_pos)",
            (log_file, log_pos),
        )
        conn.commit()
    finally:
        cur.close()


def master_position(conn):
    """主库当前 binlog 位点（SHOW MASTER STATUS）。用于判断业务库是否有已产生但未被消费的变更。"""
    cur = conn.cursor()
    try:
        cur.execute("SHOW MASTER STATUS")
        row = cur.fetchone()
        return (row[0], int(row[1])) if row else (None, 0)
    finally:
        cur.close()


def _pos_ahead(file_a, pos_a, file_b, pos_b):
    """判断 (file_a,pos_a) 是否严格领先 (file_b,pos_b)。binlog 文件名形如 binlog.000003，字典序即时间序。"""
    if not file_a or not file_b:
        return False
    if file_a != file_b:
        return file_a > file_b
    return pos_a > pos_b


def write_alert(conn, alert_type, detail):
    """写一条 CDC 告警；同一类型在节流窗口内已有记录则跳过，避免高频刷屏。"""
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT COUNT(*) FROM {ALERT_TABLE} "
            f"WHERE alert_type=%s AND alert_ts > NOW() - INTERVAL {ALERT_THROTTLE_SECONDS} SECOND",
            (alert_type,),
        )
        if cur.fetchone()[0]:
            return False
        cur.execute(
            f"INSERT INTO {ALERT_TABLE} (alert_type, detail) VALUES (%s, %s)",
            (alert_type, detail),
        )
        conn.commit()
        return True
    finally:
        cur.close()


def _check_alerts(conn):
    """单轮 CDC 健康检查：位点落后 / 消费水位滞后 → 写 ads_cdc_alert；返回本轮写入的告警类型。

    抽成单轮便于验收（不依赖常驻线程时序，可直接调用并断言告警落库）。
    """
    written = []
    pos = read_position(conn)
    if pos:
        log_file, log_pos = pos[0], pos[1]
        m_file, m_pos = master_position(conn)
        # 业务库有新写入（主库位点领先）但 CDC 位点停滞超过阈值 → 捕获链路疑似阻塞/中断。
        if _pos_ahead(m_file, m_pos, log_file, log_pos):
            # 停滞时长在 SQL 侧算：宿主机是 UTC 而 MySQL 是 +08:00，Python 侧相减会差 8 小时，
            # 用 TIMESTAMPDIFF 让两端都在服务器时区下求差，规避时区不一致导致的误判。
            cur = conn.cursor()
            try:
                cur.execute(
                    f"SELECT TIMESTAMPDIFF(SECOND, updated_at, NOW()) FROM {POSITION_TABLE} "
                    "WHERE repl_key='binlog'"
                )
                stale = int(cur.fetchone()[0])
            finally:
                cur.close()
            if stale > LAG_ALERT_WINDOW_SECONDS:
                if write_alert(
                    conn,
                    "binlog_lag",
                    f"位点停滞 {stale}s：master {m_file}:{m_pos} > cdc {log_file}:{log_pos}",
                ):
                    written.append("binlog_lag")
    # 消费水位：ODS 已产出事件但下游（ods_cdc_consumer_offset）未跟上。
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT COALESCE((SELECT MAX(id) FROM {LOG_TABLE}),0) - "
            f"COALESCE((SELECT last_id FROM ods_cdc_consumer_offset WHERE consumer=%s),0)",
            (CONSUMER_NAME,),
        )
        lag = int(cur.fetchone()[0])
        if lag > CONSUMER_LAG_THRESHOLD:
            if write_alert(conn, "consumer_lag", f"下游消费水位落后 {lag} 条事件"):
                written.append("consumer_lag")
    finally:
        cur.close()
    return written


def _monitor_loop(root_params):
    """常驻进程内的 CDC 健康监控（独立线程 + 独立连接，避免与写线程争用）。

    为什么放 main.py：CDC 捕获进程是全链路单点常驻，与 binlog 位点的关系最直接；
    即使流暂时无新事件（blocking 等事件），本线程仍按 60s 周期自查并落告警。
    """
    conn = pymysql.connect(**root_params, charset="utf8mb4")
    try:
        _ensure_alert_table(conn)
        while True:
            try:
                _check_alerts(conn)
            except Exception as e:  # noqa: BLE001 - 监控线程不能因单次检查失败退出
                print(f"[cdc] monitor error: {e}", flush=True)
            time.sleep(MONITOR_INTERVAL_SECONDS)
    finally:
        conn.close()


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
    # 默认 1002 与既有部署一致；允许换值跑独立校验/备用实例——MySQL 按 server_id 单点注册，
    # 若用同一 id 起第二个流，主库会把前一个连接踢掉。
    ap.add_argument("--server-id", type=int, default=1002, help="binlog 复制 server_id")
    args = ap.parse_args()

    env = load_env()
    host = env.get("MYSQL_HOST", "127.0.0.1")
    port = int(env.get("MYSQL_PORT", "3306"))
    user = env.get("MYSQL_CDC_USER", "spacefin_cdc")
    pwd = env.get("MYSQL_CDC_PASSWORD", "spacefin_cdc_dev")
    out_dbname = "spacefin_crawler"

    # 日志/位点/告警表（root 建表，避免 app 无 DDL）
    root = {
        "host": host,
        "port": port,
        "user": "root",
        "password": env.get("MYSQL_ROOT_PASSWORD", ""),
        "database": out_dbname,
    }
    wconn = pymysql.connect(**root, charset="utf8mb4")
    _ensure_log_table(wconn)
    _ensure_position_table(wconn)
    _ensure_alert_table(wconn)

    # CDC 健康监控：独立 root 连接，60s 一轮（位点滞后/消费水位滞后 → ads_cdc_alert）。
    threading.Thread(target=_monitor_loop, args=(root,), daemon=True).start()

    stream_kwargs = {
        "connection_settings": {
            "host": host,
            "port": port,
            "user": user,
            "passwd": pwd,
            "charset": "utf8mb4",
        },
        "server_id": args.server_id,
        "blocking": True,
        "only_schemas": ["spacefin"],
        "only_tables": TABLES,
        # 必须保留 resume_stream=True：本版本库把它用作 COM_BINLOG_DUMP 的 binlog_pos 开关，
        # 置 False 会让主库从 binlog 文件头（pos=4）重发，导致整文件重放。
        "resume_stream": True,
    }
    persisted = read_position(wconn)
    if persisted:
        # 有持久化位点：从该位点续读，停机期间新增的 binlog 事件会被补拉，不丢事件。
        stream_kwargs["log_file"] = persisted[0]
        stream_kwargs["log_pos"] = persisted[1]
        print(
            f"[cdc] resume from {persisted[0]}:{persisted[1]} (updated {persisted[2]})",
            flush=True,
        )
    stream = BinLogStreamReader(**stream_kwargs)
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
            # 事件全部落 ODS 之后进位点：崩溃最多重放本事件（下游幂等），不丢后续事件。
            write_position(wconn, stream.log_file, stream.log_pos)
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


def _insert_log(conn, table, event_type, before, after, max_retries=3):
    """落 ods_cdc_log（可 SQL 查询、1 分钟内可见）。

    健壮性约束（C 类）：INSERT 失败**禁止吞错**——因为 main() 在「_write_ods_lake +
    _insert_log 全部成功之后」才 write_position 推进 binlog 位点。一旦日志表未落库却吞掉
    异常，位点仍会推进，重启后该事件不会被重放 → 静默丢事件。

    这里改为 fail-fast：先有界重试（应对瞬时抖动），重试耗尽仍失败则上抛；上抛后 main()
    的 for 循环自然中断，write_position 不会再为本事件推进位点，重启从上一持久化位点重放
    （ODS 湖按事件幂等，重放安全）。
    """
    last_err = None
    for _ in range(1, max_retries + 1):
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
            return
        except Exception as e:  # noqa: BLE001 - 有界重试后上抛，由调用方决定是否推进位点
            last_err = e
            if hasattr(conn, "rollback"):
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            cur.close()
    raise RuntimeError(f"[cdc] ods_cdc_log insert failed after {max_retries} attempts: {last_err}")


if __name__ == "__main__":
    main()
