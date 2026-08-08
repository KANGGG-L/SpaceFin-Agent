"""/proxy/report 失败退款接口测试（DECRGT0 + Nonce 防重 + 退还统计）。"""

from urllib.parse import urlencode

import pytest
from orchmods import get_json, running_master

RUN = "2026-08-04"


@pytest.fixture
def master(load_master):
    return load_master(CRAWL_RUN_ID=RUN)


@pytest.fixture
def worker_mod(load_worker):
    return load_worker(MASTER_URL="http://127.0.0.1:9")


def test_proxy_report_decr_ip_used_and_incr_refunded(master, rdb):
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 5)
    rdb.set(f"{master.IP_BUDGET_PREFIX}gz:sale", 60)

    with running_master(master, rdb) as base:
        params = {
            "city": "gz",
            "type": "sale",
            "src": "qg",
            "page": "1",
            "proxy": "1.2.3.4:8888",
            "attempt": "worker-1-1-1000-a1b2",
        }
        res = get_json(base, f"/proxy/report?{urlencode(params)}")

    assert res["refunded"] is True
    assert res["used"] == 4
    assert res["budget"] == 60
    assert int(rdb.get(f"{master.IP_USED_PREFIX}gz:sale") or 0) == 4
    assert int(rdb.get(f"{master.IP_REFUNDED_PREFIX}gz:sale") or 0) == 1


def test_proxy_report_nonce_idempotency(master, rdb):
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 5)
    rdb.set(f"{master.IP_BUDGET_PREFIX}gz:sale", 60)
    attempt = "worker-1-1-1000-nonce99"

    with running_master(master, rdb) as base:
        params = {
            "city": "gz",
            "type": "sale",
            "src": "qg",
            "page": "1",
            "proxy": "1.2.3.4:8888",
            "attempt": attempt,
        }
        res1 = get_json(base, f"/proxy/report?{urlencode(params)}")
        res2 = get_json(base, f"/proxy/report?{urlencode(params)}")

    assert res1["refunded"] is True
    assert res1["used"] == 4

    assert res2["refunded"] is False
    assert res2["used"] == 4
    assert int(rdb.get(f"{master.IP_USED_PREFIX}gz:sale") or 0) == 4
    assert int(rdb.get(f"{master.IP_REFUNDED_PREFIX}gz:sale") or 0) == 1


def test_proxy_report_empty_attempt_rejected(master, rdb):
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 5)
    rdb.set(f"{master.IP_BUDGET_PREFIX}gz:sale", 60)

    with running_master(master, rdb) as base:
        params = {
            "city": "gz",
            "type": "sale",
            "src": "qg",
            "page": "1",
            "proxy": "1.2.3.4:8888",
            "attempt": "",
        }
        res = get_json(base, f"/proxy/report?{urlencode(params)}")

    assert res["refunded"] is False
    assert "attempt is required" in res.get("error", "")
    assert int(rdb.get(f"{master.IP_USED_PREFIX}gz:sale") or 0) == 5


def test_proxy_report_only_for_qg_source(master, rdb):
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 5)

    with running_master(master, rdb) as base:
        params = {
            "city": "gz",
            "type": "sale",
            "src": "free",
            "page": "1",
            "proxy": "1.2.3.4:8888",
            "attempt": "attempt-free-1",
        }
        res = get_json(base, f"/proxy/report?{urlencode(params)}")

    assert res["refunded"] is False
    assert int(rdb.get(f"{master.IP_USED_PREFIX}gz:sale") or 0) == 5


def test_proxy_report_decrgt0_floor_at_zero(master, rdb):
    rdb.set(f"{master.IP_USED_PREFIX}gz:sale", 0)

    with running_master(master, rdb) as base:
        params = {
            "city": "gz",
            "type": "sale",
            "src": "qg",
            "page": "1",
            "proxy": "1.2.3.4:8888",
            "attempt": "attempt-zero-1",
        }
        res = get_json(base, f"/proxy/report?{urlencode(params)}")

    assert res["refunded"] is True
    assert res["used"] == 0
    assert int(rdb.get(f"{master.IP_USED_PREFIX}gz:sale") or 0) == 0


def test_proxy_report_invalid_scope_returns_false(master, rdb):
    with running_master(master, rdb) as base:
        params = {"city": "invalid", "type": "sale", "src": "qg", "attempt": "att-1"}
        res = get_json(base, f"/proxy/report?{urlencode(params)}")
    assert res["refunded"] is False


def test_worker_report_proxy_failure_logic(master, rdb, worker_mod, monkeypatch):
    with running_master(master, rdb) as base:
        rdb.set(f"{master.IP_USED_PREFIX}sz:fangyuan", 3)
        monkeypatch.setattr(worker_mod, "PROXY_REPORT_ENABLED", True)
        ok = worker_mod.report_proxy_failure(base, "sz", "fangyuan", "qg", 1, "1.2.3.4:80")
        assert ok is True
        assert int(rdb.get(f"{master.IP_USED_PREFIX}sz:fangyuan") or 0) == 2

        # PROXY_REPORT_ENABLED is False -> no-op
        monkeypatch.setattr(worker_mod, "PROXY_REPORT_ENABLED", False)
        ok2 = worker_mod.report_proxy_failure(base, "sz", "fangyuan", "qg", 2, "1.2.3.4:80")
        assert ok2 is False
        assert int(rdb.get(f"{master.IP_USED_PREFIX}sz:fangyuan") or 0) == 2
