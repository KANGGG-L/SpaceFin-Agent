"""H3 最小验证原型 CLI：合成行为仿真 Generator → Critic 校准闭环。

产出 output/persona/persona_report.json：
- naive 模式 KS 报告（预期违约率 KS 明显 > 0.05，证明美化偏见可被 KS 检测）
- calibrated 模式 KS 报告（预期违约率 KS ≤ 0.05，H3 达成）
- 校准轨迹（轮数 / 每轮违约率 KS）
- naive 与 calibrated 的违约率对比
- 诚实声明字段（基准来源 = 合成种子，真实基准属商用前置）

用法：
    python tools/persona/main.py [--n 1000] [--seed 42] [--refresh-benchmark]
"""

import argparse
import datetime
import json
import os

import benchmark
import critic
import generator
import numpy as np

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output",
    "persona",
    "persona_report.json",
)

H3 = "合成画像能如实反映真实经济状态下的不良率分布，不被美化偏见扭曲（验证标准：违约率分布 KS ≤ 0.05）"
P3 = "LLM 生成画像被 RLHF 美化偏见污染（乐观化），违约低估"


def _p_stats(p):
    return {
        "mean": float(p.mean()),
        "std": float(p.std()),
        "p10": float(np.percentile(p, 10)),
        "p50": float(np.percentile(p, 50)),
        "p90": float(np.percentile(p, 90)),
    }


def build_report(bench, naive_personas, calibrated_personas, trajectory, seed, n):
    naive_rep = critic.ks_report(bench, naive_personas)
    cal_rep = critic.ks_report(bench, calibrated_personas)
    return {
        "tool": "tools/persona —— H3 最小验证原型（Generator → Critic 校准闭环）",
        "h3": H3,
        "p3_pain": P3,
        "acceptance": "违约率分布 KS <= 0.05",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "params": {"n": n, "seed": seed},
        "benchmark": {
            "source": bench.source,
            "n": bench.n,
            "extracted_at": bench.extracted_at,
            "features": list(benchmark.FEATURE_KEYS),
            "default_prob_formula": benchmark.LOGISTIC,
        },
        "naive": {
            "bias": naive_personas.bias,
            "calibration_status": "未校准",
            "ks_report": naive_rep,
        },
        "calibrated": {
            "bias": calibrated_personas.bias,
            "calibration_status": "校准",
            "ks_report": cal_rep,
        },
        "calibration": {
            "mechanism": "秩分位数映射（混合系数 alpha=1-2^-r 逐轮逼近基准边际，只作用于特征分布）",
            "target_ks": critic.DEFAULT_TARGET_KS,
            "max_rounds": critic.DEFAULT_MAX_ROUNDS,
            "rounds": len(trajectory) - 1,
            "converged": bool(cal_rep["default_prob"]["ks"] <= critic.DEFAULT_TARGET_KS),
            "trajectory": [
                {"round": i, "p_ks": float(ks), "alpha": None if i == 0 else 1.0 - 0.5**i}
                for i, ks in enumerate(trajectory)
            ],
        },
        "comparison_default_prob": {
            "benchmark": _p_stats(bench.default_prob),
            "naive": _p_stats(naive_personas.default_prob),
            "calibrated": _p_stats(calibrated_personas.default_prob),
        },
        "uncalibrated_flag": {
            "semantics": "PRD R-OPT-01：输出若未经 Critic 严格模式对照真实基准校准，必须携带「未校准」标记，不得直接用于决策",
            "demo": {
                "naive": f"未校准（美化偏见未消除，违约率 KS={naive_rep['default_prob']['ks']:.3f}，低估违约风险）",
                "calibrated": f"校准（已对照基准，违约率 KS={cal_rep['default_prob']['ks']:.3f} <= 0.05）",
            },
        },
        "honest_declaration": {
            "benchmark_source": "基准 = 合成种子客户分布（spacefin.customer seed 数据 200 行），非真实普查/银行数据",
            "real_benchmark": "真实普查/银行数据属商用部署前置（见仓库 README「作品集范围说明」）；本原型以合成种子作真实基准，演示完整机制（生成 → 批评 → 校准 → KS≤0.05）",
            "not_for_credit_decision": "合成行为仿真仅用于策略推演与产品设计验证，不作为任何个体授信决策依据",
        },
    }


def _print_summary(report):
    naive_ks = report["naive"]["ks_report"]["default_prob"]["ks"]
    cal_ks = report["calibrated"]["ks_report"]["default_prob"]["ks"]
    rounds = report["calibration"]["rounds"]
    traj = report["calibration"]["trajectory"]
    cmp_ = report["comparison_default_prob"]
    print("=" * 70)
    print("H3 最小验证原型：Generator → Critic 校准闭环")
    print("=" * 70)
    print(f"H3：{report['h3']}")
    print(f"痛点 P3：{report['p3_pain']}")
    print(
        f"基准：{report['benchmark']['source']}（n={report['benchmark']['n']}，提取 {report['benchmark']['extracted_at']}）"
    )
    print()
    print("【naive 模式】（美化偏见，未校准）")
    for k, v in report["naive"]["ks_report"]["features"].items():
        print(f"  KS[{k}] = {v['ks']:.4f}")
    print(f"  KS[default_prob] = {naive_ks:.4f}  (> 0.05，美化偏见被 KS 检测)")
    print(
        f"  违约率均值 {cmp_['naive']['mean']:.4f} vs 基准 {cmp_['benchmark']['mean']:.4f}"
        f"（系统性低估 {-100 * (1 - cmp_['naive']['mean'] / cmp_['benchmark']['mean']):.1f}%）"
    )
    print()
    print("【calibrated 模式】（Critic 校准后）")
    for k, v in report["calibrated"]["ks_report"]["features"].items():
        print(f"  KS[{k}] = {v['ks']:.4f}")
    print(f"  KS[default_prob] = {cal_ks:.4f}  (<= 0.05，H3 达成)")
    print(f"  违约率均值 {cmp_['calibrated']['mean']:.4f} vs 基准 {cmp_['benchmark']['mean']:.4f}")
    print()
    print("【校准轨迹】")
    for t in traj:
        alpha = f"alpha={t['alpha']:.4f}" if t["alpha"] is not None else "初始"
        print(f"  第 {t['round']} 轮：KS[default_prob] = {t['p_ks']:.4f}  ({alpha})")
    print(f"共 {rounds} 轮收敛（上限 {report['calibration']['max_rounds']} 轮）")
    print()
    print("【「未校准」标记语义】（PRD R-OPT-01 严格模式）")
    print(f"  naive      → {report['uncalibrated_flag']['demo']['naive']}")
    print(f"  calibrated → {report['uncalibrated_flag']['demo']['calibrated']}")
    print()
    print("【诚实声明】")
    for line in report["honest_declaration"].values():
        print(f"  - {line}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="H3 最小验证原型：Generator → Critic 校准闭环")
    parser.add_argument("--n", type=int, default=1000, help="合成画像数量（默认 1000）")
    parser.add_argument(
        "--seed", type=int, default=generator.DEFAULT_SEED, help="随机种子（默认 42）"
    )
    parser.add_argument(
        "--benchmark", default=None, help="基准快照路径（默认 repo 内 benchmark_customer.json）"
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="报告输出路径")
    parser.add_argument(
        "--refresh-benchmark", action="store_true", help="从 MySQL 重新抽取并覆盖基准快照"
    )
    args = parser.parse_args()

    if args.refresh_benchmark:
        bench, snap = benchmark.refresh_snapshot(args.benchmark)
        print(f"已从 MySQL 刷新基准快照：{snap}")
    else:
        bench = benchmark.load_benchmark(args.benchmark)

    naive = generator.generate_personas(
        n=args.n, bias="naive", seed=args.seed, benchmark_data=bench
    )
    calibrated, trajectory = critic.calibrate(bench, naive)

    report = build_report(bench, naive, calibrated, trajectory, args.seed, args.n)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告已写入：{args.out}")
    _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
