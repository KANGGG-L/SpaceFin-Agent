"""
SpaceFin Agent - Stage 2 数据底座 PoC
=====================================

目的：
  1. 演示湖仓分层（ODS -> DWD -> DWS -> ADS）的内存流转概念。
  2. 论证 H1：空间感知模型显著优于全局基线（自动化估值 AVM）。
     用"全局 OLS"对比"距离加权局部回归（GWR-lite）"，量化 R^2 提升与 MAPE 下降。

约束：
  - 仅依赖 Python 标准库，零第三方依赖，可在任意环境运行。
  - 数据为合成样本，用于论证方向与架构，非生产实现。
  - 生产实现以 mgwr 库（完整多尺度带宽迭代）+ 真实成交样本校准为准。

运行：
  python avm_poc.py
"""

import csv
import math
import os
import random


# ---------------------------------------------------------------------------
# 1. 生成合成房产数据（含空间变化系数，使空间模型明显占优）
# ---------------------------------------------------------------------------
def generate(n=500, seed=42):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        lat = 31.0 + rng.uniform(0, 0.5)  # 模拟城市纬度带
        lng = 121.0 + rng.uniform(0, 0.5)  # 模拟城市经度带
        area = rng.uniform(40, 140)  # 建筑面积 (m^2)
        age = rng.uniform(0, 30)  # 房龄 (年)

        # 空间变化系数：beta_area 随地理位置非线性波动（模拟"地理学第一定律"）
        beta_area = 60 + 50 * math.sin(lat * 6.0) * math.cos(lng * 6.0)
        beta0 = 20 + 20 * math.cos(lat * 5.0)
        beta_age = -4.0

        true = beta0 + beta_area * area + beta_age * age + rng.gauss(0, 4)
        rows.append(
            {
                "id": i,
                "lat": lat,
                "lng": lng,
                "area": round(area, 2),
                "age": round(age, 2),
                "true_price": round(true, 2),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 2. 湖仓分层（内存模拟）：ODS -> DWD -> DWS -> ADS
# ---------------------------------------------------------------------------
def layer(rows):
    # ODS：贴源快照（原始字段，含原始精度）
    ods = rows

    # DWD：清洗 + 标准化（此处仅做字段规整与小数截断，模拟标准化编码）
    dwd = []
    for r in ods:
        dwd.append(
            {
                "id": r["id"],
                "lat": round(r["lat"], 4),
                "lng": round(r["lng"], 4),
                "area": r["area"],
                "age": r["age"],
                "true_price": r["true_price"],
            }
        )

    # DWS：轻度聚合（按"行政区网格"统计均价与样本量）
    grid = {}
    for r in dwd:
        gx = int(r["lat"] * 10)  # 粗略网格
        gy = int(r["lng"] * 10)
        key = (gx, gy)
        grid.setdefault(key, []).append(r["true_price"])

    dws = []
    for (gx, gy), prices in grid.items():
        dws.append(
            {
                "grid": f"{gx}_{gy}",
                "sample_n": len(prices),
                "avg_price": round(sum(prices) / len(prices), 2),
            }
        )

    # ADS：应用层指标（全市均价、最高网格、最低网格）
    all_prices = [r["true_price"] for r in dwd]
    ads = [
        {
            "metric": "city_avg_price",
            "value": round(sum(all_prices) / len(all_prices), 2),
        },
        {
            "metric": "grid_count",
            "value": len(dws),
        },
    ]

    return ods, dwd, dws, ads


def write_csv(path, header, rows, value_keys=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            if value_keys:
                w.writerow([r[k] for k in value_keys])
            else:
                w.writerow(r)


# ---------------------------------------------------------------------------
# 3. 模型：全局 OLS 基线 vs 距离加权局部回归（GWR-lite）
# ---------------------------------------------------------------------------
def _solve(A, b):
    """解线性方程组 A x = b（高斯消元 + 部分主元），A 为 n x n 列表。"""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        # 部分主元
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for j in range(col, n + 1):
            M[col][j] /= pv
        for r in range(n):
            if r != col:
                factor = M[r][col]
                if factor != 0:
                    for j in range(col, n + 1):
                        M[r][j] -= factor * M[col][j]
    return [M[i][n] for i in range(n)]


def _design(points):
    """构造设计矩阵 X = [1, area, age]，返回 (X, y)。"""
    X, y = [], []
    for p in points:
        X.append([1.0, p["area"], p["age"]])
        y.append(p["true_price"])
    return X, y


def ols_fit(train):
    X, y = _design(train)
    # 正规方程 A = X^T X, b = X^T y
    k = 3
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    n = len(X)
    for i in range(n):
        for a in range(k):
            b[a] += X[i][a] * y[i]
            for c in range(k):
                A[a][c] += X[i][a] * X[i][c]
    beta = _solve(A, b)
    return beta


def gwr_fit_predict(train, test, bandwidth=0.1):
    """对每个测试点，用距离高斯核做加权最小二乘（GWR-lite）。"""
    preds = []
    k = 3
    for t in test:
        # 构造加权正规方程
        A = [[0.0] * k for _ in range(k)]
        b = [0.0] * k
        for p in train:
            d = math.hypot(t["lat"] - p["lat"], t["lng"] - p["lng"])
            w = math.exp(-((d / bandwidth) ** 2))
            if w < 1e-6:
                continue
            row = [1.0, p["area"], p["age"]]
            yy = p["true_price"]
            for a in range(k):
                b[a] += w * row[a] * yy
                for c in range(k):
                    A[a][c] += w * row[a] * row[c]
        try:
            beta = _solve(A, b)
        except Exception:
            beta = None
        if beta is None:
            preds.append(None)
            continue
        xt = [1.0, t["area"], t["age"]]
        preds.append(sum(xt[i] * beta[i] for i in range(k)))
    return preds


def _metrics(test, preds):
    ys, ps = [], []
    for p, pr in zip(test, preds):
        if pr is None:
            continue
        ys.append(p["true_price"])
        ps.append(pr)
    n = len(ys)
    mean = sum(ys) / n
    sst = sum((y - mean) ** 2 for y in ys)
    sse = sum((y - pr) ** 2 for y, pr in zip(ys, ps))
    r2 = 1 - sse / sst if sst > 0 else 0.0
    mape = sum(abs(y - pr) / abs(y) for y, pr in zip(ys, ps)) / n * 100
    return r2, mape, n


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    rows = generate(n=500, seed=42)

    # 分层并落盘（演示湖仓概念）
    ods, dwd, dws, ads = layer(rows)
    write_csv(
        os.path.join(out_dir, "ods.csv"),
        ["id", "lat", "lng", "area", "age", "true_price"],
        ods,
        ["id", "lat", "lng", "area", "age", "true_price"],
    )
    write_csv(
        os.path.join(out_dir, "dwd.csv"),
        ["id", "lat", "lng", "area", "age", "true_price"],
        dwd,
        ["id", "lat", "lng", "area", "age", "true_price"],
    )
    write_csv(
        os.path.join(out_dir, "dws.csv"),
        ["grid", "sample_n", "avg_price"],
        dws,
        ["grid", "sample_n", "avg_price"],
    )
    write_csv(os.path.join(out_dir, "ads.csv"), ["metric", "value"], ads, ["metric", "value"])

    # 切分训练 / 测试
    random.Random(7).shuffle(rows)
    split = int(len(rows) * 0.7)
    train, test = rows[:split], rows[split:]

    beta_ols = ols_fit(train)
    ols_preds = [
        [1.0, t["area"], t["age"]][0] * beta_ols[0]
        + [1.0, t["area"], t["age"]][1] * beta_ols[1]
        + [1.0, t["area"], t["age"]][2] * beta_ols[2]
        for t in test
    ]
    # 更清晰的写法：
    ols_preds = [beta_ols[0] + beta_ols[1] * t["area"] + beta_ols[2] * t["age"] for t in test]

    gwr_preds = gwr_fit_predict(train, test, bandwidth=0.1)

    r2_ols, mape_ols, _ = _metrics(test, ols_preds)
    r2_gwr, mape_gwr, _ = _metrics(test, gwr_preds)

    print("=" * 60)
    print("SpaceFin Agent - Stage 2 数据底座 PoC")
    print("=" * 60)
    print(f"样本量: 总 {len(rows)} / 训练 {len(train)} / 测试 {len(test)}")
    print("-" * 60)
    print("湖仓分层已落盘: ods.csv / dwd.csv / dws.csv / ads.csv")
    print("-" * 60)
    print("AVM 模型对比 (H1 论证)")
    print(f"  全局 OLS : R^2 = {r2_ols * 100:5.1f}%   MAPE = {mape_ols:5.2f}%")
    print(f"  GWR-lite : R^2 = {r2_gwr * 100:5.1f}%   MAPE = {mape_gwr:5.2f}%")
    print("-" * 60)
    r2_gain = (r2_gwr - r2_ols) * 100
    mape_drop = mape_ols - mape_gwr
    print(f"空间模型 R^2 提升 : {r2_gain:+.1f} 个百分点  (H1 阈值 >= 10pp)")
    print(f"空间模型 MAPE 下降: {mape_drop:.2f} 个百分点")
    verdict = "通过" if r2_gain >= 10 else "未达阈值"
    print(f"H1 结论          : {verdict}")
    print("=" * 60)


if __name__ == "__main__":
    main()
