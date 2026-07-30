#!/usr/bin/env python3
"""
实验：undetected-chromedriver（无头）突破反爬（阶段二尝试）

结论：❌ 无头模式下即便 undetected-chromedriver 也被 Canvas/WebGL 指纹识别，且 IP 即时拉黑。
依赖：undetected-chromedriver, selenium。
"""

import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By

print("[+] 启动 undetected-chromedriver...")
options = uc.ChromeOptions()
options.add_argument("--headless")
options.add_argument("--disable-gpu")

try:
    driver = uc.Chrome(options=options)
    print("[+] 访问安居客广州二手房...")
    driver.get("https://guangzhou.anjuke.com/sale/p1/")
    time.sleep(5)

    print("[+] Current URL:", driver.current_url)
    print("[+] Page Title:", driver.title)

    if "deny.do" in driver.current_url or "security.anjuke.com" in driver.current_url:
        print("[-] 被 58/安居客 antibot 重定向拦截。")
    else:
        print("[+] 页面加载成功！")
        cards = driver.find_elements(By.CSS_SELECTOR, ".property, .property-card, .list-item")
        print(f"[+] 找到 {len(cards)} 个房源元素。")
        for card in cards[:3]:
            print("-", card.text.replace("\n", " | "))

    driver.quit()
except Exception as e:
    print(f"[-] undetected-chromedriver 出错: {e}")
