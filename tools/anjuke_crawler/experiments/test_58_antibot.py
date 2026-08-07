#!/usr/bin/env python3
"""
实验：curl_cffi + Session 预热打 58/安居客 antibot 网关（阶段三验证脚本）

结论：✅ 能绕过 @@xxzlGatewayUrl JS 网关拿到真实列表页；但每个新 IP 约只放行第 1 页。
这是主采集链路 fetcher.py 的技术原型。依赖：curl_cffi, lxml。
"""

import time

from curl_cffi import requests
from lxml import etree

HOME = "https://guangzhou.anjuke.com/"
LISTING = "https://guangzhou.anjuke.com/sale/p1/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

session = requests.Session(impersonate="chrome")

print("[1] 预热首页建立会话...")
r1 = session.get(HOME, headers=HEADERS, timeout=15)
print("    homepage:", r1.status_code, "| cookies:", list(session.cookies.jar))

time.sleep(2)
HEADERS["Referer"] = HOME
print("[2] 带 cookie + Referer 请求列表页...")
r2 = session.get(LISTING, headers=HEADERS, timeout=15)
print("    listing:", r2.status_code, "| url:", r2.url, "| len:", len(r2.text))

if "antibot/deny.do" in r2.text or "deny.do" in r2.url:
    print("[-] 仍被 58 antibot JS 网关拦截。")
else:
    print("[+] 通过 58 antibot 网关！")
    tree = etree.HTML(r2.text)
    items = tree.xpath("//div[@class='property']")
    print(f"[+] 解析到 {len(items)} 个房源卡片。")
