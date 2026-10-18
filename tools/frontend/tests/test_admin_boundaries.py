"""admin_boundaries（区县边界 + 点面匹配）单元测试——零网络、零 DB。

测什么 / 为什么这么测：
- 纯几何（assign_district / _point_in_rings）用合成多边形直接构造：0.1°×0.1°
  方形 + 洞环，内外/洞/边界带全部可控，不依赖任何下载；
- load_city_districts 的解析与缓存命中用 tmp_path 注入缓存目录 + monkeypatch
  掉网络层（_download_boundaries / urlopen）：真实下载在 CI / 无外网环境不可
  复现，但「缓存命中不重下」「下载失败返回 None 而非抛异常」这两个降级纪律
  必须钉死——真机验证（curl 走广州情景）覆盖真实下载路径，测试覆盖降级路径。
"""

import json
import os
import sys
import urllib.error

# tools/spatial 以 `tools.spatial` 包形式 import：repo 根必须在 sys.path
# （运行期由 frontend/db.py 注入；本文件独立于 frontend 测试时自行注入）。
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.spatial import admin_boundaries as ab  # noqa: E402

# ---------------- 合成几何：0.1°×0.1° 方形（约 11km 见方） ----------------

_SQUARE = [
    (100.0, 20.0),
    (100.1, 20.0),
    (100.1, 20.1),
    (100.0, 20.1),
]
# 洞环（在方形内部，0.02° 见方）：even-odd 规则下洞内的点必须判「外」
_HOLE = [
    (100.04, 20.04),
    (100.06, 20.04),
    (100.06, 20.06),
    (100.04, 20.06),
]


def _district(name, rings, bbox=None):
    if bbox is None:
        lons = [x for ring in rings for x, _ in ring]
        lats = [y for ring in rings for _, y in ring]
        bbox = (min(lons), min(lats), max(lons), max(lats))
    return {"name": name, "polygons": rings, "bbox": bbox}


# ---------------- assign_district：内外 / 洞 / 多面 ----------------


def test_assign_inside_outside_and_hole():
    d = _district("测试区", [_SQUARE, _HOLE])
    # 方形内（非洞）
    assert ab.assign_district(100.02, 20.02, [d]) == "测试区"
    # 洞内：外环与洞环各穿越一次、合计偶数 → even-odd 判外
    assert ab.assign_district(100.05, 20.05, [d]) is None
    # 方形外（右侧 / 左侧 / 上方）
    assert ab.assign_district(100.15, 20.05, [d]) is None
    assert ab.assign_district(99.95, 20.05, [d]) is None
    assert ab.assign_district(100.05, 20.15, [d]) is None


def test_assign_boundary_and_corner():
    # 顶点 / 边线恰在多边形边上的点：even-odd 对退化位置不做承诺（shapely 同样
    # 不保证），只要求**结果稳定**——同一坐标反复判定必须一致，不得在多次调用间漂移。
    d = _district("测试区", [_SQUARE])
    for pt in ((100.0, 20.0), (100.05, 20.0), (100.1, 20.1)):
        results = {ab.assign_district(pt[0], pt[1], [d]) for _ in range(5)}
        assert len(results) == 1, f"边界点判定不稳定：{pt} → {results}"


def test_assign_multi_polygon_flattened_rings():
    # 两个不相交方形压平成一个 ring 列表（DataV MultiPolygon 的真实形态）：
    # 点落在第二面上必须命中——even-odd 对互不重叠的多面统一计数即等价逐面判定。
    poly2 = [
        (101.0, 20.0),
        (101.1, 20.0),
        (101.1, 20.1),
        (101.0, 20.1),
    ]
    d = _district("双面区", [_SQUARE, poly2])
    assert ab.assign_district(101.05, 20.05, [d]) == "双面区"
    assert ab.assign_district(100.05, 20.05, [d]) == "双面区"
    # 两面之间的空隙 → None
    assert ab.assign_district(100.5, 20.05, [d]) is None


def test_assign_bbox_prefilter_correctness():
    # bbox 预筛只裁「不可能命中」的候选，不改变判定结果：两区 bbox 相邻时，
    # 落在各 bbox 内的点必须命中正确的区（预筛剪错会把 A 区的点判给 B 区）。
    d1 = _district("西区", [_SQUARE])
    east = [
        (100.1, 20.0),
        (100.2, 20.0),
        (100.2, 20.1),
        (100.1, 20.1),
    ]
    d2 = _district("东区", [east])
    assert ab.assign_district(100.05, 20.05, [d1, d2]) == "西区"
    assert ab.assign_district(100.15, 20.05, [d1, d2]) == "东区"


# ---------------- load_city_districts：解析 / 缓存 / 降级 ----------------


def _geojson_text():
    """两个 Feature 的最小 FeatureCollection：Polygon + MultiPolygon（含洞）。"""
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "甲区"},
                    "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in _SQUARE]]},
                },
                {
                    "type": "Feature",
                    "properties": {"name": "乙区"},
                    "geometry": {
                        "type": "MultiPolygon",
                        "coordinates": [
                            [[list(p) for p in _SQUARE], [list(p) for p in _HOLE]],
                            [[list(p) for p in _HOLE]],
                        ],
                    },
                },
            ],
        }
    )


def test_load_city_districts_parses_rings_and_bbox(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(ab, "_download_boundaries", lambda adcode: _geojson_text())
    districts, level = ab.load_city_districts("gz")
    assert districts is not None and [d["name"] for d in districts] == ["甲区", "乙区"]
    assert level == "district"  # DataV 区级名称以「区」结尾 → 判区级
    jia, yi = districts
    # Polygon → 1 条 ring；MultiPolygon 两个面（面1=外环+洞，面2=1 环）→ 3 条 ring
    assert len(jia["polygons"]) == 1
    assert len(yi["polygons"]) == 3
    # bbox 取全环并集（含洞环，洞不会扩 bbox，但并集计算必须不漏环）
    assert jia["bbox"] == (100.0, 20.0, 100.1, 20.1)
    # 解析出的 ring 直接可用于 assign_district（接口闭环）
    assert ab.assign_district(100.02, 20.02, districts) == "甲区"


def test_load_city_districts_cache_hit_skips_network(tmp_path, monkeypatch):
    # 缓存命中（文件存在且非空）→ 不发任何网络请求：把 urlopen 换成「一调就炸」，
    # 仍能从缓存读出完整结果，即证明命中路径零网络。
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    (tmp_path / "440100.json").write_text(_geojson_text(), encoding="utf-8")

    def _boom(*a, **kw):
        raise AssertionError("缓存命中时不得发起网络请求")

    monkeypatch.setattr(ab.urllib.request, "urlopen", _boom)
    districts, level = ab.load_city_districts("gz")
    assert [d["name"] for d in districts] == ["甲区", "乙区"]


def test_download_writes_cache_and_survives_restart(tmp_path, monkeypatch):
    # 首次下载成功 → 落盘；再次调用（新进程语义：直接读盘）不吃网络。
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    calls = []

    class _Resp:
        def read(self):
            return _geojson_text().encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(url, timeout):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(ab.urllib.request, "urlopen", _fake_urlopen)
    districts, level = ab.load_city_districts("gz")
    assert districts is not None
    assert len(calls) == 1 and "440100" in calls[0]
    assert (tmp_path / "440100.json").exists()
    # 第二次：网络层已撤（不再 monkeypatch 后其真实版本不可用），缓存命中即成
    monkeypatch.setattr(ab.urllib.request, "urlopen", _fake_urlopen)
    districts, level = ab.load_city_districts("gz")
    assert districts is not None
    assert len(calls) == 1, "缓存写入后不得重复下载"


def test_download_failure_returns_none_no_raise(tmp_path, monkeypatch):
    # 无外网（URLError / 普通 OSError / 空响应）→ 返回 None，绝不抛异常穿透到请求层
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))

    def _boom(*a, **kw):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(ab.urllib.request, "urlopen", _boom)
    assert ab.load_city_districts("gz") is None
    assert not list(tmp_path.iterdir()), "下载失败不得留下半截缓存文件"


def test_empty_cache_file_is_treated_as_miss(tmp_path, monkeypatch):
    # 缓存命中判据是「存在且非空」：0 字节文件当未命中，重新下载
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    (tmp_path / "440100.json").write_text("", encoding="utf-8")

    class _Resp:
        def read(self):
            return _geojson_text().encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ab.urllib.request, "urlopen", lambda url, timeout: _Resp())
    districts, level = ab.load_city_districts("gz")
    assert districts is not None and len(districts) == 2


def test_unknown_city_code_returns_none():
    assert ab.load_city_districts("xx") is None


def test_adcode_table_covers_all_guangdong_city_codes():
    # 表必须覆盖 risk/config.CITY_MAP 的全部 21 个城市码（逐一对照中文名核实过的
    # 国标 adcode）——漏一城，该城情景按区分组就会静默回落 industry。
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tools", "frontend"))
    import db as frontend_db  # noqa: E402  （顺带把 repo 根 / risk 路径挂好）

    assert set(frontend_db.config.CITY_MAP.values()) == set(ab.CITY_ADCODE.keys())
    # 抽查三个码值的正确性（全表逐项断言等于把数据文件复制进测试，无增量价值）
    assert ab.CITY_ADCODE["gz"] == "440100"
    assert ab.CITY_ADCODE["sz"] == "440300"
    assert ab.CITY_ADCODE["yf"] == "445300"


# ---------------- loan_district_map：城市过滤 / 未识别 / 数据不可用 ----------------


def test_loan_district_map_filters_and_unmatched(monkeypatch):
    monkey_districts = [_district("天河区", [_SQUARE])]
    monkeypatch.setattr(ab, "load_city_districts", lambda code: (monkey_districts, "district"))
    loans = [
        {"loan_id": 1, "city": "gz", "lat": 20.05, "lng": 100.02},  # 落在区内
        {"loan_id": 2, "city": "gz", "lat": 20.5, "lng": 100.5},  # 区外 → None
        {"loan_id": 3, "city": "gz", "lat": None, "lng": None},  # 坐标缺失 → None
        {"loan_id": 4, "city": "sz", "lat": 20.05, "lng": 100.02},  # 非本城 → 不进映射
    ]
    out = ab.loan_district_map(loans, "gz")
    assert out == {1: "天河区", 2: None, 3: None}
    assert 4 not in out


def test_loan_district_map_unavailable_returns_none_not_empty(monkeypatch):
    # 「数据不可用」返回 None（不是空表）：调用方据此降级分组维度，而「逐笔
    # 匹配不到」返回带 None 值的表、归「未识别」组——两种事实必须可区分。
    monkeypatch.setattr(ab, "load_city_districts", lambda code: None)
    assert (
        ab.loan_district_map([{"loan_id": 1, "city": "gz", "lat": 20.0, "lng": 100.0}], "gz")
        is None
    )


def test_loan_district_grouping_carries_level(monkeypatch):
    # loan_district_grouping = 映射 + 分组层级（直筒子市镇级 vs 其余区级）
    monkeypatch.setattr(
        ab, "load_city_districts", lambda code: ([_district("小榄镇", [_SQUARE])], "town")
    )
    mapping, level = ab.loan_district_grouping(
        [{"loan_id": 1, "city": "zs", "lat": 20.05, "lng": 100.02}], "zs"
    )
    assert mapping == {1: "小榄镇"} and level == "town"
    monkeypatch.setattr(ab, "load_city_districts", lambda code: None)
    assert ab.loan_district_grouping([{"loan_id": 1, "city": "zs"}], "zs") == (None, None)


# ---------------- 镇级（直筒子市 Overpass）：ring 拼接 / level / 解析 / failover ----------------
# 全部零网络：拼接与判定是纯函数直接构造；下载路径 monkeypatch urlopen。


def test_stitch_rings_two_ways_shared_endpoint():
    # 两段 way 共享端点拼成闭合环：链尾接下一段链首，收口点回到起点
    way1 = [_SQUARE[0], _SQUARE[1], _SQUARE[2]]
    way2 = [_SQUARE[2], _SQUARE[3], _SQUARE[0]]
    rings = ab._stitch_rings([way1, way2])
    assert len(rings) == 1
    ring = rings[0]
    assert ring[0] == tuple(_SQUARE[0]) and ring[-1] == tuple(_SQUARE[0])
    assert len(ring) == 5  # 4 顶点 + 收口点
    # 环可直接喂 even-odd 点面判定（与区级同一出口）
    assert ab.assign_district(100.02, 20.02, [_district("甲镇", rings)]) == "甲镇"


def test_stitch_rings_reverse_orientation_join():
    # 相邻 way 方向相反（Overpass member 常见）：尾接尾 / 头接头都须翻转后拼接
    way1 = [_SQUARE[0], _SQUARE[1], _SQUARE[2]]
    way2 = [_SQUARE[0], _SQUARE[3], _SQUARE[2]]  # 尾 == way1 尾
    rings = ab._stitch_rings([way1, way2])
    assert len(rings) == 1 and len(rings[0]) == 5
    way3 = [_SQUARE[2], _SQUARE[3], _SQUARE[0]]  # 头 == way1 尾（正向）
    way4 = [_SQUARE[0], _SQUARE[3], _SQUARE[2]]  # 头 == way3 尾
    rings2 = ab._stitch_rings([way3, way4])
    assert len(rings2) == 1 and len(rings2[0]) == 5


def test_stitch_rings_open_chain_dropped():
    # 接不满的链（数据缺一段）整链丢弃：开曲线冒充闭合环会把 even-odd 全判错
    way1 = [_SQUARE[0], _SQUARE[1], _SQUARE[2]]
    way2 = [_SQUARE[3], (101.0, 20.0), (101.0, 20.1)]
    assert ab._stitch_rings([way1, way2]) == []
    assert ab._stitch_rings([]) == []
    assert ab._stitch_rings([[_SQUARE[0]]]) == []  # 单点残段


def test_stitch_rings_duplicate_consecutive_points_deduped():
    # 连续重复点去重后再拼（Overpass geometry 偶发），不产生假端点
    way1 = [_SQUARE[0], _SQUARE[0], _SQUARE[1], _SQUARE[2]]
    way2 = [_SQUARE[2], _SQUARE[3], _SQUARE[0]]
    rings = ab._stitch_rings([way1, way2])
    assert len(rings) == 1 and len(rings[0]) == 5


def test_stitch_rings_outer_and_hole_are_independent_rings():
    # 外环 + 洞环各拼各的环，压平后由 even-odd 统一处理洞（与区级 _rings_of 同构）
    outer = [_SQUARE[0], _SQUARE[1], _SQUARE[2], _SQUARE[3], _SQUARE[0]]
    hole = [_HOLE[0], _HOLE[1], _HOLE[2], _HOLE[3], _HOLE[0]]
    rings = ab._stitch_rings([outer, hole])
    assert len(rings) == 2
    d = _district("甲镇", rings)
    assert ab.assign_district(100.02, 20.02, [d]) == "甲镇"
    assert ab.assign_district(100.05, 20.05, [d]) is None  # 洞内判外


def test_detect_level_town_vs_district():
    # 层级判据：镇/街道结尾占比 > 0.5 → town（中山实测 23 个全是镇/街道）
    assert ab._detect_level(["小榄镇", "石岐街道", "东区街道", "三乡镇"]) == "town"
    assert ab._detect_level(["天河区", "越秀区", "海珠区"]) == "district"
    assert ab._detect_level(["小榄镇", "天河区"]) == "district"  # 占比不过半不误判
    assert ab._detect_level([]) == "district"


def _ov_element(name, ways):
    """构造 Overpass relation element：ways 是 [(lng, lat), ...] 列表的列表，
    逐点转成 Overpass geometry 的 {lat, lon} 对象序（lat 在前）。"""
    return {
        "type": "relation",
        "tags": {"name": name, "boundary": "administrative", "admin_level": "8"},
        "members": [
            {
                "type": "way",
                "ref": 1000 + i,
                "role": "outer",
                "geometry": [{"lat": lat, "lon": lon} for (lon, lat) in way],
            }
            for i, way in enumerate(ways)
        ],
    }


def _overpass_text():
    """缩小版 Overpass 响应：甲镇（两段 way 拼一环）、乙镇（外环+洞两环）、
    一个无名 relation 与一个无 ways 的 relation（都应跳过）。"""
    outer = [_SQUARE[0], _SQUARE[1], _SQUARE[2], _SQUARE[3], _SQUARE[0]]
    return json.dumps(
        {
            "elements": [
                _ov_element("甲镇", [outer[:3], outer[2:]]),
                _ov_element(
                    "乙镇",
                    [outer[:3], outer[2:], [_HOLE[0], _HOLE[1], _HOLE[2], _HOLE[3], _HOLE[0]]],
                ),
                {"type": "relation", "tags": {"admin_level": "8"}, "members": []},
                _ov_element("空壳镇", []),
                {"type": "way", "tags": {"name": "不是关系"}, "geometry": []},
            ]
        }
    )


def test_parse_overpass_builds_towns_and_skips_unusable():
    towns = ab._parse_overpass(json.loads(_overpass_text()))
    assert towns is not None and [t["name"] for t in towns] == ["甲镇", "乙镇"]
    jia, yi = towns
    assert len(jia["polygons"]) == 1
    assert len(yi["polygons"]) == 2  # 外环 + 洞环
    assert jia["bbox"] == (100.0, 20.0, 100.1, 20.1)
    # 解析结果直接可用于点面判定（接口闭环）
    assert ab.assign_district(100.02, 20.02, towns) == "甲镇"
    # 空响应 / 非 dict → None（数据不可用，由调用方降级）
    assert ab._parse_overpass({"elements": []}) is None
    assert ab._parse_overpass({}) is None


def test_parse_overpass_member_without_geometry_skipped():
    # 个别 member 无 geometry（数据缺失）→ 跳过该 way，其余 way 照常拼接
    el = _ov_element("甲镇", [[_SQUARE[0], _SQUARE[1], _SQUARE[2], _SQUARE[3], _SQUARE[0]]])
    el["members"].append({"type": "way", "ref": 9999, "role": "inner", "geometry": None})
    towns = ab._parse_overpass({"elements": [el]})
    assert towns is not None and len(towns[0]["polygons"]) == 1


def test_load_city_districts_town_level_for_direct_city(tmp_path, monkeypatch):
    # 直筒子市（zs）走 Overpass 镇级：缓存命中 → (镇列表, "town")
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    (tmp_path / "442000_towns.json").write_text(_overpass_text(), encoding="utf-8")

    def _boom(*a, **kw):
        raise AssertionError("镇级缓存命中时不得发起网络请求")

    monkeypatch.setattr(ab.urllib.request, "urlopen", _boom)
    districts, level = ab.load_city_districts("zs")
    assert level == "town"
    assert [d["name"] for d in districts] == ["甲镇", "乙镇"]


def test_load_city_districts_direct_city_falls_back_to_datav_when_towns_fail(tmp_path, monkeypatch):
    # 镇级取不到（Overpass 全挂）→ 落 DataV 兜底；直筒子市 full.json 无区县面
    # （features 为空）→ 整体 None（与区级失败同一降级出口），绝不抛异常。
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(ab, "_download_towns", lambda adcode: None)
    monkeypatch.setattr(ab, "_download_boundaries", lambda adcode: json.dumps({"features": []}))
    assert ab.load_city_districts("dg") is None


def test_town_download_failover_to_next_endpoint(tmp_path, monkeypatch):
    # 主端点 504 → 换镜像端点成功：failover 顺序即 _OVERPASS_URLS 顺序
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 504, "gateway timeout", {}, None)
        return _Resp(_overpass_text().encode("utf-8"))

    monkeypatch.setattr(ab.urllib.request, "urlopen", _fake_urlopen)
    towns = ab._load_towns("442000")
    assert [t["name"] for t in towns] == ["甲镇", "乙镇"]
    assert calls == list(ab._OVERPASS_URLS)
    assert (tmp_path / "442000_towns.json").exists()


def test_town_download_bad_content_tries_next_endpoint(tmp_path, monkeypatch):
    # HTTP 200 + 坏内容（HTML 错误页 / 空 elements）同样视为该端点失败换下一个，
    # 且坏响应绝不落成缓存
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    bodies = [b"<html>overpass 502</html>", _overpass_text().encode()]

    def _fake_urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp(bodies[len(calls) - 1])

    monkeypatch.setattr(ab.urllib.request, "urlopen", _fake_urlopen)
    towns = ab._load_towns("442000")
    assert [t["name"] for t in towns] == ["甲镇", "乙镇"]
    assert len(calls) == 2
    assert (tmp_path / "442000_towns.json").exists()


def test_town_download_empty_elements_everywhere_returns_none(tmp_path, monkeypatch):
    # HTTP 200 但 elements 为空（查询命中 0 条，如市名对不上）：内容校验失败
    # 全部端点走完仍 None，且不留缓存
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout):
        calls.append(req.full_url)
        return _Resp(json.dumps({"elements": []}).encode())

    monkeypatch.setattr(ab.urllib.request, "urlopen", _fake_urlopen)
    assert ab._load_towns("442000") is None
    assert len(calls) == len(ab._OVERPASS_URLS)
    assert not list(tmp_path.iterdir())


def test_town_download_all_endpoints_fail_returns_none_no_cache(tmp_path, monkeypatch):
    # 全部端点失败 → None（与区级失败同一降级纪律），不留半截缓存文件
    monkeypatch.setattr(ab, "_CACHE_DIR", str(tmp_path))
    calls = []

    def _boom(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(ab.urllib.request, "urlopen", _boom)
    assert ab._load_towns("442000") is None
    assert len(calls) == len(ab._OVERPASS_URLS)
    assert not list(tmp_path.iterdir()), "下载失败不得留下半截缓存文件"
