#!/usr/bin/env python3

"""
动态代理池客户端（对接 jhao104/proxy_pool 一类代理池服务）

背景：安居客列表页按 IP 频次拦截（每个新 IP 约只放行第 1 页），多页/可持续抓取
必须每请求轮换 IP。代理池暴露 REST API（默认 :5010）：
    GET /get/            取一个可用代理
    GET /pop/            取出并移除一个代理
    GET /delete/?proxy=  剔除失效代理
本客户端按需取代理，抓取异常时自动 /delete/ 剔除，形成"用坏即扔"的闭环。

默认未启用：仅当显式传入 api_base 或设置 SPACEFIN_PROXY_BASE 环境变量时才工作；
否则 get_proxy() 恒返回 None（直连）。
"""

import os

from curl_cffi import requests

PROXY_API_BASE = os.environ.get("SPACEFIN_PROXY_BASE")  # 例如 http://127.0.0.1:5010


class ProxyClient:
    def __init__(self, api_base=PROXY_API_BASE):
        self.api_base = api_base.rstrip("/") if api_base else None

    @property
    def enabled(self):
        return bool(self.api_base)

    def get_proxy(self):
        if not self.api_base:
            return None
        try:
            r = requests.get(f"{self.api_base}/get/", timeout=3)
            if r.status_code == 200:
                proxy_str = r.json().get("proxy")
                if proxy_str:
                    return {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}
        except Exception:
            pass
        return None

    def pop_proxy(self):
        if not self.api_base:
            return None
        try:
            r = requests.get(f"{self.api_base}/pop/", timeout=3)
            if r.status_code == 200:
                proxy_str = r.json().get("proxy")
                if proxy_str:
                    return {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}
        except Exception:
            pass
        return None

    def delete_proxy(self, proxy_dict):
        if not self.api_base or not proxy_dict or "http" not in proxy_dict:
            return
        proxy_str = proxy_dict["http"].replace("http://", "")
        try:
            requests.get(f"{self.api_base}/delete/?proxy={proxy_str}", timeout=3)
        except Exception:
            pass
