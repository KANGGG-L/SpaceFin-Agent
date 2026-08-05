"""tools/spatial 测试的公共 fixture。

被测模块在 spatmods.py（唯一模块名，原因见该文件顶部）。
整个目录不连数据库：只测纯几何 / 聚合计算，store 层的 SQL 不在本轮范围内。
"""

import pytest
import spatmods


@pytest.fixture
def gz_listings():
    """广州天河附近一簇挂牌行：同一 0.02° 网格内 25 条，单价 90000 上下。

    25 条是为了越过 config.MIN_ZONE_SAMPLES（20），让区块能被构建出来。
    """
    return [
        spatmods.sale_row(23.1300 + i * 0.0001, 113.3200 + i * 0.0001, 90_000.0 + i * 100)
        for i in range(25)
    ]
