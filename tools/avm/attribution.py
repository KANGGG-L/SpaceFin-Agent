"""AVM 特征归因可解释报告（C-02，PRD R-cmp-2 / 设计 P9）。

方法：**permutation importance**（`sklearn.inspection.permutation_importance`），
**不是 SHAP**：

- `shap` 未安装（本项目零依赖风格，不引入新重依赖）；
- HistGradientBoostingRegressor 在 quantile loss 下可能没有 `feature_importances_`；
- permutation importance 模型无关、与 loss 无关，只依赖 predict + 评分指标。

口径：
- 模型输出 log(单价)；评分时 `exp` 回单价口径再算 neg MAPE（总价 MAPE = 单价
  MAPE，乘性误差，与 avm_report.json 的总价口径一致）。报告 `scoring` 字段
  诚实标注为 `neg_mean_absolute_percentage_error`。
- 特征中文名/说明复用 avm.md 第 4 节 28 特征清单（FEATURE_META）；映射不出
  的留英文名。

用法：
    # 训练收尾自动生成（train.py 内调用）
    from attribution import compute_attribution

    # 独立 CLI：加载 output/avm/model.joblib + 从库重取测试集
    python tools/avm/attribution.py --model output/avm/model.joblib --out-dir output/avm
    python tools/avm/attribution.py --n-repeats 3 --max-rows 20000   # 控制时长
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MODEL_PATH = os.path.join(REPO_ROOT, "output", "avm", "model.joblib")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "output", "avm")

# 与 train.py FEATURE_NAMES / predict.py 保持同序。评分口调用同一
# neg MAPE（单价口径 %），报告 scoring 字段标注 sklearn 的评分器名。
SCORING = "neg_mean_absolute_percentage_error"
SCORING_NOTE = (
    "模型输出 log(单价)，评分时 exp 回单价口径再算 MAPE；"
    "总价 MAPE = 单价 MAPE（乘性误差），与 avm_report.json 口径一致"
)

# 28 特征中文名与说明（对齐 avm.md 第 4 节特征清单；映射不出留英文名）
FEATURE_META = {
    "area": ("面积", "建筑面积（㎡）"),
    "log_area": ("面积对数", "log(面积)，压缩面积量纲"),
    "bed": ("卧室数", "bedrooms"),
    "hall": ("客厅数", "halls"),
    "bath": ("卫生间数", "bathrooms"),
    "rooms": ("房间总数", "室+厅+卫之和"),
    "area_per_bed": ("平均每间房面积", "面积 / 卧室数"),
    "age": ("楼龄", "building_age，截断到 [0,60]"),
    "floor_level": ("楼层区位", "低/中/高层 0/1/2"),
    "floor_total": ("总层数", "共 N 层"),
    "floor_ratio": ("楼层相对位置", "楼层区位 / 总层数"),
    "direction": ("朝向编码", "南/南北/东南/... 字典映射"),
    "parking": ("车位数量", "parking_count"),
    "city_code": ("城市编码", "广东 21 城码，HistGBR 类别特征"),
    "lat": ("纬度", "坐标回填后的纬度"),
    "lng": ("经度", "坐标回填后的经度"),
    "comm_mean": ("小区目标编码均值", "同(城市,小区)训练折内 log 单价均值（OOF，防泄漏）"),
    "comm_median": ("小区目标编码中位数", "同(城市,小区)训练折内 log 单价中位数"),
    "comm_n": ("小区样本量", "训练折内同小区行数"),
    "comm_minus_city": ("小区相对城市偏移", "小区均值 − 城市均值"),
    "city_mean": ("城市目标编码均值", "训练折内城市 log 单价均值（OOF）"),
    "city_median": ("城市目标编码中位数", "训练折内城市 log 单价中位数"),
    "city_n": ("城市样本量", "训练折内同城市行数"),
    "nn_med_k3": ("邻域单价中位数(k=3)", "训练坐标 k=3 最近邻 log 单价中位数"),
    "nn_med_k8": ("邻域单价中位数(k=8)", "训练坐标 k=8 最近邻 log 单价中位数"),
    "nn_med_k20": ("邻域单价中位数(k=20)", "训练坐标 k=20 最近邻 log 单价中位数"),
    "nn_med_k50": ("邻域单价中位数(k=50)", "训练坐标 k=50 最近邻 log 单价中位数"),
    "nn_dist": ("最近邻距离", "到最近训练样本的欧氏距离"),
}


def feature_meta(name: str) -> dict:
    """特征中文名 + 说明；FEATURE_META 没收录就回退英文名。"""
    cn, desc = FEATURE_META.get(name, (name, name))
    return {"chinese_name": cn, "description": desc}


def _neg_mape_price(est, x, y_true):
    """评分器：neg MAPE（单价口径，%），higher = better。

    permutation_importance 用 `baseline_score − permuted_score` 当 importance：
    重要特征被打乱后 MAPE 变差 → neg MAPE 下降 → importance 为正。
    """
    y_pred = np.exp(est.predict(x))
    return (
        -float(
            np.mean(
                np.abs(y_pred - np.asarray(y_true, dtype=float)) / np.asarray(y_true, dtype=float)
            )
        )
        * 100.0
    )


def compute_attribution(
    model,
    x_test,
    y_test,
    feature_names,
    n_repeats: int = 5,
    seed: int = 42,
    top_n: int | None = None,
    version: str = "unknown",
) -> dict | None:
    """计算 permutation importance 并组装归因报告 dict。

    Args:
        model: 已训练模型（HistGBR）；也接受 `{"model": estimator, ...}` 产物 dict。
        x_test: 测试集特征矩阵（与训练同序，28 列）。
        y_test: 测试集目标（**单价口径**，即 exp(log 单价)；模型输出 log 单价，
            函数内部 exp 回单价口径再评 MAPE）。
        feature_names: 特征名列表（train.FEATURE_NAMES）。
        n_repeats: 每个特征打乱次数（越大越稳、越慢）。
        seed: permutation 随机种子（可复现）。
        top_n: 只返回 importance 前 N；None 返回全部。
        version: 模型版本号，透传到报告。

    Returns:
        报告 dict；模型为 None / 特征数不匹配 / x-y 长度不一致时返回 None
        （优雅失败，不抛栈——调用方提示重训）。
    """
    if model is None:
        return None
    if isinstance(model, dict):
        if "model" not in model or model["model"] is None:
            return None
        estimator = model["model"]
    else:
        estimator = model
    if estimator is None:
        return None
    if x_test is None or y_test is None:
        return None
    if feature_names is None:
        return None
    xa = np.asarray(x_test, dtype=float)
    ya = np.asarray(y_test, dtype=float)
    if xa.ndim != 2 or len(xa) != len(ya):
        return None
    if xa.shape[1] != len(feature_names):
        return None
    if n_repeats < 1:
        raise ValueError("n_repeats 必须 >= 1")

    from sklearn.inspection import permutation_importance

    res = permutation_importance(
        estimator,
        xa,
        ya,
        scoring=_neg_mape_price,
        n_repeats=int(n_repeats),
        random_state=seed,
    )

    items = []
    for i, name in enumerate(feature_names):
        items.append(
            {
                "feature": name,
                "importance_mean": round(float(res.importances_mean[i]), 6),
                "importance_std": round(float(res.importances_std[i]), 6),
                **feature_meta(name),
            }
        )
    items.sort(key=lambda it: it["importance_mean"], reverse=True)
    # full_importances 含全部特征（不随 top_n 截断），top_features 才是 Top-N
    full_importances = {
        it["feature"]: {
            "importance_mean": it["importance_mean"],
            "importance_std": it["importance_std"],
        }
        for it in items
    }
    if top_n is not None:
        items = items[: max(0, int(top_n))]

    return {
        "version": version,
        "method": "permutation_importance",
        "n_repeats": int(n_repeats),
        "seed": seed,
        "scoring": SCORING,
        "scoring_note": SCORING_NOTE,
        "n_test": int(len(ya)),
        "top_features": items,
        "full_importances": full_importances,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load_test_data(artifact: dict, seed: int = 42, max_rows: int | None = None):
    """从库重取数据，按 train.py 同口径复现测试集（feature 编码用产物 encoders）。

    返回 (X_test, y_test_unit_price, test_rows)；模型缺失 / DB 不可达 / 无数据
    返回 None（优雅失败）。行序依赖 DB 返回顺序，与训练时切分是近似复现。
    """
    if artifact is None or "encoders" not in artifact:
        return None
    try:
        import pymysql
        from train import build_features, clean_rows, crawl_params, load_env, load_rows
    except Exception:
        return None
    try:
        env = load_env()
        conn = pymysql.connect(**crawl_params(env), charset="utf8mb4")
        try:
            raw_rows = load_rows(conn, max_rows)
        finally:
            conn.close()
        from coord_backfill import backfill_coords, load_coord_dict
        from data_clean import clean_rows_with_stats
        from sklearn.model_selection import train_test_split

        raw_comm_by_id = {id(r): (r.get("community") or "").strip() or None for r in raw_rows}
        raw_rows, _ = clean_rows_with_stats(raw_rows, parse_all=True)
        rows = clean_rows(raw_rows, raw_comm_by_id)
        backfill_coords(rows, load_coord_dict(env))
        if len(rows) < 2:
            return None
        y_log = np.array([np.log(r["up"]) for r in rows])
        idx = np.arange(len(rows))
        tr_i, te_i = train_test_split(idx, test_size=0.2, random_state=seed)
        te = [rows[i] for i in te_i]
        full_enc = artifact["encoders"]
        test_mat = build_features(
            te,
            y_log[te_i],
            full_enc,
            exclude_self=False,
            smooth_k=float(artifact.get("smooth_k", 0.0)),
            smooth_mode=artifact.get("smooth_mode", "fixed"),
        )
        return test_mat, np.exp(y_log[te_i]), te
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="AVM 特征归因报告（permutation importance）")
    ap.add_argument("--model", default=DEFAULT_MODEL_PATH, help="model.joblib 路径")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="报告输出目录")
    ap.add_argument("--n-repeats", type=int, default=5, help="每个特征打乱次数")
    ap.add_argument("--seed", type=int, default=42, help="permutation 随机种子")
    ap.add_argument("--top-n", type=int, default=None, help="只保留 importance 前 N")
    ap.add_argument("--max-rows", type=int, default=None, help="限制读入行数（控制时长）")
    args = ap.parse_args()

    # 惰性导入 predict（模块顶层无 sklearn/joblib 依赖）
    from predict import load_model

    artifact = load_model(args.model)
    if artifact is None:
        print(
            f"[avm-attribution] 模型缺失或损坏（{args.model}），请先运行 tools/avm/train.py 重训",
            file=sys.stderr,
        )
        raise SystemExit(1)

    feature_names = list(artifact["feature_names"])
    data = load_test_data(artifact, seed=42, max_rows=args.max_rows)
    if data is None:
        print(
            "[avm-attribution] 无法从库重取测试集（DB 不可达/无数据），"
            "请直接运行 tools/avm/train.py（训练收尾自动生成归因报告）",
            file=sys.stderr,
        )
        raise SystemExit(1)
    x_test, y_test, _te = data

    att = compute_attribution(
        artifact["model"],
        x_test,
        y_test,
        feature_names,
        n_repeats=args.n_repeats,
        seed=args.seed,
        top_n=args.top_n,
        version=artifact.get("version", "unknown"),
    )
    if att is None:
        print(
            "[avm-attribution] 归因计算失败（特征数不匹配？），建议重训模型",
            file=sys.stderr,
        )
        raise SystemExit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "attribution_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(att, f, ensure_ascii=False, indent=2)
    print(
        f"[avm-attribution] 已写入 {path}（method=permutation_importance，n_repeats={args.n_repeats}）"
    )
    print("[avm-attribution] Top-5 特征：")
    for t in att["top_features"][:5]:
        print(f"    {t['feature']:<18} imp={t['importance_mean']:>8.4f}  {t['chinese_name']}")


if __name__ == "__main__":
    main()
