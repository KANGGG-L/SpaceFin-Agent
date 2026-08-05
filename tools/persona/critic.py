"""Critic：批评者——对照真实基准检验合成画像，并对美化偏见做迭代校准。

- ks_report(benchmark, personas)：对三特征 + 派生违约概率各算 scipy.stats.ks_2samp，
  输出每特征 KS 与违约率分布 KS（KS 越大越不像基准）。
- calibrate(benchmark, personas)：校准闭环「生成 → 对照基准 → 修正 → 再验」，
  迭代至违约率分布 KS ≤ 0.05 或最多 20 轮。

校准机制（分位数映射，可复现、只作用于特征分布）：
每轮对三特征各做「秩分位数映射」——把特征值按其当前分布中的秩位，映射到基准
同秩位的分位数，再按混合系数 alpha_r = 1 - 2^{-r} 与旧值混合（逐轮逼近基准边际，
保证修正轨迹单调收敛）。违约概率始终由固定公式从修正后的特征重算，再验 KS。

对应 PRD：
- H3 验证标准 KS ≤ 0.05（合成画像不被美化偏见扭曲）；
- R-OPT-01 严格模式强制对照真实基准校准，否则输出带「未校准」标记（见 main 报告）。
"""

import benchmark
import generator
import numpy as np
from scipy.stats import ks_2samp

DEFAULT_TARGET_KS = 0.05
DEFAULT_MAX_ROUNDS = 20


# ------------------------------------------------------------------ KS 报告
def ks_report(benchmark_data, personas):
    """对三特征 + 派生违约概率各算 ks_2samp，返回结构化报告 dict。"""
    report = {
        "n_benchmark": benchmark_data.n,
        "n_synthetic": personas.n,
        "features": {},
        "default_prob": None,
    }
    for key in benchmark.FEATURE_KEYS:
        stat, pvalue = ks_2samp(personas.features[key], benchmark_data.features[key])
        report["features"][key] = {
            "ks": float(stat),
            "pvalue": float(pvalue),
            "passed_ks_le_0_05": bool(stat <= DEFAULT_TARGET_KS),
        }
    stat, pvalue = ks_2samp(personas.default_prob, benchmark_data.default_prob)
    report["default_prob"] = {
        "ks": float(stat),
        "pvalue": float(pvalue),
        "passed_ks_le_0_05": bool(stat <= DEFAULT_TARGET_KS),
    }
    return report


# ------------------------------------------------------------------ 校准闭环
def _qmap_rank(values, target):
    """秩分位数映射：把 values 按其分布中的秩位，映射到 target 同秩位分位数。

    对 values 排序，第 k 个（绘制位 (k+0.5)/n）映射到 target 经验分位数函数
    (线性插值) 的同位点。输出保持 values 的秩序，边际分布逼近 target 的边际。
    全程无随机，可复现。
    """
    values = np.asarray(values, dtype=float)
    n = len(values)
    order = np.argsort(values, kind="stable")
    ranks = np.empty(n)
    ranks[order] = (np.arange(1, n + 1) - 0.5) / n
    t = np.sort(np.asarray(target, dtype=float))
    m = len(t)
    return np.interp(ranks, np.linspace(0.0, 1.0, m), t)


def calibrate(benchmark_data, personas, target_ks=DEFAULT_TARGET_KS, max_rounds=DEFAULT_MAX_ROUNDS):
    """校准闭环：对合成画像迭代做分位数映射，直到违约率分布 KS ≤ target_ks。

    返回 (calibrated_personas, trajectory)：
        calibrated_personas: 校准后的 Personas（特征已修正，违约概率重算）
        trajectory: 每轮违约率 KS 的轨迹，[0] 为校准前基线

    校准只作用于特征分布；违约概率一律由固定公式重算，绝不被直接改写。
    """
    features = np.column_stack([personas.features[k] for k in benchmark.FEATURE_KEYS])
    bench_feats = np.column_stack([benchmark_data.features[k] for k in benchmark.FEATURE_KEYS])

    def p_ks(feat):
        p = benchmark.derive_default_prob(feat[:, 1], feat[:, 2])
        return float(ks_2samp(p, benchmark_data.default_prob).statistic)

    trajectory = [p_ks(features)]
    if trajectory[0] <= target_ks:
        # 已达标，无需修正：返回原画像与单点轨迹
        return personas, trajectory

    for rnd in range(1, max_rounds + 1):
        alpha = 1.0 - 0.5**rnd  # 0.5, 0.75, 0.875, ... 逐轮逼近 1，保证单调收敛
        mapped = np.column_stack([_qmap_rank(features[:, j], bench_feats[:, j]) for j in range(3)])
        features = (1.0 - alpha) * features + alpha * mapped
        cur = p_ks(features)
        trajectory.append(cur)
        if cur <= target_ks:
            break

    calibrated = generator.Personas(
        income_monthly=features[:, 0],
        debt_ratio=features[:, 1],
        credit_score=features[:, 2],
        bias=f"{personas.bias}→calibrated",
        seed=personas.seed,
    )
    return calibrated, trajectory
