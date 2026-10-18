# AVM 自动估值模型卡（Automated Valuation Model Model Card）

> **定位**：本文件是 AVM（自动估值）的版本台账与模型卡，回答可追溯性契约 Q5（出问题退回已知良好状态）。可追溯性的唯一入口是 [`../TRACEABILITY.md`](../TRACEABILITY.md)；相关决策见 [`../adr/ADR-0002-avm-selection-convergence.md`](../adr/ADR-0002-avm-selection-convergence.md)。
>
> **兄弟台账**：[LLM 台账](LLM_REGISTRY.md) · [LLM 回归用例集](LLM_EVAL_CASES.md)
>
> **权威来源**：[`docs/tech/components/avm.md`](../tech/components/avm.md)（组件技术说明，含完整版本历史与局限）；本卡为其收口摘要，数值一律回溯到该文件或 [`tools/avm/README.md`](../../tools/avm/README.md)。
>
> **证据分级**（见 TRACEABILITY §1）：本卡的 AVM 指标均为 **文档记录（recorded）**——来自历史运行报告，训练产物 `output/avm/` 未随仓库分发（`output/` 被 `.gitignore` 排除，`.gitignore:98`），**不能**仅凭仓库文件复算出这些数字。

---

## 血缘（Lineage）

- 训练/推理代码指纹记录在 [`MANIFEST.json`](MANIFEST.json)（`tools/avm/train.py`、`tools/avm/predict.py` 的 `sha256`）。模型产物 `output/avm/model.joblib`（约 22MB）不随仓库分发，故以「代码指纹 + 本卡数值」绑定**版本 ↔ 配置 ↔ 代码**血缘，便于离线核对。
- 自检：`python tools/dev/trace_extra.py --manifests` 校验上述指纹未漂移。
- 注意：该血缘**不**等同训练数据快照哈希——训练数据来自 `spacefin_crawler.crawl_housing_sale`（DWD），当前不随仓库分发。

## 用途与边界

AVM 用于替代风险引擎中「(城市, 小区) 中位单价 × 面积」的粗糙估值（`tools/risk/valuation.py` 口径），用机器学习模型估计单位面积价格（元/㎡）再乘面积还原总价（元）。

- **是**：离线训练的估价模型，供风险引擎（`tools/risk/`）调用，输出抵押物估值总价（元）与可比案例支撑度（置信分）；置信分与风险侧 AC-04 `low_confidence` 机制衔接，可比案例不足时主动弃权转人工。
- **不是**：不是实时/流式独立模型。`ads_stream_ltv_alerts` 复用同一 `output/avm/model.joblib`，其 `model_version` 即当前落盘的 r2；「实时」指增量计算链路，**不是独立模型版本**（avm.md 开头版本声明）。
- **可追溯**：每次训练生成 `version`（形如 `YYYY-MM-DD-rN`），随模型产物落盘，风险引擎 `valuation.model_version()` 可追踪「哪一版模型在跑」。

## 输入与特征

- **输入**：单笔或批量抵押物规格（`city_code` / `community` / `area_sqm` / `building_age` / `bedrooms` / `latitude` / `longitude` / `hall` / `bath` / `floor_level` / `floor_total` / `direction` / `parking`）。
- **必填约束**：`hall`/`bath`/`floor_level`/`floor_total`/`parking` 缺失时单笔接口 `estimate_total_price` **显式抛 `ValueError`**（不允许置 NaN 占位，防与训练分布错位）；批量接口 `batch_estimate_total_price` 对不可估值笔静默跳过返回 `None`，由调用方回退（`tools/avm/predict.py:293-310`、`tools/avm/predict.py:199-222`）。
- **28 维特征**（`FEATURE_NAMES`，训练 `tools/avm/train.py:404-434` 与推理 `tools/avm/predict.py:39-69` 同顺序）：
  1. **基础属性（16 维）**：`area`/`log_area`/`bed`/`hall`/`bath`/`rooms`/`area_per_bed`/`age`/`floor_level`/`floor_total`/`floor_ratio`/`direction`/`parking`/`city_code`/`lat`/`lng`；
  2. **目标编码（7 维，防泄漏）**：`comm_mean`/`comm_median`/`comm_n`/`comm_minus_city`/`city_mean`/`city_median`/`city_n`；
  3. **空间特征（GWR-lite 近似，5 维）**：`nn_med_k3`/`nn_med_k8`/`nn_med_k20`/`nn_med_k50`（训练坐标 k 近邻中位单价）+ `nn_dist`（最近邻距离）。
- **防泄漏口径**：小区/城市目标编码只用**训练折内**统计（5 折 OOF，`tools/avm/train.py:764-775`）；测试集用完整训练集字典映射；新小区回退 小区中位 → 城市中位 → 全局中位。特征严禁使用 `unit_price_yuan`/`total_price_wan` 及其直接派生。
- **目标变量**：`log(unit_price_yuan)`（选 log 单价而非直接预测总价：房价误差天然乘性，log 目标把乘性误差变加性，与 MAPE 口径一致）。
- **小区编码收缩**：`smooth_mode=eb`（按城市估 `k=σ²组内/τ²组间`，`tools/avm/train.py:233-265`），训练与推理同公式（predict 读 artifact 的 `smooth_k`/`smooth_mode`/`eb_k`）。实测 17 城 k 全部撞下限 0.5（少收缩）。

## 训练与评估口径

- **数据源**：`spacefin_crawler.crawl_housing_sale`（sale DWD）。
- **数据量（2026-08-20-r2 口径快照，avm.md §2）**：n_raw **36,652** 行 → 外市清洗后 **34,104** 行（剔除 2,548 行）→ 字段缺失/总价一致性/分位数截尾后 **33,410** 行（`rows_after_clean`）。
- **切分**：train/test = 80/20，`seed=42`，`n_train` **26,728** / `n_test` **6,682**。
- **估计器**：`HistGradientBoostingRegressor`（`tools/avm/train.py:798-811` 确认类名；`version` 随 artifact 落盘见 `tools/avm/train.py:898`）。
- **canonical 超参**：`loss=quantile(0.45)`；`learning_rate=0.03`、`max_leaf_nodes=150`、`min_samples_leaf=12`、`l2_regularization=2.0`、`max_iter=2000`、`early_stopping`（`train.py` 默认值）。
- **指标口径**：商用 AVM 报「精度 @ 覆盖率」——可比案例充足的估值放行、不足的主动弃权转人工（对应 AC-04 `low_confidence`）。置信度 = 可比案例支撑度：`score = log1p(cnt±2%) + 0.5·log1p(cnt±5%)`（同 城市/小区/房型 且面积落在 ±X% 区间；无小区或无可比案例 → 0 分弃权）。
- **外部清洗**：四层规则（坐标围栏 / URL 子域城市 / 文字标记 / 城市价格上下限），`tools/avm/data_clean.py`；仅活在 `clean_rows_with_stats`——绕过它会重新引入外市污染（avm.md §3 ⚠️）。
  - 分项（r2 快照）：围栏 23 / URL 2,147 / 标记 20 / 价格 358，合计 2,548。
  - 关键事实：zs/yf/zh/dg 四城标签数据 100% 为外市错标（URL 子域证实），已整城剔除。

## 当前版本与指标

**当前已发布模型 = `2026-08-20-r2`（官方生产基准，canonical 超参 `HistGBR + quantile(0.45) + 小区编码 EB 收缩`，28 特征）**。指标如下（来源：avm.md §2/§5、`tools/avm/README.md` 指标表，`output/avm/avm_report.json` 的 `version` 字段记录版本）：

| 指标 | r2（当前发布） | 来源 |
| --- | --- | --- |
| 全量 MAPE | **15.00%** | avm.md §2 / README 指标表 |
| 45% 覆盖 MAPE（AC-07 口径） | **9.94%** ≤ 10% | avm.md §2 / README 指标表 |
| MdAPE（100% 覆盖） | **9.90%** | avm.md §5 |
| R² | **0.855** | avm.md §2 |
| 基线（(城市,小区)中位 × 面积）MAPE | **20.58%**（相对提升 27.1%） | avm.md §2 / README 指标表 |
| n_test | 6,682 | avm.md §2 |

**r2 误差分解**（avm.md §5）：有小区+有坐标 **88.1%**（n=5,884，MAPE **13.86%**）、有小区无坐标 **0%**、无小区 **11.9%**（n=798，MAPE **23.42%**）；分城市 top：zh 27.94（n=41）、gz 27.87（n=295）、sz 23.76（n=311），其余城市均 < 18%。

> 注意区分两个接近 9.9% 的数：**45% 覆盖率的 MAPE（9.88%/9.94%）** 与 **100% 覆盖率的 MdAPE（9.90%）** 是不同口径，勿混（avm.md §5 原话）。

## 版本历史

> `version` 由 `train.py make_version()` 生成（`YYYY-MM-DD-rN`，同日重训递增 `rN`，`tools/avm/train.py:367-385`），随 `model.joblib` 与 `avm_report.json` 落盘。下表所有指标均为 **文档记录（recorded）**：产品未随仓库分发。

| 版本号 | 日期 | 指标（45%覆盖 MAPE / 全量 MAPE / 基线） | 用途 | 状态 | 取证来源 |
| --- | --- | --- | --- | --- | --- |
| `2026-08-20-r2` | 2026-08-20 | 9.94% / **15.00%** / 20.58% | 当前生产基准（全量数据清洗补全后重训，canonical 超参） | **现行 · 已发布** | 文档记录：`avm.md` §2/§5、`tools/avm/README.md` 指标表 |
| `2026-08-05-r11` | 2026-08-05 | **9.88%** / 14.59% / 20.64% | AC-07 原验收里程碑（r11 历史 canonical，保留作历史对比） | 历史 | 文档记录：`avm.md` §7 验收记录（`trained_at` 2026-08-05 13:58:34） |
| `2026-08-06-r20` | 2026-08-06 | 未记录 / 未记录 / 未记录 | 演示 D4 事件（广州挂牌价下探）驱动的重训快照，仅用于演示事件传导（r17→r20） | **已废弃**（历史上曾误覆盖产物导致精度退化） | 文档记录：`avm.md` 开头版本声明 |
| `2026-08-04 版（r4）` | 未记录（仅记为「08-04 版」） | 未记录（全量 15.51% / 基线 21.50%） | 历史版本，仅保留作对比 | 历史 | 文档记录：`tools/avm/README.md` 指标表、`avm.md` §9.2 |

> **r11 覆盖率曲线**（历史 canonical）：45% 覆盖 MAPE 9.88%（MdAPE 6.885%，n=3,618）；覆盖率 30–45% 区间全部 ≤ 10%（30%→9.88 / 35%→9.90 / 40%→9.93 / 45%→9.88）；置信分层（测试集）high 764 / mid 3,381 / low 3,896。来源：`avm.md` §5/§7。
>
> **r11 误差分解**（按位置信号完整度，历史口径，avm.md §5）：有小区+有坐标 33.2%/14.52%/MdAPE 9.64%；有小区无坐标 56.6%/**13.16%**/8.69%；无小区 10.1%/**22.78%**/16.22%。有小区无坐标段反而最好——小区标签才是关键特征，坐标是辅助。
>
> **r4 → r11 → r2 的说明**：README 记载 S6 新增 URL 子域清洗后 MAPE 15.94% → 15.51%、MdAPE 10.58 → 10.66、R² 0.827 → 0.834。`avm.md` §5 明确 r2 相对 r11 的差异（全量 MAPE 由 14.59% 变为 15.00%，提升幅度由约 29% 收敛至 27.1%）源于训练/测试集规模变化。

## 已知局限

1. **挂牌价非成交价——噪声决定误差下界（最硬约束）**：标签是 asking price，同栋同户型因急售/装修/议价可挂出 2 倍价差。留一法噪声下界 **≈12%**（同城同小区同房型面积 ±2% 下界 **9.40%**，完全相同 **7.68%**）；**oracle 下界 12.65%**（含测试集小区真实中位价的作弊上界，模型不可达），与之互相印证。**全量 MAPE 天花板 ≈12%**，接入网签/评估价作标签才能下压数个点。（avm.md §8）
2. **超参与置信函数在测试集上迭代过（诚实标注）**：quantile/EB 超参与置信函数选择在测试集上做过迭代（r8→r11），严格做法应在验证折上选；覆盖率曲线的最终数仍为测试集评估。（avm.md §5 ⚠️）
3. **坐标覆盖历史**：早期 r11 受腾讯 geocoder 每日 6000 配额限制仅 **~32.7%**；当前 2026-08-20-r2 已 **100%** 覆盖（`coord_rows_pct=100.0`，本轮 `n_backfilled=0`）。但消融实测坐标**不是精度杠杆**：`--drop-coords` 把覆盖率从 33.4% 打到 0%，MAPE 仅 15.508% → 15.858%（+0.35pp）；线性外推到 100% 覆盖最多再降 ~0.7pp（→14.8%）。补坐标是数据完整性/前端地图工作，不是达标手段。（avm.md §8/§9.1）
4. **DWD 最小样本门槛**：`train.py --min-train-samples` 默认 **100**（`MIN_TRAIN_SAMPLES=100`，`tools/avm/train.py:364`）——清洗后样本低于该值直接 `exit 3` 并提示「样本不足，降级人工」，不再因 `train_test_split` 样本过少崩溃（B-06 缺陷修复）。
5. **无小区段是结构性下界**：title 纯营销文本解析不出楼盘名（r11 权重 10.1%、MAPE 22.78%），无任何位置信号，只能靠城市中位 + 属性估计。
6. **gz/sz 区间价差极大**：核心区（天河 5–10 万/㎡）与郊区（增城/南沙 0.8–1.5 万/㎡）同在一个城市中位里，即便有坐标，gz/sz 仍是分城市 MAPE 最高的两城（27.87 / 23.76）。
7. **小区名仍是稀疏标签**：归一后仍有多数小区仅出现 1 次，OOF 下回退城市中位，无法给新小区提供位置信号。
8. **zs/yf/zh/dg 四城无本地数据**：模型对四城估值退化为全局中位回退，需爬虫提供真实本地房源后才能可靠估值。

## 证据与复跑

**重新生成模型（训练 + 评估，读 DWD）：**

```bash
PY=tools/orchestrator/.venv/bin/python

# canonical 配置（quantile(0.45) + EB 收缩）
$PY tools/avm/train.py --out-dir output/avm --loss quantile --quantile 0.45 --smooth-mode eb

# 样本门槛（清洗后样本 < 100 时 exit 3，降级人工）
$PY tools/avm/train.py --min-train-samples 500

# 消融：丢弃全部坐标，量化空间特征边际贡献
$PY tools/avm/train.py --drop-coords
```

**产物路径（均在 `output/avm/`，已被 `.gitignore:98` 排除）：**

| 产物 | 内容 |
| --- | --- |
| `output/avm/model.joblib` | 模型 + 编码字典（encoders/eb_k/nn）+ cities + 特征元数据 + `smooth_k`/`smooth_mode` + **`version`** |
| `output/avm/avm_report.json` | 指标、清洗统计（含分城市明细）、误差分解、特征缺失率、置信分层与覆盖率曲线、`version` |
| `output/avm/attribution_report.json` | 特征归因报告（C-02，permutation importance，非 SHAP） |

**可复现性声明（诚实）**：`output/` 整目录被 `.gitignore` 排除，训练产物**不随仓库分发**。因此上表中的 r2/r11/r4 指标**无法仅凭本仓库复算**——它们是历史运行报告的 **文档记录（recorded）**，不是可由仓库文件直接复算的 `verified` 指标。要复核当前值，须在具备 DWD 数据（`spacefin_crawler.crawl_housing_sale`）与 `.env` DB 凭据的环境中按上述命令重训，并读取新生成的 `avm_report.json`。CI 会把重算的证据快照作为 artifact 归档（见 TRACEABILITY §3）。

**相关证据条目**（`docs/evidence/registry.json`，见 TRACEABILITY §2）：`avm_mape_at_45pct_r2`、`avm_mape_full_r2`、`avm_baseline_r2`、`avm_mape_at_45pct_r11`、`avm_error_floor_oracle`。
