#!/usr/bin/env python3

"""
DrissionPage 隐身采集模块（持久化 chrome_profile 会话重放 + 代理自动切换）

这是把安居客房源**真正抓下来**的关键组件，对应反爬演进里"持久化登录态浏览器"一环
（curl_cffi 解决 TLS 指纹，本模块解决"被标记的陌生机器"问题）。机制：

1. 复用本地持久化 Chrome 用户目录（默认 tools/anjuke_crawler/chrome_profile/），
   保留 58/安居客 cookie 与会话——人工登录/过验证一次后，后续抓取直接带登录态。
2. 注入反自动化检测脚本（隐藏 navigator.webdriver 等），降低被识别为脚本的概率。
3. Session 预热：先访问首页，再进列表页。
4. 代理自动切换：检测到本地代理池（默认 :5010）则优先走代理；直连被拦时自动换代理重试，
   用坏的代理即时剔除（闭环见 proxy.py）。
5. 触发反爬（deny.do / security.anjuke.com）时等待一段时间，避免立刻硬刚。

⚠️ 合规红线（docs/product/01/数据现状摸底.md §3.3）：
- 仅采集公开、非 PII 的房源数据；数据源锁定安居客（已知悉并接受其 TOS/反爬风险）。
- chrome_profile 内是登录会话，**勿提交入库**（已 gitignore）；勿采集任何个人信息。
"""

import os
import time

from .proxy import ProxyClient

PROFILE_DIR = os.environ.get(
    "ANJUKE_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile"),
)
CHROME_BIN = os.environ.get("SPACEFIN_CHROME_BIN")  # 可选：自定义 Chrome 路径

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


def _is_blocked(page) -> bool:
    url = page.url
    return "deny.do" in url or "security.anjuke.com" in url


def make_stealth_browser(proxy=None, headless=False):
    """构造带持久化 profile + 反检测的 ChromiumPage。proxy 形如 'http://ip:port'。"""
    from DrissionPage import ChromiumOptions, ChromiumPage

    os.makedirs(PROFILE_DIR, exist_ok=True)
    co = ChromiumOptions()
    co.set_user_data_path(PROFILE_DIR)  # 持久化 58/安居客会话的关键
    if CHROME_BIN:
        co.set_browser_path(CHROME_BIN)
    if headless:
        co.headless()
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--lang=zh-CN,zh;q=0.9")
    co.set_user_agent(UA)
    co.set_argument("--window-size=1440,900")
    if proxy:
        co.set_proxy(proxy)

    page = ChromiumPage(co)
    page.run_js(STEALTH_JS)
    return page


def scrape_listing(
    city_code: str,
    page_no: int = 1,
    proxy_client: ProxyClient = None,
    headless: bool = False,
    district_path: str = "sale",
    captcha_wait: int = 15,
) -> str:
    """抓取列表页 HTML。直连优先；被拦且有代理池时自动换代理重试。返回页面 HTML（被拦返回拦截页）。"""
    from .fetcher import CITY_SUBDOMAIN

    sub = CITY_SUBDOMAIN.get(city_code, city_code)
    home = f"https://{sub}.anjuke.com/"
    target = f"https://{sub}.anjuke.com/{district_path}/p{page_no}/"

    proxy_client = proxy_client or ProxyClient()

    def _run(proxy_str):
        page = make_stealth_browser(proxy=proxy_str, headless=headless)
        try:
            page.get(home)  # 步骤 1：预热首页
            time.sleep(3)
            page.get(target)  # 步骤 2：进列表页
            time.sleep(3)
            if _is_blocked(page):  # 触发反爬：等待，避免硬刚
                start = time.time()
                while _is_blocked(page) and time.time() - start < captcha_wait:
                    time.sleep(2)
            return page.html, _is_blocked(page), page.url
        finally:
            page.quit()

    # 直连优先（profile 自带登录态，通常够用）
    html, blocked, final_url = _run(None)
    if not blocked:
        return html

    # 直连被拦 → 尝试代理池
    if proxy_client.enabled:
        proxy = proxy_client.get_proxy()
        if proxy:
            proxy_str = proxy["http"]
            html2, blocked2, final_url2 = _run(proxy_str)
            if blocked2:
                proxy_client.delete_proxy(proxy)  # 用坏即扔
            return html2
    return html  # 无代理可用，返回拦截页 HTML（调用方自行判断）


def scrape_listing_to_records(city_code: str, page_no: int = 1, **kwargs):
    """便捷封装：抓取列表页并解析为 17 字段记录列表。"""
    from ..parse import parse_numeric_schema_housing

    html = scrape_listing(city_code, page_no, **kwargs)
    return parse_numeric_schema_housing(html, district_tag=city_code)
