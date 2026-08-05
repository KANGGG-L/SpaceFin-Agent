"""orchestrator 测试的公共辅助函数（非 pytest fixture）。

为什么不写在 conftest.py 里、让测试 `from conftest import ...`：
仓库里多个 tests 目录都有 conftest.py 且都没有 __init__.py，pytest 的 prepend 导入模式
会把各 tests 目录挨个插进 sys.path，`import conftest` 只能拿到**最先进入 sys.path 的那个**。
于是 `pytest tools/` 联跑时，本目录的 `from conftest import get_json` 会串到
tools/anjuke_crawler/tests/conftest.py 上并 ImportError。

约定：conftest.py 只放 @pytest.fixture，普通辅助函数放本文件（唯一模块名，不会串目录）。
tools/{risk,reporting,alerting,spatial}/tests 下的 riskmods/g11mods/alertmods/spatmods 同理。
"""

import contextlib
import json
import threading
import urllib.request


@contextlib.contextmanager
def running_master(master_mod, rdb, port=15100):
    """在后台线程真起 master 的 HTTPServer（绑定 db 15 的 rdb），yield base url。"""
    srv = None
    last = None
    for p in (port, 0):
        try:
            srv = master_mod.MasterServer(("127.0.0.1", p), rdb)
            break
        except OSError as e:  # 端口被占用 → 退化到临时端口
            last = e
    if srv is None:
        raise last
    srv.role = "leader"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def get_json(base, path):
    """打真实 HTTP 请求。必须显式禁代理：系统代理会拦截 python urllib。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def seed_pool(rdb, pool_key, proxies, source):
    """向代理池注入若干可弹出的代理（结构与 master 注入格式一致）。"""
    for p in proxies:
        rdb.hset(pool_key, p, json.dumps({"proxy": p, "source": source, "https": True}))


def seed_tasks(master_mod, rdb, finished=False, **extra):
    """按 master 的 42 任务定义写 task hash（默认 finished=0）。"""
    for t in master_mod.DEFAULT_TASKS:
        key = master_mod._task_key(t["city"], t["type"])
        state = {
            "city": t["city"],
            "type": t["type"],
            "pages": t["pages"],
            "target": t["target"],
            "round": 0,
            "status": "pending",
            "finished": "1" if finished else "0",
            "finish_reason": "",
            "worker": "",
            "count": 0,
            "new_count": 0,
            "dup_count": 0,
            "worker_hb": 0,
        }
        state.update(extra)
        rdb.hset(key, mapping=state)
