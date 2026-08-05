"""AVM 训练数据增强清洗（S2）。

两条增强规则解决 README「已知局限」中的结构性瓶颈：

1. **外市数据清洗**：sale DWD 混入了大量北京/燕郊/南昌/杭州湾等外市房源
   （爬虫来源错配），集中在 zs/yf/zh/dg 等城市，拉高城市中位价、贡献了
   全表最高误差段（dg MAPE 103%、zh 50%、yf 48%）。用四层可复现规则判定：
   a) 坐标围栏：经纬度落在「广东 21 城超围栏」之外的行（如 lat>25.6 在
      东北/北京、lng<109.4 在广西/云南）→ 剔除；
   b) URL 子域城市：url 子域能解析出城市码且 ≠ district 的行（外市页面抓取
      错标成广东城市，价格与本地区间重叠，前三层抓不到的残留污染）→ 剔除；
   c) 文字标记：title/community 命中北京/南昌等地名或「集中供暖/胡同/家属院」
      等北方市场专属词汇的行（带例外表防误杀广东同名地名）→ 剔除；
   d) 城市价格上/下限：单价超过城市真实天花板（如云浮 >1.5 万、中山 >3 万）
      或低于昂贵城市地板价（如深圳 <1.2 万、珠海 <8 千）→ 剔除（例：云浮
      不存在 2 万/㎡ 的房源，深圳不存在 3 千/㎡ 的房源）。

2. **title 回填小区名**：31% 的行既无小区名也无坐标，只能靠城市中位+属性
   估计（该段 MAPE 38%）。这些行的 title 里通常带楼盘名（如「宏天广场
   中心区采光好 242 平方 5 房 2 厅」），用「楼盘尾缀 + 描述词过滤」的
   保守解析器把楼盘名回填到 community 字段。解析保守：解析不出宁可留空，
   绝不把「刚需小三居/精装」这类描述词当小区名。

所有规则写入代码即文档，清洗统计随训练一并输出到 avm_report.json。
"""

from __future__ import annotations

import re
from collections import Counter

# ---------------------------------------------------------------------------
# 广东 21 城超围栏：覆盖全部城市行政区加裕量，用于剔除明显外省坐标。
# 取值依据：广东最南（湛江徐闻 ~20.1N）、最北（韶关乐昌 ~25.5N）、
# 最西（湛江廉江 ~109.6E）、最东（潮州饶平 ~117.2E）各留 0.4° 裕量。
# ---------------------------------------------------------------------------
GD_BOX = (19.9, 25.6, 109.4, 117.6)  # (lat_min, lat_max, lng_min, lng_max)


def in_gd_box(lat: float | None, lng: float | None) -> bool:
    if lat is None or lng is None:
        return False
    lat_min, lat_max, lng_min, lng_max = GD_BOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


# ---------------------------------------------------------------------------
# URL 子域城市校验：第四层外市判定（S6 新增，零成本离线信号）。
#
# 爬虫 url 的子域记录了房源实际抓取自哪个城市市场（shenzhen.anjuke.com /
# jinan.anjuke.com / gz.58.com 等）。前三层规则（坐标/标记/价格）剔掉 2057 行
# 后仍有 766 行「district 标签城市 != URL 实际城市」的残留外市房源（zs->bj 332、
# zh->bj 169、zh->gz 94、zs->jn 67 等，北京/济南/保定房源被错标成广东城市），
# 其价格与本地区间重叠，坐标/文字标记/价格上下限都抓不到——正是 README「已知
# 局限」第 4 条残留污染，也是 zs/zh/yf/dg 段误差的主要来源。
#
# 规则：url 子域能解析出城市码且 != district 即判为外市剔除。子域解析不出
# （www/m 等通用域名）一律视为无信号、不误杀。只收录确定映射，宁可漏杀。
# ---------------------------------------------------------------------------
_GD_CITY_CODES = {
    "gz",
    "sz",
    "fs",
    "dg",
    "zs",
    "zh",
    "yf",
    "hui",
    "jm",
    "zq",
    "sg",
    "qy",
    "jy",
    "st",
    "sw",
    "cz",
    "mz",
    "hy",
    "mm",
    "zj",
    "yj",
}
# anjuke/58 子域拼音 -> 城市码（广东 21 城 + 常见外市；广东城直接命中 _GD_CITY_CODES）
URL_SUBDOMAIN_TO_CODE = {
    "shenzhen": "sz",
    "guangzhou": "gz",
    "foshan": "fs",
    "dongguan": "dg",
    "zhongshan": "zs",
    "zhuhai": "zh",
    "yunfu": "yf",
    "huizhou": "hui",
    "jiangmen": "jm",
    "zhaoqing": "zq",
    "shaoguan": "sg",
    "qingyuan": "qy",
    "jieyang": "jy",
    "shantou": "st",
    "shanwei": "sw",
    "chaozhou": "cz",
    "meizhou": "mz",
    "heyuan": "hy",
    "maoming": "mm",
    "zhanjiang": "zj",
    "yangjiang": "yj",
    "beijing": "bj",
    "shanghai": "sh",
    "nanjing": "nj",
    "hangzhou": "hz",
    "nanchang": "nc",
    "jinan": "jn",
    "changchun": "cc",
    "shenyang": "sy",
    "tianjin": "tj",
    "chengdu": "cd",
    "chongqing": "cq",
    "wuhan": "wh",
    "changsha": "cs",
    "xian": "xa",
    "zhengzhou": "zz",
    "hefei": "hf",
    "suzhou": "su",
    "wuxi": "wx",
    "ningbo": "nb",
    "xiamen": "xm",
    "fuzhou": "fz",
    "kunming": "km",
    "guiyang": "gy",
    "nanning": "nn",
    "haikou": "hk",
    "lanzhou": "lz",
    "yinchuan": "yc",
    "harbin": "heb",
    "dalian": "dl",
    "qingdao": "qd",
    "yantai": "yt",
    "weifang": "wf",
    "tangshan": "ts",
    "langfang": "lf",
    "baoding": "bd",
    "shijiazhuang": "sjz",
    "taiyuan": "ty",
    "hohhot": "hhht",
    "luoyang": "ly",
    "wenzhou": "wz",
    "jinhua": "jh",
    "taizhou": "tz",
    "nantong": "nt",
    "yangzhou": "yz",
    "shaoxing": "sx",
    "jiaxing": "jx",
    "putian": "pt",
    "quanzhou": "qz",
    "zhangzhou": "zz2",
    "yancheng": "yc",  # 江苏盐城：实测被错标成 zh 的外市主流之一
    "deyang": "dy",  # 四川德阳：实测被错标成 dg 的外市主流之一
    "dingzhou": "dz",  # 河北定州：实测被错标成 zh 的外市之一
    # 58.com 子域直接用城市短码（bj.58.com / sh.58.com）：仅收录确定城市短码，
    # 避免把 esf/zu/m 等频道子域误判成城市。短码之间无歧义：任一短码 != 广东
    # 城市码即判外市，方向恒正确。
    "bj": "bj",
    "sh": "sh",
    "tj": "tj",
    "cq": "cq",
    "cd": "cd",
    "hz": "hz",
    "nj": "nj",
    "wh": "wh",
    "cs": "cs",
    "xa": "xa",
    "zz": "zz",
    "hf": "hf",
    "su": "su",
    "wx": "wx",
    "nb": "nb",
    "xm": "xm",
    "fz": "fz",
    "jn": "jn",
    "nc": "nc",
    "cc": "cc",
    "sy": "sy",
    "dl": "dl",
    "qd": "qd",
    "yt": "yt",
    "wf": "wf",
    "ts": "ts",
    "lf": "lf",
    "bd": "bd",
    "sjz": "sjz",
    "ty": "ty",
    "hhht": "hhht",
    "ly": "ly",
    "wz": "wz",
    "jh": "jh",
    "tz": "tz",
    "nt": "nt",
    "yz": "yz",
    "sx": "sx",
    "jx": "jx",
    "pt": "pt",
    "qz": "qz",
    "km": "km",
    "gy": "gy",
    "nn": "nn",
    "hk": "hk",
    "lz": "lz",
    "yc": "yc",
    "heb": "heb",
    "gl": "gl",
}
_URL_CITY_RE = re.compile(r"https?://([a-z0-9]+)\.(?:anjuke|58)\.com")


def url_city_code(url: str | None) -> str | None:
    """从 url 子域解析房源实际城市码；解析不出返回 None（无信号，不判外市）。"""
    if not url:
        return None
    m = _URL_CITY_RE.search(url)
    if not m:
        return None
    sub = m.group(1)
    # 58.com 子域直接是城市码（gz.58.com）；anjuke 子域是拼音
    if sub in _GD_CITY_CODES:
        return sub
    return URL_SUBDOMAIN_TO_CODE.get(sub)


# ---------------------------------------------------------------------------
# 外市文字标记。全部经全量数据人工核对：列表内每个标记匹配的行均为外市
# 房源（或由例外表排除的广东本地假阳性），避免误杀「北京路(广州)」「北京城
# (中山/珠海)」「中梁壹号院(江门)」「地上地下(别墅描述)」等广东本地行。
# ---------------------------------------------------------------------------
FOREIGN_MARKERS = [
    # 北京各区/县/板块/地标（北京独有，广东无同名）
    "海淀",
    "丰台",
    "石景山",
    "门头沟",
    "房山",
    "通州",
    "顺义",
    "昌平",
    "大兴",
    "密云",
    "延庆",
    "怀柔",
    "平谷",
    "马连洼",
    "回龙观",
    "天通苑",
    "亦庄",
    "月坛",
    "复兴门",
    "潘家园",
    "方庄",
    "中关村",
    "玉泉营",
    "良乡",
    "五棵松",
    "公主坟",
    "苹果园",
    "西二旗",
    "亚运村",
    "劲松",
    "东坝",
    "常营",
    "垡头",
    "宋庄",
    "北七家",
    "望都家园",
    "上地东里",
    "通州北关",
    "望京",
    "上地",
    "清河",
    "丰台科技园",
    "朝青",
    "德胜门",
    "天坛",
    "新街口",
    "西直门",
    "崇文门",
    "西单",
    "灵境胡同",
    "美术馆",
    "魏公村",
    "万寿路",
    "车公庄",
    "六铺炕",
    "外交部街",
    "铁科院",
    "四季青",
    "慈寿寺",
    "裕中西里",
    "朝阳区",
    "海淀区",
    "丰台区",
    "石景山区",
    "朝阳公园",
    "姚家园",
    "五道口",
    "甜水园",
    "朝外",
    "双井",
    "广安门",
    "牛街",
    "东直门",
    "广渠门",
    "右安门",
    "永定门",
    # 北京环线（北京特有的 x 环表述）
    "北四环",
    "北五环",
    "西四环",
    "东四环",
    "东三环",
    "西二环",
    "东二环",
    "北二环",
    # 环京卫星城 / 河北
    "燕郊",
    "白沟",
    "保定",
    "潮白",
    "香河",
    "固安",
    "廊坊",
    # 北方市场专属词汇（广东无集中供暖/胡同/家属院等表述）
    "集中供暖",
    "胡同",
    "家属楼",
    "家属院",
    "公房",
    "央产",
    "部委",
    "单位分房",
    # 南昌 / 杭州湾（浙江宁波）
    "红谷滩",
    "青山湖",
    "南昌西站",
    "南昌二中",
    "南昌十中",
    "南昌十九中",
    "南昌润府",
    "杭州湾",
    # 北京特定楼盘/通勤表述
    "北京南站",
    "北京大兴",
    "北京风景",
    "北京新天地",
    "北京怡园",
    "北京通勤",
    "安家北京",
    "进京",
    "北京东",
    # 北京常见小区/板块（在 zs/yf/zh/dg 污染段高频出现，广东无同名）
    "怡海花园",
    "武夷花园",
    "华业东方玫瑰",
    "翡翠公园",
    "刘家窑",
    "丽泽",
    "石佛营",
    "蒲黄榆",
    "西红门",
    "百子湾",
    "沿海赛洛城",
    "翠成馨园",
    "京棉新城",
    "四惠",
    "邑上苑",
    "领秀慧谷",
    "京贸国际",
    "立水桥",
    "中滩村",
    "百环家园",
    "西马金润",
    "紫萝园",
    "建材城",
    "中建国际港",
    "运河商务区",
    "九棵树",
    "大红门",
    "角门",
    "南三环",
    "南四环",
    "西三环",
    "南五环",
    "东五环",
    "西五环",
    "梅源",
    "枣园",
    "正阳小区",
    # ---- 第二轮新增：华东/华北残留污染（全量核对过，广东无同名） ----
    # 江苏盐城/淮安/徐州等（zh 段混入大量"盐城/射阳城/阜宁城/东台城"房源）
    "盐城",
    "射阳",
    "阜宁",
    "东台",
    # 北方供暖/交易术语（广东无地暖/双气/老证说法）
    "地暖",
    "双气",
    "老证",
    # 外省街道/学校/地名（山东济南/四川德阳/河北保定石家庄/河南漯河）
    "堤口路",
    "岷山路",
    "保定",
    "正定",
    "漯河",
    # ---- 第三轮新增：dg 段残留的北京/四川/内蒙污染（全量核对） ----
    # 北京环线/地标/家属院表述
    "五环",
    "新宫",
    "家属大院",
    "有燃气",
    "北京高档",
    # 四川德阳/绵竹/广汉 与 内蒙呼和浩特 区县
    "绵竹",
    "雒城",
    "旌东",
    "玉泉区",
    # 上海/北方表述：三轨交汇（北京）、轨交·/轨交房（上海杭州系楼盘名）
    "三轨交汇",
    "轨交·",
    "轨交房",
    "轨交缦",
]

# 例外表：标记命中但属于广东本地语境时不判为外市。
# 例：揭阳「保利壹号公馆望京灶大桥」、茂名「北京东二路」、广州「北京路」。
FOREIGN_EXCEPTIONS = {
    "望京": ["望京灶"],
    "上地": ["地上"],  # 「地上地下4层」别墅描述
    "清河": ["小清河"],  # 佛山/清远「小清河公园」
    "房山": ["山海湾", "三房山", "房山姆"],  # 阳江「山海湾」、清远「三房山景」
    "北京东": ["北京东二路"],  # 茂名「北京东二路」路名
}


def detect_foreign(
    district: str,
    community: str | None,
    title: str | None,
    lat: float | None,
    lng: float | None,
    unit_price: float | None,
    price_caps: dict[str, float] | None = None,
    price_floors: dict[str, float] | None = None,
    url: str | None = None,
) -> str | None:
    """判定一行是否属于外市混入数据。

    优先级：坐标围栏 → URL 子域城市 → 文字标记 → 城市价格上/下限。
    返回判定原因，None 表示本地。
    """
    if lat is not None and lng is not None:
        if not in_gd_box(float(lat), float(lng)):
            return "coord_outside_gd"
    # URL 子域是「房源真实市场」的直接证据：district 标签与 URL 城市不一致
    # 说明该行是从外市页面抓下来又被错标成广东城市（价格与本地区间重叠，
    # 前三层规则都抓不到的残留污染）。只认能解析出确定城市的子域。
    uc = url_city_code(url)
    if uc is not None and uc != district:
        return f"url_mismatch:{uc}"
    text = f"{(title or '')}|{(community or '')}"
    for marker in FOREIGN_MARKERS:
        if marker in text:
            if any(e in text for e in FOREIGN_EXCEPTIONS.get(marker, [])):
                continue
            return f"text:{marker}"
    if unit_price is not None:
        up = float(unit_price)
        caps = price_caps if price_caps is not None else PRICE_CAPS
        cap = caps.get(district)
        if cap and up > cap:
            return f"price_over_{cap:g}"
        floors = price_floors if price_floors is not None else PRICE_FLOORS
        floor = floors.get(district)
        if floor and up < floor:
            return f"price_below_{floor:g}"
    return None


# ---------------------------------------------------------------------------
# 城市真实价格天花板（元/㎡）：超过即视为外市/异常高价。取值依据为各城
# 真实市场顶部水平，且不低于本表中有坐标城市的坐标最大单价，避免误杀本地
# 豪宅；对无坐标城市（yf/zs/dg/zh 等）收紧以剔除北京高价污染。
# ---------------------------------------------------------------------------
PRICE_CAPS: dict[str, float] = {
    "gz": 200000.0,  # 广州：二沙岛/珠江新城顶级
    "sz": 300000.0,  # 深圳：深圳湾/华侨城顶级
    "fs": 60000.0,  # 佛山：千灯湖/顺德核心
    "dg": 50000.0,  # 东莞：松山湖/南城核心（北京污染行普遍 >5 万）
    "zh": 60000.0,  # 珠海：横琴/吉大顶级
    "zs": 30000.0,  # 中山：东区/火炬高端，>3 万基本是北京污染
    "hui": 50000.0,  # 惠州：惠城区高端
    "jm": 50000.0,  # 江门：新会/鹤山别墅
    "zq": 30000.0,  # 肇庆
    "sg": 25000.0,  # 韶关
    "qy": 30000.0,  # 清远
    "jy": 25000.0,  # 揭阳
    "st": 40000.0,  # 汕头：龙湖核心
    "sw": 25000.0,  # 汕尾
    "cz": 25000.0,  # 潮州
    "mz": 25000.0,  # 梅州
    "hy": 25000.0,  # 河源
    "mm": 25000.0,  # 茂名
    "zj": 25000.0,  # 湛江
    "yj": 25000.0,  # 阳江
    "yf": 15000.0,  # 云浮：市区高端仅 ~1.2 万，>1.5 万基本是北京污染
}

# 城市真实价格下限（元/㎡）：低于即视为异常低价。昂贵的城市（广州/深圳/
# 珠海/中山等）混入了河北/江苏/杭州湾/惠州等地低价房源（如 zh 里出现
# 「射阳城/阜城/燕都鑫城」，zs 里出现「绿地听海苑」杭州湾项目），单价
# 2-7 千/㎡ 在深圳/珠海不存在；按城市下限剔除，防止污染城市中位价。
PRICE_FLOORS: dict[str, float] = {
    "gz": 4500.0,
    "sz": 12000.0,
    "fs": 2500.0,
    "dg": 2500.0,
    "zh": 8000.0,
    "zs": 6000.0,
}


# ---------------------------------------------------------------------------
# title 回填小区名：保守解析器
# ---------------------------------------------------------------------------
# 楼盘名尾缀：优先双字尾缀；单字尾缀（城/湾/苑/府/居/庭/园/阁/轩/堡/都）
# 要求名字长度 >= 3，避免把「公园/内海湾/可做小阁」这类描述词当小区名。
COMM_SUFFIXES = [
    "花园",
    "花苑",
    "家园",
    "华府",
    "公馆",
    "广场",
    "山庄",
    "豪庭",
    "名邸",
    "雅居",
    "御景",
    "帝景",
    "华庭",
    "龙庭",
    "天下",
    "都会",
    "首府",
    "壹号",
    "公寓",
    "世家",
    "云居",
    "御园",
    "雅苑",
    "嘉园",
    "馨园",
    "名庭",
    "丽都",
    "丽湾",
    "尚城",
    "湾畔",
    "华苑",
    "尚府",
    "府邸",
    "绿洲",
    "名都",
    "新城",
    "金岸",
    "天禧",
    "领峰",
    "天峰",
    "高峰",
    # 第二轮新增尾缀（现代楼盘命名：华堂里/新都汇/世纪海岸/时代水岸/
    # 金源盛世/东岸国际/裕和天地/云星洲/华润悦里 等）
    "海岸",
    "水岸",
    "盛世",
    "国际",
    "天地",
    "悦里",
    "城",
    "湾",
    "苑",
    "府",
    "居",
    "庭",
    "园",
    "阁",
    "轩",
    "堡",
    "都",
    "里",
    "汇",
    "洲",
]

# 描述词：候选名字里出现即判定不是楼盘名（宁可留空）。
DESC_WORDS = [
    "刚需",
    "学区",
    "中学",
    "小学",
    "大学",
    "学院",
    "医院",
    "地铁",
    "轻轨",
    "高铁",
    "车站",
    "公园",
    "片区",
    "商圈",
    "板块",
    "地段",
    "市场",
    "超市",
    "商场",
    "商业",
    "配套",
    "产权",
    "红本",
    "证件",
    "首付",
    "总价",
    "单价",
    "均价",
    "钥匙",
    "看房",
    "随时",
    "诚心",
    "业主",
    "房东",
    "房主",
    "装修",
    "精装",
    "毛坯",
    "电梯",
    "户型",
    "朝向",
    "采光",
    "楼层",
    "高层",
    "中层",
    "低层",
    "南北",
    "东南",
    "西南",
    "西北",
    "东北",
    "笋盘",
    "出售",
    "急售",
    "拎包",
    "满五",
    "满二",
    "未住",
    "入住",
    "全新",
    "保真",
    "可谈",
    "视野",
    "安静",
    "舒适",
    "小区",
    "楼盘",
    "现房",
    "期房",
    "楼龄",
    "周边",
    "距离",
    "栋",
    "单元",
    "号院",
    "环路",
    "门口",
    "对面",
    "旁边",
    "沿线",
    "旁",
    "边",
    "靓",
    "绝",
    "好",
    "优",
    "真",
    "价",
    "可做",
    "内海",
    "主家",
    "免",
    "送",
    "带",
    "含",
    "有",
    "无",
    "在",
    "近",
    "临",
    "靠",
    "距",
    # 户型/房间描述（防止把「小三居/大两居/三室」当小区名）
    "一居",
    "两居",
    "二居",
    "三居",
    "四居",
    "五居",
    "六居",
    "一室",
    "两室",
    "二室",
    "三室",
    "四室",
    "五室",
    "一房",
    "两房",
    "二房",
    "三房",
    "四房",
    "五房",
    "一厅",
    "两厅",
    "二厅",
    "三厅",
    "四厅",
    "小三",
    "大两",
    "大三",
    "大四",
    "小两",
    "小四",
    "南北通透",
    "南向",
    "北向",
    "朝南",
    "朝北",
    "东向",
    "西向",
    # 看花园/望江 等景观描述
    "望花园",
    "看花园",
    "向花园",
    "对花园",
    "望江",
    "看江",
    "江景",
    "园景",
    "前后花园",
    "带花园",
    "花园洋房",
    "低密",
    "宜居",
    "别墅",
    "独栋",
    "联排",
    "复式",
    "平层",
    "洋房",
]

# 前缀词：候选名字以此开头即判定不是楼盘名（营销/方位词开头）。
# 注意：不要包含「中/大/小/高/低/景」等——「中海/中骏/景峰/金景」等
# 品牌/小区名以这些字开头，误杀会显著损失回填覆盖率。
STOP_PREFIX = [
    "笋",
    "急",
    "新",
    "全",
    "精",
    "毛",
    "无",
    "带",
    "送",
    "临",
    "近",
    "旁",
    "边",
    "对",
    "内",
    "可",
    "好",
    "优",
    "真",
    "出",
    "售",
    "买",
    "卖",
    "价",
    "业",
    "主",
    "楼",
    "层",
    "房",
    "家",
    "区",
    "片",
    "圈",
    "板",
    "块",
    "路",
    "街",
    "道",
    "千",
    "元",
    "平",
    "方",
    "套",
    "靓",
    "绝",
    "超",
    "特",
    "稀",
    "抢",
    "捡",
    "豪",
    # 第二轮新增：房产交易/营销动作词（放盘/新收/让利/转手/换房 后紧跟楼盘名）
    "放",
    "收",
    "让",
    "转",
    "换",
    "看",
]

# 纯净品牌名/描述词：解析出的候选名精确命中即放弃（宁可留空）。
# 例：「碧桂园」「雅居乐」是开发商品牌不是小区；「马德里」是楼盘昵称的一部分；
# 「入户花园/大花园」是户型赠送描述。注意只做精确匹配，不误伤「五邑碧桂园」
# 「雅居乐花园」「赤湖纯水岸」这类带限定词的完整楼盘名。
BAD_COMMUNITY_NAMES = {
    "碧桂园",
    "雅居乐",
    "保利",
    "万科",
    "恒大",
    "中海",
    "招商",
    "龙光",
    "时代",
    "星河",
    "金地",
    "华侨城",
    "深业",
    "华润",
    "绿城",
    "融创",
    "世茂",
    "敏捷",
    "旭辉",
    "奥园",
    "佳兆业",
    "中洲",
    "马德里",
    "东海岸",
    "香榭丽",
    "入户花园",
    "大花园",
    "私家花园",
    "前后花园",
    "空中花园",
    "望花园",
}

# 营销前缀双字词：出现在候选名最前时整体剥掉（例「新收紫麟城二期」→「紫麟城」）。
# 与 STOP_PREFIX 单字词不同，这里只剥明确的营销双字，避免误伤「新城和樾」这类
# 「新」是楼盘名一部分的名称。
MARKETING_PREFIX2 = {
    "新收",
    "新上",
    "新装",
    "急售",
    "笋盘",
    "低价",
    "特价",
    "清盘",
    "直降",
    "降价",
    "诚心",
    "房源",
    "放盘",
    "业主",
    "房东",
    "出售",
    "急出",
    "随时",
}

_HANZI = re.compile(r"[\u4e00-\u9fff]")
_SINGLE_CHAR_SUFFIXES = {
    "城",
    "湾",
    "苑",
    "府",
    "居",
    "庭",
    "园",
    "阁",
    "轩",
    "堡",
    "都",
    "里",
    "汇",
    "洲",
}
# 二期/三期/X期 模式：楼盘名后直接跟期数，名本身不带楼盘尾缀（海博熙泰三期）
_QI_RE = re.compile(r"([\u4e00-\u9fff]{2,12})[一二三四五六七八九十\d]期")


def parse_community_from_title(title: str | None) -> str | None:
    """从 title 保守提取楼盘/小区名；提取不出返回 None。

    做法：逐个楼盘尾缀在 title 中定位，往前回退连续汉字得到候选名，再过滤：
    - 长度 3-12；单字尾缀要求 >= 3 字；
    - 候选名不含任何描述词（保证不是「刚需小三居/精装」之类）；
    - 回退遇营销/方位词（STOP_PREFIX）即停，不把「万买泰安花园」的「万买」
      收进名里（上一版会误收营销词导致名以「买」开头被整名拒绝）；
    - 候选名不精确命中品牌/描述词表（BAD_COMMUNITY_NAMES）；
    - 额外识别「XX三期/X期」模式（楼盘名后直接带期数，如海博熙泰三期）。
    """
    if not title:
        return None
    best: str | None = None
    for suffix in COMM_SUFFIXES:
        min_len = 3 if suffix in _SINGLE_CHAR_SUFFIXES else 3
        idx = title.find(suffix)
        while idx != -1:
            start = idx
            while start > 0 and _HANZI.match(title[start - 1]):
                if title[start - 1] in STOP_PREFIX:
                    break
                start -= 1
            name = title[start : idx + len(suffix)]
            if min_len <= len(name) <= 12:
                if any(w in name for w in DESC_WORDS):
                    pass
                elif name in BAD_COMMUNITY_NAMES:
                    pass
                elif len(name) > len(best or ""):
                    best = name
            idx = title.find(suffix, idx + 1)
    # 期 模式：候选名剥掉开头营销双字（新收紫麟城 → 紫麟城）。
    # 只剥明确的营销双字，不剥单字「新」（新城和樾 的「新」是楼盘名一部分）。
    for m in _QI_RE.finditer(title):
        name = m.group(1)
        while name[:2] in MARKETING_PREFIX2:
            name = name[2:]
        if len(name) >= 3:
            if any(w in name for w in DESC_WORDS):
                pass
            elif name in BAD_COMMUNITY_NAMES:
                pass
            elif len(name) > len(best or ""):
                best = name
    return best


# ---------------------------------------------------------------------------
# 清洗编排 + 统计
# ---------------------------------------------------------------------------
def clean_rows_with_stats(rows: list[dict], *, parse_all: bool = False) -> tuple[list[dict], dict]:
    """对外市混入行做剔除 + title 回填小区名。

    输入行需含：community/title/latitude/longitude/unit_price_yuan/district。
    parse_all=True 时用 title 解析出的楼盘名统一替换所有行的 community
    （解析失败保留原值），把爬虫给的整句噪音标签归一为楼盘名，标签更一致。
    返回 (保留行, 清洗统计字典)。
    """
    n_comm_missing_before = sum(1 for r in rows if not (r.get("community") or "").strip())
    parsed = 0
    if parse_all:
        for r in rows:
            name = parse_community_from_title(r.get("title"))
            if name:
                r["community"] = name
                parsed += 1

    reason = Counter()
    kept: list[dict] = []
    for r in rows:
        up = r.get("unit_price_yuan")
        try:
            up_f = float(up) if up is not None else None
        except (TypeError, ValueError):
            up_f = None
        why = detect_foreign(
            r.get("district"),
            r.get("community"),
            r.get("title"),
            r.get("latitude"),
            r.get("longitude"),
            up_f,
            PRICE_CAPS,
            url=r.get("url"),
        )
        if why:
            reason[why] += 1
        else:
            kept.append(r)

    # 回填：仅对 community 为空的行做 title 解析，解析失败保留空
    # 注意：回填会就地修改 kept 中行的 community（调用方传入的行随之更新）
    backfilled = 0
    for r in kept:
        if not (r.get("community") or "").strip():
            name = parse_community_from_title(r.get("title"))
            if name:
                r["community"] = name
                backfilled += 1

    # 按城市细分：为什么被剔除（坐标围栏 / URL 城市 / 文字标记 / 价格上/下限）
    by_city_coord: Counter = Counter()
    by_city_marker: Counter = Counter()
    by_city_price: Counter = Counter()
    by_city_url: Counter = Counter()
    for r in rows:
        up = r.get("unit_price_yuan")
        up_f = float(up) if up is not None else None
        why = detect_foreign(
            r.get("district"),
            r.get("community"),
            r.get("title"),
            r.get("latitude"),
            r.get("longitude"),
            up_f,
            url=r.get("url"),
        )
        if why is None:
            continue
        city = r.get("district")
        if why.startswith("text:"):
            by_city_marker[city] += 1
        elif why.startswith("price_over") or why.startswith("price_below"):
            by_city_price[city] += 1
        elif why.startswith("url_mismatch"):
            by_city_url[city] += 1
        else:
            by_city_coord[city] += 1

    return kept, {
        "n_raw": len(rows),
        "n_kept": len(kept),
        "n_dropped": len(rows) - len(kept),
        "n_dropped_coord": sum(by_city_coord.values()),
        "n_dropped_marker": sum(by_city_marker.values()),
        "n_dropped_price": sum(by_city_price.values()),
        "n_dropped_url": sum(by_city_url.values()),
        "dropped_by_coord": dict(by_city_coord),
        "dropped_by_marker": dict(by_city_marker),
        "dropped_by_price_cap": dict(by_city_price),
        "dropped_by_url": dict(by_city_url),
        "n_comm_missing_before": n_comm_missing_before,
        "n_backfilled_community": parsed + backfilled,
        "n_comm_missing_after": sum(1 for r in kept if not (r.get("community") or "").strip()),
    }
