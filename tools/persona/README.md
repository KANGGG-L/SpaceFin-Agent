# SpaceFin 合成行为仿真 H3 最小验证原型（Generator → Critic 校准闭环）

对应 PRD 假设 **H3**：合成画像能如实反映真实经济状态下的不良率分布，不被美化偏见
扭曲（验证标准：违约率分布 KS ≤ 0.05）。痛点 **P3**：LLM 生成画像被 RLHF 美化偏见
污染（乐观化），违约低估。本原型以**合成种子客户分布作为真实基准**，演示完整机制：
**生成 → 批评 → 校准 → KS≤0.05**。

> **诚实声明**：本原型的「真实基准」= `spacefin.customer` 的合成 seed 数据
> （200 行，确定性生成），**不是**真实普查/银行数据。真实基准属商用部署前置
> （见根 README「项目性质与范围说明」）。原型证明的是**机制可行**（美化偏见可被 KS
> 检测、可被 Critic 校准闭环拉回），不是真实世界违约率水平。

## 定位

- **零重依赖**：仅 numpy + scipy。快照 `benchmark_customer.json` 已提交进 repo，
  业务运行（main / 测试）不依赖 MySQL。
- **确定性**：固定 random seed（默认 42），同参数两次运行结果完全一致。
- **机制透明**：每条链路的公式 / 变换 / 校准机制都在代码里固定并注释。

## 模块与机制

| 模块           | 职责                                                                                                                                                                                                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `benchmark.py` | 基准分布：从 MySQL 抽取 `spacefin.customer` 三特征（income_monthly / debt_ratio / credit_score，200 行）并落盘快照；派生违约概率公式固定在此（logistic，违约率随 debt_ratio 升、随 credit_score 降）。`load_benchmark()` 离线加载，`extract_from_mysql()` 仅用于刷新快照 |
| `generator.py` | 合成画像生成器：固定 seed、自助采样基准 200 行。`bias=naive`（收入 ×1.2、负债率 ×0.8，乐观画像，违约率系统性压低）/ `bias=realistic`（贴近基准）                                                                                                                         |
| `critic.py`    | 批评者：`ks_report()` 对三特征 + 违约概率各算 `ks_2samp`；`calibrate()` 校准闭环（秩分位数映射，混合系数 alpha=1-2^-r 逐轮逼近基准边际，迭代至违约率 KS ≤ 0.05 或最多 20 轮）。校准**只作用于特征分布**，违约概率一律由固定公式重算                                      |
| `main.py`      | CLI：产出 `output/persona/persona_report.json` + 人读摘要                                                                                                                                                                                                                |

派生违约概率公式（`benchmark.py` 固定）：

```
p_default = sigmoid(b0 + b1*debt_ratio + b2*(credit_score - score_center))
b0 = -2.0, b1 = 3.0, b2 = -0.006, score_center = 680.0
```

在基准 200 行上违约率均值约 0.36、p10-p90 约 0.17-0.56，分布有足够区分度。

## 用法

```bash
# 运行报告（离线加载基准快照，零 DB）
python tools/persona/main.py                 # 默认 --n 1000 --seed 42
python tools/persona/main.py --n 2000 --seed 7
python tools/persona/main.py --refresh-benchmark   # 重新从 MySQL 抽取并覆盖快照

# 测试（零 DB，18 例）
python -m pytest tools/persona/tests -v
```

产物：`output/persona/persona_report.json`（`output/` 已被 .gitignore）。

## 结果（seed=42，n=1000）

| 模式                        | 违约率分布 KS      | 违约率均值                     | 结论                 |
| --------------------------- | ------------------ | ------------------------------ | -------------------- |
| naive（美化偏见，未校准）   | **0.257** (> 0.05) | 0.296（基准 0.356，低估 ~17%） | 美化偏见可被 KS 检测 |
| calibrated（Critic 校准后） | **0.028** (≤ 0.05) | 0.354                          | **H3 达成**          |

校准轨迹（每轮违约率 KS，3 轮收敛，上限 20 轮）：

```
第 0 轮（初始） 0.257
第 1 轮 alpha=0.50  0.158
第 2 轮 alpha=0.75  0.056
第 3 轮 alpha=0.875 0.028  <= 0.05 达标
```

naive 模式各特征 KS：income 0.257 / debt_ratio 0.232 / credit_score 0.019；
calibrated 模式：income 0.018 / debt_ratio 0.040 / credit_score 0.014。

## 「未校准」标记（PRD R-OPT-01 严格模式）

Critic 严格模式强制对照真实基准校准；若输出未经校准（如 naive 画像），必须携带
**「未校准」标记**，不得直接用于决策。报告 JSON 中 `naive.calibration_status="未校准"`、
`calibrated.calibration_status="校准"`，人读摘要亦打印标记语义演示。

## 诚实声明

1. **基准来源 = 合成种子客户分布**（`spacefin.customer` seed 数据），非真实普查 /
   银行数据；真实基准属商用部署前置。
2. 本原型演示的是机制（检测 + 校准闭环），不是真实世界违约率水平。
3. 合成行为仿真仅用于策略推演与产品设计验证，不作为任何个体授信决策依据。

## 已知局限

- 基准仅 200 行合成种子，边际/联合分布都是 seed 生成器的形状，不代表真实客群；
- KS 只检验单变量边际分布，不检验特征间的联合依赖结构（H3 验证口径即违约率单变量
  KS，与 PRD 一致）；
- 真实基准（普查 / 银行数据）接入后，仅需替换 `benchmark_customer.json` 重跑
  main.py，机制不变。
