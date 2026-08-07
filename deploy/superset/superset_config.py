#!/usr/bin/env python
"""Superset 自定义配置（投产加固 · 经 SUPERSET_CONFIG_PATH 挂载启用）。

本文件由 docker-compose.yml 通过 SUPERSET_CONFIG_PATH=/app/superset_config.py 挂载，
在 Superset 启动时导入。它只做一件事：用 FLASK_APP_MUTATOR 注册一个 after_request
钩子，对「查询 / 探索 / 数据集导出」类请求写操作审计，复用驾驶舱同一张审计表
spacefin.ads_export_audit（见 audit_writer.py），从而 Superset 与驾驶舱审计闭环合并。

审计触发路径（与合规相关的读/导出动作）：
  - /api/v1/chart/data   图表查询（核心）
  - /sqllab*             SQL Lab 探索查询
  - /api/v1/database*    数据集/数据库导出类动作
不审计 /health、静态资源、登录等。匿名请求不审计。
"""

from __future__ import annotations

import os
import sys

# audit_writer.py 随本文件一起挂载在 /app，加入 path 以便 import。
sys.path.insert(0, "/app")

import audit_writer  # noqa: E402
from flask import request  # noqa: E402
from flask_login import current_user  # noqa: E402

# ---------------------------------------------------------------------------
# 注意：SUPERSET_CONFIG_PATH 挂载的配置文件会「合并覆盖」默认配置。本文件未设置的
# 项（如元数据库 URI / SECRET_KEY）将回退到 Superset 内置默认（SQLite）。因此这里
# 必须显式把关键配置从环境变量接回来，避免 Superset 退化回 SQLite。
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URI = os.getenv(
    "SQLALCHEMY_DATABASE_URI",
    "sqlite:////app/superset_home/superset.db",
)
SUPERSET_SECRET_KEY = os.getenv("SUPERSET_SECRET_KEY", "spacefin-superset-dev-secret-fixed")

# 需要写审计的请求路径前缀。
AUDIT_PATH_PREFIXES = ("/api/v1/chart/data", "/sqllab", "/api/v1/database")


def _resolve_user():
    """返回 (username, role_str)。优先 current_user；Bearer JWT 的 sub 为用户 ID，
    经 security_manager 反查（after_request 中 API 请求的 current_user 常为匿名）。"""
    user = current_user
    if user is not None and getattr(user, "is_authenticated", False):
        username = getattr(user, "username", "anonymous")
        roles = getattr(user, "roles", []) or []
        return username, ",".join(sorted(r.name for r in roles)) if roles else "N/A"
    # JWT 回退：解析 Authorization: Bearer <jwt>，payload.sub = 用户 ID。
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            import base64
            import json as _json

            seg = auth[len("Bearer ") :].split(".")[1]
            seg += "=" * (-len(seg) % 4)
            payload = _json.loads(base64.urlsafe_b64decode(seg))
            uid = payload.get("sub")
            if uid is not None:
                from superset import security_manager

                u = security_manager.get_user_by_id(int(uid))
                if u is not None:
                    roles = getattr(u, "roles", []) or []
                    return u.username, ",".join(sorted(r.name for r in roles)) if roles else "N/A"
        except Exception:
            pass
    return None, None


def _after_request(resp):
    """after_request 钩子：对合规相关请求留痕到 ads_export_audit（静默降级）。"""
    try:
        path = request.path
        if not any(path.startswith(p) for p in AUDIT_PATH_PREFIXES):
            return resp
        username, role = _resolve_user()
        if not username:
            return resp

        detail = f"{request.method} {path}"
        # 图表查询附带数据集标识，便于审计定位。
        try:
            if path.startswith("/api/v1/chart/data") and request.is_json:
                ds = (request.get_json(silent=True) or {}).get("datasource")
                if isinstance(ds, dict) and ds.get("datasource"):
                    detail += f" datasource={ds.get('datasource')}"
        except Exception:
            pass

        ip = request.remote_addr
        audit_writer.write_audit("superset_query", username, role, detail, "success", ip)
    except Exception:
        # 审计失败绝不影响业务查询。
        pass
    return resp


# Superset 在 app 初始化后调用此 mutator，借此把钩子挂到 app 上。
# 此名称是 Superset 约定的配置键（必须大写），故 noqa: N802。
def FLASK_APP_MUTATOR(app):  # noqa: N802
    app.after_request(_after_request)
