"""P5 底图瓦片静态回归防线（失败态不粘住 + 本地瓦片目录默认值，F1 + F2）。

F2（默认值推导）是纯 Python 逻辑，直接函数式断言三条优先级，不启 HTTP 服务、不连数据库。
F1（失败态粘住）运行在浏览器里，pytest 触达不到 Leaflet 运行时，故沿用同目录
``test_p6_*_static.py`` 一批的文本级断言风格，锁死两条最小必要条件：
1. 存在 ``.p5-tile-overlay`` 的**移除路径**——修复前整个前端目录里该类名只出现在创建处一行；
2. 存在成功信号监听（``tileload`` / ``load``）——修复前全仓 0 处，提示因此永不撤销。
外加 §4 列出的「不要回退」项（非阻断样式、去重、errorTileUrl、缩放封顶）做防回归。
"""

import importlib
import os
import re
from pathlib import Path

import pages.p5_spatial as p5

ROOT = Path(__file__).resolve().parents[3]  # tests/ 的上三级 = 仓库根
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p5_spatial.js"

OSM_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
LOCAL_URL = "/tiles/{z}/{x}/{y}.png"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _tile_layer_region(text: str) -> str:
    """取底图 tileLayer 从创建到挂完事件的那段源码（事件挂完后接 L.control.scale）。"""
    m = re.search(r"const tileLayer = L\.tileLayer\(.*?L\.control\.scale", text, re.S)
    assert m, "未找到 tileLayer 创建 + 事件挂载区段"
    return m.group(0)


def _load_handler_body() -> str:
    """取 tileLayer「整批瓦片结束（load）」处理器源码，批次判定与计数器清零都必须在里面。"""
    region = _tile_layer_region(_js_text())
    m = re.search(r'\.on\(\s*["\']load["\'].{0,40}?\{(.*?)\n {6}\}', region, re.S)
    assert m, 'tileLayer 上没有 "load"（整批结束）处理器 → 提示无撤销时机，F1 未修'
    return m.group(1)


# ---------------- F1：失败提示必须能撤销（先红项） ----------------


def test_tile_layer_listens_for_success_signal():
    region = _tile_layer_region(_js_text())
    assert re.search(r'\.on\(\s*["\']tileload["\']', region), (
        "tileLayer 无 tileload 监听 → 无法知道本批瓦片已成功（F1 根因：只监听 tileerror）"
    )
    assert re.search(r'\.on\(\s*["\']load["\']', region), (
        "tileLayer 无 load（整批结束）监听 → 没有撤销提示 / 复位去重标记的时机（F1 根因）"
    )


def test_success_counter_excludes_error_tile_placeholder():
    """§5.2 假信号：出错瓦片的 src 会被换成 errorTileUrl（data: 占位图），它自身能解码，
    于是整批失败也会跟着触发一堆 tileload。不剔掉它，「无一成功」永远判不出来。"""
    region = _tile_layer_region(_js_text())
    m = re.search(r'\.on\(\s*["\']tileload["\'][\s\S]{0,500}?\}\)', region)
    assert m, "tileLayer 上没有 tileload 处理器实现，无从校验成功计数"
    assert "data:" in m.group(0), (
        "tileload 计数必须排除 data: 占位图，否则失败批次会被误判为有成功瓦片（§5.2）"
    )


def test_tile_error_overlay_has_removal_path():
    text = _js_text()
    assert re.search(
        r"""querySelector(?:All)?\(\s*["']\.p5-tile-overlay["']\s*\)[\s\S]{0,200}?\.(remove|removeChild)\(
            |removeChild\([\s\S]{0,120}p5-tile-overlay""",
        text,
        re.X,
    ), "全文件找不到 .p5-tile-overlay 的移除路径 → 一次瞬时失败会留下永久假失败态（F1）"


def test_tile_warning_gated_on_zero_success():
    load_body = _load_handler_body()
    assert re.search(r"(ok\w*\s*===?\s*0|!\s*ok\w*)", load_body), (
        "提示必须只在「本批一张都没成功」时出现，否则边缘瓦片偶发 404 就会误报（F1 期望行为 2）"
    )
    assert re.search(r"err\w*\s*>\s*0", load_body), (
        "无失败瓦片时不应撤销/显示提示，需 err > 0 前置条件"
    )


def test_idle_batch_does_not_cancel_warning():
    """出错瓦片的 data: 占位图回填后，Leaflet 会再触发一次 ok/err 全 0 的 load。
    撤销分支若写成「非失败批就撤」，提示会在出现的同一刻被自己撤掉。"""
    body = _load_handler_body()
    assert re.search(r"if \(\s*ok\w*\s*>\s*0\s*\)", body), (
        "撤销提示必须以「本批确有成功瓦片」为条件，而不是「不是失败批」（空转批会误撤）"
    )


def test_tile_counters_reset_after_batch():
    load_body = _load_handler_body()
    assert re.search(r"\bok\w*\s*=\s*0\b", load_body), "批次结束后必须清零 ok 计数器"
    assert re.search(r"\berr\w*\s*=\s*0\b", load_body), (
        "批次结束后必须清零 err 计数器，否则下一批的判定会带上上一批的账"
    )


def test_tile_warning_non_blocking_design_preserved():
    """§4 明确「不要回退」的既有点，逐条钉住。"""
    text = _js_text()
    region = _tile_layer_region(text)
    assert 'pointerEvents: "none"' in region, "覆盖层必须保持非阻断，不得拦截地图交互"
    assert "zIndex: 500" in region, "覆盖层层级保持 500，不得改成遮罩式高层级"
    assert "errorTileUrl" in region, "保留浅灰占位瓦片，避免出现裂图"
    assert "_tileErrShown" in region, "保留整批只提示一次的去重标记（但必须可复位）"
    assert "maxNativeZoom: 12" in region, "原生缩放封顶 12 是调过的，不要动"
    assert "minZoom: 4" in text and "maxZoom: 18" in text, "地图缩放上下限是调过的，不要动"


def test_no_leftover_debug_probe_on_window():
    """验收 A2 允许临时探针，但提交前必须删掉（工单 §2 F1 验收 A2 括注）。"""
    assert "__p5tile" not in _js_text(), "残留调试探针会污染全局命名空间"


# ---------------- F2：本地瓦片默认值不再只靠 .env ----------------


def test_f2_explicit_env_wins(tmp_path):
    got = p5._resolve_tile_url(
        env={"SPATIAL_TILE_URL": "/custom/{z}/{x}/{y}.png"}, tile_dir=str(tmp_path)
    )
    assert got == "/custom/{z}/{x}/{y}.png", "显式 SPATIAL_TILE_URL 必须优先级最高（内网部署可控）"


def test_f2_local_tile_dir_present_falls_to_local(tmp_path):
    (tmp_path / "9.png").write_bytes(b"\x89PNG")  # 目录非空即认为本地产物在
    assert p5._resolve_tile_url(env={}, tile_dir=str(tmp_path)) == LOCAL_URL


def test_f2_tile_dir_missing_or_empty_falls_back_to_osm(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert p5._resolve_tile_url(env={}, tile_dir=str(empty)) == OSM_URL, "空目录不算本地产物"
    assert p5._resolve_tile_url(env={}, tile_dir=str(tmp_path / "nope")) == OSM_URL, (
        "目录不存在必须回落 OSM"
    )
    assert p5._resolve_tile_url(env={}, tile_dir=str(ROOT / "requirements.txt")) == OSM_URL, (
        "路径存在但不是目录时不得当作瓦片目录（os.listdir 会抛 NotADirectoryError）"
    )


def test_f2_module_constant_uses_the_derivation(monkeypatch):
    monkeypatch.delenv("SPATIAL_TILE_URL", raising=False)
    importlib.reload(p5)
    if p5._has_local_tiles(p5._LOCAL_TILE_DIR):  # 跑过 download_tiles.py 的机器
        assert p5.TILE_URL == LOCAL_URL, "本地瓦片在位却仍默认公网 OSM = 换启动方式就灰图（F2）"
    else:  # fresh clone，行为必须不变
        assert p5.TILE_URL == OSM_URL


def test_f2_no_reverse_import_from_app_entry():
    """app.py 会 import pages，页面反向 import app 会成环（工单 F2 根因位置注记）。"""
    src = (ROOT / "tools" / "frontend" / "pages" / "p5_spatial.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+app\b", src, re.M), (
        "pages/ 不得 import app（循环导入）"
    )
    assert os.path.isabs(p5._LOCAL_TILE_DIR), "静态瓦片目录应在本文件内算成绝对路径，不依赖 cwd"


# ---------------- 本地瓦片覆盖下界（F4）+ 视口补边（vieww） ----------------
#
# 本段全部是**源码级**断言：`static/tiles/` 被 .gitignore（第 79 行），fresh clone 上根本不存在，
# 任何依赖瓦片目录内容的用例都不可接受。F4 的真实数据验收（A1/A2/A3）走人工实测。
# F5（dev_server.py 清洗 SF_HOTRELOAD）的用例已拆入独立文件 test_dev_server_static.py，随 dev commit 提交。

#: §6.4 要求「先红」——把 HEAD 版本 `git show HEAD:… > /tmp/…` 后用这个环境变量把解析目标
#: 指过去跑一次，证明修复前用例确实 fail。默认（未设置时）永远指向真实仓库文件，不允许借它放宽判定。
_DL_OVERRIDE = os.environ.get("SF_P5_DOWNLOAD_TILES_SRC")


def _download_tiles_text() -> str:
    p = Path(_DL_OVERRIDE) if _DL_OVERRIDE else ROOT / "tools" / "frontend" / "download_tiles.py"
    return p.read_text(encoding="utf-8")


def _js_int(pattern: str) -> int:
    m = re.search(pattern, _js_text())
    assert m, f"p5_spatial.js 里取不到 {pattern}"
    return int(m.group(1))


def _arg_default(src: str, flag: str) -> int:
    """取 download_tiles.py 里 argparse 某个参数的 default= 整数值。"""
    m = re.search(rf'add_argument\(\s*["\']--{flag}["\'][^)]*?default=(\d+)', src)
    assert m, f"download_tiles.py 里取不到 --{flag} 的 default="
    return int(m.group(1))


def test_tile_pyramid_floor_matches_map_zoom_bounds():
    """F4 的成因类防线：底图金字塔下界与地图 minZoom 是两个独立编辑的常量，一旦倒挂 z4..z7 永久 404。

    修复前 `--minz default=8` 而 `minZoom: 4` ⇒ 本用例必红（工单 §6.4 已按 HEAD 版本实测）。
    """
    src = _download_tiles_text()
    min_zoom = _js_int(r"L\.map\([\s\S]{0,160}?minZoom:\s*(\d+)")
    max_native = _js_int(r"maxNativeZoom:\s*(\d+)")
    minz, maxz = _arg_default(src, "minz"), _arg_default(src, "maxz")
    assert minz <= min_zoom, (
        f"离线瓦片金字塔下界 z{minz} 高于地图 minZoom {min_zoom} ⇒ z{min_zoom}..z{minz - 1} 永久 404（F4）"
    )
    assert maxz >= max_native, (
        f"离线瓦片金字塔上界 z{maxz} 低于 maxNativeZoom {max_native} ⇒ 高层级永久 404"
    )


def test_tile_pyramid_docstring_no_longer_claims_default_fit_at_z8():
    """F4 根因里那条被证伪的前提不能以注释形式留在仓库（§1.1：560px 容器装不下 z8 的 901px 高）。

    用精确串匹配，不用宽泛正则——「出现过 z8 字样」判不了任何东西。
    """
    assert "地图默认 fit 落在此区间" not in _download_tiles_text(), (
        "docstring 仍写着「地图默认 fit 落在此区间」＝与 p5_spatial.js 矛盾的假前提，正是 F4 的成因"
    )


def test_download_tiles_rejects_bad_pages_by_png_magic_only():
    """§4.1 不要回退：低缩放层大量海面/空区是合法的 1-bit PNG，改成按体积阈值丢弃会把 z4/z5 洗成缺口。"""
    body = re.search(r"def fetch\(.*?\ndef main", _download_tiles_text(), re.S).group(0)
    assert 'data[:8] != b"\\x89PNG' in body, (
        "fetch() 的 PNG 魔数判定被改动（F4 补齐低层级依赖它保留小体积合法瓦片）"
    )
    assert "getsize" not in body and "st_size" not in body, "fetch() 不得按体积阈值丢瓦片（§4.1）"


# ---------------- vieww 视口补边（A4） ----------------
#
# _tile_rect() 按「数据 bbox」铺瓦片时会漏掉窄于视口的贴边一圈（A4 灰块），补边量必须随视口反算：
#   低层级 bbox 窄于视口 → 行列补满到盖住视口；bbox 已宽过视口的层级 → 自动补 0。
# 这里是纯函数级断言，直接加载 download_tiles.py（支持 SF_P5_DOWNLOAD_TILES_SRC 指向 HEAD 做「先红」验证）。


def _load_download_tiles():
    import importlib.util as iu

    p = Path(_DL_OVERRIDE) if _DL_OVERRIDE else ROOT / "tools" / "frontend" / "download_tiles.py"
    spec = iu.spec_from_file_location("_sf_download_tiles", p)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gd_defaults() -> dict:
    """从 download_tiles.py 的 argparse 默认值取 bbox，保证测试与脚本抓取范围同步。"""
    src = _download_tiles_text()
    return dict(
        lng0=_arg_default(src, "lng0"),
        lng1=_arg_default(src, "lng1"),
        lat0=_arg_default(src, "lat0"),
        lat1=_arg_default(src, "lat1"),
    )


def test_vieww_arg_default_is_2560():
    assert _arg_default(_download_tiles_text(), "vieww") == 2560, (
        "--vieww 默认必须是 2560px（A4 补边基准，改小会在宽屏视口贴边露 errorTile 灰块）"
    )


def test_vieww_low_zoom_exact_counts():
    """z4–z9 bbox 窄于视口，补边后的瓦片数量钉死（默认 bbox + vieww=2560）。

    数值由当前算法实测；若退回固定片数或按层级常数补边，这些计数立刻变化。
    """
    dt = _load_download_tiles()
    gd = _gd_defaults()
    expected = {4: 108, 5: 144, 6: 144, 7: 132, 8: 132, 9: 156}
    for z, n in expected.items():
        x_lo, x_hi, y_lo, y_hi = dt._tile_rect(z, **gd, vieww=2560)
        got = (x_hi - x_lo + 1) * (y_hi - y_lo + 1)
        assert got == n, f"z{z} 补边后应下 {n} 张，实际 {got} 张"


def test_vieww_high_zoom_padding_drops_to_zero():
    """bbox 已宽过视口的层级（z10+）补边自动归 0：rect 必须严格等于原始 bbox。"""
    dt = _load_download_tiles()
    gd = _gd_defaults()
    for z in range(10, 13):
        xs, ys = dt.tile_ranges(z, **gd)
        x_lo, x_hi, y_lo, y_hi = dt._tile_rect(z, **gd, vieww=2560)
        assert (x_lo, x_hi, y_lo, y_hi) == (min(xs), max(xs), min(ys), max(ys)), (
            f"z{z} bbox 已宽过视口仍补边 ⇒ 多下无收益边缘（固定扩 2 片会在 z12 多 ~700 张）"
        )


def test_vieww_z12_exact_count():
    """z12 是抓取体积的大头（约 7.7k 张），数量必须被钉住：多补一圈就是上千张无收益下载。"""
    dt = _load_download_tiles()
    gd = _gd_defaults()
    x_lo, x_hi, y_lo, y_hi = dt._tile_rect(12, **gd, vieww=2560)
    got = (x_hi - x_lo + 1) * (y_hi - y_lo + 1)
    assert got == 7725, f"z12 应恰好 7725 张（不加多余边缘），实际 {got} 张"
