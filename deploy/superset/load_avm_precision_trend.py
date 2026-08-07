"""将 AVM 模型报告指标写入 Doris ADS 表 ads_avm_precision_trend（供 Superset 趋势图）。

每次模型重训后重跑本脚本即可追加一条快照，趋势图随之增长。
数据来源：output/avm/avm_report.json（合成种子训练的 AVM 模型，非真实基准）。

用法：
    python deploy/superset/load_avm_precision_trend.py
"""

from __future__ import annotations

import json
import os
import sys

import pymysql

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORT = os.path.join(REPO_ROOT, "output", "avm", "avm_report.json")
DATABASE = "ads"
TABLE = "ads_avm_precision_trend"


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
}


def main() -> int:
    if not os.path.exists(REPORT):
        print(f"[avm-trend] 未找到 {REPORT}，跳过（先跑 AVM pipeline 生成报告）", file=sys.stderr)
        return 0

    rep = json.load(open(REPORT, encoding="utf-8"))
    stat_date = rep.get("trained_at", "")[:10]
    version = rep.get("version", "unknown")
    model_mape = rep["metrics"]["model_total_price"]["mape"]
    baseline_mape = rep["metrics"]["baseline_median_x_area"]["mape"]
    n_test = rep["metrics"]["model_total_price"].get("n", 0)

    conn = pymysql.connect(
        host=DORIS["host"],
        port=DORIS["port"],
        user=DORIS["user"],
        password=DORIS["password"],
        database=DATABASE,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                  stat_date     DATE     NOT NULL COMMENT '模型训练日期',
                  model_version VARCHAR(32) NOT NULL COMMENT '模型版本',
                  model_mape    DECIMAL(8,3) NULL COMMENT '模型 MAPE(%)',
                  baseline_mape DECIMAL(8,3) NULL COMMENT '基线(中位价×面积) MAPE(%)',
                  n_test        INT      NULL COMMENT '测试集样本数'
                ) ENGINE = OLAP
                UNIQUE KEY(stat_date, model_version)
                DISTRIBUTED BY HASH(stat_date) BUCKETS 1
                PROPERTIES ("replication_num" = "1")
                """
            )
            conn.commit()
            cur.execute(
                f"INSERT INTO {TABLE} (stat_date, model_version, model_mape, baseline_mape, n_test) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (stat_date, version, model_mape, baseline_mape, n_test),
            )
            conn.commit()
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            print(
                f"[avm-trend] 已写入快照 stat_date={stat_date} version={version} "
                f"model_mape={model_mape}% baseline_mape={baseline_mape}%；当前表行数={cur.fetchone()[0]}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
