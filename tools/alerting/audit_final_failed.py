#!/usr/bin/env python
"""I-05 预警终态失败审计（G4）：统计当日 final_failed 预警，>0 发 Slack 告警。

alerting_push 在 DAG 层永远 exit 0（单条送达失败被内部状态机吸收、跨天重试），
故终态失败（attempt_count ≥ max_retries）对 on-call 不可见。本脚本在推送后补一步
只读审计：发现终态失败即发 Slack 告警（任务本身 exit 0，不因此把整日 DAG 标红——
交付失败本就是跨天状态机兜底的）。

依赖：config（tools/risk）、pymysql、slack_notify（tools/orchestrator）。
ads_alert_dispatch 位于 spacefin_crawler（与 alerting 写库同库）。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "risk"))

import config  # noqa: E402
import pymysql  # noqa: E402
from slack_notify import build_alert_message, post_slack  # noqa: E402


def count_final_failed(conn, date: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM ads_alert_dispatch "
        "WHERE alert_date=%s AND status='failed' AND attempt_count >= max_retries",
        (date,),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="I-05 预警终态失败审计（G4）")
    ap.add_argument("--date", required=True, help="业务日 YYYY-MM-DD")
    args = ap.parse_args()

    env = config.load_env()
    conn = pymysql.connect(**config.crawl_params(env), charset="utf-8")
    try:
        n = count_final_failed(conn, args.date)
    finally:
        conn.close()

    print(f"[audit] date={args.date} final_failed={n}", flush=True)
    if n > 0:
        webhook = os.getenv("SPACEFIN_ALERT_SLACK_WEBHOOK", "")
        mentions = os.getenv("SPACEFIN_ALERT_MENTIONS", "")
        detail = (
            f"{n} 条预警送达终态失败（待人工处置），"
            "详见 ads_alert_dispatch（alert_level / last_error）"
        )
        msg = build_alert_message("alerting_audit", args.date, "产品/审核", detail, mentions)
        post_slack(webhook, msg)
        print("[audit] 已发未送达告警", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
