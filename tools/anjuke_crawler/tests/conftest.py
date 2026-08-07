"""anjuke_crawler 组件测试共享 fixture。"""

import os
import sys

import pytest

# 让测试无论从哪里跑都能 import 到包（包根 = tests/ 的上一级 = tools/）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_listing.html")


@pytest.fixture(scope="session")
def fixture_html():
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def mock_proxy_server():
    """复刻 jhao104/proxy_pool 的 /get//pop//delete/ 契约（fakeredis 存代理）。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import fakeredis

    store = fakeredis.FakeRedis()
    for p in ["10.0.0.1:8080", "10.0.0.2:8080", "10.0.0.3:8080"]:
        store.zadd("proxies", {p: 1.0})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith(("/get/", "/pop/")):
                p = store.zpopmax("proxies", 1)
                self._json({"proxy": p[0][0].decode()} if p else {})
            elif self.path.startswith("/delete/"):
                store.zrem("proxies", self.path.split("proxy=")[-1])
                self._json({"code": 0})
            else:
                self._json({})

    httpd = HTTPServer(("127.0.0.1", 0), Handler)  # 随机空闲端口
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", store
    httpd.shutdown()
