#!/usr/bin/env python
"""Superset 查询/导出审计写入器（投产加固 · 与驾驶舱共用 ads_export_audit）。

与 tools/frontend/db.py:write_audit 同一张表、同一套字段（action/username/role/
detail/result/ip），从而驾驶舱与 Superset 的审计留痕合并到同一通道，闭环可查。

表结构（幂等建表，与 db.ensure_export_audit_table 对齐）：
  spacefin.ads_export_audit(
    id, action, username, role, detail, result, ip, created_at)

连接：默认经 host.docker.internal 连宿主 MySQL（容器内 127.0.0.1 指向自身），
环境变量 AUDIT_MYSQL_HOST/PORT/USER/PASSWORD/DB 可覆盖（见 docker-compose.yml）。
建表/写失败静默降级，不阻断 Superset 正常查询（与驾驶舱口径一致）。
"""

from __future__ import annotations

import os

import pymysql

_AUDIT_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS ads_export_audit ("
    "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
    "action VARCHAR(32) NOT NULL,"
    "username VARCHAR(32) NOT NULL,"
    "role VARCHAR(16) NOT NULL,"
    "detail TEXT,"
    "result VARCHAR(16) NOT NULL,"
    "ip VARCHAR(64),"
    "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
    "KEY idx_action_ts (action, created_at)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def _conn_params():
    return {
        "host": os.getenv("AUDIT_MYSQL_HOST", "mysql"),
        "port": int(os.getenv("AUDIT_MYSQL_PORT", "3306")),
        "user": os.getenv("AUDIT_MYSQL_USER", "root"),
        "password": os.getenv("AUDIT_MYSQL_PASSWORD", ""),
        "database": os.getenv("AUDIT_MYSQL_DB", "spacefin"),
        "charset": "utf8mb4",
    }


def ensure_audit_table():
    """幂等建审计表（仅首写时触发）。"""
    try:
        conn = pymysql.connect(**_conn_params())
        try:
            with conn.cursor() as cur:
                cur.execute(_AUDIT_TABLE_DDL)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # 静默降级：审计不可用不影响业务查询。
        pass


def write_audit(action: str, username: str, role: str, detail: str, result: str, ip: str | None):
    """写一条审计记录；建表/写失败静默降级。"""
    try:
        ensure_audit_table()
        conn = pymysql.connect(**_conn_params())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ads_export_audit (action, username, role, detail, result, ip) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (action, username, role, detail, result, ip),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
