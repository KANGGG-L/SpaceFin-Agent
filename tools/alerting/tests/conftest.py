"""tools/alerting 测试的公共 fixture。

被测模块、假 DB 连接与驱动替身都在 alertmods.py（唯一模块名，原因见该文件顶部）。
整个目录不连数据库、不发任何真实推送。
"""

import alertmods
import pytest


@pytest.fixture
def conn():
    return alertmods.FakeConn()


@pytest.fixture
def driver():
    """默认全部推送成功的记录型驱动。"""
    return alertmods.RecordingDriver()


@pytest.fixture
def make_conn():
    """按「当日预警清单 + 台账既有状态」构造一个可跑 run_dispatch 的假连接。"""

    def _make(alerts, dispatch=()):
        conn = alertmods.FakeConn()
        conn.on("FROM ads_ltv_alerts", list(alerts), alertmods.ALERT_COLS)
        conn.on("FROM ads_alert_dispatch", list(dispatch))
        return conn

    return _make
