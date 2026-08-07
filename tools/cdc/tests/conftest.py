"""tools/cdc 测试公共 fixture。被测模块与假 DB 连接都在 cdcmods.py（唯一模块名，
原因见该文件顶部）。本文件只提供录制型假连接 fixture。"""

import cdcmods
import pytest


@pytest.fixture
def conn():
    """录制型假连接：按 SQL 片段返回预置结果集。"""
    return cdcmods.CdcFakeConn()
