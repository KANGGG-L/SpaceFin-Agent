#!/usr/bin/env python3
"""
房源 URL 规范化去重键（url_key）纯函数。

跨日去重的核心：原始 URL 带 soj_info/stats_key 等 query 参数，同一房源每次抓取
URL 不同，不能直接做键。本模块从 URL 提取规范化房源 ID 作为去重键。

形态规则（提取房源 ID 后剥离 query）：
    sale·安居客  https://{city}.anjuke.com/prop/view/S4656241945109512?...  -> sale:anjuke:S4656241945109512
    sale·58新房  https://gz.58.com/xinfang/huxing/59071435-897433.html     -> sale:58:59071435-897433
    rent·zu      https://{city}.zu.anjuke.com/fangyuan/4688937808519174    -> rent:zu:4688937808519174
    rent·gfangyuan https://mz.zu.anjuke.com/gfangyuan/2580079256313869?... -> rent:zu:2580079256313869
    未知形态     （无法匹配上述任何形态）                                    -> md5:{md5(原始url)}

sale 任务里 58 新房与安居客二手是不同房源，必须带来源域前缀区分，否则会误合并。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

# 路径房源 ID 提取（注意顺序：zu.anjuke.com 含 anjuke.com，须先判出租）
_RENT_ID_RE = re.compile(r"(?:fangyuan|gfangyuan)/(\d+)")
_SALE_ANJUKE_ID_RE = re.compile(r"prop/view/([A-Za-z0-9]+)")
_SALE_58_ID_RE = re.compile(r"xinfang/huxing/([\w-]+)")

# url_key 各前缀总长须 <64（DWD 主键 VARCHAR(64)）。md5 兜底为 36 字符。
_PREFIX = {
    "sale_anjuke": "sale:anjuke:",
    "sale_58": "sale:58:",
    "rent_zu": "rent:zu:",
    "md5": "md5:",
}


def make_url_key(url: str) -> str:
    """从房源 URL 提取规范化去重键。任何输入均返回合法键（不抛异常、不丢弃）。"""
    if not url:
        return _md5_key(url)
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
    except Exception:
        return _md5_key(url)

    # 出租：zu.anjuke.com 子域（须先于 anjuke.com 判断）
    if "zu.anjuke.com" in host:
        m = _RENT_ID_RE.search(path)
        if m:
            return _PREFIX["rent_zu"] + m.group(1)
        return _md5_key(url)

    # 出售·安居客主域
    if "anjuke.com" in host:
        m = _SALE_ANJUKE_ID_RE.search(path)
        if m:
            return _PREFIX["sale_anjuke"] + m.group(1)
        return _md5_key(url)

    # 出售·58 新房
    if "58.com" in host:
        m = _SALE_58_ID_RE.search(path)
        if m:
            return _PREFIX["sale_58"] + m.group(1)
        return _md5_key(url)

    return _md5_key(url)


def _md5_key(url: str) -> str:
    return _PREFIX["md5"] + hashlib.md5(url.encode("utf-8")).hexdigest()
