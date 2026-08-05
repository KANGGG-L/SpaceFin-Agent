"""pytest 公共装置：真实 Redis(db 15) + 按环境变量动态加载 master/worker 模块。

安全约束（最重要）：
- 生产采集数据在 **db 0**（断点 crawl_progress:*、去重集合 crawled_urls:*）。
  本目录所有测试只允许操作 **db 15**，flush 前强制校验连接的 db 编号，
  误连 db 0 直接 assert 失败而不是清库。
- 不启动/重启任何容器，只连已在运行的 spacefin-redis。

master.py / worker.py 的配置常量都在 **模块导入时**读 env（IP_BUDGET_*、CRAWL_RUN_ID、
HEAD_REWIND ...），所以测试要换配置必须按 env 重新加载一份独立模块实例，
用 importlib 按唯一模块名加载，互不干扰。
"""

import importlib.util
import itertools
import os
import sys

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.dirname(TESTS_DIR)
MASTER_PY = os.path.join(ORCH_DIR, "master.py")
WORKER_PY = os.path.join(ORCH_DIR, "worker.py")

TEST_DB = 15
REDIS_HOST = os.getenv("TEST_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("TEST_REDIS_PORT", "6379"))

_seq = itertools.count()
BACKEND = {"fake": False}


def _assert_test_db(rdb):
    """严禁在非 db 15 上做写/清操作。"""
    db = getattr(rdb, "connection_pool", None)
    db = db.connection_kwargs.get("db") if db is not None else None
    assert db == TEST_DB, f"refuse to touch redis db={db!r}, tests may only use db {TEST_DB}"


def _new_client():
    import redis as _redis

    try:
        c = _redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=TEST_DB,
            decode_responses=True,
            socket_connect_timeout=3,
        )
        c.ping()
        return c
    except Exception:  # noqa: BLE001 — 真实 redis 不可用则降级 fakeredis
        try:
            import fakeredis
        except ImportError:
            pytest.skip("real redis unavailable and fakeredis not installed")
        BACKEND["fake"] = True
        return fakeredis.FakeRedis(db=TEST_DB, decode_responses=True)


@pytest.fixture
def rdb():
    """干净的 db 15 客户端；用例前后各清一次，保证可重复运行。"""
    c = _new_client()
    _assert_test_db(c)
    c.flushdb()
    yield c
    _assert_test_db(c)
    c.flushdb()


def _load(path, prefix, env):
    """按 env 加载一份独立的模块实例（模块级常量随 env 生效）。"""
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        name = f"{prefix}_t{next(_seq)}"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def load_master():
    def _loader(**env):
        return _load(MASTER_PY, "master", env)

    return _loader


@pytest.fixture
def load_worker():
    def _loader(**env):
        return _load(WORKER_PY, "worker", env)

    return _loader
