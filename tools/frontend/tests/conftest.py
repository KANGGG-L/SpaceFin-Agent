"""tools/frontend 页面测试公共 fixture（零 DB / 零 HTTP）。

被测页面模块在 pages/ 下，import 时会 `import db`（直读 MySQL 的连接层）。
测试把 `db.crawl_conn` 替换成录制型假连接（复刻 tools/cdc/tests 的思路），
让页面 handler 在无 MySQL 环境下也能验证聚合逻辑与降级路径。

FakeConn 实现已迁到 fakeconn.py（唯一模块名，避免与 persona 的 conftest
同名冲突）；conftest 仅保留 pytest fixture。
"""

import os
import sys

import pytest

# 让 `import pages` 在任意 cwd 下都能命中 tools/frontend。
_FRONTEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _FRONTEND_DIR not in sys.path:
    sys.path.insert(0, _FRONTEND_DIR)

from fakeconn import FakeConn  # noqa: E402  (sys.path 注入后才能命中)


@pytest.fixture
def conn():
    return FakeConn()


@pytest.fixture
def ctx():
    """模拟 app.py 分发给插件的 RouteCtx（只读字段，页面 handler 只用这些）。"""

    class _Ctx:
        query = {}
        body = {}
        ip = "127.0.0.1"
        user = {"user": "admin", "role": "admin", "label": "系统管理员"}

    return _Ctx()
