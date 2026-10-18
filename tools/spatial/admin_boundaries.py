"""区/县行政边界获取与点面匹配（零第三方依赖），供 P6 组合压力测试按区分组使用。

口径（为什么是这几条，写清否则数字没人敢信）：

- 为什么按坐标匹配而不按地址：抵押物地址 property_addr 在合成数据里是「小区名」
  风格的文本（tools/spatial 的 coord 口径说明），真实数据里区名覆盖率仅 5.6%
  ——按地址里的「××区」筛会把绝大多数 record 归「未识别」，分组表失去意义。
  而押品坐标（dws_spatial_feature 的 collateral lat/lng）是全仓「抵押物落点」的
  唯一口径（P5 网格命中、_impact 空间聚合都吃它），按坐标落区与既有空间链路
  同源，两处不会漂。
- 为什么零依赖射线法：venv（tools/orchestrator/.venv）没有 shapely，也不为
  一个分组维度引入几何依赖——even-odd（奇偶规则）射线法对 Polygon 的外环 +
  洞环统一做穿越奇偶计数即可正确处理洞（点在洞内穿越两次、回到偶数、判外），
  MultiPolygon 同理（行政区多面互不重叠，全局奇偶等价于逐面判定）。
  性能靠 district bbox 预筛兜底：区级 bbox 极小（0.05°~1° 见方），绝大多数贷款
  一轮 bbox 比较就出局，射线法只跑个位数候选。
- 为什么 adcode 表写死在本模块而不 import config：tools/spatial 与 tools/risk
  各有一个顶层 config 模块（同名冲突，db.py 为此专门按路径加载 risk 的），
  本模块保持零仓库内依赖、谁都能安全 import；adcode 是十年不变的行政区划事实
  （国标 GB/T 2260），写死不构成维护负担。来源：阿里 DataV GeoAtlas
  （areas_v3，https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json），
  城市码以 tools/risk/config.CITY_MAP 为准，逐个对照中文名核实。
- 直筒子市（东莞/中山）为何走 OSM Overpass：DataV 边界只到区县一层，而这
  两市是「直筒子市」（不设区，下辖镇/街道），DataV 的 441900/442000_full.json
  没有区县面（features 为空）。镇界改走 OSM Overpass API：查市域内
  admin_level=8 的行政区 relation（实测中山 23 个镇/街道、东莞 32+ 个，
  与民政部区划表一致）。Overpass 返回的是 relation + member ways（out geom
  带完整坐标），一条镇界往往被拆成多条 way——须按共享端点把 ways 拼接成
  闭合环再转成与区级相同的多边形结构（见 _stitch_rings）。多端点依次尝试
  （主站挂了走镜像），任一成功即落盘缓存；全部失败返回 None，与区级失败
  同一降级纪律，绝不抛异常穿透。
- 缓存：边界 JSON 落 data_lake/spatial/boundaries/{adcode}.json（直筒子市
  镇界为 {adcode}_towns.json；data_lake/ 整目录已在 .gitignore，几 MB 边界
  数据不入库；命中即不重新下载）。下载失败（无外网等）返回 None，由调用方
  走降级路径，绝不让分组维度把请求打成 500。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# 城市码 → 国标 adcode（6 位）。城市码与中文名逐个对照 tools/risk/config.CITY_MAP
# 核实（gz=广州 … yf=云浮，广东 21 地市全量）；adcode 来自阿里 DataV GeoAtlas。
# code → (adcode, 中文名)：中文名只用于注释可读性，不参与运行时逻辑。
CITY_ADCODE = {
    "gz": "440100",  # 广州
    "sg": "440200",  # 韶关
    "sz": "440300",  # 深圳
    "zh": "440400",  # 珠海
    "st": "440500",  # 汕头
    "fs": "440600",  # 佛山
    "jm": "440700",  # 江门
    "zj": "440800",  # 湛江
    "mm": "440900",  # 茂名
    "zq": "441200",  # 肇庆
    "hui": "441300",  # 惠州
    "mz": "441400",  # 梅州
    "sw": "441500",  # 汕尾
    "hy": "441600",  # 河源
    "yj": "441700",  # 阳江
    "qy": "441800",  # 清远
    "dg": "441900",  # 东莞（直筒子市：DataV 无区县面 → Overpass 镇级）
    "zs": "442000",  # 中山（同上）
    "cz": "445100",  # 潮州
    "jy": "445200",  # 揭阳
    "yf": "445300",  # 云浮
}

# 边界缓存目录：data_lake/ 整目录已 gitignore（ODS 本地产物同一处），边界 JSON
# 属下载产物不入库；目录不存在时下载成功后创建。
_CACHE_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    "data_lake",
    "spatial",
    "boundaries",
)

_DATAV_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
# 10s：边界 JSON 几十 KB~几 MB，正常亚秒返回；无外网时快速失败走降级，
# 不让一条分组请求吊死在网络上。
_DOWNLOAD_TIMEOUT = 10

# 直筒子市：不设区县、下辖镇/街道，区级面 DataV 拿不到 → 走 Overpass 镇级。
# 城市码以 CITY_ADCODE 里东莞/中山的注释为准（grep「直筒子」核对过）。
_TOWN_LEVEL_CODES = frozenset({"dg", "zs"})
# Overpass 查询需要市名（area 按 name 标签匹配，adcode 不是 OSM 的 relation id，
# 拿 adcode+3600000000 当 area id 是错的）；只有两个直筒子市走镇级，故只列这两个。
_TOWN_CITY_ZH = {"441900": "东莞市", "442000": "中山市"}
# Overpass 多端点依次尝试：主站配额紧张/维护时镜像可用，任一成功即可；
# 顺序即优先级（主站在前）。
_OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# 60s：镇界 relation 几十条 way、响应 1~几 MB，Overpass 高峰期排队 30s+ 常见，
# 给足超时避免正常请求被误杀（失败有下一端点与缓存兜底，不会反复吊 60s——
# 成功后落盘缓存，命中路径零网络）。
_OVERPASS_TIMEOUT = 60
# admin_level=8 在中国区划的 OSM 惯例 = 区县/乡镇级；直筒子市不设区县，
# 其镇/街道恰好落在这一级（实测中山 23 个镇街全部 admin_level=8）。
_OVERPASS_QUERY = """[out:json][timeout:{timeout}];
area["name"="{city}"]->.searchArea;
relation(area.searchArea)["boundary"="administrative"]["admin_level"="8"];
out geom;"""
# Overpass 拒绝无 User-Agent 的请求（实测 HTTP 406），必须带 UA。
_OVERPASS_UA = "SpaceFin-Agent/1.0 (district grouping; contact: repo owner)"


def _cache_path(adcode):
    return os.path.join(_CACHE_DIR, f"{adcode}.json")


def _download_boundaries(adcode):
    """adcode → GeoJSON 文本。缓存命中（文件存在且非空）不重新下载；
    下载/读盘任何一步失败返回 None（调用方降级），绝不抛异常穿透到请求层。"""
    path = _cache_path(adcode)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass  # 缓存读失败（权限/磁盘问题）→ 当作未命中重新下载
    try:
        with urllib.request.urlopen(
            _DATAV_URL.format(adcode=adcode), timeout=_DOWNLOAD_TIMEOUT
        ) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not raw:
        return None
    os.makedirs(_CACHE_DIR, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    except OSError:
        pass  # 落盘失败不影响本次结果，下次请求重试下载
    return raw


def _pt_key(pt):
    # 端点相等按 1e-7（约 1cm）取整比较：同一 OSM 节点在相邻 way 里的坐标
    # 理应逐字节一致，取整只为兜住序列化/精度舍入；再粗会误拼不同节点。
    return (round(pt[0], 7), round(pt[1], 7))


def _stitch_rings(ways):
    """way 列表（[[ (lng, lat), ... ], ...]）→ 闭合环列表 [[(lng, lat), ...], ...]。

    Overpass 把一条行政区边界拆成 relation 的多条 member way，相邻 way 共享
    端点节点。拼接算法：任取一条作链，反复把「端点相接」的 way 接到链首或
    链尾（方向相反先翻转），直到无路可接——链首尾重合即闭合环，接不满的
    （数据缺一段）整链丢弃：even-odd 口径下拿开曲线冒充闭合环会把点面判定
    全部判错，宁缺毋滥。

    复杂度 O(n²)（每轮线性扫剩余 ways）：一条镇界的 way 数是十位数量级，
    平方仍在毫秒档，不值得为它上哈希邻接表。

    洞不在这里处理：外环/洞环拼出来都是独立闭合环，交由调用方压平进
    polygons 后走 even-odd 统一计数（与区级 _rings_of 同一出口，见模块
    docstring 的零依赖射线法一段）。
    """
    # 先去掉连续重复点（Overpass geometry 偶有首尾/相邻重复），len<2 的残段直接淘汰
    chains = []
    for w in ways:
        pts = []
        for p in w:
            k = _pt_key(p)
            if not pts or _pt_key(pts[-1]) != k:
                pts.append((float(p[0]), float(p[1])))
        if len(pts) >= 2:
            chains.append(pts)
    rings = []
    while chains:
        chain = chains.pop(0)
        extended = True
        while extended and _pt_key(chain[0]) != _pt_key(chain[-1]):
            extended = False
            head, tail = _pt_key(chain[0]), _pt_key(chain[-1])
            for i, w in enumerate(chains):
                wk_head, wk_tail = _pt_key(w[0]), _pt_key(w[-1])
                if tail == wk_head:
                    chain = chain + w[1:]  # 尾接头，同向直连
                elif tail == wk_tail:
                    chain = chain + w[-2::-1]  # 尾接尾：翻转后去掉与链尾重复的点
                elif head == wk_tail:
                    chain = w[:-1] + chain  # 头接尾：w 原序，去重其末点
                elif head == wk_head:
                    chain = w[-1:0:-1] + chain  # 头接头：翻转后去掉与链头重复的点
                else:
                    continue
                chains.pop(i)
                extended = True
                break
        # 闭合且至少 4 点（3 个独立顶点 + 收口点）才算环
        if len(chain) >= 4 and _pt_key(chain[0]) == _pt_key(chain[-1]):
            rings.append(chain)
    return rings


def _detect_level(names):
    """组名列表 → 分组层级（"town" | "district"）。

    判据：以「镇 / 街道」结尾的名称占比 > 0.5 → town。层级跟着数据形状走
    而不跟着城市走——调用方（p6_policy）无需知道哪个城市是直筒子市；区级
    来源（DataV）名称一律以 区/县/旗 结尾，几乎不会误触发该判据，真出现了
    （如某市整个辖区都叫「××街道」）如实回 town 反而更诚实。
    """
    if not names:
        return "district"
    hits = sum(1 for n in names if str(n).strip().endswith(("镇", "街道")))
    return "town" if hits * 2 > len(names) else "district"


def _towns_cache_path(adcode):
    # 镇级缓存与区级同目录：data_lake/spatial/boundaries/{adcode}_towns.json
    return os.path.join(_CACHE_DIR, f"{adcode}_towns.json")


def _download_towns(adcode):
    """直筒子市镇界 Overpass 原始响应文本。缓存命中（文件存在且非空）不重下；
    多端点依次尝试，全部失败返回 None（调用方降级），绝不抛异常穿透。

    响应校验：必须能解析成带非空 elements 的 JSON——Overpass 过载时可能返回
    HTML 错误页 / 空结果，这类「成功 HTTP + 坏内容」同样视为该端点失败，
    换下一端点，不把坏响应落成缓存。
    """
    path = _towns_cache_path(adcode)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass  # 缓存读失败（权限/磁盘问题）→ 当作未命中重新下载
    query = _OVERPASS_QUERY.format(timeout=_OVERPASS_TIMEOUT, city=_TOWN_CITY_ZH[adcode])
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for url in _OVERPASS_URLS:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": _OVERPASS_UA,
                },
            )
            with urllib.request.urlopen(req, timeout=_OVERPASS_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError, ValueError):
            continue  # 本端点失败（网络/配额/HTTP 错误）→ 试下一个
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(parsed, dict) or not (parsed.get("elements") or []):
            continue
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError:
            pass  # 落盘失败不影响本次结果，下次请求重试下载
        return raw
    return None


def _parse_overpass(data):
    """Overpass 响应 dict → 镇级列表 [{name, polygons, bbox}]（结构与区级一致）。

    只认带 name 的 relation；member way 的 geometry 是 [{lat, lon}, ...] 对象
    序（Overpass 惯例 lat 在前），逐点转成 (lng, lat) 与 DataV 环同构；个别
    member 无 geometry（数据缺失）跳过该 way，拼接时由其余 way 兜。返回 None
    表示一个可用镇面都没有（调用方按「数据不可用」降级）。
    """
    towns = []
    for el in data.get("elements") or []:
        if el.get("type") != "relation":
            continue
        name = (el.get("tags") or {}).get("name")
        if not name:
            continue
        ways = []
        for member in el.get("members") or []:
            geom = member.get("geometry") or []
            pts = []
            for g in geom:
                if not g:
                    continue
                lat, lon = g.get("lat"), g.get("lon")
                if lat is None or lon is None:
                    continue
                pts.append((float(lon), float(lat)))  # Overpass [lat, lon] → (lng, lat)
            if pts:
                ways.append(pts)
        rings = _stitch_rings(ways)
        if not rings:
            continue  # 全部 ways 拼不成闭合环 → 该镇无可用面，宁缺毋滥
        lons = [pt[0] for ring in rings for pt in ring]
        lats = [pt[1] for ring in rings for pt in ring]
        if not lons or not lats:
            continue
        towns.append(
            {
                "name": name,
                "polygons": rings,
                "bbox": (min(lons), min(lats), max(lons), max(lats)),
            }
        )
    return towns or None


def _load_towns(adcode):
    """镇级获取整链：下载（多端点 + 缓存）→ 解析。任一步失败返回 None。"""
    raw = _download_towns(adcode)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return _parse_overpass(data)


def _rings_of(geom):
    """GeoJSON geometry → [ring, ...]（ring = [(lng, lat), ...] 的列表）。

    Polygon：coordinates = [外环, 洞1, 洞2, ...]，原样展开；
    MultiPolygon：coordinates = [[环...], [环...]]，压平成同一层 ring 列表——
    even-odd 射线法只看穿越总次数的奇偶，多面/洞统一计数即正确，无需分层。
    其余 geometry 类型（点/线，DataV 区划不会出现）返回空表。
    """
    if not geom:
        return []
    gtype = geom.get("type")
    if gtype == "Polygon":
        return geom.get("coordinates") or []
    if gtype == "MultiPolygon":
        rings = []
        for poly in geom.get("coordinates") or []:
            rings.extend(poly or [])
        return rings
    return []


def load_city_districts(city_code):
    """城市码 → (区/镇列表 [{name, polygons, bbox}], 分组层级)；数据不可用返回 None。

    返回的二元组第二位是分组层级："district"（DataV 区县级）或 "town"
    （直筒子市的 Overpass 镇/街道级），由 _detect_level 按名称形状判定——
    调用方据此选分组维度名（区 vs 镇街），不再自行硬编码城市特例。

    数据源按城市分派（对外接口签名不变，选源在内部）：
    - 直筒子市（dg/zs）→ 先走 Overpass 镇级；镇界取不到落到 DataV 兜底
      （441900/442000_full.json 无区县面，features 为空 → None），两条路
      都失败即「数据不可用」，与区级失败同一降级出口；
    - 其余城市 → DataV 区县级（现状不动）。

    polygons 是压平后的 ring 列表（见 _rings_of）：每个 ring 是 [(lng, lat), ...]，
    外环与洞同级，由 even-odd 规则统一处理。bbox = (min_lng, min_lat, max_lng, max_lat)，
    供 assign_district 预筛。城市码不在表内 / 下载失败 / 解析异常 → None。
    """
    adcode = CITY_ADCODE.get(city_code)
    if not adcode:
        return None
    if city_code in _TOWN_LEVEL_CODES:
        towns = _load_towns(adcode)
        if towns:
            return towns, _detect_level([t["name"] for t in towns])
        # 镇级取不到 → 落到 DataV 兜底（直筒子市该文件无区县面，走完即 None）
    raw = _download_boundaries(adcode)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    districts = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        name = props.get("name")
        rings = _rings_of(feat.get("geometry"))
        if not name or not rings:
            continue
        lons = [pt[0] for ring in rings for pt in ring]
        lats = [pt[1] for ring in rings for pt in ring]
        if not lons or not lats:
            continue
        districts.append(
            {
                "name": name,
                "polygons": rings,
                "bbox": (min(lons), min(lats), max(lons), max(lats)),
            }
        )
    if not districts:
        return None
    return districts, _detect_level([d["name"] for d in districts])


def _point_in_rings(lng, lat, rings):
    """even-odd 射线法：向 +lng 方向引射线，数所有 ring 边的穿越次数。

    对外环/洞/多面统一奇偶计数：点在洞内时外环与洞环各穿越一次（合计偶数）
    → 判外，正确处理洞；行政区多面互不重叠，跨面计数同样成立。射线取
    「向右」且边界条件两端严格不均（(yi > lat) != (yj > lat)）避免顶点重复计数。
    """
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat):
                # 边与水平线 lat 的交点横坐标；lng 在交点左侧即穿越一次
                x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
                if lng < x_cross:
                    inside = not inside
            j = i
    return inside


def assign_district(lng, lat, districts):
    """坐标 → 区名（districts 内命中者的 properties.name 原文）；任一区都不含 → None。

    bbox 预筛在前（O(区数) 次比较），射线法只对候选跑；坐标缺失（None/NaN
    由调用方保证不进来）不做容错——本函数只吃有限浮点。
    """
    for d in districts:
        min_lng, min_lat, max_lng, max_lat = d["bbox"]
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            continue
        if _point_in_rings(lng, lat, d["polygons"]):
            return d["name"]
    return None


def loan_district_grouping(loans, city_code):
    """贷款列表 → ({loan_id: 区/镇名 | None}, 分组层级)，只处理 city==city_code 的贷款。

    - 城市边界数据不可用（下载失败 / 城市码不在表内）→ 返回 (None, None)
      （不是空表）：「数据不可用」与「逐笔都匹配不到」是两种业务事实，
      调用方据此走不同路径（前者降级分组维度，后者如实出「未识别」组）；
    - city 不符的贷款不进映射（其他城市的贷款与本城分组无关）；
    - 坐标缺失或落在任何区外 → None，调用方归「未识别」组、不丢弃；
    - 层级即 load_city_districts 的 level（"district" | "town"），供调用方
      选分组维度名（区 vs 镇街）；数据不可用时层级同样无意义，随 None 置 None。
    """
    loaded = load_city_districts(city_code)
    if loaded is None:
        return None, None
    districts, level = loaded
    out = {}
    for loan in loans:
        if loan.get("city") != city_code:
            continue
        lat, lng = loan.get("lat"), loan.get("lng")
        if lat is None or lng is None:
            out[loan["loan_id"]] = None
            continue
        out[loan["loan_id"]] = assign_district(float(lng), float(lat), districts)
    return out, level


def loan_district_map(loans, city_code):
    """贷款列表 → {loan_id: 区名 | None}（不带层级的旧入口，见 loan_district_grouping）。

    城市边界数据不可用 → 返回 None（不是空表），语义同 loan_district_grouping。
    """
    return loan_district_grouping(loans, city_code)[0]
