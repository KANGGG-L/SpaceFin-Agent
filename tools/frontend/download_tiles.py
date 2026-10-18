#!/usr/bin/env python3
"""下载离线地图瓦片到 tools/frontend/static/tiles/，供空间页（P5）离线使用。

WSL 直连 OSM 主站瓦片被墙，Carto CDN 亦时通时断；高德(autonavi) 经测试稳定可达
且带中文标注，故默认源用 autonavi。Carto 作全球底图备选（--source carto）。

瓦片存为 static/tiles/{z}/{x}/{y}.png；前端把 SPATIAL_TILE_URL 设为
/tiles/{z}/{x}/{y}.png（相对同源）后即完全离线，浏览器无需外网。

下载层级默认 z4–z12（广东范围约 1.2 万张 + 低层级数百张）：
  - z4–z7  缩小到全省之外的层级。前端地图 minZoom: 4，且 #p5-leaflet 容器固定 560px 高，
           装不下 z8 显示全省 bbox 所需的约 901px，所以默认 fitBounds 只会落在 z7——
           下界必须一路跟到 minZoom，否则 z4..z7 永久 404、屏幕上出现 errorTileUrl 灰块。
  - z8–z9  全省概览
  - z10–z12 城市/区县级下钻查看
如需街道级细节，加 --maxz 13（约再 +3.6 万张，耗时与限流风险上升）。
本脚本的 --minz/--maxz 与 p5_spatial.js 的 minZoom/maxNativeZoom 由
tests/test_p5_basemap_static.py::test_tile_pyramid_floor_matches_map_zoom_bounds 钉在一起，
两边一旦倒挂该用例立刻红。

另需注意：tile_ranges() 算的是**数据 bbox** 覆盖的行列，而 Leaflet 铺满的是**视口**
（#p5-leaflet 高 560px、宽 100% 流体）。z4..z7 每级 bbox 只有 2~5 列、1900px 宽的视口却要
8 列，只按 bbox 抓会在贴边留下一圈 errorTileUrl 灰块。补边量不能写成固定片数：瓦片恒 256
世界像素，半视口折合几片与层级无关，而 bbox 却随层级翻倍变宽——常数片数必然低层级不够、
高层级浪费（实测：固定扩 2 片会在 z12 多下约 700 张毫无收益的边缘）。故改用 --vieww（默认 2560px）按视口反算，
bbox 已宽过视口的层级自动得 0。

用法：
  python3 download_tiles.py                 # 默认广东 z4–z12，autonavi 源
  python3 download_tiles.py --maxz 13       # 抓到街道级
  python3 download_tiles.py --source carto
  python3 download_tiles.py --lng0 113 --lng1 114 --lat0 23 --lat1 24  # 只抓广州

说明：瓦片按 OSM Web Mercator 行列编号存储，与 Leaflet {z}/{x}/{y} 一致。
尊重源站：默认并发 16、失败重试 3 次、跳过已存在文件（可断点续传）。
"""

import argparse
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "tiles")

SOURCES = {
    # Carto：{s} 子域 a/b/c/d，全局干净样式
    "carto": lambda s, z, x, y: f"https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    # 高德：webrd01~04 子域轮询；style=8 为路网标注底图（中文标注）
    "autonavi": lambda s, z, x, y: (
        f"https://webrd{s}.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}"
        f"&lang=zh_cn&size=1&scale=1&style=8"
    ),
}


def tile_ranges(z, lng0, lng1, lat0, lat1):
    """返回 (x_range, y_range)：Web Mercator 瓦片行列（y 北小南下大）。"""
    n = 2**z

    def xt(lng):
        return int((lng + 180) / 360 * n)

    def yt(lat):
        lat = math.radians(lat)
        return int((1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2 * n)

    x0, x1 = xt(lng0), xt(lng1)
    y_north, y_south = yt(lat1), yt(lat0)  # lat1=北，对应较小 y
    xs = range(min(x0, x1), max(x0, x1) + 1)
    ys = range(min(y_north, y_south), max(y_north, y_south) + 1)
    return xs, ys


def _tile_rect(z, lng0, lng1, lat0, lat1, vieww=2560):
    """返回补边后的瓦片矩形 (x_lo, x_hi, y_lo, y_hi)。

    tile_ranges() 只按**数据 bbox** 铺瓦片，而 Leaflet 铺满的是视口；bbox 窄于视口的
    层级会在贴边留下一圈灰块（A4）。瓦片恒为 256 世界像素，「半视口折合几片」与层级
    无关，而 bbox 却随层级翻倍变宽——故补边量 = 半视口片数 - bbox 半宽：低层级扩得多，
    bbox 已宽过视口的层级自动得 0，不会为高层级多下无收益的边缘。数值由
    tests/test_p5_basemap_static.py::test_vieww_edge_ring 钉住。
    """
    xs, ys = tile_ranges(z, lng0, lng1, lat0, lat1)
    n = 2**z
    r = vieww / 2 / 256
    ring_x = max(0, math.ceil(r - (len(xs) - 1) / 2))
    ring_y = max(0, math.ceil(r - (len(ys) - 1) / 2))
    x_lo = max(0, min(xs) - ring_x)
    x_hi = min(n - 1, max(xs) + ring_x)
    y_lo = max(0, min(ys) - ring_y)
    y_hi = min(n - 1, max(ys) + ring_y)
    return x_lo, x_hi, y_lo, y_hi


def fetch(url, dest, ua):
    if os.path.exists(dest):
        return "skip"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            # 仅按 PNG 魔数判错页（autonavi 海洋/空瓦片是合法 1-bit PNG，体积小但须保留）
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                return "notpng"
            with open(dest, "wb") as f:
                f.write(data)
            return "ok"
        except Exception:
            time.sleep(1 + attempt)
    return "fail"


def main():
    ap = argparse.ArgumentParser(description="下载离线地图瓦片")
    # 下界与前端地图 minZoom: 4 对齐（默认 fitBounds 因 560px 容器只落到 z7）；
    # 倒挂会被 tests/test_p5_basemap_static.py::test_tile_pyramid_floor_matches_map_zoom_bounds 拦住。
    ap.add_argument("--minz", type=int, default=4)
    ap.add_argument("--maxz", type=int, default=12)
    ap.add_argument("--lng0", type=float, default=109.0)
    ap.add_argument("--lng1", type=float, default=118.0)
    ap.add_argument("--lat0", type=float, default=20.0)
    ap.add_argument("--lat1", type=float, default=26.0)
    ap.add_argument("--source", default="autonavi", choices=list(SOURCES))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument(
        "--vieww",
        type=int,
        default=2560,
        help="按最宽视口补边（px）：容器 width:100%，bbox 窄于视口时两侧会露 errorTile 灰边",
    )
    args = ap.parse_args()

    subs = ["a", "b", "c", "d"] if args.source == "carto" else ["01", "02", "03", "04"]
    ua = "SpaceFin-dev-tile-downloader/1.0 (local dev; +https://example.com)"

    jobs = []
    for z in range(args.minz, args.maxz + 1):
        x_lo, x_hi, y_lo, y_hi = _tile_rect(
            z, args.lng0, args.lng1, args.lat0, args.lat1, args.vieww
        )
        for x in range(x_lo, x_hi + 1):
            for y in range(y_lo, y_hi + 1):
                s = subs[(x + y) % len(subs)]
                url = SOURCES[args.source](s, z, x, y)
                dest = os.path.join(STATIC, str(z), str(x), f"{y}.png")
                jobs.append((url, dest))

    print(
        f"[tiles] {len(jobs)} tiles  z{args.minz}-{args.maxz}  vieww={args.vieww}  src={args.source}\n"
        f"[tiles] bbox lng[{args.lng0},{args.lng1}] lat[{args.lat0},{args.lat1}] -> {STATIC}"
    )
    ok = skip = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch, url, dest, ua) for url, dest in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            if r == "ok":
                ok += 1
            elif r == "skip":
                skip += 1
            elif r == "notpng":
                fail += 1
            else:
                fail += 1
            if i % 1000 == 0 or i == len(jobs):
                print(f"[tiles] {i}/{len(jobs)} ok={ok} skip={skip} fail={fail}")
    print(f"[tiles] done ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()
