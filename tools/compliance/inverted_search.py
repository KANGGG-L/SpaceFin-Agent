"""Doris 倒排索引合规审计检索（I-04）。

在 Doris ADS 层 `ads_compliance_audit` 表的 `audit_text` 列上建中文倒排索引
（USING INVERTED, parser=chinese），提供毫秒级敏感词定位能力，满足 I-04
接口契约（研发评审.md：Doris 倒排索引检索 / SQL / 毫秒级 / 无独立集群依赖）。

连接：复用仓库统一约定——Doris FE 兼容 MySQL 协议，端口 9030，root 空密码
（见 tools/lake/config.py）。连接参数可被环境变量覆盖：
    DORIS_HOST / DORIS_QUERY_PORT / DORIS_USER / DORIS_PASSWORD

用法：
    from tools.compliance.inverted_search import search_audit
    rows, elapsed_ms = search_audit("包装流水")
"""

from __future__ import annotations

import os
import time

import pymysql

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> dict:
    env: dict[str, str] = {}
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


_ENV = _load_env()

DORIS = {
    "host": os.getenv("DORIS_HOST", _ENV.get("DORIS_HOST", "127.0.0.1")),
    "port": int(os.getenv("DORIS_QUERY_PORT", _ENV.get("DORIS_QUERY_PORT", "9030"))),
    "user": os.getenv("DORIS_USER", _ENV.get("DORIS_USER", "root")),
    "password": os.getenv("DORIS_PASSWORD", _ENV.get("DORIS_PASSWORD", "")),
    "database": "ads",
}

DATABASE = "ads"
TABLE = "ads_compliance_audit"
INDEX_NAME = "idx_audit_text"
TEXT_COL = "audit_text"


def connect() -> pymysql.connections.Connection:
    """连接 Doris FE（MySQL 协议）。"""
    return pymysql.connect(
        host=DORIS["host"],
        port=DORIS["port"],
        user=DORIS["user"],
        password=DORIS["password"],
        database=DATABASE,
        connect_timeout=10,
    )


def _index_exists(conn: pymysql.connections.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SHOW INDEX FROM {TABLE}")
        return any(r[2] == INDEX_NAME for r in cur.fetchall())


def ensure_audit_table(conn: pymysql.connections.Connection) -> None:
    """幂等建表 + 建中文倒排索引（供测试/首次接入调用）。

    生产环境由 `sql/doris/01_compliance_audit_inverted.sql` 显式管理；
    此处保证脚本/测试在干净环境也能跑通。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
              id          BIGINT       NOT NULL COMMENT '审计记录ID',
              audit_type  VARCHAR(32)  NOT NULL COMMENT '审计类型',
              audit_text  STRING       NOT NULL COMMENT '审计文本(检索目标)',
              biz_id      VARCHAR(64)  NULL     COMMENT '关联业务单号(脱敏占位)',
              operator    VARCHAR(32)  NULL     COMMENT '操作人(合成)',
              created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '审计时间'
            ) ENGINE = OLAP
            UNIQUE KEY(id)
            DISTRIBUTED BY HASH(id) BUCKETS 1
            PROPERTIES ("replication_num" = "1", "enable_unique_key_merge_on_write" = "true")
            """
        )
    conn.commit()

    if not _index_exists(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {TABLE} ADD INDEX IF NOT EXISTS "
                f'{INDEX_NAME}({TEXT_COL}) USING INVERTED PROPERTIES("parser" = "chinese")'
            )
            cur.execute(f"BUILD INDEX {INDEX_NAME} ON {TABLE}")
        conn.commit()


def search_audit(keyword: str, limit: int = 50) -> tuple[list[dict], float]:
    """倒排索引敏感词检索（I-04 契约形态：audit_text MATCH 'keyword'）。

    返回 (命中行列表, 耗时毫秒)。命中行含 id/audit_type/audit_text/biz_id/
    operator/created_at 字段。耗时含网络往返，理论上为毫秒级。
    """
    conn = connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            sql = (
                f"SELECT id, audit_type, audit_text, biz_id, operator, created_at "
                f"FROM {TABLE} WHERE {TEXT_COL} MATCH %s "
                f"ORDER BY id LIMIT %s"
            )
            t0 = time.perf_counter()
            cur.execute(sql, (keyword, limit))
            rows = cur.fetchall()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return rows, elapsed_ms
    finally:
        conn.close()


def main() -> None:
    """CLI 演示：建表(若需) + 检索敏感词 + 打印耗时。"""
    conn = connect()
    try:
        ensure_audit_table(conn)
    finally:
        conn.close()

    for kw in ("包装流水", "过桥资金", "虚假收入证明"):
        rows, elapsed_ms = search_audit(kw)
        print(f"[I-04] keyword={kw!r} hits={len(rows)} elapsed={elapsed_ms:.2f}ms")
        for r in rows:
            print(f"    #{r['id']} [{r['audit_type']}] {r['audit_text'][:40]}...")


if __name__ == "__main__":
    main()
