"""组合侧 R-UNW-03「异常估值」归因：把 38% 拆成六类可归因原因。

    读：output/avm/bias_diag/rows.json      （诊断明细，200 笔逐笔分解）
    写：output/avm/bias_diag/attribution.json（行级归因 + summary + 规则元数据）
    跑：python tools/avm/bias_attribution.py [--rows PATH] [--out PATH] [--threshold 0.30]

产物被 .gitignore（output/ 整目录忽略，有意为之：训练/诊断产物不入库）。
本脚本入库，任何人克隆后重跑一次即可重建产物——P7 页面缺文件时的降级提示
应当直接给出上面这条命令，而不是只说「分析产物缺失」。

═══════════════════════════════════════════════════════════════════════════
一、这个分析在回答什么
═══════════════════════════════════════════════════════════════════════════
dws_risk_class 200 笔里 38% 被 R-UNW-03 标为「异常估值」（|AVM − true_market_price|
/ true_market_price > 30%）。本脚本的结论是：**这 38% 里 86.8% 归因于基准侧，
真实模型误差只造成 1/200 笔异常**，因此不应该对 true_market_price 做偏差校正层
（2026-08-05 已据此判定放弃 tools/avm/calibrate.py）。

根因：true_market_price 由 **08-04 版本的 AVM 模型自己**生成
（见 seed/generate_seed.py:137 的 CITY_UNIT_PRICE 注释），当前模型是 r4。
所以 R-UNW-03 实际测的是「模型 vs 它自己的旧快照」= 版本漂移，不是估值精度。

═══════════════════════════════════════════════════════════════════════════
二、三分量分解（log 域可加）
═══════════════════════════════════════════════════════════════════════════
    logdev = log(AVM估值 / true_market_price) = A1 + A2 + B

    A1  该城 A 的中位数        —— 城市级系统性错位（冻结基准 vs 当前模型）
    A2  A − A1                 —— 模型对面积/房龄/坐标的个体响应（真实模型误差）
    B   −log(u)                —— seed 合成噪声，与模型完全无关
    u   true_market_price / (CITY_UNIT_PRICE[city] × area)，设计为 U(0.75, 1.35)
    A   log(AVM估值 / (CITY_UNIT_PRICE[city] × area))

恒等式已校验到机器精度：max|logdev − (A1+A2+B)| = 2.2e-16，max|B + log(u)| = 1.5e-16。
方差贡献（总 0.1108）：A1 0.0753 (68%) / B 0.0295 (27%) / A2 0.0246 (22%)。
反事实异常率：实际 38.0% → 消 A1 后 15.5% → 纯噪声地板 6.5%；A2 单独仅 3.0%。

═══════════════════════════════════════════════════════════════════════════
三、⚠ 六类归因的判据是【反事实】，不是「分量占 |logdev| 的份额」
═══════════════════════════════════════════════════════════════════════════
这是最容易被重新实现错的地方（P7 侧已经踩过一次：用份额法凑出 ②27/①18/③9/
④11/⑥10/⑤1，与真值差 8 笔）。**不要用份额法。**

对每笔异常行，问三个是非题——把某个分量置零后，它还越过阈值吗：

    ab(x)  = |exp(x) − 1| > 0.30        # R-UNW-03 阈值，在 log 域判定
    fix_a1 = not ab(A2 + B)             # 消掉城市系统性错位 → 变正常？
    fix_b  = not ab(A1 + A2)            # 消掉 seed 合成噪声 → 变正常？
    fix_a2 = not ab(A1 + B)             # 消掉模型内响应     → 变正常？

分支顺序**不可交换**（⑤ 只在 fix_a1 与 fix_b 都不成立时才可能命中）：

    fix_a1 and not fix_b  → ① if city in {zs, zh} else ②
    fix_b  and not fix_a1 → ③
    fix_a1 and fix_b      → ④
    fix_a2                → ⑤
    else                  → ⑥

两种方法在**接近阈值**的行上必然分歧：某分量份额虽小，但只要拿掉它就跨回 0.30
以内，它就是这笔异常的充分原因。份额法会把这类行判给份额最大的分量，从而系统性
高估 ①②、低估 ③④。

① vs ② 的区分**不是数值判据，份额法永远推不出来**——它来自一个外部事实：
基准冻结在 08-04，彼时 dg/yf 已经是全局回退状态（基准本身就是回退价），而
zs/zh 当时仍有本地训练样本（基准是真实局部价），之后才被清洗整城剔除。
所以只有 zs/zh 算「基准冻结后才失效」。**剔城本身不产生偏差；剔城发生在基准
冻结之后才产生偏差。**——这正是东莞反例的解释（见下）。

═══════════════════════════════════════════════════════════════════════════
四、东莞反例，与两个容易混淆的「隐含单价」口径
═══════════════════════════════════════════════════════════════════════════
被整城剔除的 zs/zh/dg/yf 拿到的是**完全相同**的全局中位回退，模型侧输出几乎
一模一样，异常率却是 90% / 90% / 20% / 0%。差异 100% 来自冻结基准那个数
（zs 12809、zh 12939 是本地价；dg 7939、yf 7424 本身就是回退价）。
东莞不是例外，它是「偏差测的是基准新鲜度而非模型精度」的直接证据。

⚠ 报告里出现过两个都叫「模型隐含单价」的数，**口径不同，并排展示必须声明**：

    (i)  实际口径：median(est / area)，取该城实际 200 笔中的 ~10 笔
         → zs 7157 / zh 7379 / dg 7156 / yf 7164
    (ii) 探针点口径：城中心坐标 + 100㎡ + 10 年房龄 + 未知小区下的单价
         → zs 7687 / zh 7451 / dg 8053 / yf 7516
         seed 的 CITY_UNIT_PRICE 冻结表用的是这个口径（但基于 08-04 版模型）

两者最多差 11%（dg 7156 vs 8053）。面试官逐列核对时这是个坑。
**结论方向不受影响**：两个口径下四城都紧贴 global 编码 exp(8.8378) ≈ 6890，
「模型侧对这四城输出无差别」这一点在任一口径下都成立，甚至更清楚。

═══════════════════════════════════════════════════════════════════════════
五、LTV 反馈环（勿误读为因果）
═══════════════════════════════════════════════════════════════════════════
异常率随五级分类恶化单调上升（21%→86%）**不是**「模型对坏客户估不准」。
seed 里 balance = true_market × U(.4,.9) × U(.3,1.0)，而 risk_engine.py:59,76
又用 ltv = balance / AVM 重算五级分类——同一个量被读了两遍。
铁证：纯随机噪声 B（构造上与贷款质量独立）的中位数跨分类也单调 −0.021 → −0.206。
"""

from __future__ import annotations

import argparse
import json
import math
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ROWS = os.path.join(REPO_ROOT, "output", "avm", "bias_diag", "rows.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "output", "avm", "bias_diag", "attribution.json")

# R-UNW-03 阈值，与 tools/risk/config.VALUATION_DEVIATION_THRESHOLD 同值
DEFAULT_THRESHOLD = 0.30

# 当前被整城剔除的四城；其中 dg/yf 在 08-04 基准冻结时点就已是回退状态（见文件头第三节）
EXCLUDED_NOW = {"zs", "zh", "dg", "yf"}
EXCLUDED_AT_FREEZE = {"dg", "yf"}
EXCLUDED_AFTER_FREEZE = EXCLUDED_NOW - EXCLUDED_AT_FREEZE  # {zs, zh} → 归入类别 ①

CATS = {
    "normal": "正常（未触发 R-UNW-03）",
    "c1_freeze_excluded": "① 基准冻结·剔城后失效 (zs/zh)",
    "c2_freeze_drift": "② 基准冻结·模型重训漂移",
    "c3_seed_noise": "③ 纯 seed 合成噪声 u",
    "c4_both": "④ 基准错位 + 噪声 叠加（任一消掉即正常）",
    "c5_model": "⑤ 模型内响应(面积/房龄/坐标)",
    "c6_multi": "⑥ 多因叠加，单消任一分量都不够",
}
# 归因到「基准侧」的类别：这四类之和 / 异常数 = 86.8%，是「不该做校正层」的核心依据
BASELINE_SIDE = ("c1_freeze_excluded", "c2_freeze_drift", "c3_seed_noise", "c4_both")

_NUM_FIELDS = ("A", "A1", "A2", "B", "logdev", "signed", "u", "area", "book", "est")


def ab(logratio: float, threshold: float = DEFAULT_THRESHOLD) -> bool:
    """log 域偏差是否触发 R-UNW-03：|exp(x) − 1| > threshold。"""
    return abs(math.exp(logratio) - 1.0) > threshold


def classify(row: dict, threshold: float = DEFAULT_THRESHOLD) -> tuple[str, dict]:
    """反事实归因：见文件头第三节。返回 (类别键, 三个反事实标志)。"""
    a1, a2, b, logdev = row["A1"], row["A2"], row["B"], row["logdev"]
    if not ab(logdev, threshold):
        return "normal", {"fix_a1": None, "fix_b": None, "fix_a2": None}

    fix_a1 = not ab(a2 + b, threshold)  # 消掉城市系统性错位
    fix_b = not ab(a1 + a2, threshold)  # 消掉 seed 合成噪声
    fix_a2 = not ab(a1 + b, threshold)  # 消掉模型内响应
    flags = {"fix_a1": fix_a1, "fix_b": fix_b, "fix_a2": fix_a2}

    # 分支顺序不可交换：⑤ 只在 fix_a1 与 fix_b 均不成立时才可能命中
    if fix_a1 and not fix_b:
        cat = "c1_freeze_excluded" if row["code"] in EXCLUDED_AFTER_FREEZE else "c2_freeze_drift"
    elif fix_b and not fix_a1:
        cat = "c3_seed_noise"
    elif fix_a1 and fix_b:
        cat = "c4_both"
    elif fix_a2:
        cat = "c5_model"
    else:
        cat = "c6_multi"
    return cat, flags


def build(rows: list[dict], threshold: float = DEFAULT_THRESHOLD) -> dict:
    """把 rows.json 的明细转成 attribution.json 的完整 payload。"""
    for r in rows:
        for k in _NUM_FIELDS:
            r[k] = float(r[k])

    out_rows, counts = [], dict.fromkeys(CATS, 0)
    for r in rows:
        cat, flags = classify(r, threshold)
        counts[cat] += 1
        out_rows.append(
            {
                "loan_id": r["loan_id"],
                "collateral_id": r["cid"],
                "city_code": r["code"],
                "city": r["city"],
                "cat": cat,
                "cat_label": CATS[cat],
                "abnormal": ab(r["logdev"], threshold),
                "signed_deviation": round(r["signed"], 6),
                "A1": round(r["A1"], 6),
                "A2": round(r["A2"], 6),
                "B": round(r["B"], 6),
                "logdev": round(r["logdev"], 6),
                "u": round(r["u"], 6),
                **flags,
            }
        )

    n = len(out_rows)
    n_ab = sum(1 for r in out_rows if r["abnormal"])
    summary = [
        {
            "cat": k,
            "cat_label": CATS[k],
            "n": counts[k],
            "pct_of_all": round(counts[k] / n, 4) if n else 0.0,
            "pct_of_abnormal": (round(counts[k] / n_ab, 4) if (n_ab and k != "normal") else None),
            "cities": sorted({r["city_code"] for r in out_rows if r["cat"] == k}),
        }
        for k in CATS
    ]
    summary.sort(key=lambda x: (x["cat"] == "normal", -x["n"]))
    base_n = sum(counts[k] for k in BASELINE_SIDE)

    return {
        "generated_by": "tools/avm/bias_attribution.py",
        "source": os.path.relpath(DEFAULT_ROWS, REPO_ROOT),
        "model_version": "2026-08-05-r4",
        "threshold": threshold,
        "n_total": n,
        "n_abnormal": n_ab,
        "abnormal_rate": round(n_ab / n, 4) if n else 0.0,
        "baseline_side_n": base_n,
        "baseline_side_pct_of_abnormal": round(base_n / n_ab, 4) if n_ab else None,
        "rule": {
            "kind": "counterfactual",
            "note": "不是分量占 |logdev| 的份额，而是「把该分量置零后是否仍越过阈值」",
            "ab": f"|exp(logratio) - 1| > {threshold}",
            "fix_a1": "not ab(A2 + B)",
            "fix_b": "not ab(A1 + A2)",
            "fix_a2": "not ab(A1 + B)",
            "branch_order": [
                "fix_a1 and not fix_b -> c1 if city in {zs,zh} else c2",
                "fix_b and not fix_a1 -> c3",
                "fix_a1 and fix_b     -> c4",
                "fix_a2               -> c5",
                "else                 -> c6",
            ],
            "c1_vs_c2": "非数值判据：基准冻结于 08-04，彼时 dg/yf 已回退、zs/zh 仍有本地样本",
        },
        "identities": {
            "logdev": "A1 + A2 + B = log(est / true_market_price)",
            "B": "-log(u)",
            "u": "true_market_price / (CITY_UNIT_PRICE[city] * area)",
            "note": "已校验到机器精度：max|logdev-(A1+A2+B)|=2.2e-16, max|B+log(u)|=1.5e-16",
        },
        "unit_price_caveat": {
            "actual_median": {"zs": 7157, "zh": 7379, "dg": 7156, "yf": 7164},
            "probe_point": {"zs": 7687, "zh": 7451, "dg": 8053, "yf": 7516},
            "note": "两个口径最多差 11%(dg)。actual=median(est/area)；probe=城中心+100㎡+10年+"
            "未知小区（seed CITY_UNIT_PRICE 用此口径，但基于 08-04 版模型）。并排展示须声明。",
            "global_encoding": 6890,
        },
        "summary": summary,
        "rows": out_rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="生成组合侧异常估值六类归因产物")
    ap.add_argument("--rows", default=DEFAULT_ROWS, help="输入 rows.json 路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 attribution.json 路径")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="R-UNW-03 阈值")
    args = ap.parse_args()

    if not os.path.exists(args.rows):
        raise SystemExit(
            f"[bias_attribution] 找不到输入 {args.rows}\n"
            "  该文件是组合侧诊断明细（200 笔逐笔三分量分解），同属 output/ 忽略目录。\n"
            "  它由一次性诊断分析产出，需要连库重算；如已丢失请联系 avm-bias 或重跑诊断。"
        )
    with open(args.rows, encoding="utf-8") as f:
        rows = json.load(f)

    payload = build(rows, args.threshold)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[bias_attribution] 写入 {args.out}")
    print(
        f"  总 {payload['n_total']} 笔，异常 {payload['n_abnormal']} "
        f"({payload['abnormal_rate']:.1%})，基准侧 {payload['baseline_side_n']} 笔 "
        f"占异常 {payload['baseline_side_pct_of_abnormal']:.1%}\n"
    )
    for s in payload["summary"]:
        pct = f"{s['pct_of_abnormal']:.1%}" if s["pct_of_abnormal"] is not None else "—"
        cities = ",".join(s["cities"])
        print(f"  {s['cat']:<20}{s['cat_label']:<34}{s['n']:>4}{pct:>9}  {cities}")


if __name__ == "__main__":
    main()
