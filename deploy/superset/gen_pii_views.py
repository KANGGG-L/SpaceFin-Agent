#!/usr/bin/env python
"""生成 Superset 脱敏视图 DDL（投产加固 · 单一真源派生）。

为什么有这个脚本：
  手动写 sql/doris/02_superset_pii_views.sql 的脱敏视图时，PII 列清单容易与
  tools/frontend/data_classification.py 的 COLUMN_LEVELS/PII_COLUMNS 漂移。本脚本
  以 data_classification 的 PII 列为唯一真源，自动对每张含 PII 的 Doris 表生成
  「PII 列脱敏、其余列透传」的视图 DDL，脱敏表达式与 mask_value 同口径：
      CONCAT('c****', RIGHT(col, 4))
  生成的视图可注册到 Superset，作为敏感明细的唯一入口，避免裸曝（见 02 SQL §4）。

用法：
  python deploy/superset/gen_pii_views.py            # 仅打印 DDL 到 stdout
  python deploy/superset/gen_pii_views.py --apply    # 直接 DROP+CREATE 到 Doris（幂等）
  python deploy/superset/gen_pii_views.py --write sql/doris/02_superset_pii_views.sql
        # 把生成视图（§3 部分）追加写入指定 SQL 文件

PII 真源：优先 import tools.frontend.data_classification.PII_COLUMNS；若运行环境
  无法导入（如缺 streamlit 的 pages 依赖），回退到与源文件保持一致的硬编码镜像。
Doris 连接：优先 import tools.lake.config.DORIS（单一真源）；否则读环境变量
  DORIS_HOST / DORIS_QUERY_PORT / DORIS_USER / DORIS_PASSWORD，默认 127.0.0.1:9030 root。
"""

from __future__ import annotations

import argparse
import os
import sys

# 扫描这些库寻找含 PII 列的表（与 02 SQL 中已手写视图的库一致）。
SCAN_DBS = ("ads", "dwd", "dws", "ods")

# data_classification.PII_COLUMNS 的镜像（保持一致：customer 表的客户号/客户名）。
# 若运行环境可导入 data_classification，则以导入值为准，下面的常量仅作回退。
PII_COLUMNS_FALLBACK = {
    ("customer", "customer_id"),
    ("customer", "customer_name"),
}


def load_pii_columns():
    """取 PII 列集合；优先 data_classification，失败回退硬编码镜像。"""
    try:
        sys.path.insert(0, os.getcwd())
        from tools.frontend.data_classification import PII_COLUMNS  # type: ignore

        return set(PII_COLUMNS)
    except Exception:
        return set(PII_COLUMNS_FALLBACK)


def load_doris():
    """取 Doris 连接参数字典（单一真源 tools.lake.config，失败回退环境变量）。"""
    try:
        sys.path.insert(0, os.getcwd())
        from tools.lake.config import DORIS as LAKE  # type: ignore

        return {
            "host": os.getenv("DORIS_HOST", LAKE["host"]),
            "port": int(os.getenv("DORIS_QUERY_PORT", str(LAKE["query_port"]))),
            "user": os.getenv("DORIS_USER", LAKE["user"]),
            "password": os.getenv("DORIS_PASSWORD", LAKE["password"]),
        }
    except Exception:
        return {
            "host": os.getenv("DORIS_HOST", "127.0.0.1"),
            "port": int(os.getenv("DORIS_QUERY_PORT", "9030")),
            "user": os.getenv("DORIS_USER", "root"),
            "password": os.getenv("DORIS_PASSWORD", ""),
        }


def _connect(doris: dict):
    import pymysql

    return pymysql.connect(
        host=doris["host"],
        port=doris["port"],
        user=doris["user"],
        password=doris["password"],
        charset="utf8mb4",
    )


def _find_table(cur, table: str) -> str | None:
    """在 SCAN_DBS 中定位含 PII 列的真实表：精确名 → ods_ 前缀 → 任意 _后缀。"""
    for db in SCAN_DBS:
        cur.execute(f"SHOW TABLES FROM {db}")
        names = {r[0] for r in cur.fetchall()}
        if table in names:
            return f"{db}.{table}"
        cand = f"ods_{table}"
        if cand in names:
            return f"{db}.{cand}"
        for n in names:
            if n == table or n.endswith(f"_{table}"):
                return f"{db}.{n}"
    return None


def generate(pii_columns, doris: dict, apply: bool, write_path: str | None) -> list[str]:
    """为每张含 PII 列的表生成脱敏视图 DDL；apply=True 时落到 Doris。返回 DDL 行列表。"""
    conn = _connect(doris)
    cur = conn.cursor()
    ddl_blocks: list[str] = []
    try:
        for table, col in sorted(pii_columns, key=lambda x: (x[0], x[1])):
            fq = _find_table(cur, table)
            if not fq:
                print(f"[skip] 未在 {SCAN_DBS} 找到表 {table}（PII 列 {col}）", file=sys.stderr)
                continue
            db, tbl = fq.split(".", 1)
            cur.execute(f"DESC {fq}")
            cols = [r[0] for r in cur.fetchall()]
            if col not in cols:
                print(f"[skip] {fq} 无列 {col}", file=sys.stderr)
                continue
            # 透传列（非 PII）+ 脱敏列（PII），顺序与原始表一致。
            select_parts = []
            for c in cols:
                if (table, c) in pii_columns or c == col:
                    select_parts.append(f"CONCAT('c****', RIGHT(`{c}`, 4)) AS `{c}`")
                else:
                    select_parts.append(f"`{c}`")
            view_db = db
            view_name = f"v_{tbl}_masked"
            ddl = (
                f"DROP VIEW IF EXISTS {view_db}.{view_name};\n"
                f"CREATE VIEW {view_db}.{view_name}\n"
                f"  AS SELECT {', '.join(select_parts)}\n"
                f"     FROM {fq};"
            )
            ddl_blocks.append(f"-- 自动生成：{fq} 的 PII 脱敏视图（{col} 已脱敏）\n{ddl}")
            if apply:
                for stmt in ddl.split(";\n"):
                    stmt = stmt.strip()
                    if stmt:
                        cur.execute(stmt)
                conn.commit()
                print(f"[apply] 已建视图 {view_db}.{view_name}")
    finally:
        cur.close()
        conn.close()
    if write_path:
        header = (
            "-- 以下视图由 deploy/superset/gen_pii_views.py 自动生成（PII 列脱敏）\n"
            "-- 脱敏口径对齐 tools/frontend/data_classification.mask_value："
            "CONCAT('c****', RIGHT(col,4))\n"
        )
        with open(write_path, "w", encoding="utf8") as f:
            f.write(header + "\n".join(ddl_blocks) + "\n")
        print(f"[write] 已写入 {write_path}（{len(ddl_blocks)} 个视图）")
    return ddl_blocks


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 Superset PII 脱敏视图 DDL")
    ap.add_argument("--apply", action="store_true", help="直接 DROP+CREATE 到 Doris（幂等）")
    ap.add_argument("--write", metavar="PATH", help="把生成视图写入指定 SQL 文件")
    args = ap.parse_args()

    pii_columns = load_pii_columns()
    doris = load_doris()
    blocks = generate(pii_columns, doris, apply=args.apply, write_path=args.write)
    if not args.apply and not args.write:
        if not blocks:
            print("-- 无 PII 视图可生成（未找到含 PII 列的表）", file=sys.stderr)
        else:
            print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
