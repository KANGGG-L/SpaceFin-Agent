"""tools/risk 测试的公共 fixture。

被测模块与假 DB 连接都在 riskmods.py（唯一模块名，避免跨目录 conftest 撞名，
原因见该文件顶部说明）。本文件只提供业务假数据。

隔离原则：整个目录不连任何数据库、不加载 tools/avm 的真实模型产物。
AVM 相关用例一律 monkeypatch valuation.valuation_from_avm——tools/avm 正在被
其它 agent 并行改动，测试不能依赖它的行为。
"""

import pytest
import riskmods


@pytest.fixture
def conn():
    """录制型假连接。"""
    return riskmods.FakeConn()


@pytest.fixture
def collateral():
    """一套「正常」的抵押物：广州地址、100 ㎡、估值 1000 万、空间特征齐全。

    默认值刻意落在各阈值的安全区内，单个用例只改自己关心的那一两个字段，
    断言失败时能直接定位到被改的字段。
    """
    return {
        "collateral_id": 501,
        "property_addr": "广州市天河区示例路 1 号",
        "lat": 23.13,
        "lng": 113.32,
        "area": 100.0,
        "age": 10,
        "true_market_price": 10_000_000.0,
        "poi_density": 30.0,
        "commute_min": 25.0,
        "is_high_risk_zone": 0,
        "spatial_feat_missing_pct": 0.0,
    }


@pytest.fixture
def loan():
    """余额 500 万 → 配默认抵押物估值 1000 万即 LTV 0.5（正常档）。"""
    return {
        "loan_id": 1001,
        "customer_id": 9001,
        "collateral_id": 501,
        "loan_amount": 6_000_000.0,
        "balance": 5_000_000.0,
        "interest_rate": 4.2,
        "risk_class": None,
        "origination_date": "2024-01-01",
    }


@pytest.fixture
def city_map():
    return dict(riskmods.config.CITY_MAP)


@pytest.fixture
def patch_avm(monkeypatch):
    """把 AVM 估值替换成固定返回值，隔离 tools/avm 依赖。

    传 None 表示「AVM 未命中」，回退链应继续走 DWD → true_market_price。
    """

    def _patch(value):
        monkeypatch.setattr(
            riskmods.valuation, "valuation_from_avm", lambda model, col, cmap: value
        )
        return value

    return _patch
