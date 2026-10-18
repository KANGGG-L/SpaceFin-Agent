#!/usr/bin/env python
"""S5 前端驾驶舱 · HTTP 服务（stdlib，零第三方 Web 依赖）。

为什么用 stdlib http.server 而不是 Streamlit/FastAPI：
1. 全仓唯一 Python 环境（tools/orchestrator/.venv = conda spark 3.10）已含 PyMySQL，
   装 Web 框架会往共享 conda 环境塞十几到几十个包，污染其他流水线（spark/调度依赖同一环境）；
2. 页面与数据体量小（内置 3 页 + 6 个插件页，合计 9 页；数据量级有限），
   ThreadingHTTPServer + 每请求短连接足够，不值得引入进程常驻的 ASGI 服务 + 构建链；
3. RBAC/会话在服务端强制校验（每个 API 先查角色再放行），前端仅做展示，
   与「前端隐藏页面」是两层防御，后者只是 UX 裁剪。

运行（后台）：
    nohup tools/orchestrator/.venv/bin/python tools/frontend/app.py \
        --port 8500 >> output/frontend/app.log 2>&1 &

开发热重载（前端大规模改版时启用；仅本机）：设置 SF_HOTRELOAD=1 后，
前端每 1s 轮询版本令牌，静态文件（js/css/html）改动即自动刷新，
Python（app.py / pages/*.py）改动由服务端原地重启重载：
    SF_HOTRELOAD=1 nohup tools/orchestrator/.venv/bin/python tools/frontend/app.py \
        --port 8500 >> output/frontend/app.log 2>&1 &

默认监听 127.0.0.1（仅本机）；--host 0.0.0.0 可暴露局域网，README 有说明。
生产环境不设置 SF_HOTRELOAD 即完全无热重载副作用。
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FrontendHTTPServer(ThreadingHTTPServer):
    """提高连接队列：首屏会瞬间并发 20+ 请求（静态资源 + 全部插件页 js + api），
    ThreadingHTTPServer 默认 request_queue_size=5，浏览器首开/服务重启瞬间
    backlog 打满 → TCP 层丢连接 → 首发请求偶发 "Failed to fetch"（首拉竞态根因）。
    加大队列让排队由内核吸收，而不是让浏览器侧靠重试兜底。"""

    request_queue_size = 128
    daemon_threads = True


from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import pages as page_registry  # noqa: E402
from data_classification import level_of, mask_value  # noqa: E402  # G2 导出分级脱敏

# ---- 热重载（零依赖；仅 SF_HOTRELOAD=1 时启用 Python 自动重启）----
# 原理：客户端每 1s 轮询 /api/reload-version 取版本令牌（启动时间戳:最大文件 mtime）。
# · 静态文件（js/css/html）改动 → mtime 变 → 令牌变 → 浏览器刷新（服务端按磁盘直读，无需重启）。
# · Python（app.py / pages/*.py）改动 → 该端点原地 os.execv 重启重载；重启后启动时间戳变 →
#   令牌变 → 浏览器刷新命中新进程。生产环境不设置 SF_HOTRELOAD 即完全无副作用。
BOOT_TS = time.time()


def _hotreload_watch_paths():
    base = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.abspath(__file__)]
    for root, _, files in os.walk(os.path.join(base, "pages")):
        for f in files:
            if f.endswith(".py"):
                paths.append(os.path.join(root, f))
    for root, _, files in os.walk(os.path.join(base, "static")):
        for f in files:
            if f.endswith((".js", ".css", ".html")):
                paths.append(os.path.join(root, f))
    return paths


_HOTRELOAD_PATHS = _hotreload_watch_paths()


def _hotreload_max_mtime():
    m = 0.0
    for p in _HOTRELOAD_PATHS:
        try:
            m = max(m, os.path.getmtime(p))
        except OSError:
            pass
    return m


def _hotreload_py_changed():
    # 0.5s 去抖：避免编辑器「临时文件 + 改名」写一半就触发重启。
    now = time.time()
    for p in _HOTRELOAD_PATHS:
        if not p.endswith(".py"):
            continue
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt > BOOT_TS and (now - mt) > 0.5:
            return True
    return False


# ---------------- 用户与 RBAC ----------------
# 仅开发环境内置账号；凭据写死在代码里并标注 dev-only，上线前必须接统一认证。
# 角色矩阵（PRD §7.3）：DA 只读 / 风控可写（确认/导出）/ 贷后不可见报送页。
#
# 安全加固（VPS 部署）：密码优先读环境变量 SF_PWD_<ROLE>（强制强口令）；
# 未设置时若处于 SF_DEV_MODE=1 则回退到内置弱口令（仅本地开发），否则启动时
# 随机生成强口令（持久化到 output/frontend/credentials.json，重启保持稳定，
# 避免公网暴露固定弱凭据，也避免每次重启口令漂移）。

# RBAC（精简版）：原 admin/risk/da 三角色权限完全一致，已融合为单一内部管理角色 `admin`；
# 仅保留 `postloan`（贷后资产保全）作为受限角色——按 PRD §7.3 不可见 1104 报送页。
_ROLE_LABELS = {
    "admin": "内部管理(风控/分析/管理员)",
    "postloan": "贷后资产保全",
}

_CRED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output",
    "frontend",
    "credentials.json",
)


_ENV_PWDS = {
    r: os.environ[f"SF_PWD_{r.upper()}"]
    for r in _ROLE_LABELS
    if os.environ.get(f"SF_PWD_{r.upper()}")
}


def _resolve_passwords() -> dict:
    """返回 role -> password。优先级：环境变量 > 持久化文件 > 随机生成并落盘。"""
    # 1) 环境变量最高优先
    env_pwds = dict(_ENV_PWDS)
    # 2) dev 模式回退弱口令
    if os.environ.get("SF_DEV_MODE") == "1":
        return {r: r for r in _ROLE_LABELS}  # dev-only 弱口令：密码 = 账号名
    # 3) 读持久化文件（若存在且含全部角色）
    loaded = {}
    if os.path.exists(_CRED_FILE):
        try:
            with open(_CRED_FILE, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError):
            loaded = {}
    merged = dict(loaded)
    changed = False
    for r in _ROLE_LABELS:
        if r not in merged:
            merged[r] = secrets.token_urlsafe(16)
            changed = True
    # 环境变量覆盖持久化值
    merged.update(env_pwds)
    if changed or env_pwds:
        try:
            os.makedirs(os.path.dirname(_CRED_FILE), exist_ok=True)
            with open(_CRED_FILE, "w", encoding="utf-8") as fh:
                json.dump({r: merged[r] for r in _ROLE_LABELS}, fh)
        except OSError:
            pass  # 落盘失败不影响运行，仅下次重启重新生成
    return {r: merged[r] for r in _ROLE_LABELS}


_PWDS = _resolve_passwords()

USERS = {
    role: {"password": _PWDS[role], "role": role, "label": label}
    for role, label in _ROLE_LABELS.items()
}

# 非开发模式（无 SF_DEV_MODE=1）下，强口令已生成并持久化，不打印明文口令。
if os.environ.get("SF_DEV_MODE") != "1" and not _ENV_PWDS:
    sys.stderr.write(
        f"[frontend] 凭据为强口令（非 dev 模式），已持久化至 {_CRED_FILE}；"
        "设置 SF_PWD_<ROLE> 环境变量可固定口令。\n"
    )

# 调试阶段取消 RBAC（仅本地开发）：
#   - 由 SF_DEV_MODE=1（dev.sh / dev_server.py 已置 1）或显式 SF_NO_RBAC=1 触发；
#   - 触发后免登录、所有请求按内部管理角色 admin 处理，跳过 401/403 与页面可见性限制；
#   - 生产环境不设置这些变量，仍走完整鉴权，不影响线上安全。
DEV_DISABLE_RBAC = os.environ.get("SF_DEV_MODE") == "1" or os.environ.get("SF_NO_RBAC") == "1"
if DEV_DISABLE_RBAC:
    sys.stderr.write(
        "[frontend] ⚠️ RBAC 已关闭（调试模式）：免登录、所有操作视为 admin，仅限本地开发。\n"
    )

_DEV_USER = {
    "user": "dev",
    "role": "admin",
    "label": "内部管理(风控/分析/管理员) [DEV-RBAC-OFF]",
}

# 页面可见性：role -> pages。admin 已融合原 risk/da，三者权限一致。
# 导航顺序：order 即数据链路主线的全局序号（驾驶舱 → 数据底座 → 估值 → 空间风险
# → 分类迁徙 → 触发预警 → 策略推演 → 出监管报表 → 全程留痕），分组只是视觉分区，
# 归属见 pages/__init__.py 的 _PAGE_GROUP、组的先后见 static/app.js 的 renderNav。
PAGE_VISIBILITY = {
    "admin": [
        {"id": "dashboard", "label": "资产质量驾驶舱", "group": "overview", "order": 10},
        {"id": "alerts", "label": "LTV 爆仓预警列表", "group": "monitoring", "order": 60},
        {"id": "report", "label": "1104 G11 资产质量报送", "group": "governance", "order": 80},
    ],
    "postloan": [
        {"id": "dashboard", "label": "资产质量驾驶舱", "group": "overview", "order": 10},
        {"id": "alerts", "label": "LTV 爆仓预警列表", "group": "monitoring", "order": 60},
    ],
}

# 操作级权限：仅内部管理角色 admin 可用（已融合原 risk/da）。
CAN_CONFIRM = {"admin"}


def _resolve_persona_python():
    """挑一个能 import scipy 的解释器来跑 persona 脚本（结果缓存）。

    persona_report.json 由 tools/persona/main.py 生成，依赖 scipy；服务进程自身
    解释器（orchestrator venv）不带 scipy。优先用自身解释器（若已带 scipy），
    否则在已知路径里找一个带 scipy 的（本项目测试解释器为 miniconda，见
    project-windows-wsl-runtime 记忆）；找不到就退回自身解释器并让脚本报错——
    那种情况下审计会记 failure，页面也看得到错误，不会静默。
    """
    if getattr(_resolve_persona_python, "_cached", None):
        return _resolve_persona_python._cached

    def _has_scipy(py):
        try:
            return (
                subprocess.run(
                    [py, "-c", "import scipy"], capture_output=True, timeout=20
                ).returncode
                == 0
            )
        except OSError:
            return False

    # 已知候选：自身解释器 → 环境变量指定的解释器（如宿主 conda，可在 .env/SF 环境中
    # 配置 SPACEFIN_PERSONA_PYTHON）→ PATH 上的 python3。空值候选自动剔除。
    candidates = [
        c for c in (sys.executable, os.environ.get("SPACEFIN_PERSONA_PYTHON"), "python3") if c
    ]
    for cand in candidates:
        if _has_scipy(cand):
            _resolve_persona_python._cached = cand
            return cand
    _resolve_persona_python._cached = sys.executable
    return sys.executable


CAN_EXPORT = {"admin"}
# 页面级可见性（PRD §7.3：贷后不可见报送页）。与 PAGE_VISIBILITY 联动，
# 服务端必须再次校验，不能只依赖前端隐藏导航。
CAN_VIEW_REPORT = {
    role for role, pages in PAGE_VISIBILITY.items() if any(p["id"] == "report" for p in pages)
}

SESSION_TTL_SECONDS = 12 * 3600
_sessions = {}
_sessions_lock = threading.Lock()

# G8 健康度：内存超过该值(MB)判定 degraded（与 ops/manage.sh 的 WARN_AVAIL_MB 同级口径）。
MEM_DEGRADED_MB = 2560

# 进程启动时刻，用于 uptime 计算（monotonic，不受系统时间回拨影响）。
_PROCESS_START = time.monotonic()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def _nav_for(role):
    """导航 = 内置三页（dashboard/alerts/report）+ pages/ 插件页，按 order 全局排序。

    必须排在这里而不是让两组各自有序：拼接序会让内置页永远抢占所属组的首位，
    order 就退化成「只在插件之间生效」，无法把预警排到识别类页面之后。
    内置页无 js 字段（渲染逻辑在 app.js 里），插件页带 js 供前端动态加载。
    """
    builtin = [dict(p, js=None) for p in PAGE_VISIBILITY.get(role, [])]
    return sorted(builtin + page_registry.nav_for(role), key=lambda p: p["order"])


class RouteCtx:
    """插件页面 handler 的入参：只暴露 query / body / user / ip，隔离 HTTP 细节。

    ip 由框架统一下发而非让页面自己去掏 client_address：写操作审计（R-UNW-02）要求
    可追溯到来源，逐页自取必然有人漏写，漏了还不会报错——审计缺字段是静默失败。
    """

    __slots__ = ("query", "body", "user", "ip")

    def __init__(self, query, body, user, ip="-"):
        self.query = query
        self.body = body
        self.user = user
        self.ip = ip


def _json_default(o):
    """PyMySQL 返回的 Decimal/date/datetime → JSON 可序列化。"""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def _json(data):
    return json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")


def _valid_stat_date(value):
    """1104 报送 date 参数边界校验：必须是合法 YYYY-MM-DD（防脏串进 SQL 产生怪状态）。"""
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


class SpaceFinApp(BaseHTTPRequestHandler):
    """单处理器承载全部路由；RBAC 在 API 分发前统一校验。"""

    server_version = "SpaceFinFrontend/0.1"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, data, extra=None):
        # API 响应禁用浏览器缓存：资产/风险类数据必须每次请求都拿到最新库态，
        # 否则页面看起来永远是"固定值"，与当前状态脱节。
        merged = {"Cache-Control": "no-store"}
        if extra:
            merged.update(extra)
        self._send(code, _json(data), extra=merged)

    def _send_error(self, code, msg):
        self._send_json(code, {"error": msg})

    def _parse_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------- 会话 ----------

    def _session_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "spf_session":
                return v
        return None

    def _current_user(self):
        """返回 session 里的用户信息；过期/未登录返回 None。"""
        if DEV_DISABLE_RBAC:
            return _DEV_USER
        token = self._session_token()
        if not token:
            return None
        with _sessions_lock:
            sess = _sessions.get(token)
            if not sess:
                return None
            if time.time() - sess["created"] > SESSION_TTL_SECONDS:
                _sessions.pop(token, None)
                return None
            return sess

    def _require(self, roles=None):
        """RBAC 统一入口：未登录 401，角色不符 403。roles=None 表示仅需登录。

        调试模式（DEV_DISABLE_RBAC）下直接放行，按 admin 处理，跳过全部鉴权。
        """
        if DEV_DISABLE_RBAC:
            return _DEV_USER
        user = self._current_user()
        if not user:
            self._send_error(401, "未登录或会话已过期")
            return None
        if roles and user["role"] not in roles:
            self._send_error(403, f"角色 {user['label']} 无此操作权限")
            return None
        return user

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
        elif path in ("/style.css", "/app.js", "/i18n.js"):
            self._serve_static(path.lstrip("/"))
        elif path.startswith("/pages/") and path.endswith(".js"):
            # 插件页面的前端模块；_serve_static 已做目录穿越防护。
            self._serve_static(path.lstrip("/"))
        elif path.startswith("/vendor/"):
            # 第三方前端库（本地自托管，如 Leaflet）；_serve_static 已做目录穿越防护。
            self._serve_static(path.lstrip("/"))
        elif path.startswith("/tiles/") and path.endswith(".png"):
            # 本地预下载的离线瓦片（download_tiles.py 生成）；同源提供，浏览器无需外网。
            self._serve_static(path.lstrip("/"))
        elif path == "/api/me":
            self._handle_me()
        elif path == "/api/dashboard":
            user = self._require()
            if user:
                self._send_json(200, db.dashboard())
        elif path == "/api/alerts":
            user = self._require()
            if user:
                self._handle_alerts(parsed)
        elif path == "/api/alerts/export":
            user = self._require(CAN_EXPORT)
            if user:
                self._handle_export(parsed, user)
        elif path == "/api/alerts/detail":
            user = self._require()  # 单笔详情只读，登录即可
            if user:
                self._handle_detail(parsed)
        elif path == "/api/report":
            user = self._require(CAN_VIEW_REPORT)
            if user:
                qs = parse_qs(parsed.query)
                self._send_json(200, db.report(qs.get("date", [None])[0]))
        elif path == "/api/report/dates":
            user = self._require(CAN_VIEW_REPORT)
            if user:
                self._send_json(200, {"dates": db.report_dates()})
        elif path == "/api/metrics":
            user = self._require()  # G8：健康度需登录，不对外匿名暴露
            if user:
                self._handle_metrics(user)
        elif path == "/healthz":
            # 匿名存活探针（供 Caddy / 外部探活），不暴露任何内部信息。
            self._send_json(200, {"status": "ok"})
        elif path == "/api/reload-version":
            # 热重载探针：返回版本令牌（启动时间戳:最大文件 mtime）。
            # 仅当 SF_HOTRELOAD=1 且检测到 Python 文件改动时，原地重启本进程以重载模块。
            if os.environ.get("SF_HOTRELOAD") == "1" and _hotreload_py_changed():
                sys.stdout.flush()
                sys.stderr.flush()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            self._send_json(200, {"version": f"{BOOT_TS}:{_hotreload_max_mtime()}"})
        elif not self._dispatch_plugin("GET", parsed):
            self._send_error(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/login":
            self._handle_login()
        elif path == "/api/logout":
            self._handle_logout()
        elif path == "/api/alerts/confirm":
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_confirm(user)
        elif path == "/api/alerts/dispose":
            # 处置（处置中/已恢复）与确认同权限：admin / risk。
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_dispose(user)
        elif path == "/api/report/recheck":
            # 1104 口径实时复算（只读）：与 report 同权限（admin/risk/da 登录即可）。
            user = self._require(CAN_VIEW_REPORT)
            if user:
                self._handle_recheck(user)
        elif path == "/api/report/rebuild":
            # 1104 快照重建（写）：与确认/处置同权限，admin / risk。
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_rebuild(user)
        elif path == "/api/report/submit":
            # 1104 报送确认（写状态）：与重建同权限，admin / risk。
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_submit(user)
        elif path == "/api/avm/attribution/refresh":
            # 幂等重建 P7 归因产物体（写 output/avm/bias_diag/）——与确认同权限（admin）。
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_avm_attr_refresh(user)
        elif path == "/api/persona/refresh":
            # 重建 P10 校准报告（写 output/persona/persona_report.json）——与确认同权限（admin）。
            user = self._require(CAN_CONFIRM)
            if user:
                self._handle_persona_refresh(user)
        elif not self._dispatch_plugin("POST", parsed):
            self._send_error(404, "not found")

    # ---------- 插件页面分发 ----------

    def _dispatch_plugin(self, method, parsed):
        """把请求交给 pages/ 下的插件页面。

        返回 True 表示已受理（无论成功或已回错误响应），False 表示无此路由，
        由调用方回 404。权限用页面声明的 roles 统一校验，页面模块不必自己判角色。
        """
        page, handler = page_registry.resolve(method, parsed.path)
        if handler is None:
            return False
        user = self._require(page["roles"] or None)
        if not user:
            return True  # _require 已发 401/403
        body = self._parse_body() if method == "POST" else {}
        ctx = RouteCtx(parse_qs(parsed.query), body, user, self.client_address[0])
        try:
            result = handler(ctx)
        except ValueError as exc:
            # 页面用 ValueError 表达「参数不合法」，统一转 400。
            self._send_error(400, str(exc))
            return True
        except Exception as exc:  # noqa: BLE001 —— 单页异常不应打挂整个服务
            sys.stderr.write(f"[frontend] page {page['id']} error: {exc}\n")
            self._send_error(500, "internal error")
            return True
        code, data = result if isinstance(result, tuple) else (200, result)
        # 约定：若 data 是 dict 且带 "__html__" 键 → 按 HTML 直接响应（用于 iframe 嵌入页）。
        # "__html__" 是保留键：handler 用它表达「本响应是整页 HTML」，其余一律按 JSON 回
        # （与现状逐字一致，不影响既有 API）。
        if isinstance(data, dict) and "__html__" in data:
            html = data["__html__"]
            if not isinstance(html, (str, bytes, bytearray)):
                # 不校验的话 bytes(123) 会静默产出一串 NUL 字节，比报错更难查。
                self._send_error(500, "internal error")
                return True
            body = html.encode("utf-8") if isinstance(html, str) else bytes(html)
            self._send(
                code,
                body,
                ctype="text/html; charset=utf-8",
                extra={"Cache-Control": "no-store"},
            )
        else:
            self._send_json(code, data)
        return True

    # ---------- 静态与页面 ----------

    def _serve_static(self, name):
        target = os.path.realpath(os.path.join(STATIC_DIR, name))
        # 只允许 static 目录内的文件，防目录穿越。
        if not target.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            self._send_error(403, "forbidden")
            return
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            self._send_error(404, "not found")
            return
        ext = os.path.splitext(name)[1]
        # no-store：保证热重载时浏览器总是按磁盘直读新文件，不被 HTTP 缓存挡住。
        self._send(
            200,
            body,
            ctype=MIME.get(ext, "application/octet-stream"),
            extra={"Cache-Control": "no-store"},
        )

    # ---------- API 实现 ----------

    def _handle_me(self):
        user = self._current_user()
        if not user:
            self._send_json(200, {"logged_in": False, "pages": []})
            return
        self._send_json(
            200,
            {
                "logged_in": True,
                "user": user["user"],
                "role": user["role"],
                "role_label": user["label"],
                "pages": _nav_for(user["role"]),
                "can_confirm": user["role"] in CAN_CONFIRM,
                "can_export": user["role"] in CAN_EXPORT,
            },
        )

    def _handle_login(self):
        body = self._parse_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        account = USERS.get(username)
        if not account or account["password"] != password:
            self._send_json(401, {"error": "账号或密码错误"})
            return
        token = secrets.token_hex(16)
        with _sessions_lock:
            _sessions[token] = {
                "user": username,
                "role": account["role"],
                "label": account["label"],
                "created": time.time(),
            }
        # HttpOnly + SameSite=Lax：JS 读不到 cookie，防 XSS 窃取会话。
        self._send_json(
            200,
            {"ok": True},
            extra={
                "Set-Cookie": f"spf_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200"
            },
        )

    def _handle_logout(self):
        token = self._session_token()
        if token:
            with _sessions_lock:
                _sessions.pop(token, None)
        self._send_json(
            200, {"ok": True}, extra={"Set-Cookie": "spf_session=; Path=/; HttpOnly; Max-Age=0"}
        )

    def _handle_alerts(self, parsed):
        qs = parse_qs(parsed.query)

        def first(name, cast=str):
            v = qs.get(name, [None])[0]
            if v is None or v == "":
                return None
            try:
                return cast(v)
            except ValueError:
                return None

        def to_float(v):
            return float(v) if v is not None else None

        page = first("page", int) or 1
        result = db.alerts(
            risk_class=first("risk_class"),
            ltv_min=to_float(first("ltv_min")),
            ltv_max=to_float(first("ltv_max")),
            date_from=first("date_from"),
            date_to=first("date_to"),
            source=first("source"),
            city=first("city"),
            keyword=first("q"),
            zone=first("zone"),
            page=max(1, page),
            page_size=20,
        )
        self._send_json(200, result)

    def _handle_export(self, parsed, user):
        qs = parse_qs(parsed.query)
        risk_class = qs.get("risk_class", [None])[0]
        source = qs.get("source", [None])[0]
        result = db.alerts(
            risk_class=risk_class,
            ltv_min=float(qs.get("ltv_min", [0])[0]) if qs.get("ltv_min", [""])[0] else None,
            ltv_max=float(qs.get("ltv_max", [2])[0]) if qs.get("ltv_max", [""])[0] else None,
            date_from=qs.get("date_from", [None])[0],
            date_to=qs.get("date_to", [None])[0],
            source=source,
            city=qs.get("city", [None])[0],
            page=1,
            page_size=100000,
        )
        lines = [
            "src,loan_id,customer_id,collateral_id,ltv,balance,valuation,risk_class,high_risk_zone,alert_date,address,confirmed,alert_level"
        ]
        for r in result["rows"]:
            # -------------------------------------------------------------------
            # G2 导出分级脱敏：
            #   逐列查 COLUMN_LEVELS（来自 data_classification，初值播种自
            #   P1 PRESET_SOURCES 的 data_level + PII 白名单）。PII 级字段走
            #   mask_value 脱敏（c****{后4位}），其余字段保持明文——loan_id /
            #   collateral_id 不属于个人敏感信息，按既有口径明文导出供业务核对。
            # G4 说明：本函数做的是脚本级脱敏（导出时单点完成），并写一条
            #   ads_export_audit 审计行记录 who/role/when/what/result/ip；
            #   字段级加密（KMS/字段级密钥）属于外部依赖，未在本演示中实现，
            #   仅在此声明其边界。脱敏逻辑本身不再改动（已正确）。
            # -------------------------------------------------------------------
            row_vals = []
            csv_cols = [
                ("src", None),
                ("loan_id", None),
                ("customer_id", "customer"),
                ("collateral_id", None),
                ("ltv", None),
                ("loan_balance", None),
                ("market_valuation", None),
                ("risk_class", None),
                ("is_high_risk_zone", None),
                ("alert_date", None),
                ("property_addr", None),
                ("confirmed", None),
                ("alert_level", None),
            ]
            for field, pii_table in csv_cols:
                raw = r.get(field)
                if pii_table and (field in ("customer_id", "customer_name")):
                    level = level_of(pii_table, field)
                    cell = (
                        mask_value(level, raw)
                        if level == "PII"
                        else ("" if raw is None else str(raw))
                    )
                elif field == "confirmed":
                    cell = "1" if raw else "0"
                elif field == "is_high_risk_zone":
                    cell = str(raw or 0)
                elif field == "ltv":
                    cell = f"{raw:.4f}" if raw is not None else ""
                elif field == "property_addr":
                    cell = str(raw or "").replace(",", "，")
                else:
                    cell = "" if raw is None else str(raw)
                row_vals.append(cell)
            lines.append(",".join(row_vals))
        # 加 UTF-8 BOM（utf-8-sig 自动前置 \ufeff）：Excel 双击打开 .csv 时
        # 不读 HTTP 头、按系统本地编码(GBK)解析，无 BOM 会把中文 UTF-8 字节
        # 误读成 GBK 导致乱码；BOM 是让 Excel 正确识别 UTF-8 的标准做法。
        body = ("\n".join(lines) + "\n").encode("utf-8-sig")
        # TC-06：导出留痕（who/role/when/what=筛选参数/result/ip），CSV 返回后写审计。
        db.write_audit(
            "export",
            user["user"],
            user["role"],
            json.dumps(
                {
                    "risk_class": risk_class,
                    "source": source,
                    "ltv_min": qs.get("ltv_min", [None])[0],
                    "ltv_max": qs.get("ltv_max", [None])[0],
                    "date_from": qs.get("date_from", [None])[0],
                    "date_to": qs.get("date_to", [None])[0],
                    "rows": result["total"],
                },
                ensure_ascii=False,
            ),
            "success",
            self.client_address[0],
        )
        self._send(
            200,
            body,
            ctype="text/csv; charset=utf-8",
            extra={
                "Content-Disposition": 'attachment; filename="ltv_alerts.csv"',
                "Cache-Control": "no-store",
            },
        )

    def _handle_confirm(self, user):
        body = self._parse_body()
        loan_id = body.get("loan_id")
        alert_date = body.get("alert_date")
        source = body.get("source")
        ip = self.client_address[0]
        if loan_id is None or not alert_date or source not in db.ALERT_SOURCES:
            # 参数缺失也留痕（result=failure），方便追溯异常调用来源。
            db.write_audit(
                "confirm",
                user["user"],
                user["role"],
                json.dumps(
                    {"loan_id": loan_id, "alert_date": alert_date, "source": source},
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(400, "参数缺失：loan_id / alert_date / source")
            return
        db.confirm_alert(int(loan_id), alert_date, source, user["user"])
        db.write_audit(
            "confirm",
            user["user"],
            user["role"],
            json.dumps(
                {"loan_id": int(loan_id), "alert_date": alert_date, "source": source},
                ensure_ascii=False,
            ),
            "success",
            ip,
        )
        self._send_json(200, {"ok": True, "confirmed_by": user["user"]})

    def _handle_dispose(self, user):
        """处置动作：把预警置为处置中（disposed）或已恢复（recovered）。

        与确认同权限（admin/risk）。参数不合法也留审计（result=failure），
        与 _handle_confirm 的失败留痕口径一致。
        """
        body = self._parse_body()
        loan_id = body.get("loan_id")
        alert_date = body.get("alert_date")
        source = body.get("source")
        status = (body.get("status") or "").strip()
        ip = self.client_address[0]
        detail = json.dumps(
            {"loan_id": loan_id, "alert_date": alert_date, "source": source, "status": status},
            ensure_ascii=False,
        )
        if (
            loan_id is None
            or not alert_date
            or source not in db.ALERT_SOURCES
            or status not in ("disposed", "recovered")
        ):
            db.write_audit("dispose", user["user"], user["role"], detail, "failure", ip)
            self._send_error(
                400, "参数缺失或不合法：loan_id / alert_date / source / status(disposed|recovered)"
            )
            return
        db.dispose_alert(int(loan_id), alert_date, source, status, user["user"])
        db.write_audit("dispose", user["user"], user["role"], detail, "success", ip)
        self._send_json(200, {"ok": True, "disposed_by": user["user"], "status": status})

    def _handle_recheck(self, user):
        """1104 口径实时复算：只读重算，不写表、不落审计（checked_at 取 SQL NOW()）。"""
        body = self._parse_body()
        date = (body.get("date") or "").strip() or None
        if date and not _valid_stat_date(date):
            self._send_error(400, "date 参数不合法，应为 YYYY-MM-DD")
            return
        self._send_json(200, db.report_recheck(date))

    def _handle_rebuild(self, user):
        """重建 1104 G11 快照：以 dws_risk_class 明细为基准覆盖该日快照。

        与确认/处置同权限（admin/risk）。写库在 db.report_rebuild 内单事务完成；
        本 handler 负责参数校验、审计留痕（TC-06）与错误转译（结构缺列 → 400 而非 500）。
        """
        body = self._parse_body()
        date = (body.get("date") or "").strip() or None
        ip = self.client_address[0]
        if date and not _valid_stat_date(date):
            db.write_audit(
                "report_rebuild",
                user["user"],
                user["role"],
                json.dumps(
                    {"kind": "rebuild", "date": date, "result": "bad_date"},
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(400, "date 参数不合法，应为 YYYY-MM-DD")
            return
        try:
            result = db.report_rebuild(date)
        except ValueError as exc:
            # 写入前置失败（表/列缺失、非五级档位）→ 明确报错而非 500，同时留审计。
            db.write_audit(
                "report_rebuild",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "rebuild",
                        "date": date,
                        "result": "value_error",
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(400, str(exc))
            return
        # 快照被重建 → 之前那次「已报送」对应的是旧快照，报送态必须回退 pending。
        # 落点刻意放在 handler 而非 db.report_rebuild 内：rebuild 是单事务
        # DELETE+INSERT，测试断言 conn.committed == 1，往里塞第二次写会破坏该契约。
        db.reset_report_submission(result["date"])
        db.write_audit(
            "report_rebuild",
            user["user"],
            user["role"],
            json.dumps(
                {
                    "kind": "rebuild",
                    "date": result["date"],
                    "result": "ok",
                    "rows": result["rebuilt_rows"],
                },
                ensure_ascii=False,
            ),
            "success",
            ip,
        )
        self._send_json(200, result)

    def _handle_avm_attr_refresh(self, user):
        """幂等重建 P7 归因产物体（写 output/avm/bias_diag/）。

        触发 tools/avm/gen_attribution.py：AVM 模型指纹未变且产物齐全 → 跳过
        （返回 regenerated=False）；否则依次重建 rows.json + attribution.json
        （regenerated=True）。与确认/处置同权限（admin/risk）。同步执行——该脚本是
        毫秒级 IO + 小规模计算，请求内跑完即可，无需引入异步任务。
        """
        body = self._parse_body()
        force = bool((body.get("force") or "").strip())
        ip = self.client_address[0]
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "avm",
            "gen_attribution.py",
        )
        if not os.path.exists(script):
            db.write_audit(
                "avm_attr_refresh",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "attr_refresh",
                        "result": "missing_script",
                        "path": script,
                    },
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(500, f"缺少归因生成脚本: {script}")
            return
        cmd = [sys.executable, script]
        if force:
            cmd.append("--force")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.SubprocessError as exc:
            db.write_audit(
                "avm_attr_refresh",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "attr_refresh",
                        "result": "exec_error",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(500, f"归因重建执行失败: {exc}")
            return
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        # 取末条「有意义」行作摘要：跳过纯分隔符横幅与空行（同 _handle_persona_refresh）。
        _sep = re.compile(r"^[\s=#*\-_·]+$")
        meaningful = [
            ln.strip()
            for ln in (out or "").splitlines()
            if ln.strip() and not _sep.match(ln.strip())
        ]
        ok = proc.returncode == 0
        summary = (
            meaningful[-1]
            if meaningful
            else (err.splitlines()[-1].strip() if err.strip() else f"exit={proc.returncode}")
        )
        db.write_audit(
            "avm_attr_refresh",
            user["user"],
            user["role"],
            json.dumps(
                {
                    "kind": "attr_refresh",
                    "mode": "force" if force else "fingerprint",
                    "result": "ok" if ok else "script_failed",
                    "summary": summary,
                    "stderr": err or None,
                },
                ensure_ascii=False,
            ),
            "success" if ok else "failure",
            ip,
        )
        if not ok:
            self._send_error(500, f"归因重建失败: {err or out or proc.returncode}")
            return
        self._send_json(
            200,
            {
                "ok": True,
                "force": force,
                "regenerated": "完成" in out,
                "detail": out,
                "stderr": err,
            },
        )

    def _handle_persona_refresh(self, user):
        """重建 P10 校准报告（写 output/persona/persona_report.json）。

        照 _handle_avm_attr_refresh 的骨架（同步 subprocess + 全量审计），
        唯一区别是**显式选解释器**：persona 脚本依赖 scipy，而服务进程自身
        解释器（orchestrator venv）不带 scipy——直接 subprocess(sys.executable)
        会 ModuleNotFoundError。R2-6 前端按钮不能留下一个必 500 的入口，
        故按 _resolve_persona_python() 选一个能 import scipy 的解释器。
        """
        _body = self._parse_body()  # 仅为让非法请求体在此抛错，内容本身不用
        ip = self.client_address[0]
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "persona",
            "main.py",
        )
        if not os.path.exists(script):
            db.write_audit(
                "persona_refresh",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "persona_refresh",
                        "result": "missing_script",
                        "path": script,
                    },
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(500, f"缺少校准报告生成脚本: {script}")
            return
        cmd = [_resolve_persona_python(), script]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.SubprocessError as exc:
            db.write_audit(
                "persona_refresh",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "persona_refresh",
                        "result": "exec_error",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(500, f"校准报告重建执行失败: {exc}")
            return
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        # 取末条「有意义」行作摘要：跳过纯分隔符横幅（==== / #### / ****）与空行，
        # 避免把脚本打印的装饰横幅存进审计明细（详情列原样展示会带 ==== 噪声）。
        _sep = re.compile(r"^[\s=#*\-_·]+$")
        meaningful = [
            ln.strip()
            for ln in (out or "").splitlines()
            if ln.strip() and not _sep.match(ln.strip())
        ]
        ok = proc.returncode == 0
        summary = (
            meaningful[-1]
            if meaningful
            else (err.splitlines()[-1].strip() if err.strip() else f"exit={proc.returncode}")
        )
        db.write_audit(
            "persona_refresh",
            user["user"],
            user["role"],
            json.dumps(
                {
                    "kind": "persona_refresh",
                    "result": "ok" if ok else "script_failed",
                    "summary": summary,
                    "stderr": err or None,
                },
                ensure_ascii=False,
            ),
            "success" if ok else "failure",
            ip,
        )
        if not ok:
            self._send_error(500, f"校准报告重建失败: {err or out or proc.returncode}")
            return
        self._send_json(200, {"ok": True, "detail": out, "stderr": err})

    def _handle_submit(self, user):
        """1104 报送确认：服务端自复算口径，一致才落「已报送」。

        与重建同权限（admin/risk）。口径不一致 → 409（响应体带 error 字段，前端 api()
        统一读 data.error 展示）且不写任何状态；成功/阻断均留审计（TC-06）。
        """
        body = self._parse_body()
        date = (body.get("date") or "").strip() or None
        ip = self.client_address[0]
        if date and not _valid_stat_date(date):
            db.write_audit(
                "report_submit",
                user["user"],
                user["role"],
                json.dumps(
                    {"kind": "submit", "date": date, "result": "bad_date"},
                    ensure_ascii=False,
                ),
                "failure",
                ip,
            )
            self._send_error(400, "date 参数不合法，应为 YYYY-MM-DD")
            return
        result = db.report_submit(date, user["user"])
        if not result.get("ok"):
            db.write_audit(
                "report_submit",
                user["user"],
                user["role"],
                json.dumps(
                    {
                        "kind": "submit",
                        "date": result.get("date"),
                        "result": "blocked",
                        "reason": result.get("reason"),
                    },
                    ensure_ascii=False,
                ),
                "blocked",
                ip,
            )
            # 409 Conflict：状态冲突（快照与明细口径不一致 / 尚无快照）。
            self._send_json(409, {**result, "error": result.get("reason") or "报送被阻断"})
            return
        db.write_audit(
            "report_submit",
            user["user"],
            user["role"],
            json.dumps(
                {
                    "kind": "submit",
                    "date": result["date"],
                    "result": "ok",
                    "snapshot": True,
                },
                ensure_ascii=False,
            ),
            "success",
            ip,
        )
        self._send_json(200, result)

    def _handle_detail(self, parsed):
        """单笔预警详情（行内展开）：基础 + 风险因子 + 地址 + 确认/处置记录。"""
        qs = parse_qs(parsed.query)

        def first(name):
            v = qs.get(name, [None])[0]
            return v if v not in (None, "") else None

        loan_id = first("loan_id")
        alert_date = first("alert_date")
        source = first("source")
        if loan_id is None or not alert_date or source not in db.ALERT_SOURCES:
            self._send_error(400, "参数缺失：loan_id / alert_date / source")
            return
        result = db.alert_detail(int(loan_id), alert_date, source)
        if result is None:
            self._send_error(404, "未找到该预警")
            return
        # AC-06 口径：read 接口同样对 PII 列脱敏，与导出 CSV 一致，禁止明文回传客户标识。
        # 仅 customer_id / customer_name 属 PII 级（与 data_classification.PII_COLUMNS 一致），
        # 其余字段（loan_id / collateral_id / 地址等）保持明文以便业务核对。
        # customer_id 位于 result["alert"] 子字典内（与 db.alert_detail 返回结构一致）。
        alert = result.get("alert")
        if isinstance(alert, dict):
            for pii_col in ("customer_id", "customer_name"):
                if alert.get(pii_col) is not None:
                    lvl = level_of("customer", pii_col)
                    alert[pii_col] = mask_value(lvl, alert[pii_col])
        self._send_json(200, result)

    # ---------------- G8 健康度 ----------------

    def _handle_metrics(self, user):
        """G8 /api/metrics：返回 collect_metrics() 快照；鉴权由 do_GET 统一做。"""
        self._send_json(200, collect_metrics())


def collect_metrics():
    """采集服务健康度快照（模块级函数，可脱离 HTTP 请求被测试直接调用）。

    字段：
      status                  : "ok" | "degraded"（memory_mb 超阈值即 degraded）
      uptime                  : 进程启动至今的秒数（monotonic）
      memory_mb               : 当前 RSS 物理内存(MB)
      cdc_position_advance_ok : CDC 位点是否在推进（查 ods_cdc_position 的
                                updated_at 距 NOW 是否 < 30min）；DB 不可用→False（降级）
      last_alert_count        : ads_cdc_alert 当前未解条数；DB 不可用→0（降级）

    内存优先用 psutil（RSS 精确），缺失时回退解析 /proc/self/status 的 VmRSS。
    """
    metrics = {
        "status": "ok",
        "uptime": time.monotonic() - _PROCESS_START,
        "memory_mb": 0,
        "cdc_position_advance_ok": False,
        "last_alert_count": 0,
    }

    # ---- 内存 ----
    mem_mb = 0
    try:
        import psutil  # 可选依赖；缺失即回退 /proc

        mem_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 —— psutil 未装或非 Linux
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        mem_mb = int(line.split()[1]) / 1024  # kB -> MB
                        break
        except OSError:
            mem_mb = 0
    metrics["memory_mb"] = round(mem_mb, 1)

    # ---- CDC 位点推进 / 告警计数（DB 不可用时全部降级为 False/0，不抛异常）----
    try:
        conn = db.crawl_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT TIMESTAMPDIFF(SECOND, MAX(updated_at), NOW()) "
                "FROM ods_cdc_position WHERE repl_key='binlog'"
            )
            row = cur.fetchone()
            age = int(row[0]) if row and row[0] is not None else None
            # 位点 30min 内更新过 = 仍在推进。
            metrics["cdc_position_advance_ok"] = age is not None and age < 1800
            cur.execute("SELECT COUNT(*) FROM ads_cdc_alert")
            metrics["last_alert_count"] = int(cur.fetchone()[0])
            cur.close()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 —— DB 抖动不应让 /api/metrics 直接 500
        metrics["cdc_position_advance_ok"] = False
        metrics["last_alert_count"] = 0

    # ---- 综合判定 ----
    metrics["status"] = "degraded" if metrics["memory_mb"] > MEM_DEGRADED_MB else "ok"
    return metrics


def main():
    ap = argparse.ArgumentParser(description="SpaceFin S5 前端驾驶舱（dev）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=8500, help="监听端口（默认 8500）")
    args = ap.parse_args()

    # 启动时幂等建确认表/审计表，避免首个「确认」/「导出」请求报表不存在。
    # 调试阶段（DEV_DISABLE_RBAC）若 MySQL 暂不可用（如 Docker 未起），仅告警不阻断启动，
    # 便于前端 UI / RBAC / 热重载先跑起来；生产环境仍按原样失败退出。
    if DEV_DISABLE_RBAC:
        for _fn in (
            db.ensure_alert_confirm_table,
            db.ensure_export_audit_table,
            db.ensure_report_submission_table,
            db.ensure_spatial_schema,
        ):
            try:
                _fn()
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[frontend] WARN: 启动建表跳过（{_fn.__name__}）：{exc}\n")
    else:
        db.ensure_alert_confirm_table()
        db.ensure_export_audit_table()
        db.ensure_report_submission_table()
        db.ensure_spatial_schema()  # 空间库表结构自愈：建表+补列+补索引（旧库 schema 漂移防护）

    server = FrontendHTTPServer((args.host, args.port), SpaceFinApp)
    print(f"[frontend] S5 驾驶舱启动 http://{args.host}:{args.port} (dev-only 账号见 README)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
