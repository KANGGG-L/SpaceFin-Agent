"""AVM 预测接口：供风险引擎（tools/risk/）调用，返回抵押物估值总价（元）。

设计要点：
    - 惰性导入：模块顶层不 import sklearn / joblib / numpy 之外的第三方库，
      在没装 sklearn 的环境里 `import predict` 不会炸；只有真正调用
      load_model / estimate_total_price 时才导入。
    - 单位：estimate_total_price 返回【元】。DWD 里 total_price_wan 是万元，
      模型目标 log(unit_price_yuan)，总价 = unit_price × area_sqm，均按元口径。
    - 回退语义（与训练一致，见 train.py 文档）：
        小区命中 → 小区中位价编码；
        小区未知 → 城市中位价编码；
        城市未知 → 全局中位价编码；
        有经纬度 → 叠加训练坐标最近邻邻域价；
        信息不足（面积缺失/非法）或模型缺失 → 返回 None，由调用方回退。

用法：
    from tools.avm import predict  # 或把 tools/avm 加入 sys.path
    model = predict.load_model()
    val = predict.estimate_total_price(
        model, city_code="gz", community="天河城", area_sqm=89.5,
        building_age=8, bedrooms=3,
    )
"""

from __future__ import annotations

import os

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MODEL_PATH = os.path.join(REPO_ROOT, "output", "avm", "model.joblib")

# 与 train.py 保持一致的特征顺序与编码
DIRECTIONS = ["南", "南北", "东南", "东", "西南", "北", "东北", "西", "西北"]
DIR_IDX = {d: i for i, d in enumerate(DIRECTIONS)}
SPATIAL_K = (3, 8, 20, 50)

FEATURE_NAMES = (
    [
        "area",
        "log_area",
        "bed",
        "hall",
        "bath",
        "rooms",
        "area_per_bed",
        "age",
        "floor_level",
        "floor_total",
        "floor_ratio",
        "direction",
        "parking",
        "city_code",
        "lat",
        "lng",
    ]
    + [
        "comm_mean",
        "comm_median",
        "comm_n",
        "comm_minus_city",
        "city_mean",
        "city_median",
        "city_n",
    ]
    + [f"nn_med_k{k}" for k in SPATIAL_K]
    + ["nn_dist"]
)


def _build_row(
    artifact: dict,
    *,
    city_code: str,
    community: str | None,
    area_sqm: float,
    building_age: float | None,
    bedrooms: int | float | None,
    latitude: float | None,
    longitude: float | None,
    hall: float,
    bath: float,
    floor_level: float,
    floor_total: float,
    direction: str | None,
    parking: float,
) -> np.ndarray:
    """按训练特征顺序构造单行特征向量。未知城市码用 -1（HistGBR 容忍未见类别）。

    hall/bath/floor_level/floor_total/parking 为调用方传入的真实值（已由
    estimate_total_price 校验非空）。rooms / area_per_bed / floor_ratio 为派生特征，
    与 train.py base_features 同公式，保证推理与训练特征分布完全一致：
        rooms        = bed + hall + bath
        area_per_bed = area / max(bed, 1)
        floor_ratio  = floor_level / floor_total
    direction 为朝向文本，按 DIR_IDX 编码为数值（未知/None → -1，与训练一致）。
    """
    enc = artifact["encoders"]
    cities = artifact["cities"]
    cidx = {c: i for i, c in enumerate(cities)}
    lat = float(latitude) if latitude is not None else np.nan
    lng = float(longitude) if longitude is not None else np.nan
    if np.isnan(lat) or np.isnan(lng):  # 只有单边坐标视为无坐标
        lat = lng = np.nan
    bed = float(bedrooms) if bedrooms is not None else 0.0  # 对齐 train: r["bedrooms"] or 0
    age = float(building_age) if building_age is not None else np.nan
    area = float(area_sqm)

    # 与 train.py base_features 完全一致的特征构造：派生特征用同一公式，
    # 不让推理侧与训练侧出现分布错位（见 estimate_total_price 的缺失校验）。
    hall_f = float(hall)
    bath_f = float(bath)
    fl_f = float(floor_level)
    ft_f = float(floor_total)
    park_f = float(parking)
    rooms = bed + hall_f + bath_f
    area_per_bed = area / max(bed, 1.0)
    # 对齐 train: floor_total==0 时退化为 nan（避免 ZeroDivisionError），
    # 训练侧 lv/tot 在 tot==0 亦无定义，推理与训练同口径。
    floor_ratio = fl_f / ft_f if ft_f != 0 else np.nan
    # 朝向文本 → 数值编码（与 train.py DIR_IDX 同映射；未知/None → -1）。
    direction_f = float(DIR_IDX.get(direction, -1)) if direction is not None else -1.0

    base = [
        area,
        np.log(area),
        bed,
        hall_f,
        bath_f,
        rooms,
        area_per_bed,
        age,
        fl_f,
        ft_f,
        floor_ratio,
        direction_f,
        park_f,
        float(cidx.get(city_code, -1)),
        lat,
        lng,
    ]

    g = enc["global"]
    cv = enc["city"].get(city_code, (g, g, 0))
    cmv = enc["comm"].get((city_code, community)) if community else None
    if cmv:
        # 与 train.py 一致的小区目标编码经验贝叶斯收缩（老模型无 smooth_k 键 → 不收缩）。
        # smooth_mode="eb" 时 k 按城市取训练期估的 σ²组内/τ²组间（encoders["eb_k"]），
        # 与训练时同公式——否则推理侧用固定 k 会与训练分布错位（README 记录的接入坑）。
        if artifact.get("smooth_mode") == "eb":
            k = float((enc.get("eb_k") or {}).get(city_code, artifact.get("smooth_k", 0.0)))
        else:
            k = float(artifact.get("smooth_k", 0.0))
        n = cmv[2]
        if k > 0 and n > 0:
            sm_mean = (n * cmv[0] + k * cv[0]) / (n + k)
            sm_med = (n * cmv[1] + k * cv[1]) / (n + k)
        else:
            sm_mean, sm_med = cmv[0], cmv[1]
        cat = [sm_mean, sm_med, n, sm_mean - cv[0]]
    else:
        cat = [np.nan, np.nan, 0.0, np.nan]
    cat += [cv[0], cv[1], cv[2]]

    nnf = [np.nan] * (len(SPATIAL_K) + 1)
    nn_data = enc.get("nn")
    if nn_data is not None and not np.isnan(lat):
        nn, vals, k_list = nn_data
        dist, ind = nn.kneighbors(np.array([[lat, lng]]))
        for j, k in enumerate(k_list):
            if k <= ind.shape[1]:
                nnf[j] = float(np.median(vals[ind[0, :k]]))
        nnf[-1] = float(dist[0, 0])
    return np.array(base + cat + nnf, dtype=float).reshape(1, -1)


def load_model(path: str | None = None) -> object | None:
    """加载模型产物；文件不存在或依赖缺失时返回 None，不抛异常。

    返回的 dict 含字段：model/encoders/cities/feature_names/spatial_k/smooth_k/
    version/trained_at/n_train。version 为模型版本号（如 2026-08-05-r1），
    由 train.py 生成，风险引擎无需感知——estimate_total_price 只依赖前 5 个键。
    """
    path = path or DEFAULT_MODEL_PATH
    if not os.path.exists(path):
        return None
    try:
        import joblib

        return joblib.load(path)
    except Exception:
        return None


def estimate_total_price(
    model: object | None,
    *,
    city_code: str,
    community: str | None = None,
    area_sqm: float,
    building_age: float | None = None,
    bedrooms: int | float | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    hall: float | None = None,
    bath: float | None = None,
    floor_level: float | None = None,
    floor_total: float | None = None,
    direction: str | None = None,
    parking: float | None = None,
    **kw: object,
) -> float | None:
    """估计抵押物总价（元）。

    Args:
        model: load_model() 的产物；None 时直接返回 None。
        city_code: 广东 21 城码（gz/sz/fs/...），必须提供。
        community: 小区/板块名，未知可省（回退城市/全局中位）。
        area_sqm: 建筑面积（㎡），必须 > 0。
        building_age / bedrooms: 可选属性，缺失时按未知处理。
        latitude / longitude: 可选坐标，同时提供时启用邻域空间特征。
        hall / bath / floor_level / floor_total / parking: 真实户型/楼层特征，
            **必须提供**（不得置 NaN）。这些特征训练侧（train.py base_features）是
            真实值，历史上推理侧被硬置 NaN 导致与训练分布错位。缺失则显式抛错，
            由调用方回退 DWD / true_market_price，而非产出一个特征错位的估值。
        direction: 朝向文本（如 "南"），按 DIR_IDX 编码；未知/None → -1（与训练一致），
            属于「已知即编码、未知即 -1」的合法取值，不要求必填。
        **kw: 兼容未来新增特征键；多余键被忽略，不破坏对外 API 契约。

    Returns:
        估值总价（元）；信息不足或模型缺失返回 None。

    Raises:
        ValueError: hall/bath/floor_level/floor_total/parking 任一缺失（无来源时调用方
            应捕获后回退，而不是用 NaN 占位）。
    """
    if model is None:
        return None
    if not city_code or not area_sqm or float(area_sqm) <= 0:
        return None
    # 9 维特征（hall/bath/floor*/direction/parking + 派生 rooms/area_per_bed/floor_ratio）
    # 必须由调用方提供真实值：缺失即显式报错，不允许静默置 NaN（会与训练分布错位，
    # 模型把"特征缺失"误读成一种恒定信号，推高系统性偏差）。调用方确无来源时应在此抛错
    # 后回退下一级估值，而不是容忍一个错位的预测。
    missing = [
        name
        for name, val in (
            ("hall", hall),
            ("bath", bath),
            ("floor_level", floor_level),
            ("floor_total", floor_total),
            ("parking", parking),
        )
        if val is None
    ]
    if missing:
        raise ValueError(
            "AVM 推理缺真实特征，无法构造与训练一致的特征向量: "
            + ", ".join(missing)
            + "。这些特征必须由调用方传入真实值；无来源时请勿用 NaN 占位，"
            "应让本次估值回退 DWD / true_market_price。"
        )
    from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: F401

    row = _build_row(
        model,
        city_code=city_code,
        community=community or None,
        area_sqm=float(area_sqm),
        building_age=building_age,
        bedrooms=bedrooms,
        latitude=latitude,
        longitude=longitude,
        hall=hall,
        bath=bath,
        floor_level=floor_level,
        floor_total=floor_total,
        direction=direction,
        parking=parking,
    )
    pred_log = float(model["model"].predict(row)[0])
    return round(float(np.exp(pred_log) * float(area_sqm)), 2)


if __name__ == "__main__":
    # 冒烟自测：模型缺失 / 正常调用两条路径
    print("no model:", estimate_total_price(None, city_code="gz", area_sqm=80))
    m = load_model()
    print("model loaded:", m is not None)
    if m:
        print("model version:", m.get("version"))
        print(
            "gz 天河城 89.5㎡:",
            estimate_total_price(
                m,
                city_code="gz",
                community="天河城",
                area_sqm=89.5,
                building_age=8,
                bedrooms=3,
                hall=1,
                bath=1,
                floor_level=1,
                floor_total=18,
                direction="南",
                parking=1,
            ),
        )
        print(
            "未知城市回退:",
            estimate_total_price(
                m,
                city_code="zz",
                area_sqm=80,
                hall=1,
                bath=1,
                floor_level=1,
                floor_total=18,
                direction="南",
                parking=1,
            ),
        )
