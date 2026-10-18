#!/usr/bin/env python3
"""房源城市归属判定（方案 A：以 URL 子域为准）——纯函数，无 DB / 无网络 / 无抓取栈依赖。

为什么必须存在这个模块：DWD 的城市归属原先取自**文件名城市码**（`etl.load_rows` 从
`{city}_{type}_*.jsonl` 解析）。但大批房源的原始 URL 子域不在广东 21 城（beijing、
nantong、anshan、suzhou…；计划里记的污染率是 47.4%，2026-08-31 用本模块实测
ds=2026-08-30 为**去重键 39.0% / 行 26.5%**）——免费代理 IP 在省外，安居客按 IP 返回
当地列表页。文件名只代表「本来想爬哪个城」，**子域才是房源真实所在地**。

三层兜底口径（省外与 unknown 必须分开存）：
    1. 子域命中 21 城白名单（全拼 ∪ 城市码，双向）  → 返回该 **21 城码**，进按城指标分母
    2. 子域可解析但不属 21 城                      → 返回 **子域原文**（省外桶，不进指标）
    3. 子域不可解析 / url 为空                     → 返回 `unknown`（不进指标）
只有第 3 档进 S5 定向复核队列——第 2 档是「确认不在广东」，不是「不知道是哪」。

子域形态（实测）：anjuke 是**全拼**（`chaozhou`/`nantong`），58 是**短码**（`fs`/`su`/`nb`）。
因此白名单必须双向（全拼 ∪ 码）才不会把真广东的 `fs.58.com`/`gz.58.com` 误判成省外
（实测 264 行）。

口径历史：`2026-08-31` 之前入库的行按文件名归属，**含省外污染**（决策 D-6 不回刷）。
本模块只对新观测生效。
"""

from __future__ import annotations

import ast
import os
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER_PY = os.path.join(_HERE, "worker.py")

UNKNOWN_CITY = "unknown"

# resolve_city 的第二返回值（reason）：判定结果必须可解释，不静默退化
R_URL_HIT = "url_hit"  # 子域命中 21 城，且与文件名城市码一致
R_URL_HIT_OVER_FILENAME = "url_hit_over_filename"  # 子域命中 21 城，但覆盖了文件名城市码
R_URL_FOREIGN = "url_foreign"  # 子域可解析但不属 21 城 → 省外桶
R_UNPARSED_HOST = "unparsed_host"  # 域名不是 anjuke/58，或没有三级子域
R_EMPTY_URL = "empty_url"

_CACHE: dict = {}


def city_subdomain() -> dict:
    """读 worker.CITY_SUBDOMAIN（城市码 → 子域全拼），作为 21 城白名单的唯一数据源。

    不 import worker：它模块级 `from curl_cffi import requests`，归属判定不该依赖抓取栈；
    用 ast 直读字面量也避免了「抄一份映射 → 与白名单漂移」——那正是省外污染的类成因。
    """
    if "sub" not in _CACHE:
        with open(_WORKER_PY, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=_WORKER_PY)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(t, ast.Name) and t.id == "CITY_SUBDOMAIN" for t in node.targets):
                _CACHE["sub"] = ast.literal_eval(node.value)
                break
        else:
            raise RuntimeError("worker.py 中未找到 CITY_SUBDOMAIN 字面量")
    return _CACHE["sub"]


def subdomain_to_code() -> dict:
    """子域 → 21 城码。键集合 = 21 城全拼 ∪ 21 城码（42 项）。

    含城市码是因为 anjuke 用全拼而 58 用短码（`fs.58.com`/`gz.58.com` 是真广东房源）。
    """
    if "s2c" not in _CACHE:
        sub = city_subdomain()
        _CACHE["s2c"] = {pinyin: code for code, pinyin in sub.items()}
        _CACHE["s2c"].update({code: code for code in sub})
    return _CACHE["s2c"]


def parse_host(url: str) -> tuple:
    """(registrar, sub)：registrar ∈ {anjuke, 58}，不可解析一律 (None, None)。

    `mz.zu.anjuke.com` 天然满足 anjuke 分支且 `parts[0]` 就是城市，出租侧无需特判。
    要求至少三级域名：`anjuke.com` 自身没有城市子域，不能把 'anjuke' 当城市。
    """
    if not url:
        return (None, None)
    try:
        host = (urlparse(url).hostname or "").lower()
        parts = host.split(".")
    except Exception:  # noqa: BLE001 - total 函数：畸形/非文本 URL 走兜底而不是抛给调用方
        return (None, None)
    if len(parts) < 3:
        return (None, None)
    if parts[-2:] == ["anjuke", "com"]:
        return ("anjuke", parts[0])
    if parts[-2:] == ["58", "com"]:
        return ("58", parts[0])
    return (None, None)


def resolve_city(url, filename_city: str = None) -> tuple:
    """判定房源城市归属 → (city, reason)。任何输入都不抛异常、不返回 None。

    `filename_city` 只用于标注「子域与文件名冲突」，**不参与判定**——方案 A 的立论就是
    子域优先，让文件名参与判定等于把省外污染重新放回分母。

        resolve_city("https://nantong.anjuke.com/prop/view/A1", "gz")
            -> ('nantong', 'url_foreign')        省外桶，不许 fallback 回 gz
        resolve_city("https://sz.anjuke.com/prop/view/A1", "gz")
            -> ('sz', 'url_hit_over_filename')   URL 覆盖文件名
        resolve_city("https://fs.58.com/xinfang/huxing/1-2.html")
            -> ('fs', 'url_hit')                 58 短码也是广东
        resolve_city("https://su.58.com/xinfang/huxing/1-2.html")
            -> ('su', 'url_foreign')
        resolve_city("")                          -> ('unknown', 'empty_url')
    """
    if not url:
        return (UNKNOWN_CITY, R_EMPTY_URL)
    _registrar, sub = parse_host(url)
    if not sub:
        return (UNKNOWN_CITY, R_UNPARSED_HOST)
    code = subdomain_to_code().get(sub)
    if code is None:
        return (sub, R_URL_FOREIGN)
    if filename_city and filename_city != code:
        return (code, R_URL_HIT_OVER_FILENAME)
    return (code, R_URL_HIT)
