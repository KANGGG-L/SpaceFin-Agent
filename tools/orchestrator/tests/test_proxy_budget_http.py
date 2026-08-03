"""代理发放 HTTP 契约（§3.3）：真起 master 的 HTTPServer + 真 Redis(db 15) + 真 HTTP 请求。

覆盖：qg 按城扣减、到上限后 budget_exhausted=true 且 ip_used 不越界、
free 不计入 ip_used、random 回落 free、池空时预扣回滚、无 city/type 时不计预算。
"""

import pytest
from conftest import get_json, running_master, seed_pool

SMALL_BUDGET = '{"gz": {"sale": 3, "fangyuan": 2}, "sz": {"sale": 2, "fangyuan": 1}}'


@pytest.fixture
def master(load_master):
    return load_master(IP_BUDGET_JSON=SMALL_BUDGET, CRAWL_RUN_ID="test-run")


@pytest.fixture
def base(master, rdb):
    with running_master(master, rdb) as url:
        yield url


def used(rdb, master, city, typ):
    return int(rdb.get(f"{master.IP_USED_PREFIX}{city}:{typ}") or 0)


def test_qg_consumes_budget_then_refuses_without_overshoot(master, rdb, base):
    seed_pool(rdb, master.POOL_QG, [f"10.0.0.{i}:8000" for i in range(1, 11)], "qg")

    for i in (1, 2, 3):
        r = get_json(base, "/proxy/qg?city=gz&type=sale")
        assert r["proxy"] is not None, f"call#{i} should hand out a qg proxy"
        assert r["source"] == "qg"
        assert (r["city"], r["type"]) == ("gz", "sale")
        assert r["used"] == i and r["budget"] == 3
        assert r["budget_exhausted"] is (i == 3)  # 第 3 次发放后预算即用尽
        assert used(rdb, master, "gz", "sale") == i

    # 第 4 次：拒绝发放，且 ip_used 不得超过 budget（预扣必须回滚）
    r = get_json(base, "/proxy/qg?city=gz&type=sale")
    assert r["proxy"] is None
    assert r["budget_exhausted"] is True
    assert r["error"]
    assert r["used"] == 3 and r["budget"] == 3
    assert used(rdb, master, "gz", "sale") == 3

    # 连打若干次仍不越界
    for _ in range(5):
        get_json(base, "/proxy/qg?city=gz&type=sale")
    assert used(rdb, master, "gz", "sale") == 3
    # 预算隔离：其他城/类型不受影响
    assert used(rdb, master, "gz", "fangyuan") == 0
    assert used(rdb, master, "sz", "sale") == 0


def test_free_pool_does_not_count_against_budget(master, rdb, base):
    seed_pool(rdb, master.POOL_FREE, [f"172.16.0.{i}:3128" for i in range(1, 6)], "free")
    for _ in range(5):
        r = get_json(base, "/proxy/free?city=gz&type=fangyuan")
        assert r["proxy"] is not None and r["source"] == "free"
        assert r["budget_exhausted"] is False  # 预算未动
    assert used(rdb, master, "gz", "fangyuan") == 0  # free 发放不计 ip_used

    # 预算耗尽后 free 仍照常发放，只是如实回显 budget_exhausted
    rdb.set(f"{master.IP_USED_PREFIX}gz:fangyuan", 2)
    seed_pool(rdb, master.POOL_FREE, ["172.16.9.9:3128"], "free")
    r = get_json(base, "/proxy/free?city=gz&type=fangyuan")
    assert r["proxy"] == "172.16.9.9:3128"
    assert r["budget_exhausted"] is True
    assert used(rdb, master, "gz", "fangyuan") == 2


def test_random_prefers_qg_then_falls_back_to_free(master, rdb, base):
    seed_pool(rdb, master.POOL_QG, ["10.0.1.1:8000"], "qg")
    seed_pool(rdb, master.POOL_FREE, ["172.16.1.1:3128", "172.16.1.2:3128"], "free")

    r = get_json(base, "/proxy/random?city=sz&type=fangyuan")
    assert (r["proxy"], r["source"]) == ("10.0.1.1:8000", "qg")
    assert r["used"] == 1 and r["budget"] == 1
    assert r["budget_exhausted"] is True  # sz:fangyuan 预算=1，一次即用尽

    # 预算已耗尽 → 回落 free，proxy 非空但仍回报 budget_exhausted
    r = get_json(base, "/proxy/random?city=sz&type=fangyuan")
    assert r["proxy"] is not None and r["source"] == "free"
    assert r["budget_exhausted"] is True
    assert used(rdb, master, "sz", "fangyuan") == 1  # free 不加计


def test_qg_pool_empty_rolls_back_preconsume(master, rdb, base):
    r = get_json(base, "/proxy/qg?city=sz&type=sale")  # 池空、预算充足
    assert r["proxy"] is None
    assert r["error"] == "qg pool empty"
    assert r["budget_exhausted"] is False
    assert used(rdb, master, "sz", "sale") == 0  # 未发放 → 不得计数


def test_both_pools_empty_random(master, rdb, base):
    r = get_json(base, "/proxy/random?city=sz&type=sale")
    assert r["proxy"] is None and r["source"] is None
    assert r["error"] == "both pools empty"
    assert used(rdb, master, "sz", "sale") == 0


def test_missing_or_unknown_scope_is_not_metered(master, rdb, base):
    seed_pool(rdb, master.POOL_QG, ["10.0.2.1:8000", "10.0.2.2:8000"], "qg")
    r = get_json(base, "/proxy/qg")  # 无 city/type：向后兼容，不计预算不拒绝
    assert r["proxy"] is not None
    assert r["city"] is None and r["type"] is None
    assert r["used"] == 0 and r["budget"] == 0 and r["budget_exhausted"] is False

    r = get_json(base, "/proxy/qg?city=beijing&type=sale")  # 非白名单城
    assert r["proxy"] is not None
    assert r["budget_exhausted"] is False
    assert rdb.get(f"{master.IP_USED_PREFIX}beijing:sale") is None
