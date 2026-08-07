#!/usr/bin/env python
"""建实时预警 inbox 表 ads_stream_ltv_alerts（幂等）。

与离线链路的 ads_ltv_alerts 分开：本表是 Flink 实时作业的专属落点（按 event_id
主键幂等，随事件持续追加），不干扰离线批次的当日预警替换逻辑。

用法：
    python tools/stream/init_db.py
"""

from __future__ import annotations

import os
import sys

import pymysql

_RISK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "risk")
sys.path.insert(0, _RISK_DIR)

import config  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS ads_stream_ltv_alerts (
  event_id BIGINT NOT NULL,
  loan_id INT NOT NULL,
  customer_id INT,
  collateral_id INT,
  loan_balance DECIMAL(14,2),
  market_valuation DECIMAL(14,2),
  ltv DECIMAL(8,4),
  risk_class VARCHAR(8),
  is_high_risk_zone TINYINT,
  alert_date DATE,
  received_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (event_id),
  KEY idx_stream_loan (loan_id),
  KEY idx_stream_date (alert_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def main() -> None:
    env = config.load_env()
    conn = pymysql.connect(**config.root_crawl_params(env), charset="utf8mb4")
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        conn.commit()
        cur.close()
        print("[stream-init] ads_stream_ltv_alerts ready")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
