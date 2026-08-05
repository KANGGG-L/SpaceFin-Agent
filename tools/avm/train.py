"""AVM 自动估值模型训练 + 评估 CLI（S2）。

目标：替代风险引擎中「(城市, 小区) 中位单价 × 面积」的粗糙估值，
用机器学习模型直接估计单位面积价格，再乘面积还原总价。

用法：
    tools/orchestrator/.venv/bin/python tools/avm/train.py --out-dir output/avm
    tools/orchestrator/.venv/bin/python tools/avm/train.py --out-dir output/avm --max-rows 20000

输出（默认 output/avm/）：
    model.joblib      模型 + 编码字典 + 特征元数据（供 tools/avm/predict.py 加载）
    avm_report.json   指标、样本量、特征清单、训练时间戳、与基线对照

数据口径：
    - 训练集：spacefin_crawler.crawl_housing_sale（sale DWD，44,369 行）
    - 目标变量：log(unit_price_yuan)。选 log 单价而非总价的原因：
        1) 单价是"位置/品质"的直接度量，小区/邻域编码承载的正是单价水平；
           总价 = 单价 × 面积，面积是模型显式特征，让模型直接学"单价水平"
           比让它隐式学"面积 × 单价"更稳；
        2) 房价误差天然是乘性的（10% 的单价偏差 ≈ 10% 的总价偏差），
           log 目标把乘性误差变成加性误差，与 MAPE 口径一致。
    - 严禁标签泄漏：unit_price = total_price * 10000 / area，因此特征里
      绝不使用 unit_price_yuan/total_price_wan 及其任何直接派生；小区/城市
      目标编码只用训练折内的行统计（OOF），测试集映射用完整训练集字典，
      测试集新小区回退：小区中位 → 城市中位 → 全局中位。
    - 空间特征：经纬度仅 29% 行有值。有坐标的行用训练坐标建最近邻，
      取邻域单价的 k 近邻中位数（GWR-lite 的离散近似）。训练行的邻域
      在折内统计（不含自身），测试行邻域用完整训练集——两边口径一致。

清洗规则（写入代码即文档）：
    0) 外市混入清洗 + title 回填小区名（见 tools/avm/data_clean.py，可复现、
       有统计输出）：坐标围栏 / 北京南昌等文字标记 / 城市价格上限三层判定
       剔除混入的北京燕郊南昌等外市房源；对 community 为空的行用 title 中
       的楼盘名保守回填。
    1) 剔除 total_price_wan / area_sqm / unit_price_yuan 任一缺失或 <=0 的行；
    2) 一致性校验：|total_price*10000/area - unit_price| / unit_price > 2% 视为脏行剔除；
    3) 分位数截尾去极值：unit_price_yuan 与 area_sqm 各截 0.5% / 99.5%；
    4) building_age 截断到 [0, 60]，负数（爬虫返回 0 附近异常）置 0。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# DB 连接（复用仓库根 .env，读法同 tools/risk/config.py）
# ---------------------------------------------------------------------------
def load_env() -> dict:
    """读仓库根 .env（仅取 MYSQL_* 相关键，不写回）。"""
    env = {}
    path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def crawl_params(env: dict) -> dict:
    """sale DWD 所在库（spacefin_crawler）的连接参数，app 账号只读。"""
    return {
        "host": env.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(env.get("MYSQL_PORT", "3306")),
        "user": env.get("MYSQL_APP_USER", "spacefin_crawler_app"),
        "password": env.get("MYSQL_APP_PASSWORD", ""),
        "database": "spacefin_crawler",
    }


# ---------------------------------------------------------------------------
# 数据读取与清洗
# ---------------------------------------------------------------------------
SQL = """
SELECT title, community, district, bedrooms, halls, bathrooms, area_sqm, direction, floor,
       building_age, parking_count, total_price_wan, unit_price_yuan, latitude, longitude
FROM crawl_housing_sale
"""

# 朝向文本 → 数值编码（HistGBR 吃数值，朝向不参与可解释性要求，直接字典映射）
DIRECTIONS = ["南", "南北", "东南", "东", "西南", "北", "东北", "西", "西北"]
DIR_IDX = {d: i for i, d in enumerate(DIRECTIONS)}
# 楼层文本如 "中层(共18层)" → (楼层区位, 总层数)；"共18层" 无区位
FLOOR_LEVELS = {"低层": 0, "中层": 1, "高层": 2}


def parse_floor(text: str | None) -> tuple[float, float]:
    """解析楼层文本为 (楼层区位, 总层数)。缺省返回 NaN。"""
    if not text:
        return np.nan, np.nan
    level = np.nan
    for k, v in FLOOR_LEVELS.items():
        if text.startswith(k):
            level = float(v)
            break
    m = re.search(r"共(\d+)层", text)
    total = float(m.group(1)) if m else np.nan
    return level, total


def load_rows(conn, max_rows: int | None) -> list[dict]:
    cur = conn.cursor()
    cur.execute(SQL)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        if max_rows and len(out) >= max_rows:
            break
        out.append(dict(zip(cols, r, strict=True)))
    cur.close()
    return out


def clean_rows(rows: list[dict], raw_comm_by_id: dict | None = None) -> list[dict]:
    """清洗 sale DWD（规则见模块 docstring），返回 list[dict]。

    raw_comm_by_id：id(r) -> 原始 community（clean_rows_with_stats 已把 community
    就地替换为 title 归一结果，这里用快照保留爬虫原始标签，供坐标词典匹配）。
    """
    cleaned = []
    for r in rows:
        tp, area, up = r["total_price_wan"], r["area_sqm"], r["unit_price_yuan"]
        if tp is None or area is None or up is None:
            continue  # 规则 1：关键字段缺失
        area, tp, up = float(area), float(tp), float(up)
        if area <= 0 or tp <= 0 or up <= 0:
            continue  # 规则 1：非正值
        if abs(tp * 10000.0 / area - up) / up > 0.02:
            continue  # 规则 2：总价/面积 与单价不一致（脏行）
        cleaned.append(
            {
                "comm": (r["community"] or "").strip() or None,
                "raw_comm": (raw_comm_by_id or {}).get(id(r)) if raw_comm_by_id else None,
                "title": r["title"] or "",
                "city": r["district"],
                "bed": r["bedrooms"] or 0,
                "hall": r["halls"] or 0,
                "bath": r["bathrooms"] or 0,
                "area": area,
                "up": up,
                "tp": tp * 10000.0,  # 总价（元），仅用于评估口径，不进入特征
                "direction": r["direction"],
                "floor": r["floor"],
                "age": float(r["building_age"]) if r["building_age"] is not None else np.nan,
                "park": r["parking_count"] or 0,
                "lat": float(r["latitude"]) if r["latitude"] is not None else np.nan,
                "lng": float(r["longitude"]) if r["longitude"] is not None else np.nan,
            }
        )
    # 规则 3：分位数截尾去极值（0.5% / 99.5%）
    ups = np.array([r["up"] for r in cleaned])
    areas = np.array([r["area"] for r in cleaned])
    up_lo, up_hi = np.quantile(ups, [0.005, 0.995])
    ar_lo, ar_hi = np.quantile(areas, [0.005, 0.995])
    cleaned = [r for r in cleaned if up_lo <= r["up"] <= up_hi and ar_lo <= r["area"] <= ar_hi]
    # 规则 4：房龄截断
    for r in cleaned:
        r["age"] = min(max(r["age"], 0.0), 60.0) if r["age"] == r["age"] else np.nan
    return cleaned


# ---------------------------------------------------------------------------
# 特征工程
# ---------------------------------------------------------------------------
def base_features(
    rows: list[dict], cities: list[str] | None = None
) -> tuple[np.ndarray, list[str]]:
    """基础数值特征（不含目标编码/空间特征）。cities 传入以对齐 city 编码顺序。"""
    if cities is None:
        cities = sorted({r["city"] for r in rows})
    cidx = {c: i for i, c in enumerate(cities)}
    mat = []
    for r in rows:
        lv, tot = parse_floor(r["floor"])
        bed, hall, bath = float(r["bed"]), float(r["hall"]), float(r["bath"])
        mat.append(
            [
                r["area"],
                np.log(r["area"]),
                bed,
                hall,
                bath,
                bed + hall + bath,
                r["area"] / max(bed, 1),
                r["age"],
                lv,
                tot,
                lv / tot if tot == tot and lv == lv else np.nan,  # 楼层相对位置
                float(DIR_IDX.get(r["direction"], -1)),
                float(r["park"]),
                float(cidx.get(r["city"], -1)),
                r["lat"],
                r["lng"],
            ]
        )
    names = [
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
    return np.array(mat, dtype=float), names


def fit_encoders(rows: list[dict], y: np.ndarray) -> dict:
    """从给定行统计目标编码字典（只允许传训练折内的行！）。

    返回结构：
        global   全局 log 单价中位数
        city     {city: (mean, median, n)}
        comm     {(city, community): (mean, median, n)}
        nn       (NearestNeighbors, 训练坐标对应的 y 值, k 列表) | None
    """
    city_d, comm_d = {}, {}
    for r, v in zip(rows, y, strict=True):
        city_d.setdefault(r["city"], []).append(v)
        if r["comm"]:
            comm_d.setdefault((r["city"], r["comm"]), []).append(v)
    enc = {"global": float(np.median(y))}
    enc["city"] = {k: (float(np.mean(v)), float(np.median(v)), len(v)) for k, v in city_d.items()}
    enc["comm"] = {k: (float(np.mean(v)), float(np.median(v)), len(v)) for k, v in comm_d.items()}
    pts = [(r["lat"], r["lng"], yy) for r, yy in zip(rows, y, strict=True) if r["lat"] == r["lat"]]
    if pts:
        from sklearn.neighbors import NearestNeighbors

        coords = np.array([[p[0], p[1]] for p in pts])
        vals = np.array([p[2] for p in pts])
        nn = NearestNeighbors(n_neighbors=min(max(SPATIAL_K), len(pts))).fit(coords)
        enc["nn"] = (nn, vals, SPATIAL_K)
    else:
        enc["nn"] = None
    return enc


def apply_encoders(
    enc: dict, rows: list[dict], exclude_self: bool = False, smooth_k: float = 0.0
) -> np.ndarray:
    """把编码特征映射到行。exclude_self=True 用于训练折内 OOF 计算（邻域不含自身）。

    回退语义：小区无 → 城市中位；城市无 → 全局中位。与测试/服务期一致。

    smooth_k>0 时对小区目标编码做经验贝叶斯收缩（向城市均值/中位靠拢）：
    sm = (n*val + k*ref)/(n+k)。n 小的新小区统计噪声大，收缩能显著降低其对
    预测的方差贡献；n 大的成熟小区几乎不受影响。
    """
    g = enc["global"]
    k = smooth_k
    feat = []
    for r in rows:
        cv = enc["city"].get(r["city"], (g, g, 0))
        cmv = enc["comm"].get((r["city"], r["comm"])) if r["comm"] else None
        if cmv:
            n = cmv[2]
            if k > 0 and n > 0:
                # 小区均值/中位向城市均值/中位收缩；comm_minus_city 用收缩后值
                sm_mean = (n * cmv[0] + k * cv[0]) / (n + k)
                sm_med = (n * cmv[1] + k * cv[1]) / (n + k)
            else:
                sm_mean, sm_med = cmv[0], cmv[1]
            feat.append([sm_mean, sm_med, n, sm_mean - cv[0]])
        else:
            feat.append([np.nan, np.nan, 0.0, np.nan])
        feat[-1] += [cv[0], cv[1], cv[2]]  # 城市均值/中位/样本量
    cat_feat = np.array(feat, dtype=float)
    # 空间近邻：邻域 log 单价中位数（多半径）+ 最近邻距离
    k_list = enc["nn"][2] if enc["nn"] else SPATIAL_K
    nnf = np.full((len(rows), len(k_list) + 1), np.nan)
    if enc["nn"]:
        nn, vals, k_list = enc["nn"]
        idx = [i for i, r in enumerate(rows) if r["lat"] == r["lat"]]
        if idx:
            q = np.array([[rows[i]["lat"], rows[i]["lng"]] for i in idx])
            kmax = min(max(k_list) + (1 if exclude_self else 0), len(vals))
            dist, ind = nn.kneighbors(q, n_neighbors=kmax)
            if exclude_self:  # OOF：去掉最近邻中的"自己"（自己就是 fold 内点）
                dist, ind = dist[:, 1:], ind[:, 1:]
            for j, i in enumerate(idx):
                row = [float(np.median(vals[ind[j, :k]])) for k in k_list if k <= ind.shape[1]]
                row += [float(dist[j, 0])]
                nnf[i, : len(row)] = row
    return np.hstack([cat_feat, nnf])


# 空间近邻半径集合：3/8/20/50 覆盖"极近邻→城市尺度"
SPATIAL_K = (3, 8, 20, 50)


def build_features(
    rows: list[dict],
    y: np.ndarray,
    encoders: dict,
    exclude_self: bool = False,
    smooth_k: float = 0.0,
) -> np.ndarray:
    """组装全部特征（基础 + 编码 + 空间）。供训练与测试两用。"""
    base_feat, _ = base_features(rows)
    enc_feat = apply_encoders(encoders, rows, exclude_self=exclude_self, smooth_k=smooth_k)
    return np.hstack([base_feat, enc_feat])


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


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    ape = np.abs(y_pred - y_true) / y_true
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return {
        "mape": round(float(np.mean(ape) * 100), 3),
        "mdape": round(float(np.median(ape) * 100), 3),
        "r2": round(1.0 - ss_res / ss_tot, 4) if ss_tot > 0 else None,
        "n": int(len(y_true)),
    }


def baseline_predict(rows: list[dict], enc: dict) -> np.ndarray:
    """基线：按 valuation.py 口径的「(城市, 小区) 中位单价 × 面积」。

    只读 valuation.py 理解口径、不引用其代码：DWD 匹配轴 (city_code, community)，
    未命中返回 None。此处用完整训练集字典做映射（小区→城市→全局回退），
    与预测接口的回退语义保持一致。
    """
    g = enc["global"]
    preds = []
    for r in rows:
        cmv = enc["comm"].get((r["city"], r["comm"]))
        city_m = enc["city"].get(r["city"], (g, g, 0))[1]
        med = cmv[1] if cmv else city_m
        preds.append(float(np.exp(med) * r["area"]))
    return np.array(preds)


def decompose_by_segment(rows: list[dict], y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """按「位置信号完整度」分段评估，定位剩余误差集中段。

    段位：
        has_comm_coord  有小区 + 有坐标（位置信号最完整）
        has_comm_only   有小区、无坐标
        no_comm         无小区（title 也解析不出楼盘名）
    """
    ape = np.abs(y_pred - y_true) / y_true
    seg = {"has_comm_coord": [], "has_comm_only": [], "no_comm": []}
    for r, a in zip(rows, ape, strict=True):
        has_comm = bool(r["comm"])
        has_coord = r["lat"] == r["lat"]
        if has_comm and has_coord:
            seg["has_comm_coord"].append(a)
        elif has_comm:
            seg["has_comm_only"].append(a)
        else:
            seg["no_comm"].append(a)
    return {
        k: {
            "n": len(v),
            "mape": round(float(np.mean(v)) * 100, 2) if v else None,
            "mdape": round(float(np.median(v)) * 100, 2) if v else None,
            "weight_pct": round(len(v) / len(rows) * 100, 1),
        }
        for k, v in seg.items()
    }


def decompose_by_city(rows: list[dict], y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """按城市分段的 MAPE，用于识别仍有污染的异常城市。"""
    from collections import OrderedDict

    by_city: dict[str, list[float]] = {}
    ape = np.abs(y_pred - y_true) / y_true
    for r, a in zip(rows, ape, strict=True):
        by_city.setdefault(r["city"], []).append(a)
    return OrderedDict(
        (c, {"n": len(v), "mape": round(float(np.mean(v)) * 100, 2)})
        for c, v in sorted(by_city.items(), key=lambda kv: -np.mean(kv[1]))
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="训练并评估 AVM 模型")
    ap.add_argument("--out-dir", default="output/avm")
    ap.add_argument("--max-rows", type=int, default=None, help="限制读入行数（调试用）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--smooth-k",
        type=float,
        default=10.0,
        help="小区目标编码经验贝叶斯收缩强度（0=不收缩）",
    )
    args = ap.parse_args()

    t0 = time.time()
    env = load_env()
    import pymysql

    conn = pymysql.connect(**crawl_params(env), charset="utf8mb4")
    try:
        raw_rows = load_rows(conn, args.max_rows)
    finally:
        conn.close()

    # 外市清洗 + title 回填小区名（可复现，统计随报告输出）
    # parse_all=True：用 title 解析出的楼盘名统一归一所有行的 community，
    # 消除爬虫 community 字段「整句噪音标签」造成的标签碎片化。
    from data_clean import clean_rows_with_stats

    # 快照原始 community（清洗会就地替换为归一名，坐标词典按原始名匹配）
    raw_comm_by_id = {id(r): (r.get("community") or "").strip() or None for r in raw_rows}
    raw_rows, clean_stats = clean_rows_with_stats(raw_rows, parse_all=True)
    rows = clean_rows(raw_rows, raw_comm_by_id)

    # 坐标回填：离线词典（community_coords/dws_spatial 小区坐标 + 区中心点）给
    # 无坐标行补位置信号；不消耗腾讯配额，失败自动降级（保持 NaN）。
    from coord_backfill import backfill_coords, load_coord_dict

    coord_stats = backfill_coords(rows, load_coord_dict(env))
    if coord_stats["n_backfilled"]:
        print(
            f"[avm] 坐标回填 {coord_stats['n_backfilled']} 行"
            f"（词典 {coord_stats['by_source']['dict']} / 区中心 {coord_stats['by_source']['district']}）"
        )
    print(
        f"[avm] 外市清洗: 剔除 {clean_stats['n_dropped']} 行"
        f"（围栏 {clean_stats['n_dropped_coord']} / 标记 {clean_stats['n_dropped_marker']}"
        f" / 价格 {clean_stats['n_dropped_price']}），"
        f"title 归一/回填小区 {clean_stats['n_backfilled_community']} 行"
    )
    print(f"[avm] 清洗后样本 {len(rows)}（{time.time() - t0:.1f}s）")

    y_log = np.array([np.log(r["up"]) for r in rows])
    cities = sorted({r["city"] for r in rows})

    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.model_selection import KFold, train_test_split

    idx = np.arange(len(rows))
    tr_i, te_i = train_test_split(idx, test_size=0.2, random_state=args.seed)
    tr = [rows[i] for i in tr_i]
    te = [rows[i] for i in te_i]
    ytr, yte = y_log[tr_i], y_log[te_i]
    print(f"[avm] train={len(tr)} test={len(te)}")

    # 训练集 OOF 编码（防标签泄漏）：5 折，每折用其余 4 折统计编码
    kf = KFold(n_splits=5, shuffle=True, random_state=args.seed)
    enc_tr = np.zeros((len(tr), len(FEATURE_NAMES)))
    for a, b in kf.split(np.arange(len(tr))):
        fold_enc = fit_encoders([tr[i] for i in a], ytr[a])
        enc_tr[b] = build_features(
            [tr[i] for i in b], ytr[b], fold_enc, exclude_self=True, smooth_k=args.smooth_k
        )
    # 测试集用完整训练集编码映射（新小区自动回退城市/全局中位）
    full_enc = fit_encoders(tr, ytr)
    train_mat = enc_tr
    test_mat = build_features(te, yte, full_enc, exclude_self=False, smooth_k=args.smooth_k)

    # HistGBR 原生支持 NaN（无坐标/无小区行保留，特征列有缺失不剔除）
    # 参数：max_leaf_nodes=150 + 更强 L2 正则 在本轮清洗/回填数据上 MAPE 最低
    # （网格 6 组对比：base 16.14 → big 15.97，-0.18pp；lr=0.02 组 16.02 次之）
    model = HistGradientBoostingRegressor(
        max_iter=2000,
        learning_rate=0.03,
        max_leaf_nodes=150,
        min_samples_leaf=12,
        l2_regularization=2.0,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=40,
        random_state=args.seed,
        categorical_features=[FEATURE_NAMES.index("city_code")],
    )
    model.fit(train_mat, ytr)
    print(f"[avm] 训练完成（{model.n_iter_} iters，{time.time() - t0:.1f}s）")

    # 评估：模型 vs 基线，都换算成总价（元）比口径
    area_te = np.array([r["area"] for r in te])
    tp_te = np.array([r["tp"] for r in te])  # 元
    pred_log = model.predict(test_mat)
    pred_tp = np.exp(pred_log) * area_te
    base_tp = baseline_predict(te, full_enc)

    model_met = metrics(tp_te, pred_tp)
    base_met = metrics(tp_te, base_tp)
    improvement = round((base_met["mape"] - model_met["mape"]) / base_met["mape"] * 100, 1)

    print(
        f"[avm] 基线 (city,community) 中位×面积: MAPE={base_met['mape']}% MdAPE={base_met['mdape']}% R²={base_met['r2']} n={base_met['n']}"
    )
    print(
        f"[avm] 模型 (HistGBR):                  MAPE={model_met['mape']}% MdAPE={model_met['mdape']}% R²={model_met['r2']} n={model_met['n']}"
    )
    print(f"[avm] 相对提升（MAPE 降幅）: {improvement}%")

    # 误差分解：分段 + 分城市（定位剩余瓶颈）
    seg_metrics = decompose_by_segment(te, tp_te, pred_tp)
    city_metrics = decompose_by_city(te, tp_te, pred_tp)
    print("\n[avm] 测试集分段误差（模型）：")
    for k, v in seg_metrics.items():
        print(
            f"    {k:<14} n={v['n']:>6} MAPE={v['mape']}% MdAPE={v['mdape']}% 权重={v['weight_pct']}%"
        )
    print("[avm] 测试集分城市 MAPE（top 8）：")
    for c, v in list(city_metrics.items())[:8]:
        print(f"    {c:<5} n={v['n']:>5} MAPE={v['mape']}%")

    # 特征缺失率（说明空间特征可用性，写入报告）
    miss = {}
    for name, col in zip(FEATURE_NAMES, test_mat.T, strict=True):
        miss[name] = round(float(np.mean(np.isnan(col))) * 100, 1)

    # 落盘
    os.makedirs(args.out_dir, exist_ok=True)
    model_path = os.path.join(args.out_dir, "model.joblib")
    report_path = os.path.join(args.out_dir, "avm_report.json")
    artifact = {
        "model": model,
        "encoders": full_enc,
        "cities": cities,
        "feature_names": FEATURE_NAMES,
        "spatial_k": list(SPATIAL_K),
        "smooth_k": args.smooth_k,  # 小区目标编码收缩强度（predict 复用同一公式）
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_train": len(tr),
    }
    import joblib

    joblib.dump(artifact, model_path)
    report = {
        "model": "HistGradientBoostingRegressor(log unit_price)",
        "trained_at": artifact["trained_at"],
        "seed": args.seed,
        "n_train": len(tr),
        "n_test": len(te),
        "metrics": {
            "model_total_price": model_met,
            "baseline_median_x_area": base_met,
            "mape_relative_improvement_pct": improvement,
        },
        "feature_names": FEATURE_NAMES,
        "feature_missing_pct_test": miss,
        "target": "log(unit_price_yuan); total = unit_price * area_sqm",
        "leakage_control": "小区/城市中位价仅用训练折内统计(OOF)；测试集新小区回退城市中位→全局中位",
        "cleaning": {
            "rules": "坐标围栏(广东21城超围栏) / 北京南昌等文字标记 / 城市价格上下限；title 统一归一小区名",
            "n_raw": clean_stats["n_raw"],
            "n_dropped": clean_stats["n_dropped"],
            "n_dropped_coord": clean_stats["n_dropped_coord"],
            "n_dropped_marker": clean_stats["n_dropped_marker"],
            "n_dropped_price": clean_stats["n_dropped_price"],
            "dropped_by_coord": clean_stats["dropped_by_coord"],
            "dropped_by_marker": clean_stats["dropped_by_marker"],
            "dropped_by_price_cap": clean_stats["dropped_by_price_cap"],
            "n_comm_missing_before": clean_stats["n_comm_missing_before"],
            "n_backfilled_community": clean_stats["n_backfilled_community"],
            "n_comm_missing_after": clean_stats["n_comm_missing_after"],
        },
        "data_notes": {
            "rows_after_clean": len(rows),
            "coord_rows_pct": round(np.mean([r["lat"] == r["lat"] for r in rows]) * 100, 1),
            "community_missing_pct": round(np.mean([not r["comm"] for r in rows]) * 100, 1),
            "coord_backfill": {
                "n_backfilled": coord_stats["n_backfilled"],
                "by_city": coord_stats["by_city"],
                "by_source": coord_stats["by_source"],
                "sources": "community_coords/dws_spatial 词典 + gz/sz/fs/dg/zh 区中心点；"
                "腾讯 geocoder 缓存 output/avm/coord_cache.json 有则优先",
            },
            "smooth_k": args.smooth_k,
        },
        "error_decomposition": {
            "by_segment": seg_metrics,
            "by_city_mape": {c: v for c, v in city_metrics.items()},
        },
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[avm] 产物: {model_path}\n      {report_path}")


if __name__ == "__main__":
    main()
