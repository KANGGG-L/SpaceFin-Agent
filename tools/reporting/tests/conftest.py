"""tools/reporting 测试的公共 fixture。

被测模块、假 DB 连接与构造辅助都在 g11mods.py（唯一模块名，原因见该文件顶部）。
整个目录不连数据库：所有读写都走 g11mods.FakeConn。
"""

import g11mods
import pytest


@pytest.fixture
def conn():
    return g11mods.FakeConn()


@pytest.fixture
def internal():
    """一份三出口自洽的出口①：五级俱全、占比之和为 1、总额 1000。"""
    return g11mods.internal_of(
        正常=(3, 500.0, 0.5),
        关注=(2, 250.0, 0.25),
        次级=(1, 150.0, 0.15),
        可疑=(1, 100.0, 0.1),
    )
