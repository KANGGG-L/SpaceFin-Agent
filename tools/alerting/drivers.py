"""预警推送驱动抽象与内置实现（I-05 的 driver 替换点）。

驱动只负责「把一条预警送达」，不关心去重 / 重试 / 状态机——那是 main.py 的职责。
新增推送通道（短信 / 企业微信 / 真实贷后系统 HTTP 接口）只需实现 AlertDriver.send
并在 make_driver 注册，调度层无需改动。

内置两个可用的驱动：
- site_inbox：写站内告警表 ads_alert_inbox（UNIQUE(loan_id, alert_date) 幂等，
  同一条预警重试重推不会产生重复站内消息）。
- file：逐条追加 JSONL 到输出目录（本地联调 / 验收用，无需 DB）。

PostloanHttpDriver 是「真实贷后系统」的预留替换点：目前只声明接口约定并抛
NotImplementedError，接入时按目标系统 API 实现 send 即可，不注册进默认列表。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

INBOX_TABLE = "ads_alert_inbox"

INBOX_DDL = f"""
CREATE TABLE IF NOT EXISTS {INBOX_TABLE} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loan_id INT,
    customer_id INT,
    collateral_id INT,
    loan_balance DECIMAL(14,2),
    market_valuation DECIMAL(14,2),
    ltv DECIMAL(8,4),
    risk_class VARCHAR(8),
    is_high_risk_zone TINYINT,
    alert_date DATE,
    received_ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_loan_date (loan_id, alert_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class AlertDriver(ABC):
    """预警推送驱动抽象。send 失败抛异常，由 main.py 的状态机转入重试。"""

    name: str = "base"

    @abstractmethod
    def send(self, alert: dict) -> None:
        """推送单条预警；失败抛异常。alert 为 ads_ltv_alerts 的一行（dict）。"""


class SiteInboxDriver(AlertDriver):
    """站内告警表驱动：写 ads_alert_inbox。

    用 UNIQUE(loan_id, alert_date) 幂等：重试重推同一预警时只刷新原消息，不重复落行。
    """

    name = "site_inbox"

    def __init__(self, conn):
        self._conn = conn

    def send(self, alert: dict) -> None:
        cur = self._conn.cursor()
        cur.execute(
            f"INSERT INTO {INBOX_TABLE} "
            "(loan_id, customer_id, collateral_id, loan_balance, market_valuation, "
            " ltv, risk_class, is_high_risk_zone, alert_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            " loan_balance=VALUES(loan_balance), market_valuation=VALUES(market_valuation), "
            " ltv=VALUES(ltv), risk_class=VALUES(risk_class), "
            " is_high_risk_zone=VALUES(is_high_risk_zone), received_ts=CURRENT_TIMESTAMP",
            (
                alert["loan_id"],
                alert.get("customer_id"),
                alert.get("collateral_id"),
                alert.get("loan_balance"),
                alert.get("market_valuation"),
                alert.get("ltv"),
                alert.get("risk_class"),
                alert.get("is_high_risk_zone"),
                alert["alert_date"],
            ),
        )
        self._conn.commit()
        cur.close()


class FileDriver(AlertDriver):
    """文件驱动：逐条追加 JSONL（本地联调 / 验收用，无需 DB 依赖）。"""

    name = "file"

    def __init__(self, out_dir: str, alert_date: str):
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"alert_push_{alert_date}.jsonl")

    def send(self, alert: dict) -> None:
        # ads_ltv_alerts 数值列是 Decimal，JSON 不原生支持 → default=str 兜底，
        # 保证推送日志可序列化（数值精度损失可忽略，明细以台账/站内表为准）。
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert, ensure_ascii=False, default=str) + "\n")


class PostloanHttpDriver(AlertDriver):
    """真实贷后系统推送的替换点（预留）。

    接入时实现 send：把 alert 组装成贷后系统接口报文并调用其 HTTP 接口，失败抛异常。
    业务约定：同一 (loan_id, alert_date) 的预警在贷后系统应幂等（系统侧用预警日期去重），
    否则重试会产生重复工单。
    """

    name = "postloan_http"

    def send(self, alert: dict) -> None:
        raise NotImplementedError(
            "真实贷后系统接口未接入；请实现 PostloanHttpDriver.send 后注册到 make_driver"
        )


def make_driver(
    name: str, *, conn=None, out_dir: str | None = None, alert_date: str = ""
) -> AlertDriver:
    """按名称构造驱动；新增通道（短信 / IM / 贷后系统）在此注册。"""
    if name == "site_inbox":
        if conn is None:
            raise ValueError("site_inbox 驱动需要 root 连接（含建表）")
        return SiteInboxDriver(conn)
    if name == "file":
        return FileDriver(out_dir or "output/alerting", alert_date)
    if name == "postloan_http":
        return PostloanHttpDriver()
    raise ValueError(f"未知推送驱动: {name!r}（可选 site_inbox/file）")
