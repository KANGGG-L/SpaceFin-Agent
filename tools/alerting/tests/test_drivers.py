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
    assert isinstance(drivers.make_driver("postloan_http"), drivers.PostloanHttpDriver)


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


# ================================================================ 预留通道


def test_postloan_http_driver_raises_not_implemented():
    """预留点必须抛 NotImplementedError，不能假装成功——否则预警会静默消失。"""
    with pytest.raises(NotImplementedError, match="未接入"):
        drivers.PostloanHttpDriver().send(alert(1))


def test_reserved_driver_not_in_default_list():
    """默认 site_inbox,file；未实现的通道被默认启用会让整批推送全失败。"""
    from alertmods import alerting

    assert drivers.PostloanHttpDriver.name not in "site_inbox,file"
    assert alerting.DEFAULT_MAX_RETRIES == 3
