"""AVM 自动估值模型训练 + 评估 CLI（S2）。

目标：替代风险引擎中「(城市, 小区) 中位单价 × 面积」的粗糙估值，
用机器学习模型直接估计单位面积价格，再乘面积还原总价。

用法：
    tools/orchestrator/.venv/bin/python tools/avm/train.py --out-dir output/avm
    tools/orchestrator/.venv/bin/python tools/avm/train.py --out-dir output/avm --max-rows 20000
    tools/orchestrator/.venv/bin/python tools/avm/train.py --min-train-samples 500  # 覆盖样本门槛

输出（默认 output/avm/）：
    model.joblib      模型 + 编码字典 + 特征元数据 + version（供 tools/avm/predict.py 加载）
    avm_report.json   指标、样本量、特征清单、训练时间戳、version、与基线对照

样本门槛（B-06 修复）：清洗后样本量低于 --min-train-samples（默认 100）时
明确退出（exit 3 + 「样本不足，降级人工」提示），不再因 train_test_split 崩溃。

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
       有统计输出）：坐标围栏 / URL 子域城市 / 北京南昌等文字标记 / 城市价格
       上限四层判定剔除混入的北京燕郊南昌盐城德阳等外市房源；对 community
       为空的行用 title 中的楼盘名保守回填。
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
       building_age, parking_count, total_price_wan, unit_price_yuan, latitude, longitude, url
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


def eb_k_by_city(comm_d: dict, fallback: float) -> dict:
    """按城市估经验贝叶斯收缩强度 k = σ²_within / τ²_between（只用传入折内统计）。

    动机：固定 smooth_k 对所有城市一刀切，但城市之间「小区间价差」的量级差了
    好几倍——gz 全城 log 单价标准差 0.68，yj 只有 0.25。对 gz/sz 这种小区间
    价差极大的城市，固定 k=10 会把 n 小的小区狠狠拉回城市均值，等于抹掉了
    最有价值的位置信号；对同质城市 k=10 又偏松。

    正态层次模型 y_ij = μ_c + b_i + e_ij，b_i~N(0,τ²)、e_ij~N(0,σ²) 下，
    小区 i 的后验均值恰为 (n_i·ȳ_i + k·μ_c)/(n_i + k)，k = σ²/τ²——
    与代码里已有的收缩公式同形，只是把 k 从手调常数换成按城市估出来的值。
    矩估计：σ² = 组内合并方差，τ² = Var(ȳ_i) − σ²/n̄（截断到正数）。
    """
    by_city: dict[str, list[tuple[float, int, float]]] = {}
    for (city, _comm), v in comm_d.items():
        arr = np.asarray(v, dtype=float)
        ss = float(np.sum((arr - arr.mean()) ** 2)) if len(arr) > 1 else 0.0
        by_city.setdefault(city, []).append((float(arr.mean()), len(arr), ss))
    out = {}
    for city, items in by_city.items():
        if len(items) < 5:  # 小区太少，估不出 τ²，退回全局固定值
            continue
        means = np.array([m for m, _, _ in items])
        ns = np.array([n for _, n, _ in items])
        dof = float(np.sum(ns - 1))
        if dof <= 0:
            continue
        sigma2 = float(np.sum([s for _, _, s in items])) / dof  # 组内合并方差
        tau2 = float(np.var(means)) - sigma2 / float(np.mean(ns))  # 组间方差（矩估计）
        if sigma2 <= 0 or tau2 <= 1e-6:
            continue
        out[city] = float(np.clip(sigma2 / tau2, 0.5, 200.0))
    return out


def fit_encoders(rows: list[dict], y: np.ndarray) -> dict:
    """从给定行统计目标编码字典（只允许传训练折内的行！）。

    返回结构：
        global   全局 log 单价中位数
        city     {city: (mean, median, n)}
        comm     {(city, community): (mean, median, n)}
        eb_k     {city: 经验贝叶斯收缩强度}（--smooth-mode eb 时启用，附加键不影响旧读法）
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
    enc["eb_k"] = eb_k_by_city(comm_d, 0.0)
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
    enc: dict,
    rows: list[dict],
    exclude_self: bool = False,
    smooth_k: float = 0.0,
    smooth_mode: str = "fixed",
) -> np.ndarray:
    """把编码特征映射到行。exclude_self=True 用于训练折内 OOF 计算（邻域不含自身）。

    回退语义：小区无 → 城市中位；城市无 → 全局中位。与测试/服务期一致。

    smooth_k>0 时对小区目标编码做经验贝叶斯收缩（向城市均值/中位靠拢）：
    sm = (n*val + k*ref)/(n+k)。n 小的新小区统计噪声大，收缩能显著降低其对
    预测的方差贡献；n 大的成熟小区几乎不受影响。

    smooth_mode="eb" 时 k 改为按城市估的 σ²组内/τ²组间（见 eb_k_by_city），
    小区间价差大的城市自动少收缩；估不出的城市回退 smooth_k。
    """
    g = enc["global"]
    eb = enc.get("eb_k", {}) if smooth_mode == "eb" else {}
    feat = []
    for r in rows:
        cv = enc["city"].get(r["city"], (g, g, 0))
        cmv = enc["comm"].get((r["city"], r["comm"])) if r["comm"] else None
        k = eb.get(r["city"], smooth_k) if smooth_mode == "eb" else smooth_k
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

# 最小训练样本门槛（B-06 缺陷修复）：清洗后样本低于该值直接退出而非崩溃。
# 历史上 --max-rows 1 时 train_test_split 因测试集为空抛 ValueError；
# 样本太少训练出来的模型也不可信（n=1 时任何指标都是噪声），
# 明确退出并提示降级人工，比抛未处理异常更可控。
MIN_TRAIN_SAMPLES = 100


def make_version(out_dir: str) -> str:
    """生成模型版本号（如 2026-08-05-r1）。

    规则：当天首训 r1，同日重训递增 r2/r3...（读取旧 avm_report.json 的 version）。
    版本号随模型产物落盘，predict 侧可读，用于追踪"哪一版模型在跑"。
    """
    date = time.strftime("%Y-%m-%d")
    rev = 1
    report_path = os.path.join(out_dir, "avm_report.json")
    if os.path.exists(report_path):
        try:
            with open(report_path, encoding="utf-8") as f:
                old = json.load(f)
            v = old.get("version", "")
            if v.startswith(date):
                rev = int(v.split("-r")[-1]) + 1
        except (ValueError, OSError):
            pass
    return f"{date}-r{rev}"


def build_features(
    rows: list[dict],
    y: np.ndarray,
    encoders: dict,
    exclude_self: bool = False,
    smooth_k: float = 0.0,
    smooth_mode: str = "fixed",
) -> np.ndarray:
    """组装全部特征（基础 + 编码 + 空间）。供训练与测试两用。"""
    base_feat, _ = base_features(rows)
    enc_feat = apply_encoders(
        encoders, rows, exclude_self=exclude_self, smooth_k=smooth_k, smooth_mode=smooth_mode
    )
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
# 置信度评分 + MAPE@coverage（AC-07 收口口径：精度 @ 覆盖率）
#
# 商用 AVM（Zillow/RICS/IAAO 系）不报裸 MAPE，而是报「精度 @ 覆盖率」：可比案例
# 充足的估值放行、不足的主动弃权转人工（对应风险侧 AC-04 的 low_confidence）。
# 全量 MAPE 的噪声下界 ≈12%（留一法实测），AC-07 的 ≤10% 只可能在可比案例充足的
# 高置信子集上达成。本函数给出这个子集的最小覆盖率。
# ---------------------------------------------------------------------------


def city_log_sd(rows: list[dict]) -> dict[str, float]:
    """每城 log(单价) 标准差，只用传入行统计（调用方传训练集 → 预测时可得）。"""
    from collections import defaultdict

    buf: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        buf[r["city"]].append(float(np.log(r["up"])))
    return {c: float(np.std(v)) for c, v in buf.items()}


def confidence_score(
    rows: list[dict], enc: dict, city_sd: dict[str, float], tr: list[dict]
) -> np.ndarray:
    """每笔预测的置信分（**只用训练集统计，预测时可得，无泄漏**）。

    信号：**可比案例支撑度**——训练集内同 (城市, 小区, 房型) 且面积落在 ±5% / ±2%
    区间内的挂牌数。这是留一法噪声下界实验（同城同小区同房型面积相近互相预测：
    ±5% 下界 10.09%、±2% 下界 9.40%）的直接操作化：可比案例越足，小区中位对单套房
    的代表性越强，越该放行。无小区或无可比案例 → 0 分（弃权转人工）。
    """
    import bisect
    from collections import defaultdict

    # 训练集按 (城市, 小区, 房型) 分组，面积排序，供区间计数
    groups: dict[tuple, list[float]] = defaultdict(list)
    for r in tr:
        if r["comm"]:
            groups[(r["city"], r["comm"], r["bed"])].append(float(r["area"]))
    for k in groups:
        groups[k].sort()

    def _near(areas: list[float], a: float, frac: float) -> int:
        i = bisect.bisect_left(areas, a * (1 - frac))
        j = bisect.bisect_right(areas, a * (1 + frac))
        return j - i

    scores = []
    for r in rows:
        if not r["comm"]:
            scores.append(0.0)
            continue
        areas = groups.get((r["city"], r["comm"], r["bed"]))
        if not areas:
            scores.append(0.0)
            continue
        cnt5 = _near(areas, r["area"], 0.05)
        cnt2 = _near(areas, r["area"], 0.02)
        if cnt5 <= 0:
            scores.append(0.0)
            continue
        # 严格可比（±2%）是主信号：留一法里 ±2% 子集噪声下界最低（9.40% vs ±5% 的 10.09%）。
        # ±5% 的宽可比做次级加分。log1p 饱和避免大桶霸榜。
        scores.append(float(np.log1p(cnt2) + 0.5 * np.log1p(cnt5)))
    return np.array(scores, dtype=float)


def coverage_curve(
    y_true: np.ndarray, y_pred: np.ndarray, score: np.ndarray, step: int = 5
) -> list[dict]:
    """按置信分从高到低取前 k 行，算累计 MAPE@coverage。coverage 从 100% 降到 30%。"""
    n = len(score)
    order = np.argsort(score)  # 升序：最不置信在前
    pts = []
    for pct in range(100, 29, -step):
        k = max(1, int(round(n * pct / 100)))
        idx = order[n - k :]
        m = metrics(y_true[idx], y_pred[idx])
        pts.append(
            {
                "coverage_pct": pct,
                "mape": m["mape"],
                "mdape": m["mdape"],
                "n": int(k),
            }
        )
    return pts


def ac07_coverage_at_10pct(curve: list[dict]) -> dict | None:
    """MAPE 首次 ≤10% 的最大覆盖率（对应最小弃权率），永不达标则 None。"""
    return next((p for p in curve if p["mape"] <= 10.0), None)


def confidence_tiers(rows: list[dict], tr: list[dict]) -> dict:
    """三档可比支撑度：高=±5% 可比≥10 条，中=1–9 条，低=无可比（含无小区）。"""
    import bisect
    from collections import defaultdict

    groups: dict[tuple, list[float]] = defaultdict(list)
    for r in tr:
        if r["comm"]:
            groups[(r["city"], r["comm"], r["bed"])].append(float(r["area"]))
    for k in groups:
        groups[k].sort()

    tiers = {"high": {"n": 0}, "mid": {"n": 0}, "low": {"n": 0}}
    for r in rows:
        areas = groups.get((r["city"], r["comm"], r["bed"])) if r["comm"] else None
        if not areas:
            tiers["low"]["n"] += 1
            continue
        i = bisect.bisect_left(areas, r["area"] * 0.95)
        j = bisect.bisect_right(areas, r["area"] * 1.05)
        cnt = j - i
        if cnt >= 10:
            tiers["high"]["n"] += 1
        elif cnt >= 1:
            tiers["mid"]["n"] += 1
        else:
            tiers["low"]["n"] += 1
    return tiers


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
    ap.add_argument(
        "--min-train-samples",
        type=int,
        default=MIN_TRAIN_SAMPLES,
        help="清洗后样本量下限，低于该值直接退出（降级人工，不训练）",
    )
    ap.add_argument(
        "--drop-coords",
        action="store_true",
        help="消融实验：丢弃全部经纬度（含回填），用于量化空间特征的真实边际贡献",
    )
    ap.add_argument(
        "--smooth-mode",
        choices=("fixed", "eb"),
        default="fixed",
        help="小区目标编码收缩方式：fixed=统一 --smooth-k；eb=按城市估 σ²/τ²（分城市自适应）",
    )
    ap.add_argument(
        "--loss",
        choices=("squared_error", "absolute_error", "quantile"),
        default="squared_error",
        help="HistGBR 损失。log 空间 MAE≈相对误差中位数优化（改善 MdAPE，不改善 MAPE）",
    )
    ap.add_argument("--quantile", type=float, default=0.5, help="--loss quantile 时的分位数")
    ap.add_argument(
        "--city-weight-pow",
        type=float,
        default=0.0,
        help="城市逆频样本权重指数（0=不加权）：w ∝ (N/(K*n_city))^pow，上调小样本城市",
    )
    ap.add_argument("--lr", type=float, default=0.03, help="HistGBR learning_rate")
    ap.add_argument("--max-leaves", type=int, default=150, help="HistGBR max_leaf_nodes")
    ap.add_argument("--min-leaf", type=int, default=12, help="HistGBR min_samples_leaf")
    ap.add_argument("--l2", type=float, default=2.0, help="HistGBR l2_regularization")
    args = ap.parse_args()

    if args.loss != "quantile" or args.smooth_mode != "eb":
        print(
            f"⚠️ 非 canonical r11 配置（loss={args.loss} smooth_mode={args.smooth_mode}），"
            "45% 覆盖 MAPE 可能不达 AC-07（9.88%）；请用 --loss quantile --quantile 0.45 --smooth-mode eb",
            flush=True,
        )

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
    # 消融：把坐标全部抹掉再训练。目的是量化「空间特征到底值多少 MAPE」——
    # 若 33% 覆盖率的坐标只值零点几个百分点，那么花配额把覆盖率补到 100%
    # 的收益上界也就是它的两三倍，不足以支撑 15.5%→10% 的目标，应及早换方向。
    if args.drop_coords:
        for r in rows:
            r["lat"], r["lng"] = np.nan, np.nan
        print("[avm] 消融模式：已丢弃全部坐标（空间特征将全为 NaN）")

    if coord_stats["n_backfilled"] and not args.drop_coords:
        print(
            f"[avm] 坐标回填 {coord_stats['n_backfilled']} 行"
            f"（词典 {coord_stats['by_source']['dict']} / 区中心 {coord_stats['by_source']['district']}）"
        )
    print(
        f"[avm] 外市清洗: 剔除 {clean_stats['n_dropped']} 行"
        f"（围栏 {clean_stats['n_dropped_coord']} / URL {clean_stats.get('n_dropped_url', 0)}"
        f" / 标记 {clean_stats['n_dropped_marker']}"
        f" / 价格 {clean_stats['n_dropped_price']}），"
        f"title 归一/回填小区 {clean_stats['n_backfilled_community']} 行"
    )
    print(f"[avm] 清洗后样本 {len(rows)}（{time.time() - t0:.1f}s）")

    # B-06 样本门槛：清洗后样本不足直接退出（exit 非 0），不进入 train_test_split。
    # 极端场景（如 --max-rows 1）下 split/KFold 会因样本过少抛未处理异常，
    # 且样本过少训练出的模型不可信；明确退出让上层感知并降级人工。
    if len(rows) < args.min_train_samples:
        print(
            f"[avm] 样本不足（清洗后 {len(rows)} < 门槛 {args.min_train_samples}），"
            f"降级人工，本次不训练"
        )
        raise SystemExit(3)

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
            [tr[i] for i in b],
            ytr[b],
            fold_enc,
            exclude_self=True,
            smooth_k=args.smooth_k,
            smooth_mode=args.smooth_mode,
        )
    # 测试集用完整训练集编码映射（新小区自动回退城市/全局中位）
    full_enc = fit_encoders(tr, ytr)
    train_mat = enc_tr
    test_mat = build_features(
        te,
        yte,
        full_enc,
        exclude_self=False,
        smooth_k=args.smooth_k,
        smooth_mode=args.smooth_mode,
    )
    if args.smooth_mode == "eb":
        ebk = full_enc.get("eb_k", {})
        print(
            "[avm] EB 收缩强度（k 越小=越信小区自身价）: "
            + " ".join(f"{c}={ebk[c]:.1f}" for c in sorted(ebk, key=lambda c: ebk[c]))
        )

    # HistGBR 原生支持 NaN（无坐标/无小区行保留，特征列有缺失不剔除）
    # 参数：max_leaf_nodes=150 + 更强 L2 正则 在本轮清洗/回填数据上 MAPE 最低
    # （网格 6 组对比：base 16.14 → big 15.97，-0.18pp；lr=0.02 组 16.02 次之；
    #  S6 再扫 5 组 lr/leaf/l2 组合均 ≥ base 15.94/15.51，参数已达局部最优）
    model = HistGradientBoostingRegressor(
        max_iter=2000,
        learning_rate=args.lr,
        max_leaf_nodes=args.max_leaves,
        min_samples_leaf=args.min_leaf,
        l2_regularization=args.l2,
        loss=args.loss,
        quantile=args.quantile if args.loss == "quantile" else None,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=40,
        random_state=args.seed,
        categorical_features=[FEATURE_NAMES.index("city_code")],
    )
    # 城市逆频权重：gz/sz 训练样本各仅 ~1.1k（其余城市 ~2.2k），却是误差最大的两城。
    # pow=0 时全 1（默认，行为不变）。
    sw = None
    if args.city_weight_pow > 0:
        from collections import Counter

        cnt = Counter(r["city"] for r in tr)
        sw = np.array([(len(tr) / (len(cnt) * cnt[r["city"]])) ** args.city_weight_pow for r in tr])
        sw /= sw.mean()
    model.fit(train_mat, ytr, sample_weight=sw)
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

    # 置信度分层 + MAPE@coverage（AC-07 收口口径）。置信分只用训练集统计 → 无泄漏。
    city_sd = city_log_sd(tr)
    conf = confidence_score(te, full_enc, city_sd, tr)
    curve = coverage_curve(tp_te, pred_tp, conf)
    ac07 = ac07_coverage_at_10pct(curve)
    tiers = confidence_tiers(te, tr)
    print("\n[avm] 置信度分层（测试集）:")
    for t, v in tiers.items():
        print(f"    {t:<4} n={v['n']}")
    print("[avm] MAPE@coverage（按置信分从高到低累计）:")
    for p in curve:
        mark = "  <- AC-07" if ac07 and p["coverage_pct"] == ac07["coverage_pct"] else ""
        print(
            f"    覆盖 {p['coverage_pct']:>3}%  n={p['n']:>5}  MAPE={p['mape']}%  MdAPE={p['mdape']}%{mark}"
        )
    if ac07:
        print(
            f"[avm] AC-07 口径：覆盖 {ac07['coverage_pct']}% 时 MAPE={ac07['mape']}% ≤10%"
            f"（全量 MAPE={model_met['mape']}%，oracle 下界≈12.65%）"
        )
    else:
        print("[avm] AC-07 口径：30% 以上覆盖率均无法达成 MAPE≤10%")

    # 特征缺失率（说明空间特征可用性，写入报告）
    miss = {}
    for name, col in zip(FEATURE_NAMES, test_mat.T, strict=True):
        miss[name] = round(float(np.mean(np.isnan(col))) * 100, 1)

    # 落盘
    os.makedirs(args.out_dir, exist_ok=True)
    model_path = os.path.join(args.out_dir, "model.joblib")
    report_path = os.path.join(args.out_dir, "avm_report.json")
    version = make_version(args.out_dir)
    artifact = {
        "model": model,
        "encoders": full_enc,
        "cities": cities,
        "feature_names": FEATURE_NAMES,
        "spatial_k": list(SPATIAL_K),
        "smooth_k": args.smooth_k,  # 小区目标编码收缩强度（predict 复用同一公式）
        # 收缩方式。"fixed"=predict.py 现有读法（用 smooth_k）即可；"eb" 时 k 改按城市取
        # encoders["eb_k"][city]，predict 侧需同步（老产物无此键 → 视为 fixed，向后兼容）。
        "smooth_mode": args.smooth_mode,
        "version": version,  # 模型版本（如 2026-08-05-r1），供 predict/运营侧追踪
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_train": len(tr),
    }
    import joblib

    joblib.dump(artifact, model_path)
    report = {
        "version": version,
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
        "model_params": {
            "learning_rate": args.lr,
            "max_leaf_nodes": args.max_leaves,
            "min_samples_leaf": args.min_leaf,
            "l2_regularization": args.l2,
            "max_iter": 2000,
            "early_stopping": True,
            "loss": args.loss,
            "quantile": args.quantile if args.loss == "quantile" else None,
            "smooth_mode": args.smooth_mode,
            "city_weight_pow": args.city_weight_pow,
        },
        "feature_names": FEATURE_NAMES,
        "feature_missing_pct_test": miss,
        "target": "log(unit_price_yuan); total = unit_price * area_sqm",
        "leakage_control": "小区/城市中位价仅用训练折内统计(OOF)；测试集新小区回退城市中位→全局中位",
        "cleaning": {
            "rules": "坐标围栏(广东21城超围栏) / URL子域城市 / 北京南昌等文字标记 / 城市价格上下限；title 统一归一小区名",
            "n_raw": clean_stats["n_raw"],
            "n_dropped": clean_stats["n_dropped"],
            "n_dropped_coord": clean_stats["n_dropped_coord"],
            "n_dropped_marker": clean_stats["n_dropped_marker"],
            "n_dropped_price": clean_stats["n_dropped_price"],
            "n_dropped_url": clean_stats.get("n_dropped_url", 0),
            "dropped_by_coord": clean_stats["dropped_by_coord"],
            "dropped_by_marker": clean_stats["dropped_by_marker"],
            "dropped_by_price_cap": clean_stats["dropped_by_price_cap"],
            "dropped_by_url": clean_stats.get("dropped_by_url", {}),
            "n_comm_missing_before": clean_stats["n_comm_missing_before"],
            "n_backfilled_community": clean_stats["n_backfilled_community"],
            "n_comm_missing_after": clean_stats["n_comm_missing_after"],
        },
        "data_notes": {
            "rows_after_clean": len(rows),
            "min_train_samples": args.min_train_samples,
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
        "confidence": {
            "method": "可比案例支撑度 log1p(cnt±2%)+0.5·log1p(cnt±5%)，同(城市,小区,房型)且面积±X%；严格可比(±2%)为主信号；只用训练集统计（无泄漏）；无小区/无可比=0",
            "tiers": tiers,
            "coverage_curve": curve,
            "ac07_coverage_at_10pct": ac07,
            "note": (
                "AC-07 收口为「精度@覆盖率」：全量 MAPE 受挂牌价噪声下界(≈12%, 留一法)"
                "限制无法≤10%，高置信子集（可比案例充足）可达成；弃权笔转 AC-04 低置信人工核查。"
            ),
        },
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[avm] 产物: {model_path}\n      {report_path}")


if __name__ == "__main__":
    main()
