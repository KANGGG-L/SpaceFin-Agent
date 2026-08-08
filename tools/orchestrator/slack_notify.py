"""Slack incoming-webhook 通知（零依赖，stdlib only）。

供两处复用：
- DAG 的 on_failure_callback（Airflow 进程内，仅依赖本文件 stdlib）；
- CLI 脚本（crawl_quality_gate.py / audit_final_failed.py，在 spark venv 跑）。

设计约束：通知失败绝不能反噬主流程——任何异常都被吞掉并返回 False。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# 风控日终链路的通知分发角色（产品 / 开发 / QA / 审核）。
ROLE_DISTRIBUTION = "产品/开发/QA/审核"


def build_alert_message(
    task_id: str, ds: str, owner_roles: str, detail: str, mentions: str = ""
) -> str:
    """组装告警文案：含链路/任务/业务日/负责角色/四角色分发/详情。"""
    suffix = f" {mentions}" if mentions else ""
    return (
        "[SpaceFin 风控日终告警]\n"
        f"链路: guangdong_daily_crawl\n"
        f"任务: {task_id}\n"
        f"业务日: {ds}\n"
        f"负责: {owner_roles}\n"
        f"通知: {ROLE_DISTRIBUTION}{suffix}\n"
        f"详情: {detail}"
    )


def post_slack(webhook: str, text: str, *, timeout: float = 10) -> bool:
    """POST 纯文本告警到 Slack incoming-webhook。webhook 为空或任意异常 → 返回 False。"""
    if not webhook:
        return False
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= (resp.getcode() or 0) < 300
    except urllib.error.HTTPError as exc:  # 非 2xx 也只当通知失败，不抛出
        return 200 <= (exc.code or 0) < 300
    except Exception:  # noqa: BLE001 - 网络错误 / 超时一律交给调用方记录，不反噬主流程
        return False
