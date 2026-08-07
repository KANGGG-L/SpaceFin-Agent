#!/usr/bin/env python3
"""
实验：selenium-stealth 插件（无头）突破反爬（阶段二尝试）

结论：❌ 无头 + stealth 注入仍被识别（Canvas/WebGL 硬件渲染指纹对不上）。
依赖：selenium, selenium-stealth。
"""

import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium_stealth import stealth

print("[+] 配置 Selenium + Stealth 插件...")
chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--window-size=1440,900")
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
    print("[+] 访问安居客广州二手房...")
    driver.get("https://guangzhou.anjuke.com/sale/p1/")
    time.sleep(4)

    print("[+] Current URL:", driver.current_url)
    print("[+] Page Title:", driver.title)

    from urllib.parse import urlparse

    _host = urlparse(driver.current_url).netloc.split(":")[0].lower()
    if (
        "deny.do" in driver.current_url
        or _host == "security.anjuke.com"
        or _host.endswith(".security.anjuke.com")
    ):
        print("[-] 被 58/安居客 antibot 网关拦截。")
    else:
        print("[+] 页面加载成功！")
        cards = driver.find_elements(By.CSS_SELECTOR, ".property, .property-card, .list-item")
        print(f"[+] 找到 {len(cards)} 个房源元素。")
        for card in cards[:3]:
            print("-", card.text.replace("\n", " | "))
finally:
    driver.quit()
