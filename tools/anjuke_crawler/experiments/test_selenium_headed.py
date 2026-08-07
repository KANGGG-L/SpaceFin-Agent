#!/usr/bin/env python3
"""
实验：selenium-stealth（有头）+ 首页预热 + 人工过验证（阶段二→三过渡尝试）

结论：⚠️ 有头模式 WebGL/Canvas 与真实硬件一致，配合人工过验证可取到数据；
但无法自动化、吞吐极低——这正是后来转向"持久化 profile 会话重放"（stealth.py）的原因。
依赖：selenium, selenium-stealth。
"""

import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium_stealth import stealth

print("[+] 有头模式 + Stealth 启动...")
chrome_options = Options()
# 不加 --headless，让 WebGL/Canvas 与真实 GPU 一致
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--window-size=1440,900")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
chrome_options.add_argument(
    "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

driver = webdriver.Chrome(options=chrome_options)
stealth(
    driver,
    languages=["zh-CN", "zh", "en"],
    vendor="Google Inc.",
    platform="MacIntel",
    webgl_vendor="Intel Inc.",
    renderer="Intel Iris OpenGL Engine",
    fix_hairline=True,
)

try:
    print("[1] 访问首页初始化 58 cookie...")
    driver.get("https://guangzhou.anjuke.com/")
    time.sleep(3)

    print("[2] 进入二手房列表页...")
    driver.get("https://guangzhou.anjuke.com/sale/p1/")
    time.sleep(4)

    print("[+] Current URL:", driver.current_url)
    print("[+] Page Title:", driver.title)

    _host = urlparse(driver.current_url).netloc.split(":")[0].lower()
    if (
        "deny.do" in driver.current_url
        or _host == "security.anjuke.com"
        or _host.endswith(".security.anjuke.com")
    ):
        print("[-] 触发 58 验证页。请在浏览器窗口手动完成验证。")
        time.sleep(10)
    else:
        print("\n=== 通过 58 antibot 网关 ===")
        cards = driver.find_elements(By.CSS_SELECTOR, ".property, .property-card, .list-item")
        print(f"[+] 找到 {len(cards)} 个房源！")
        for card in cards[:5]:
            lines = [line.strip() for line in card.text.split("\n") if line.strip()]
            print(" ->", " | ".join(lines[:4]))
finally:
    driver.quit()
