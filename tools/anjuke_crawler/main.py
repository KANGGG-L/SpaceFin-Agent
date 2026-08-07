#!/usr/bin/env python3

"""
anjuke_crawler 包入口

子命令：
    parse     离线解析本地 HTML（fixture 模式，不联网，可复现）
    geocode   本地离线地理编码自测
    crawl     在线抓取（curl_cffi + Session 预热，可选代理/cookie）

示例：
    # 离线解析随包附带的真实列表页样本（应得 71 条，可复现）
    python -m anjuke_crawler.main parse \
        --html anjuke_crawler/tests/sample_listing.html \
        --district sh_pudong --out output/anjuke_parse.csv

    # 在线抓取（每 IP 约仅放行 1 页，详见 docs/poc/data-source/anjuke-exploratory/README.md）
    python -m anjuke_crawler.main crawl --city gz --pages 1 --out output/anjuke_gz.csv

    # 接代理池后多页抓取
    export SPACEFIN_PROXY_BASE=http://127.0.0.1:5010
    python -m anjuke_crawler.main crawl --city gz --pages 5 --out output/anjuke_gz.csv
"""

import argparse
import os

from .fetch import Fetcher, ProxyClient
from .geocoder import LocalGeocoder
from .parse import parse_numeric_schema_housing, save_numeric_schema_csv


def cmd_parse(args):
    with open(args.html, encoding="utf-8") as f:
        html = f.read()
    houses = parse_numeric_schema_housing(html, district_tag=args.district)
    save_numeric_schema_csv(houses, args.out)
    print(f"[+] parsed {len(houses)} records from fixture")


def cmd_geocode(args):
    geocoder = LocalGeocoder()
    lat, lng = geocoder.geocode(args.name)
    print(f"[+] LocalGeocoder('{args.name}'): lat={lat}, lng={lng}")


def cmd_crawl(args):
    proxy = ProxyClient()  # 读 SPACEFIN_PROXY_BASE；未设则直连
    fetcher = Fetcher(proxy_client=proxy, cookie_file=args.cookie)
    all_rows = []
    ok = 0
    for page in range(1, args.pages + 1):
        res = fetcher.fetch_listing(args.city, page, district_path=args.type)
        rows = [] if res.blocked else parse_numeric_schema_housing(res.html, district_tag=args.city)
        all_rows.extend(rows)
        flag = "✓" if rows else "✗"
        print(
            f"  [{flag}] p{page}: status={res.status} size={len(res.html)} "
            f"records={len(rows)} note={res.note} final={res.final_url[:55]}"
        )
        if rows:
            ok += 1
        if args.delay > 0:
            import time

            time.sleep(args.delay)
    save_numeric_schema_csv(all_rows, args.out)
    print(f"[+] crawl 成功页 {ok}/{args.pages} | 共 {len(all_rows)} 条 -> {args.out}")
    if not all_rows:
        print(
            "[!] 0 条：列表页被 IP 频次拦截。多页抓取请设置 SPACEFIN_PROXY_BASE 接代理池，"
            "或改用 stealth 子命令（持久化 profile 会话重放）。"
        )


def cmd_parse_advanced(args):
    from .parse import parse_advanced_housing_data, save_advanced_csv

    with open(args.html, encoding="utf-8") as f:
        html = f.read()
    houses = parse_advanced_housing_data(html, use_local_geocoding=True)
    save_advanced_csv(houses, args.out)
    print(f"[+] parsed(advanced) {len(houses)} records from fixture")


def cmd_stealth(args):
    from .fetch import scrape_listing_to_records
    from .parse import save_numeric_schema_csv

    proxy = ProxyClient()  # 读 SPACEFIN_PROXY_BASE；未设则仅靠 profile 登录态
    all_rows = []
    for page in range(1, args.pages + 1):
        rows = scrape_listing_to_records(
            args.city, page, proxy_client=proxy, headless=args.headless, district_path=args.type
        )
        all_rows.extend(rows)
        print(f"  [{'✓' if rows else '✗'}] p{page}: records={len(rows)}")
        if args.delay > 0:
            import time

            time.sleep(args.delay)
    save_numeric_schema_csv(all_rows, args.out)
    print(f"[+] stealth 抓取 共 {len(all_rows)} 条 -> {args.out}")
    if not all_rows:
        print(
            "[!] 0 条：profile 登录态可能已失效。先用有头模式人工登录/过验证一次"
            "（python -c 调 stealth.make_stealth_browser(headless=False)），再重跑。"
        )


def main():
    ap = argparse.ArgumentParser(description="anjuke_crawler 包入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("parse", help="离线解析本地 HTML")
    pp.add_argument("--html", required=True)
    pp.add_argument("--district", default="pudong")
    pp.add_argument("--out", default="output/anjuke_parse.csv")
    pp.set_defaults(func=cmd_parse)

    pg = sub.add_parser("geocode", help="本地地理编码自测")
    pg.add_argument("--name", default="证大家园")
    pg.set_defaults(func=cmd_geocode)

    pc = sub.add_parser("crawl", help="在线抓取（阶段三：curl_cffi + Session 预热）")
    pc.add_argument("--city", default="gz")
    pc.add_argument("--type", default="sale", choices=["sale", "fangyuan"])
    pc.add_argument("--pages", type=int, default=1)
    pc.add_argument("--delay", type=float, default=2.0)
    pc.add_argument("--cookie", default=None, help="Playwright 导出的 cookie JSON 路径（可选）")
    pc.add_argument("--out", default="output/anjuke_crawl.csv")
    pc.set_defaults(func=cmd_crawl)

    pa = sub.add_parser("parse-advanced", help="离线解析为 18 字段增强 schema（含户型串/车位）")
    pa.add_argument("--html", required=True)
    pa.add_argument("--out", default="output/anjuke_advanced.csv")
    pa.set_defaults(func=cmd_parse_advanced)

    ps = sub.add_parser("stealth", help="持久化 profile 会话重放抓取（DrissionPage，需 Chrome）")
    ps.add_argument("--city", default="gz")
    ps.add_argument("--type", default="sale", choices=["sale", "fangyuan"])
    ps.add_argument("--pages", type=int, default=1)
    ps.add_argument("--delay", type=float, default=2.0)
    ps.add_argument(
        "--headless", action="store_true", help="无头（首次登录请去掉此参数用有头模式）"
    )
    ps.add_argument("--out", default="output/anjuke_stealth.csv")
    ps.set_defaults(func=cmd_stealth)

    args = ap.parse_args()
    # 输出目录默认 output/（已 gitignore）
    out_dir = os.path.dirname(getattr(args, "out", "") or "")
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
