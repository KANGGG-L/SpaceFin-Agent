#!/usr/bin/env python3

"""
抓取层：curl_cffi + Session 预热（突破 58/安居客反爬网关）

反爬攻克的核心技术（本项目验证有效）：
  1) curl_cffi impersonate="chrome" 伪装 Chrome 的 TLS/JA3 指纹与 HTTP/2 Client Hello，
     绕过对原生 requests/urllib 的指纹检测；
  2) Session 预热——先访问首页拿到 sessid/ctid/xxzl_cid 等 cookie，再带 Referer 请求列表页。
代理池通过 ProxyClient 注入；用户登录态 cookie 可通过 Playwright 导出的 JSON 注入。

实测结论（见 docs/poc/data-source/anjuke-exploratory/README.md）：
  安居客列表页闸门是 IP 频次而非登录态——每个新 IP 约只放行第 1 页，随后即 deny。
  因此多页/多区抓取必须配合 proxy_pool 轮换 IP，本模块已留好接口。
"""

import json
import os

from curl_cffi import requests

from .proxy import ProxyClient

CITY_SUBDOMAIN = {
    # ---- 广东省 21 个地级市 ----
    "gz": "guangzhou",  # 广州
    "sz": "shenzhen",  # 深圳
    "zh": "zhuhai",  # 珠海
    "st": "shantou",  # 汕头
    "fs": "foshan",  # 佛山
    "sg": "shaoguan",  # 韶关
    "zj": "zhanjiang",  # 湛江
    "zq": "zhaoqing",  # 肇庆
    "jm": "jiangmen",  # 江门
    "mm": "maoming",  # 茂名
    "hui": "huizhou",  # 惠州
    "mz": "meizhou",  # 梅州
    "sw": "shanwei",  # 汕尾
    "hy": "heyuan",  # 河源
    "yj": "yangjiang",  # 阳江
    "qy": "qingyuan",  # 清远
    "dg": "dongguan",  # 东莞
    "zs": "zhongshan",  # 中山
    "cz": "chaozhou",  # 潮州
    "jy": "jieyang",  # 揭阳
    "yf": "yunfu",  # 云浮
    # ---- 其他已接入城市 ----
    "bj": "beijing",
    "sh": "shanghai",
    "hz": "hangzhou",
    "nj": "nanjing",
    "wh": "wuhan",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchResult:
    def __init__(self, ok, status, final_url, html, note=""):
        self.ok = ok
        self.status = status
        self.final_url = final_url
        self.html = html
        self.note = note

    @property
    def blocked(self):
        return not self.ok


def load_cookies_into_session(session, cookie_file):
    """把 Playwright 导出的 cookie JSON 注入 curl_cffi session（修正 .jar.set 误用）。"""
    if not cookie_file or not os.path.exists(cookie_file):
        return 0
    with open(cookie_file, encoding="utf-8") as f:
        raw = json.load(f)
    n = 0
    for c in raw:
        dom = c.get("domain", "")
        if "anjuke.com" not in dom:
            continue
        try:
            session.cookies.set(
                c["name"],
                c["value"],
                domain=dom,
                path=c.get("path", "/"),
                secure=c.get("secure", False),
            )
            n += 1
        except Exception:
            continue
    return n


class Fetcher:
    def __init__(self, proxy_client=None, cookie_file=None, impersonate="chrome"):
        self.proxy = proxy_client or ProxyClient()
        self.cookie_file = cookie_file
        self.impersonate = impersonate

    def _new_session(self):
        s = requests.Session(impersonate=self.impersonate)
        load_cookies_into_session(s, self.cookie_file)
        return s

    @staticmethod
    def _is_blocked(final_url, html):
        markers = ("deny.do", "antibot", "verifycode", "esfcommon-captcha", "captcha")
        low = final_url.lower()
        return any(m in final_url or m in low for m in markers) or "xxzlGatewayUrl" in html[:2000]

    def fetch_listing(self, city_code, page, district_path="sale"):
        """抓取列表页。district_path: sale=出售 / fangyuan=出租。"""
        sub = CITY_SUBDOMAIN.get(city_code, city_code)
        home = f"https://{sub}.anjuke.com/"
        listing = f"https://{sub}.anjuke.com/{district_path}/p{page}/"
        proxy = self.proxy.get_proxy()
        proxies = proxy if proxy else None
        try:
            s = self._new_session()
            headers = {"Referer": "https://www.baidu.com/", "User-Agent": UA}
            # 步骤 1：预热首页，收获关键 cookie
            s.get(home, headers=headers, timeout=15, proxies=proxies)
            # 步骤 2：带 cookie + Referer 请求列表页
            r = s.get(
                listing,
                headers={"Referer": home, "User-Agent": UA},
                timeout=20,
                proxies=proxies,
                allow_redirects=True,
            )
            blocked = self._is_blocked(r.url, r.text)
            if blocked and proxy:
                self.proxy.delete_proxy(proxy)  # 用坏即扔
            note = "blocked/gateway" if blocked else "passed"
            return FetchResult(
                ok=not blocked, status=r.status_code, final_url=r.url, html=r.text, note=note
            )
        except Exception as exc:  # noqa: BLE001
            if proxy:
                self.proxy.delete_proxy(proxy)
            return FetchResult(
                ok=False, status=-1, final_url=listing, html="", note=f"ERROR: {exc}"
            )
