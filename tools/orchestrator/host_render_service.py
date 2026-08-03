#!/usr/bin/env python3
"""宿主机 fangyuan 渲染服务（替代容器内 Chrome 渲染）。

背景：容器内 Chrome 151 被站点反爬按环境指纹"软拦截"（只回空心壳、0 卡片），
而宿主机 Chrome 150 已验证能稳定拿到 zu-itemmod 卡片（~12s/35 条）。
本服务在宿主机渲染列表页，容器 worker 的 fangyuan 分支通过 HTTP 调用它。

关键：宿主机必须绕过 macOS 系统代理（127.0.0.1 的代理软件会拦掉该站点），
所以这里自建 ChromiumOptions 并加 --no-proxy-server，不走 make_stealth_browser。

用法（宿主 venv）：
    /tmp/spacefin_captcha_venv/bin/python tools/orchestrator/host_render_service.py [port]
默认端口 8899；容器内经 host.docker.internal:8899 访问。

串行渲染：同一源 chrome_profile 的 Chrome 单实例约束，锁串行最稳。
（已改为 RENDER_SLOTS 个独立 profile 槽并行渲染，见下方 _init_slots / Handler）
"""

import base64
import json
import os
import queue
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE_SRC = os.path.join(ROOT, "tools", "anjuke_crawler", "chrome_profile")
# 必须在 import stealth 之前设置，stealth 在导入时读取 ANJUKE_PROFILE_DIR
os.environ["ANJUKE_PROFILE_DIR"] = PROFILE_SRC
sys.path.insert(0, os.path.join(ROOT, "tools", "anjuke_crawler"))

from fetch.stealth import STEALTH_JS, UA  # noqa: E402


def _load_env():
    """launchd 不加载 .env：服务自行从仓库根 .env 读取 QG_* 凭据（gitignored，不入库）。"""
    env_path = os.path.join(ROOT, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_env()
QG_USER = os.getenv("QG_USER", "")
QG_PWD = os.getenv("QG_PWD", "")

RENDER_SLOTS = int(os.getenv("RENDER_SLOTS", 5))  # 并发渲染槽数（对齐 worker 数，实现 5 路并行）
SLOT_BASE = os.path.join(ROOT, "output", "guangdong", "render_slots")
DEBUG_PORT_BASE = int(os.getenv("RENDER_DEBUG_PORT_BASE", 9222))  # 每槽独占 CDP 端口

RENDER_HANG_S = 180  # 单个槽渲染超过该秒数视为挂死，watchdog 自杀交给 launchd 重启
_WATCHDOG_INTERVAL = 5

# 并发槽：每个槽一份独立 chrome_profile 副本（避免 SingletonLock 争抢），
# 全局串行锁改为 N 路并行（5 个容器 worker 同时渲染 fangyuan）。
_slot_pool = queue.Queue()
SLOT_DIRS = []
_slot_held = {}  # slot_idx -> 起始 monotonic 或 None
_slot_state_lock = threading.Lock()


def _init_slots():
    """启动时把源 chrome_profile 复制为 N 份独立槽目录（含已过的验证码会话）。"""
    os.makedirs(SLOT_BASE, exist_ok=True)
    for i in range(RENDER_SLOTS):
        dst = os.path.join(SLOT_BASE, f"slot{i}")
        if not os.path.exists(dst):
            try:
                if os.path.exists(PROFILE_SRC):
                    # Singleton* 是指向宿主 Chrome 实例的悬空软链，复制会报错且会让新实例
                    # 误判「已有实例在跑」，必须排除
                    shutil.copytree(PROFILE_SRC, dst, ignore=shutil.ignore_patterns("Singleton*"))
                else:
                    os.makedirs(dst, exist_ok=True)
            except Exception as e:  # noqa: BLE001
                print(f"slot{i} copy profile failed: {e}", flush=True)
                os.makedirs(dst, exist_ok=True)
        SLOT_DIRS.append(dst)
        _slot_held[i] = None
        _slot_pool.put(i)
    print(f"render slots initialized: {len(SLOT_DIRS)} (copies under {SLOT_BASE})", flush=True)


def _mark_slot_start(i):
    with _slot_state_lock:
        _slot_held[i] = time.monotonic()


def _mark_slot_end(i):
    with _slot_state_lock:
        _slot_held[i] = None


def _max_slot_held_sec():
    """当前占用最久的槽已持有时长（秒）；供 /health 与 watchdog。"""
    with _slot_state_lock:
        mx = 0.0
        for v in _slot_held.values():
            if v is not None:
                mx = max(mx, time.monotonic() - v)
        return mx


def _watchdog():
    """后台看门狗：任意槽渲染超过 RENDER_HANG_S 即 os._exit(1) 由 launchd 重启。"""
    while True:
        time.sleep(_WATCHDOG_INTERVAL)
        held = _max_slot_held_sec()
        if held > RENDER_HANG_S:
            print(
                f"watchdog: a render slot hung {held:.0f}s > {RENDER_HANG_S}s, "
                "exiting for launchd restart",
                flush=True,
            )
            os._exit(1)


def _clean_orphan_chrome(slot_dir):
    """清掉占用该槽 profile 目录的残留 Chrome（按 user-data-dir 精确匹配，不误杀其他槽）。

    上次渲染异常退出会留下孤儿 Chrome 占着该槽 profile 目录，
    导致新 Chrome 起不来（DrissionPage 报 'NoneType' object has no attribute 'send'）。
    """
    try:
        os.system(f"pkill -9 -f '{slot_dir}' >/dev/null 2>&1 || true")
    except Exception:  # noqa: BLE001
        pass
    for name in ("SingletonLock", "SingletonCookie"):
        try:
            os.remove(os.path.join(slot_dir, name))
        except OSError:
            pass


# ---------------- 本地转发代理（让渲染真实走青果/免费代理） ----------------
# DrissionPage/Chrome 不支持带账号密码的代理（set_proxy 会提示不支持并忽略）。
# 方案：本地起一个无认证 CONNECT 转发代理，Chrome 走 --proxy-server=http://127.0.0.1:PORT，
# 转发代理收到 CONNECT 后连接上游代理，若为青果则在请求头注入
# Proxy-Authorization: Basic（青果按 Basic 认证放行）→ 渲染请求从代理 IP 出口
# （已原型验证：Chrome egress IP 与青果代理 IP 完全一致）。
class _RelayProxy:
    """本地 CONNECT 转发代理：无认证入口 → 上游代理（可选 Basic 认证注入）。"""

    def __init__(self, upstream, auth_b64=None):
        self.upstream = upstream  # "ip:port"
        self.auth_b64 = auth_b64  # base64(user:pwd) 或 None（免费代理）
        self._sock = None
        self.port = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def close(self):
        try:
            self._sock.close()  # accept 循环随即因 OSError 退出
        except OSError:
            pass

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        up = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    client.close()
                    return
                head += chunk
            first = head.split(b"\r\n", 1)[0].decode("iso-8859-1")
            parts = first.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.close()
                return
            host, _, port = parts[1].rpartition(":")
            up_host, up_port = self.upstream.rsplit(":", 1)
            up = socket.create_connection((up_host, int(up_port)), timeout=15)
            up.settimeout(30)
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if self.auth_b64:
                req += f"Proxy-Authorization: Basic {self.auth_b64}\r\n"
            req += "\r\n"
            up.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = up.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b"200" not in resp.split(b"\r\n", 1)[0]:
                try:
                    client.sendall(resp)  # 透传上游错误（如 407/408）
                except OSError:
                    pass
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            t1 = threading.Thread(target=self._pump, args=(client, up), daemon=True)
            t2 = threading.Thread(target=self._pump, args=(up, client), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except Exception:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            if up is not None:
                try:
                    up.close()
                except OSError:
                    pass

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def _render(city, page_no, proxy=None, auth=0, profile_dir=None, debug_port=None):
    """用宿主 Chrome 渲染列表页，返回 HTML。

    profile_dir 为该并发槽独立的 chrome_profile 副本（避免 SingletonLock 争抢）。
    debug_port 为该槽独占的 CDP 调试端口：DrissionPage 默认全局 9222，多槽并行时
    后起的实例会连到前一个实例上，任一实例 quit() 就让其它槽 PageDisconnectedError。
    proxy 经本地转发代理真实走代理池出口（青果 Basic 认证 / 免费无认证）。
    硬性禁止直连：proxy 为空直接抛错，绝不让 Chrome 用宿主 IP 出口
    （宿主 IPv6 出口已被安居客 58 反爬验证码墙标记，直连必然空转且违反「全程走 IP 池」）。
    """
    from DrissionPage import ChromiumOptions, ChromiumPage

    if not proxy:
        raise ValueError("proxy is required: direct host-IP rendering is disabled")

    _clean_orphan_chrome(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)
    co = ChromiumOptions()
    co.set_user_data_path(profile_dir)
    if debug_port:
        co.set_local_port(debug_port)
    auth_b64 = (
        base64.b64encode(f"{QG_USER}:{QG_PWD}".encode()).decode() if (auth and QG_USER) else None
    )
    relay = _RelayProxy(proxy, auth_b64=auth_b64).start()
    co.set_argument(f"--proxy-server=http://127.0.0.1:{relay.port}")
    # DevTools/CDP on 127.0.0.1 must bypass the relay proxy, else Chrome
    # routes the CDP websocket through it and the upstream proxy returns 404.
    co.set_argument("--proxy-bypass-list=<-loopback>")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--lang=zh-CN,zh;q=0.9")
    co.set_user_agent(UA)
    co.set_argument("--window-size=1440,900")
    co.headless()

    page = ChromiumPage(co)
    try:
        for _ in range(3):
            try:
                page.run_js(STEALTH_JS)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        home = f"https://{city}.zu.anjuke.com/"
        target = f"https://{city}.zu.anjuke.com/fangyuan/p{page_no}/"
        page.get(home, timeout=20)
        time.sleep(3)
        page.get(target, timeout=20)
        time.sleep(3)
        return page.html
    finally:
        try:
            page.quit()
        except Exception:
            pass
        # quit() 在异常路径（PageDisconnectedError 等）常留下孤儿 Chrome 占着本槽的
        # profile 与 CDP 端口，下次渲染 attach 到僵尸实例。渲染后按槽路径再清一次。
        _clean_orphan_chrome(profile_dir)
        if relay is not None:
            relay.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            body = json.dumps(
                {
                    "status": "ok",
                    "slot_held_sec": round(_max_slot_held_sec(), 1),
                    "slots": len(SLOT_DIRS),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/render"):
            self.send_error(404)
            return
        q = parse_qs(urlparse(self.path).query)
        city = (q.get("city") or ["zs"])[0]
        page = int((q.get("page") or ["1"])[0])
        proxy = (q.get("proxy") or [None])[0]
        auth = int((q.get("auth") or ["0"])[0])
        # 硬性禁止直连：不带 proxy 的渲染请求直接拒绝（不走宿主 IP 出口）
        if not proxy:
            body = json.dumps(
                {"error": "proxy required: direct host-IP rendering is disabled"}
            ).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 取一个空闲并发槽（满则阻塞至有空槽）；N 槽 = N 路并行渲染
        slot_idx = _slot_pool.get()
        try:
            _mark_slot_start(slot_idx)
            try:
                try:
                    html = _render(
                        city,
                        page,
                        proxy=proxy,
                        auth=auth,
                        profile_dir=SLOT_DIRS[slot_idx],
                        debug_port=DEBUG_PORT_BASE + slot_idx,
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"slot{slot_idx} render {city} p{page} failed: {e!r}", flush=True)
                    body = json.dumps({"error": repr(e)}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            finally:
                _mark_slot_end(slot_idx)
        finally:
            _slot_pool.put(slot_idx)
        body = (html or "").encode("utf-8", "ignore")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _raise_fd_limit(target=8192):
    """抬高文件描述符软上限。

    launchd 启动时继承的 maxfiles 软上限仅 256，5 路并行 Chrome + CDP 套接字会打满，
    表现为 OSError(24, 'Too many open files') 后渲染槽整体失效。plist 里也配了
    SoftResourceLimits 作为第一道，这里再自查兜底（手动 / start_all.sh 启动时同样生效）。
    """
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= target:
        return soft
    new = min(target, hard) if hard != resource.RLIM_INFINITY else target
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (new, hard))
        return new
    except Exception as e:  # noqa: BLE001
        print(f"raise fd limit failed ({soft} -> {new}): {e!r}", flush=True)
        return soft


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    nofile = _raise_fd_limit()
    _init_slots()
    print(f"host render service on :{port} profile={PROFILE_SRC} nofile={nofile}", flush=True)
    threading.Thread(target=_watchdog, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
