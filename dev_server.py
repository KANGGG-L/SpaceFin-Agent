#!/usr/bin/env python3
"""SpaceFin 前后端开发热重载监督器（零依赖，纯 stdlib 实现）。

前端: tools/frontend/app.py        -> http://127.0.0.1:${FRONTEND_PORT:-8500}
后端: tools/orchestrator/master.py -> http://127.0.0.1:${BACKEND_PORT:-5102}

与 Docker orchestrator 栈并存的设计：
  - 后端默认跑在 5102，避开 Docker master 占用的 5100，互不冲突、互不重启。
  - 后端连 Redis 默认用 db=1（REDIS_DB），与 Docker master（db=0）隔离，
    不抢 spacefin:master:leader 锁，不会打断真实采集链路。

特性：
  - 改源码自动重启对应进程（轮询文件 mtime，无需 watchdog / entr / npm）。
  - 前端监听整个 tools/frontend（含 db.py / data_classification.py / pages / static），
    弥补内置 SF_HOTRELOAD 只覆盖 app.py+pages+static 的盲区。
  - 后端监听 master.py；进程崩溃后每 10s 自愈重试（基础设施就绪后自动拉起）。
  - 启动前做端口预检，被占用时给出明确可执行提示而非静默重启循环。
  - 日志同时落 output/dev/*.log 并带 [frontend]/[backend] 前缀打到控制台。
  - Ctrl+C / SIGTERM 优雅退出并回收子进程。

前置：MySQL（make up / docker start spacefin-mysql）+ Redis（make infra / docker start spacefin-redis）已在 127.0.0.1 起好。
用法：
    python3 dev_server.py                      # 拉起前后端 + 热重载（前端8500 / 后端5102）
    python3 dev_server.py --backend-port 5102  # 指定后端端口
    python3 dev_server.py --no-frontend        # 只拉后端
    python3 dev_server.py --no-backend         # 只拉前端
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.abspath(__file__))
VENV = sys.executable  # Force use current Python
if not os.path.exists(VENV):
    VENV = sys.executable  # 兜底：用当前解释器

FRONTEND_DIR = os.path.join(REPO, "tools", "frontend")
BACKEND_DIR = os.path.join(REPO, "tools", "orchestrator")
LOG_DIR = os.path.join(REPO, "output", "dev")
os.makedirs(LOG_DIR, exist_ok=True)

# 监听时跳过的目录
SKIP_DIRS = {"__pycache__", ".venv", "tests", "sql", ".git", "node_modules"}
WATCH_EXTS = (".py", ".js", ".css", ".html")


def load_env():
    """加载仓库 .env，作为子进程环境（不覆盖已存在的环境变量），与 Makefile 行为一致。"""
    env = dict(os.environ)
    ep = os.path.join(REPO, ".env")
    if os.path.exists(ep):
        with open(ep, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                env.setdefault(k, v)
    return env


def iter_files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    if f.endswith(WATCH_EXTS):
                        yield os.path.join(root, f)


def preflight_port(port, name):
    """启动前检测端口是否被占，被占则打印可执行提示并退出。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        print(f"[dev] ERROR: {name} 端口 {port} 已被占用。")
        print(f"[dev]   查看占用： ss -ltnp | grep :{port}")
        print(
            "[dev]   若是 Docker master 冲突，可换端口： python3 dev_server.py --backend-port 5102"
        )
        print("[dev]   或停掉它： docker stop spacefin-crawler-master-primary")
        sys.exit(1)
    finally:
        s.close()


class Proc:
    def __init__(self, name, cmd, cwd, env, watch, log_path):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.watch = watch
        self.log_path = log_path
        self.p = None
        self.reader = None
        self.dead_since = 0.0
        self.last_sig = self._signature()

    def _signature(self):
        m = 0.0
        for f in iter_files(self.watch):
            try:
                m = max(m, os.path.getmtime(f))
            except OSError:
                pass
        return m

    def changed(self):
        s = self._signature()
        if s != self.last_sig:
            self.last_sig = s
            return True
        return False

    def start(self):
        self.logfile = open(self.log_path, "a", buffering=1, encoding="utf-8")
        self.p = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        print(f"[dev] {self.name} started pid={self.p.pid} -> {self.log_path}", flush=True)

    def _pump(self):
        assert self.p is not None and self.p.stdout is not None
        try:
            for line in self.p.stdout:
                self.logfile.write(line)
                self.logfile.flush()
                sys.stdout.write(f"[{self.name}] {line}")
        finally:
            try:
                self.logfile.close()
            except Exception:
                pass

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
        if self.reader:
            self.reader.join(timeout=2)

    @property
    def alive(self):
        return self.p is not None and self.p.poll() is None


def main():
    ap = argparse.ArgumentParser(description="SpaceFin 前后端热重载监督器")
    ap.add_argument("--frontend-port", type=int, default=int(os.getenv("FRONTEND_PORT", 8500)))
    ap.add_argument("--backend-port", type=int, default=int(os.getenv("BACKEND_PORT", 5102)))
    ap.add_argument(
        "--redis-db",
        type=int,
        default=int(os.getenv("DEV_REDIS_DB", 1)),
        help="后端连的 Redis DB（默认 1，与 Docker master 的 db=0 隔离）",
    )
    ap.add_argument("--no-frontend", action="store_true")
    ap.add_argument("--no-backend", action="store_true")
    args = ap.parse_args()

    # 端口预检：被占用直接给提示退出，避免静默重启循环
    if not args.no_frontend:
        preflight_port(args.frontend_port, "前端")
    if not args.no_backend:
        preflight_port(args.backend_port, "后端")

    env = load_env()
    # 本地直连 docker 暴露到宿主的 Redis（master 默认连 docker 服务名 spacefin-redis，本地需改 127.0.0.1）
    env["REDIS_HOST"] = "127.0.0.1"
    env["REDIS_PORT"] = env.get("REDIS_PORT", "6379")
    env["SF_DEV_MODE"] = "1"  # 前端本地开发用内置弱口令登录，省去记随机强口令
    # 注意：这里刻意不注入 SF_HOTRELOAD。app.py 的内置自热重载是「收到
    # /api/reload-version 请求时才 os.execv」，浏览器 1s 轮询会让它在下面 Proc
    # 监督器 1s 轮询之前抢先重启；Windows 上 execv 是新起进程 + 旧进程退出，
    # 监督器随后自愈又拉起一个 → 8501 上出现两个 LISTENING 进程（实测 7272/16700），
    # 请求会被随机分配到两个不同版本的前端。监督器监听整个 tools/frontend，
    # 覆盖 pages/*.py 与 static，功能上是内置热重载的超集，故只保留监督器一条路径。
    # 「不注入」≠「清洗」：load_env() 以 dict(os.environ) 起始，启动 shell 里继承来的
    # SF_HOTRELOAD=1 会原样漏给孩子，于是监督器与 app.py 的 os.execv 同时武装 → 一次编辑重启两次。
    env.pop("SF_HOTRELOAD", None)
    env["TZ"] = "Asia/Shanghai"  # Fix tzdata issue for ZoneInfo

    procs = []
    if not args.no_frontend:
        procs.append(
            Proc(
                "frontend",
                [
                    VENV,
                    "tools/frontend/app.py",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(args.frontend_port),
                ],
                cwd=REPO,
                env=dict(env),
                watch=[FRONTEND_DIR],
                log_path=os.path.join(LOG_DIR, "frontend.log"),
            )
        )
    if not args.no_backend:
        be = dict(env)
        be["REDIS_HOST"] = "127.0.0.1"
        be["REDIS_DB"] = str(args.redis_db)  # 与 Docker master 隔离
        be["MASTER_PORT"] = str(args.backend_port)
        procs.append(
            Proc(
                "backend",
                [VENV, "tools/orchestrator/master.py"],
                cwd=REPO,
                env=be,
                watch=[os.path.join(BACKEND_DIR, "master.py")],
                log_path=os.path.join(LOG_DIR, "backend.log"),
            )
        )

    for pr in procs:
        pr.start()

    stop = threading.Event()

    def handle(signum, _frame):
        print(f"\n[dev] received signal {signum}, shutting down...")
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    print("[dev] ==========================================================")
    print(
        f"[dev] 前端: http://127.0.0.1:{args.frontend_port}   后端 API: http://127.0.0.1:{args.backend_port} (redis db={args.redis_db})"
    )
    print("[dev] 改动源码自动热重载；Ctrl+C 退出。日志: output/dev/*.log")
    print("[dev] ==========================================================")

    while not stop.is_set():
        time.sleep(1)
        now = time.time()
        for pr in procs:
            if pr.p is None:
                # 曾崩溃：每 10s 自愈重试（基础设施就绪后自动拉起）
                if now - pr.dead_since > 10:
                    print(f"[dev] {pr.name} self-heal retry...", flush=True)
                    try:
                        pr.start()
                    except Exception as e:  # noqa: BLE001
                        print(f"[dev] {pr.name} start failed: {e}", flush=True)
                        pr.dead_since = now
                continue
            if not pr.alive:
                print(f"[dev] {pr.name} exited (code={pr.p.returncode})", flush=True)
                pr.p = None
                pr.dead_since = now
                continue
            if pr.changed():
                print(f"[dev] {pr.name} source changed -> restarting", flush=True)
                pr.stop()
                pr.start()

    for pr in procs:
        pr.stop()
    print("[dev] stopped.")


if __name__ == "__main__":
    main()
