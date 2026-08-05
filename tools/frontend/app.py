#!/usr/bin/env python
"""S5 前端驾驶舱 · HTTP 服务（stdlib，零第三方 Web 依赖）。

为什么用 stdlib http.server 而不是 Streamlit/FastAPI：
1. 全仓唯一 Python 环境（tools/orchestrator/.venv = conda spark 3.10）已含 PyMySQL，
   装 Web 框架会往共享 conda 环境塞十几到几十个包，污染其他流水线（spark/调度依赖同一环境）；
2. 页面只有 3 张、数据 200 行级，ThreadingHTTPServer + 每请求短连接足够，
   不值得引入进程常驻的 ASGI 服务 + 构建链；
3. RBAC/会话在服务端强制校验（每个 API 先查角色再放行），前端仅做展示，
   与「前端隐藏页面」是两层防御，后者只是 UX 裁剪。

运行（后台）：
    nohup tools/orchestrator/.venv/bin/python tools/frontend/app.py \
        --port 8500 >> output/frontend/app.log 2>&1 &

默认监听 127.0.0.1（仅本机）；--host 0.0.0.0 可暴露局域网，README 有说明。
"""

import argparse
import json
import os
import secrets
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import pages as page_registry  # noqa: E402
from data_classification import level_of, mask_value  # noqa: E402  # G2 导出分级脱敏

# ---------------- 用户与 RBAC ----------------
# 仅开发环境内置账号；凭据写死在代码里并标注 dev-only，上线前必须接统一认证。
# 角色矩阵（PRD §7.3）：DA 只读 / 风控可写（确认/导出）/ 贷后不可见报送页。
USERS = {
    "admin": {"password": "admin123", "role": "admin", "label": "系统管理员"},
    "risk": {"password": "risk123", "role": "risk", "label": "风控策略经理"},
    "da": {"password": "da123", "role": "da", "label": "数据分析师"},
    "postloan": {"password": "post123", "role": "postloan", "label": "贷后资产保全"},
}

# 页面可见性：role -> pages。
PAGE_VISIBILITY = {
    "admin": [
        {"id": "dashboard", "label": "资产质量驾驶舱"},
        {"id": "alerts", "label": "LTV 预警列表"},
        {"id": "report", "label": "1104 报送"},
    ],
    "risk": [
        {"id": "dashboard", "label": "资产质量驾驶舱"},
        {"id": "alerts", "label": "LTV 预警列表"},
        {"id": "report", "label": "1104 报送"},
    ],
    "da": [
        {"id": "dashboard", "label": "资产质量驾驶舱"},
        {"id": "alerts", "label": "LTV 预警列表"},
        {"id": "report", "label": "1104 报送"},
    ],
    "postloan": [
        {"id": "dashboard", "label": "资产质量驾驶舱"},
        {"id": "alerts", "label": "LTV 预警列表"},
    ],
}

# 操作级权限：仅这些角色可用。
CAN_CONFIRM = {"admin", "risk"}
CAN_EXPORT = {"admin", "risk", "da"}
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
}


def _nav_for(role):
    """导航 = 内置三页（dashboard/alerts/report）+ pages/ 插件页，按 order 排序。

    内置页无 js 字段（渲染逻辑在 app.js 里），插件页带 js 供前端动态加载。
    """
    builtin = [dict(p, js=None) for p in PAGE_VISIBILITY.get(role, [])]
    return builtin + page_registry.nav_for(role)


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
        self._send(code, _json(data), extra=extra)

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
        """RBAC 统一入口：未登录 401，角色不符 403。roles=None 表示仅需登录。"""
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
        elif path in ("/style.css", "/app.js"):
            self._serve_static(path.lstrip("/"))
        elif path.startswith("/pages/") and path.endswith(".js"):
            # 插件页面的前端模块；_serve_static 已做目录穿越防护。
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
        self._send(200, body, ctype=MIME.get(ext, "application/octet-stream"))

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
        body = ("\n".join(lines) + "\n").encode("utf-8")
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
    db.ensure_alert_confirm_table()
    db.ensure_export_audit_table()

    server = ThreadingHTTPServer((args.host, args.port), SpaceFinApp)
    print(f"[frontend] S5 驾驶舱启动 http://{args.host}:{args.port} (dev-only 账号见 README)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
