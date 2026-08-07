#!/usr/bin/env python
"""S5 前端页面插件注册表。

为什么要插件化：设计评审定义 P1~P10 十个页面，若全部塞进 app.py 的 if/elif 路由链，
单文件会膨胀到不可维护，且多人（多 agent）并行开发同一文件必然冲突。
这里把「一个页面 = 一个模块文件」固化下来：新增页面只需在本目录放一个 pN_xxx.py，
无需改动 app.py / index.html / app.js 任何一行。

模块契约——每个页面模块必须导出名为 PAGE 的 dict：

    PAGE = {
        "id":     "migration",              # 唯一标识，前端 section id = page-{id}
        "label":  "五级分类迁徙矩阵",        # 导航栏显示名
        "roles":  {"admin", "risk", "da"},  # 可见角色；服务端强制校验，非仅前端隐藏
        "order":  30,                       # 导航排序，小的在前
        "js":     "p3_migration.js",        # static/pages/ 下的前端模块文件名
        "routes": {
            ("GET", "/api/migration"): handler,
        },
    }

handler 签名统一为 handler(ctx) -> dict | (int, dict)：
  - 返回 dict          → 200 + JSON
  - 返回 (code, dict)  → 自定义状态码 + JSON
  - ctx.query : dict[str, list[str]]，已 parse_qs
  - ctx.body  : dict，POST 的 JSON body（GET 为 {}）
  - ctx.user  : dict，当前登录用户 {"username","role","label"}

路由权限：routes 的可访问角色 == PAGE["roles"]，由 app.py 在分发前统一校验，
页面模块内部无需再判角色（写操作若需更严格的角色，在 handler 内二次校验）。
"""

import importlib
import os
import pkgutil

# 加载失败的页面记录在此，便于启动日志排查（不让单个页面异常拖垮整个服务）。
LOAD_ERRORS = []


def _discover():
    """扫描本目录下所有 pN_*.py 并收集其 PAGE 声明。

    单个模块 import 失败只记录不抛出——一个页面写坏不应导致整个驾驶舱起不来。
    """
    pages = []
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in pkgutil.iter_modules([here]):
        name = mod.name
        if not name.startswith("p") or "_" not in name:
            continue
        try:
            m = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：隔离单页故障
            LOAD_ERRORS.append(f"{name}: {exc}")
            continue
        page = getattr(m, "PAGE", None)
        if not isinstance(page, dict) or "id" not in page:
            LOAD_ERRORS.append(f"{name}: 缺少合法的 PAGE 声明")
            continue
        page.setdefault("roles", set())
        page.setdefault("order", 999)
        page.setdefault("routes", {})
        page.setdefault("js", None)
        pages.append(page)
    pages.sort(key=lambda p: (p["order"], p["id"]))
    return pages


PAGES = _discover()

# (method, path) -> page，供 app.py 做 O(1) 路由分发。
ROUTES = {}
for _p in PAGES:
    for _key in _p["routes"]:
        ROUTES[_key] = _p


def nav_for(role):
    """返回某角色可见的导航项（含前端模块文件名，供动态加载）。"""
    return [
        {"id": p["id"], "label": p["label"], "js": p["js"]} for p in PAGES if role in p["roles"]
    ]


def resolve(method, path):
    """路由查找：命中返回 (page, handler)，未命中返回 (None, None)。"""
    page = ROUTES.get((method, path))
    if page is None:
        return None, None
    return page, page["routes"][(method, path)]
