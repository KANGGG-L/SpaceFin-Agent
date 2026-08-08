"""推送驱动（drivers.py）：通道注册、幂等语义与失败契约。

驱动的唯一契约是「送达；失败抛异常」——状态机靠异常判定成败，
所以任何吞掉异常的驱动都会让重试机制失效。
"""

import decimal
import json

import pytest
from alertmods import drivers


def alert(loan_id=1, date="2026-08-05", **kw):
    base = {
        "loan_id": loan_id,
        "customer_id": 9000 + loan_id,
        "collateral_id": 500 + loan_id,
        "loan_balance": 1_000_000.0,
        "market_valuation": 1_050_000.0,
        "ltv": 0.92,
        "risk_class": "可疑",
        "is_high_risk_zone": 0,
        "alert_level": None,
        "alert_date": date,
    }
    base.update(kw)
    return base


# ================================================================ 通道注册


def test_make_driver_builds_builtin_drivers_by_name(tmp_path):
    from alertmods import FakeConn

    assert isinstance(drivers.make_driver("site_inbox", conn=FakeConn()), drivers.SiteInboxDriver)
    assert isinstance(drivers.make_driver("file", out_dir=str(tmp_path)), drivers.FileDriver)
    # postloan_http 需配置 webhook 才能构造（否则显式报错，绝不静默退化）。
    assert isinstance(
        drivers.make_driver(
            "postloan_http", env={"SPACEFIN_POSTLOAN_WEBHOOK_URL": "https://postloan.test/hook"}
        ),
        drivers.PostloanHttpDriver,
    )


def test_unknown_driver_name_raises_instead_of_silently_skipping():
    """拼错通道名却静默不推，是最难排查的一类线上事故。"""
    with pytest.raises(ValueError, match="未知推送驱动"):
        drivers.make_driver("wechat")


def test_site_inbox_driver_requires_connection():
    with pytest.raises(ValueError, match="需要 root 连接"):
        drivers.make_driver("site_inbox", conn=None)


def test_abstract_driver_cannot_be_instantiated():
    with pytest.raises(TypeError):
        drivers.AlertDriver()


# ================================================================ 站内告警表驱动


def test_site_inbox_write_is_idempotent_per_loan_and_date():
    """UNIQUE(loan_id, alert_date) + UPSERT：重试重推只刷新原消息，不重复落行。"""
    from alertmods import FakeConn

    conn = FakeConn()
    drivers.SiteInboxDriver(conn).send(alert(1))

    sql, params = conn.find_sql("INSERT INTO ads_alert_inbox")
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params[0] == 1
    assert params[8] is None  # alert_level（无等级行落 NULL）
    assert params[-1] == "2026-08-05"
    assert conn.commits == 1


def test_site_inbox_persists_alert_level_two_tiers():
    """两档等级原样落库：warn/strong 区分推送强度，强预警不能降级成警示。"""
    from alertmods import FakeConn

    for level in ("warn", "strong"):
        conn = FakeConn()
        drivers.SiteInboxDriver(conn).send(alert(1, alert_level=level))

        _, params = conn.find_sql("INSERT INTO ads_alert_inbox")
        assert params[8] == level


def test_site_inbox_tolerates_missing_optional_fields():
    """ads_ltv_alerts 允许部分列为 NULL，驱动用 .get 取值，不能因缺列崩掉。"""
    from alertmods import FakeConn

    conn = FakeConn()

    drivers.SiteInboxDriver(conn).send({"loan_id": 1, "alert_date": "2026-08-05"})

    _, params = conn.find_sql("INSERT INTO ads_alert_inbox")
    assert params[1] is None  # customer_id


def test_site_inbox_raises_when_idempotency_key_missing():
    """loan_id/alert_date 是幂等键，缺了不能瞎写——抛出去让状态机记 failed。"""
    from alertmods import FakeConn

    with pytest.raises(KeyError):
        drivers.SiteInboxDriver(FakeConn()).send({"ltv": 0.9})


# ================================================================ 文件驱动


def test_file_driver_uses_one_file_per_alert_date(tmp_path):
    d = drivers.FileDriver(str(tmp_path), "2026-08-05")

    assert d.path.endswith("alert_push_2026-08-05.jsonl")


def test_file_driver_creates_output_directory(tmp_path):
    out = tmp_path / "nested" / "dir"

    drivers.FileDriver(str(out), "2026-08-05").send(alert(1))

    assert (out / "alert_push_2026-08-05.jsonl").exists()


def test_file_driver_appends_one_json_line_per_alert(tmp_path):
    d = drivers.FileDriver(str(tmp_path), "2026-08-05")

    d.send(alert(1))
    d.send(alert(2))

    lines = open(d.path, encoding="utf-8").read().strip().splitlines()
    assert [json.loads(x)["loan_id"] for x in lines] == [1, 2]


def test_file_driver_serializes_decimal_values(tmp_path):
    """ads_ltv_alerts 的数值列是 DECIMAL，pymysql 取回来是 Decimal，JSON 不原生支持。"""
    d = drivers.FileDriver(str(tmp_path), "2026-08-05")

    d.send(alert(1, ltv=decimal.Decimal("0.9231")))

    assert json.loads(open(d.path, encoding="utf-8").read())["ltv"] == "0.9231"


def test_file_driver_keeps_chinese_unescaped(tmp_path):
    d = drivers.FileDriver(str(tmp_path), "2026-08-05")

    d.send(alert(1))

    assert "可疑" in open(d.path, encoding="utf-8").read()


def test_file_driver_appends_duplicate_line_on_repush(tmp_path):
    """⚠️ 已知缺口：FileDriver 是纯 append，没有幂等键。

    多驱动场景下只要有一个通道失败，整条预警下一轮会重推所有通道，
    这里就会多出一行。推送日志因此是「至少一次」语义，不能当去重后的台账用
    （真正的台账是 ads_alert_dispatch）。
    """
    d = drivers.FileDriver(str(tmp_path), "2026-08-05")

    d.send(alert(1))
    d.send(alert(1))

    assert len(open(d.path, encoding="utf-8").read().strip().splitlines()) == 2

    # ================================================================ 通道开关
    """默认 site_inbox,file；真实通道绝不静默默认启用（未配置 webhook 时启用会整批失败）。"""
    from alertmods import alerting

    assert drivers.PostloanHttpDriver.name not in alerting.DEFAULT_DRIVERS
    assert alerting.DEFAULT_MAX_RETRIES == 3


# ================================================================ 真实贷后 HTTP 驱动


class _FakeResp:
    def __init__(self, code):
        self._code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self._code


class _RecordingTransport:
    """拦截 urlopen 调用，记录请求并返回指定状态码（无需真实网络）。"""

    def __init__(self, code=200):
        self.code = code
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        return _FakeResp(self.code)


def test_postloan_http_driver_requires_configured_webhook():
    """未配置 SPACEFIN_POSTLOAN_WEBHOOK_URL 时，make_driver 必须显式报错，
    不能偷偷退化成「假装成功」——否则预警会静默消失。"""
    with pytest.raises(ValueError, match="SPACEFIN_POSTLOAN_WEBHOOK_URL"):
        drivers.make_driver("postloan_http", env={})


def test_postloan_http_driver_posts_json_with_idempotency_and_auth():
    d = drivers.PostloanHttpDriver(
        "https://postloan.test/hook", token="sec", transport=_RecordingTransport(200)
    )
    d.send(alert(1, alert_level="strong"))

    assert len(d._transport.requests) == 1
    req, timeout = d._transport.requests[0]
    assert req.get_method() == "POST"
    assert req.get_full_url() == "https://postloan.test/hook"
    body = json.loads(req.data.decode("utf-8"))
    assert body["loan_id"] == 1
    assert body["alert_level"] == "strong"
    assert body["alert_date"] == "2026-08-05"
    # urllib 对多词头做 capitalize（Content-type），用 header_items 小写归一化核对。
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer sec"
    # 幂等键 (loan_id, alert_date)，接收方据此去重，重试不产生重复工单。
    assert headers["idempotency-key"] == "1:2026-08-05"


def test_postloan_http_driver_serializes_decimal_fields():
    d = drivers.PostloanHttpDriver("https://postloan.test/hook", transport=_RecordingTransport(200))
    d.send(alert(1, ltv=decimal.Decimal("0.9231"), loan_balance=decimal.Decimal("1000000.00")))

    body = json.loads(d._transport.requests[0][0].data.decode("utf-8"))
    assert body["ltv"] == 0.9231
    assert body["loan_balance"] == 1000000.0


def test_postloan_http_driver_raises_on_non_2xx():
    """非 2xx 视为送达失败，交给状态机重试。"""
    d = drivers.PostloanHttpDriver("https://postloan.test/hook", transport=_RecordingTransport(500))
    with pytest.raises(drivers.PostloanPushError, match="非 2xx"):
        d.send(alert(1))


def test_postloan_http_driver_raises_on_network_error():
    """网络错误 / 超时一律交给状态机重试，不能吞掉。"""

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    d = drivers.PostloanHttpDriver("https://postloan.test/hook", transport=_boom)
    with pytest.raises(drivers.PostloanPushError, match="推送贷后系统失败"):
        d.send(alert(1))


def test_resolve_drivers_auto_appends_postloan_when_configured():
    """配置 webhook 后无需改命令，resolve_drivers 自动接通 I-05 真实闭环。"""
    from alertmods import FakeConn, alerting

    drivers_list = alerting.resolve_drivers(
        "site_inbox,file",
        conn=FakeConn(),
        out_dir="/tmp",
        date="2026-08-05",
        env={"SPACEFIN_POSTLOAN_WEBHOOK_URL": "https://postloan.test/hook"},
    )
    assert "postloan_http" in [d.name for d in drivers_list]


def test_resolve_drivers_does_not_append_when_no_webhook():
    """未配置 webhook 时不自动启用真实通道，避免整批推送因缺配而全失败。"""
    from alertmods import FakeConn, alerting

    drivers_list = alerting.resolve_drivers(
        "site_inbox,file",
        conn=FakeConn(),
        out_dir="/tmp",
        date="2026-08-05",
        env={},
    )
    assert [d.name for d in drivers_list] == ["site_inbox", "file"]
