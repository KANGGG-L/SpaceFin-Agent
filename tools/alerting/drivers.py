"""预警推送驱动抽象与内置实现（I-05 的 driver 替换点）。

驱动只负责「把一条预警送达」，不关心去重 / 重试 / 状态机——那是 main.py 的职责。
新增推送通道（短信 / 企业微信 / 真实贷后系统 HTTP 接口）只需实现 AlertDriver.send
并在 make_driver 注册，调度层无需改动。

内置三个驱动：
- site_inbox：写站内告警表 ads_alert_inbox（UNIQUE(loan_id, alert_date) 幂等，
  同一条预警重试重推不会产生重复站内消息）。
- file：逐条追加 JSONL 到输出目录（本地联调 / 验收用，无需 DB）。
- postloan_http：真实贷后系统 HTTP 推送（I-05 闭环出口）。配置
  SPACEFIN_POSTLOAN_WEBHOOK_URL 后由 main.resolve_drivers 自动启用，未配置时
  make_driver 显式报错——绝不「假装成功」，否则预警会静默消失。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable

# 真实贷后系统 webhook 的配置键（与 .env / Airflow Variable 约定一致）。
POSTLOAN_WEBHOOK_URL_ENV = "SPACEFIN_POSTLOAN_WEBHOOK_URL"
POSTLOAN_WEBHOOK_TOKEN_ENV = "SPACEFIN_POSTLOAN_WEBHOOK_TOKEN"
# 推送超时（秒），可被 SPACEFIN_POSTLOAN_TIMEOUT 覆盖。
POSTLOAN_WEBHOOK_TIMEOUT = float(os.getenv("SPACEFIN_POSTLOAN_TIMEOUT", "10"))

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
    alert_level VARCHAR(8),
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
            " ltv, risk_class, is_high_risk_zone, alert_level, alert_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            " loan_balance=VALUES(loan_balance), market_valuation=VALUES(market_valuation), "
            " ltv=VALUES(ltv), risk_class=VALUES(risk_class), "
            " is_high_risk_zone=VALUES(is_high_risk_zone), alert_level=VALUES(alert_level), "
            " received_ts=CURRENT_TIMESTAMP",
            (
                alert["loan_id"],
                alert.get("customer_id"),
                alert.get("collateral_id"),
                alert.get("loan_balance"),
                alert.get("market_valuation"),
                alert.get("ltv"),
                alert.get("risk_class"),
                alert.get("is_high_risk_zone"),
                alert.get("alert_level"),
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


class PostloanPushError(RuntimeError):
    """贷后系统 HTTP 推送失败；由 main.py 状态机捕获并计入 attempt_count 重试。"""


def _jsonable(value):
    """DECIMAL / 其他非 JSON 原生类型转可序列化值（数值精度以台账 / 站内表为准）。"""
    import decimal

    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


class PostloanHttpDriver(AlertDriver):
    """真实贷后系统 HTTP 推送驱动（I-05 闭环出口）。

    把单条预警以 POST JSON 推到配置的 webhook；失败抛 PostloanPushError，
    由 main.py 状态机计入 attempt_count 后重试。幂等键 (loan_id, alert_date)
    经 Idempotency-Key 头与报文体一并下发，接收方据此去重，重试不产生重复工单。

    transport 默认 urllib.request.urlopen，可注入替身以便单测（无需真实网络）。
    """

    name = "postloan_http"

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        timeout: float = POSTLOAN_WEBHOOK_TIMEOUT,
        source: str = "SpaceFin-Agent",
        transport: Callable = urllib.request.urlopen,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self.source = source
        self._transport = transport

    def send(self, alert: dict) -> None:
        payload = {
            "loan_id": alert["loan_id"],
            "customer_id": alert.get("customer_id"),
            "collateral_id": alert.get("collateral_id"),
            "loan_balance": _jsonable(alert.get("loan_balance")),
            "market_valuation": _jsonable(alert.get("market_valuation")),
            "ltv": _jsonable(alert.get("ltv")),
            "risk_class": alert.get("risk_class"),
            "is_high_risk_zone": alert.get("is_high_risk_zone"),
            "alert_level": alert.get("alert_level"),
            "alert_date": str(alert["alert_date"]),
            "source": self.source,
        }
        idem_key = f"{alert['loan_id']}:{alert['alert_date']}"
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Idempotency-Key", idem_key)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._transport(req, timeout=self.timeout) as resp:
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            raise PostloanPushError(f"贷后系统返回 HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络错误 / 超时一律交给状态机重试
            raise PostloanPushError(f"推送贷后系统失败: {exc}") from exc
        if status is None or status >= 300:
            raise PostloanPushError(f"贷后系统返回非 2xx 状态码 {status}")


def make_driver(
    name: str,
    *,
    conn=None,
    out_dir: str | None = None,
    alert_date: str = "",
    env: dict | None = None,
) -> AlertDriver:
    """按名称构造驱动；新增通道（短信 / IM / 贷后系统）在此注册。

    postloan_http 需要 SPACEFIN_POSTLOAN_WEBHOOK_URL 已配置，未配置显式报错，
    避免「未实现的通道被默认启用导致整批推送全失败」。
    """
    if name == "site_inbox":
        if conn is None:
            raise ValueError("site_inbox 驱动需要 root 连接（含建表）")
        return SiteInboxDriver(conn)
    if name == "file":
        return FileDriver(out_dir or "output/alerting", alert_date)
    if name == "postloan_http":
        env = env if env is not None else os.environ
        endpoint = env.get(POSTLOAN_WEBHOOK_URL_ENV)
        if not endpoint:
            raise ValueError(
                f"postloan_http 驱动需要配置 {POSTLOAN_WEBHOOK_URL_ENV}（真实贷后系统 webhook），"
                "未配置时不应启用"
            )
        token = env.get(POSTLOAN_WEBHOOK_TOKEN_ENV)
        return PostloanHttpDriver(endpoint, token=token, timeout=POSTLOAN_WEBHOOK_TIMEOUT)
    raise ValueError(f"未知推送驱动: {name!r}（可选 site_inbox/file/postloan_http）")
