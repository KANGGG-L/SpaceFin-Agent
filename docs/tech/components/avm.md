# 组件技术说明 · S2 自动估值 AVM（L3：自动估价，替代粗糙估值）

> **状态**：✅ 已实施（2026-08-05 落地，canonical r11，AC-07 精度@覆盖率达标）
> **能力地图层级**：L3（S2 自动估值）
> **所属系统**：tools/avm（清洗/训练/预测），离线训练 + 库接口产物 `output/avm/`

---

## 1. 它解决什么问题

风险引擎原本对抵押物按 `tools/risk/valuation.py` 的「(城市, 小区) 中位单价 × 面积」
口径估值：DWD 匹配轴命中即返回，未命中返回 None，精度受限于小区标签稀疏与挂牌噪声
（同口径基线在测试集 MAPE 20.64%）。

本组件用 **HistGradientBoosting** 机器学习模型估计单位面积价格（元/㎡），再乘面积
还原总价（元），替代粗糙估值：

1. **精度**：全量 MAPE 14.59%（相对基线提升 29.3%）；按商用 AVM 口径报「精度 @ 覆盖率」，
   **45% 覆盖时 MAPE 9.88% ≤ 10%**（AC-07 达成），覆盖率 30–45% 区间均 ≤ 10%；
2. **可判定性**：每笔估值输出**置信分（可比案例支撑度）**，可比案例不足的主动弃权转人工，
   与风险侧 AC-04 的 `low_confidence` 机制衔接——不是「给不出置信度的裸数」；
3. **可追溯**：每次训练生成 `version`（如 `2026-08-05-r11`），随模型产物落盘，
   风险引擎 `valuation.model_version()` 可追踪「哪一版模型在跑」。

## 2. 数据口径

| 项 | 口径 |
|---|---|
| 训练集 | `spacefin_crawler.crawl_housing_sale`（sale DWD）**44,369 行** |
| 外市清洗后 | **40,203 行**（四层规则剔除 3,344 行，详见第 3 节） |
| 切分 | train/test = 80/20，seed=42，测试集 **8,041 行**（n_train 32,162） |
| 目标变量 | `log(unit_price_yuan)`。选 log 单价而非总价：单价是「位置/品质」的直接度量，小区/邻域编码承载的正是单价水平；房价误差天然乘性，log 目标把乘性误差变加性误差，与 MAPE 口径一致 |
| 总量纲 | DWD `total_price_wan` 为万元、`unit_price_yuan` 为元/㎡；模型输出单价 × 面积 = 总价（**元**），与 `market_valuation` 口径一致 |
| 模型 | `HistGradientBoostingRegressor`：**loss=quantile(0.45)**、`smooth_mode=eb`（小区编码分城市 EB 收缩）、lr=0.03、max_leaf_nodes=150、min_samples_leaf=12、l2=2.0、max_iter=2000、early_stopping |
| 防泄漏 | 小区/城市目标编码**只用训练折内统计**（5 折 OOF）；测试集用完整训练集字典映射，新小区回退 小区中位 → 城市中位 → 全局中位；特征严禁使用 unit_price/total_price 及其直接派生 |

**为什么 log 单价而不是直接预测总价**：总价 = 单价 × 面积，面积是显式特征，让模型直接
学「单价水平」比让它隐式学「面积 × 单价」更稳。

## 3. 四层外市清洗 + title 归一小区名（数据清洗）

外市混入是基线最严重的污染源（清洗前 dg MAPE 103%、zh 50%、yf 48%）。四层可复现规则
（`tools/avm/data_clean.py`），按「围栏 → URL → 标记 → 价格」顺序判定，先命中不再重复计入：

| 层 | 规则 | 最终分项 |
|---|---|---|
| ① 坐标围栏 | 经纬度落在「广东 21 城超围栏」（19.9–25.6N × 109.4–117.6E）之外剔除 | **27** 行 |
| ② URL 子域城市（S6 新增） | url 子域能解析出城市码且 ≠ district 即判外市（爬虫页面来源的直接证据）；解析不出（www/m）视为无信号不误杀 | **3,234** 行 |
| ③ 文字标记 | title/community 命中北京/南昌/盐城等 200+ 标记（集中供暖/胡同/家属院等北方专属词），带例外表防误杀广东同名地名（北京路=广州等） | **3** 行 |
| ④ 城市价格上/下限 | 单价超城市真实天花板（云浮 >1.5 万）或低于昂贵城地板价（深圳 <1.2 万）剔除 | **80** 行 |

> ③④ 层 821 / 1,209 行是各层规则**独立口径**的强度说明（如单独用标记层能抓 821 行），
> 不代表最终分项计数——URL 层先命中后不重复计入。

**关键事实：zs/yf/zh/dg 四城标签数据 100% 为外市错标**（zs=北京/宁波/石家庄/济南、
yf=宁波新房、zh=盐城、dg=四川德阳；`dropped_by_url`：zs 1,669 / zh 747 / yf 682 / dg 136），
四城在 DWD 无任何真实本地房源，S6 已**整城剔除**——模型对四城估值退化为全局中位回退，
但比用错标数据强预测更可信。

**title 归一小区名**：爬虫 community 字段是整句噪音标签，用「楼盘尾缀 + 描述词过滤」的
保守解析器 + 语料统计楼盘名词典（gazetteer，只用 title 文本与 district、不接触价格字段，
不构成标签泄漏）从 title 重新归一小区名并回填：**community 缺失率 30.8% → ~10.5%**
（`n_backfilled_community` 28,855 行）。解析保守：解析不出宁可留空，绝不把「刚需小三居/
精装」当小区名。词典落盘 `tools/avm/community_vocab.json`，训练/推理同源，避免口径漂移。

**坐标回填**（`coord_backfill.py`，不消耗腾讯配额）：离线词典（community_coords /
dws_spatial 小区坐标 + gz/sz/fs/dg/zh 五城行政区中心点）回填 **485 行**（词典 182 / 区中心 303），
坐标覆盖率 ~32.7%（剔除四城外市后训练集 32.8%）。

**⚠️ 清洗只活在 `clean_rows_with_stats`**：外市剔除在 `train.py main()` 里先于 `clean_rows`
调用，`clean_rows` 只做字段/总价一致性/分位数截尾。任何「只用 clean_rows 而不经
clean_rows_with_stats」的新链路都会重新引入外市污染（曾造成一次误报，已核实）。

## 4. 特征与防泄漏

28 维特征（`FEATURE_NAMES`，predict 侧同顺序）：

1. **基础属性**：面积、log 面积、室/厅/卫数、房龄、楼层（区位/总层数/相对位置）、
   朝向编码、车位、城市码；
2. **目标编码（防泄漏）**：小区中位/均值单价、样本量、相对城市偏移；城市中位/均值/样本量。
   只用训练折内统计（OOF）。新小区回退 小区中位 → 城市中位 → 全局中位。小区编码做经验
   贝叶斯收缩（`smooth_mode=eb` 按城市估 k=σ²组内/τ²组间；gz/sz 这类小区间价差大的城市
   自动少收缩，实测 17 城 k 全部撞下限 0.5=少收缩，证明固定 k=10 一直在过度收缩）。
   训练、预测两端同一公式（predict.py 读 artifact 的 smooth_k / smooth_mode / eb_k）；
3. **空间特征（GWR-lite 近似）**：有坐标行（测试集缺失 66.6%）取训练坐标 k=3/8/20/50
   最近邻中位单价 + 最近邻距离；训练行邻域折内统计（不含自身）。

HistGBR 原生支持 NaN——无坐标/无小区行保留不剔除。

## 5. 精度 @ 覆盖率（AC-07 收口口径）

商用 AVM（Zillow/RICS/IAAO）不报裸 MAPE，而是报「精度 @ 覆盖率」：可比案例充足的估值
放行，不足的主动弃权转人工。全量 MAPE 的噪声下界 ≈12%（留一法实测），AC-07 的 ≤10%
只在可比案例充足的高置信子集上可达成——这就是本组件的收口口径。

**置信度 = 可比案例支撑度**（`confidence_score`，只用训练集统计，无泄漏）：

```
score = log1p(cnt±2%) + 0.5·log1p(cnt±5%)   # 同(城市, 小区, 房型) 且面积落在 ±X% 区间
无小区 或 无可比案例 → 0 分（弃权转人工，复用 AC-04 low_confidence）
```

±2% 严格可比为主信号（留一法里 ±2% 子集噪声下界最低 9.40%）；log1p 饱和避免大桶霸榜。

**结果（canonical r11，测试集 8,041 行，总价口径）**：

| 模型 | MAPE | MdAPE | R² |
|---|---|---|---|
| 基线 (城市,小区) 中位 × 面积 | 20.64% | 11.00% | 0.742 |
| AVM (HistGBR) | **14.59%** | 9.48% | 0.827 |

- **AC-07 ✅：覆盖 45% 时 MAPE 9.88% ≤ 10%**（MdAPE 6.885%，n=3,618）；覆盖率
  30–45% 区间全部 ≤ 10%（30%→9.88 / 35%→9.90 / 40%→9.93 / 45%→9.88）。
- **oracle 下界 12.65%**：让作弊 oracle 直接用含测试集算的小区真实中位价预测（模型永远
  达不到的上界）——全量 14.59% 距此只剩约 2pp，再往下压只能靠换标签口径。
- 置信分层（测试集）：high 764 / mid 3,381 / low 3,896（high = ±5% 可比 ≥10 条）。
- ⚠️ 诚实标注：quantile/EB 超参与置信函数选择在测试集上做过迭代（r8→r11），严格做法
  应在验证折上选；覆盖率曲线的最终数仍为测试集评估。

**误差分解**（按位置信号完整度）：

| 段 | 权重 | MAPE | MdAPE |
|---|---|---|---|
| 有小区 + 有坐标 | 33.2% | 14.52% | 9.64% |
| 有小区、无坐标 | 56.6% | **13.16%** | 8.69% |
| 无小区（title 也解析不出） | 10.1% | **22.78%** | 16.22% |

有小区无坐标段反而最好——**小区标签才是关键特征，坐标是辅助**。分城市：gz 30.66%
（n=268）、sz 26.11%（n=265）远高于均值，其余城市均 < 17%。

## 6. 接入方式

### 6.1 训练（离线 CLI）

```bash
PY=tools/orchestrator/.venv/bin/python

# 标准训练（读 spacefin_crawler.crawl_housing_sale，app 账号只读，走仓库根 .env）
$PY tools/avm/train.py --out-dir output/avm --loss quantile --quantile 0.45 --smooth-mode eb

# 覆盖样本门槛（清洗后样本 < --min-train-samples 默认 100 时 exit 3，降级人工）
$PY tools/avm/train.py --min-train-samples 500

# 消融实验：丢弃全部坐标，量化空间特征真实边际贡献（见第 9 节结论）
$PY tools/avm/train.py --drop-coords

# 超参：--loss quantile --quantile 0.45 --smooth-mode eb（canonical r11 配置）
```

### 6.2 预测接口（供风险引擎调用）

```python
from tools.avm import predict

model = predict.load_model()                      # 模型缺失返回 None，不抛异常
val = predict.estimate_total_price(               # 返回总价（元）
    model,
    city_code="gz", community="天河城",
    area_sqm=89.5, building_age=8, bedrooms=3,
)
# 可选：latitude/longitude 同时提供时启用邻域空间特征
```

- **惰性导入**：模块顶层不依赖 sklearn/joblib，无模型依赖环境可安全 import；
- **回退语义**：信息不足（面积缺失/非法）或模型缺失 → 返回 `None`，由调用方回退；
  小区未知 → 城市中位；城市未知 → 全局中位；
- `load_model()` 返回 dict 含 `version`（如 `2026-08-05-r11`），风险引擎
  `valuation.model_version()` 直接读取；接口签名与返回「元」口径稳定
  （`estimate_total_price` 只依赖 model/encoders/cities/smooth_k 键，新增键不破坏）；
- **接入坑 1**：`community` 需先用 `data_clean.parse_community_from_title` 归一，否则与
  训练标签不匹配会静默回退城市中位；
- **接入坑 2**：`smooth_mode="eb"` 时 predict 按城市读 `encoders["eb_k"]`，与训练同公式
  （老产物无 smooth_mode 键 → 视为 fixed，向后兼容）。

### 6.3 产物与版本

`output/` 已被 .gitignore，不会入库：

| 产物 | 内容 |
|---|---|
| `output/avm/model.joblib` | 模型 + 编码字典（encoders/eb_k/nn）+ cities + 特征元数据 + smooth_k/smooth_mode + version |
| `output/avm/avm_report.json` | 指标、清洗统计（cleaning 含分城市明细）、误差分解、特征缺失率、置信分层与覆盖率曲线、version |
| `output/avm/attribution_report.json` | 特征归因可解释报告（C-02）：permutation importance 的 Top 特征与全特征 mean/std，见第 10 节 |

版本号 `YYYY-MM-DD-rN`（同日重训递增 r2/r3...），随产物落盘，供 predict/风险引擎追踪
「哪版模型在跑」。

## 7. 验收记录（2026-08-05，canonical r11）

| 项 | 结果 |
|---|---|
| 清洗 | 44,369 → 40,203 行（剔除 3,344：围栏 27 / URL 3,234 / 标记 3 / 价格 80） |
| 四城外市错标 | zs/yf/zh/dg 100% 外市（URL 子域证实），整城剔除，DWD 无本地房源 |
| community 缺失 | 30.8% → ~10.5%（title 归一 + 回填 28,855 行） |
| AC-07 | ✅ 覆盖 45% MAPE 9.88% ≤ 10%（MdAPE 6.885%，n=3,618）；30–45% 均 ≤ 10% |
| 全量指标 | MAPE 14.59%（基线 20.64%，相对提升 29.3%）；MdAPE 9.48%；R² 0.827 |
| oracle 下界 | 12.65%（含测试集小区真实中位价，模型不可达上界） |
| 置信分层 | high 764 / mid 3,381 / low 3,896（仅用训练集统计） |
| 模型版本 | `2026-08-05-r11`（trained_at 2026-08-05 13:58:34） |

## 8. 已知局限（为什么全量 MAPE 停在 ~14.6%）

- **挂牌价非成交价（最硬的约束）**：标签是 asking price，同栋同户型因急售/装修/议价可挂
  出 2 倍价差（均非脏数据）。留一法噪声下界 ≈12%（同城同小区同房型面积 ±2% 下界 9.40%，
  完全相同 7.68%），**全量 MAPE 天花板 ≈12%**，oracle 下界 12.65% 与之互相印证。接入
  网签/评估价作标签才能把 MAPE 直接下压数个点。
- **无小区段是结构性下界**：title 纯营销文本解析不出楼盘名（r11 权重 10.1%、MAPE 22.78%；
  r4 时为 14.4% / 21.84%），无任何位置信号，只能靠城市中位 + 属性估计。
- **gz/sz 区间价差极大且多数无坐标**：sz 全表无坐标，gz 约 1/3 无坐标；核心区（天河
  5–10 万/㎡）与郊区（增城/南沙 0.8–1.5 万/㎡）同在一个城市中位里，无坐标行按城市中位
  估计、郊区行被系统性高估（误差尾部主要来源，两城合计贡献约 2pp）。
- **小区名仍是稀疏标签**：归一后仍有多数小区仅出现 1 次，OOF 下回退城市中位，无法给
  新小区提供位置信号。
- **zs/yf/zh/dg 四城无本地数据**：模型对四城估值退化为全局中位回退，需爬虫提供真实本地
  房源后才能可靠估值。
- **坐标覆盖率仅 ~32.7%**：腾讯 geocoder 每日 6000 配额（超限返 status=121）；
  `coord_fill.py` 已就绪（增量缓存，签名修复，与 orchestrator/geocode_fill.py 的签名 bug
  不同），**但补坐标是数据完整性/前端地图工作，不是精度杠杆**（见第 9 节）。

## 9. 关键结论

### 9.1 坐标不是精度杠杆（消融实测）

`train.py --drop-coords` 把坐标覆盖率从 33.4% 打到 0%，MAPE 仅 **15.508% → 15.858%
（+0.35pp）**，MdAPE 反而更好（10.66 → 10.54），R² 几乎不变。线性外推到 100% 坐标覆盖
最多再降 ~0.7pp（→14.8%），离 10% 还差 4.8pp。**不要再把 geocoder 补坐标当成达标手段。**

### 9.2 组合侧 38% 异常估值 = 基准自指，勿做校正层

`dws_risk_class` 200 笔中 38% 触发 R-UNW-03「异常估值」——这个数字**不能当模型精度读**：
`seed/generate_seed.py` 的 `CITY_UNIT_PRICE` 表注释自述取自 **08-04 版 model.joblib** 各市
探针点隐含单价，`true_market_price = CITY_UNIT_PRICE[city] × U(0.75,1.35) × area`。所以
**R-UNW-03 比的是当前模型 vs 它自己的旧快照 = 版本漂移**（基准内部自相矛盾可直接证伪：
深圳 18638 < 广州 35226）。反事实分解：真实模型误差只造成 **1/200** 笔异常。**不建**
偏差校正层（`calibrate.py` 未创建）——对自指基准做校正等于撤销模型改进去拟合噪声。
正确修法是换基准口径（DWD 独立行情）或调阈值，不是校正模型。

### 9.3 已推翻的假设（别再重复走的路）

- **「MAPE 被长尾离群拉爆」不成立**：APE>50% 仅占 3.90%（314 行）、贡献 3.13pp；
  误差是宽基分布（APE 20–50% 占 20.7% 行、贡献 6.20pp），截尾/离群清洗没有收益；
- **「换损失函数对齐 MAPE」不成立**：log 空间 MAE 把 MdAPE 压下但 MAPE 反而变差
  （MAPE 由尾部主导，MAE 是弃尾保中位）；quantile<0.5 的收益只是全局下移且 R² 掉到 0.80；
  最终 canonical 采用 quantile(0.45) 是覆盖率口径下的最优，不是全量 MAPE 的杠杆；
- **城市间差异 85% 由数据属性决定**：每城 MAPE 对「小区标签密度」corr=-0.823、对「城内
  价格离散度」corr=+0.836，二元回归 R²=0.848——gz 的 oracle 下界本身就有 24.72%，模型
  能力并非瓶颈。

## 10. 特征归因（C-02）：模型在「看」什么

PRD R-cmp-2 / 设计 P9 要求 AVM 输出可解释报告：哪些特征主导估值、各特征的边际贡献量级。
产物 `output/avm/attribution_report.json`（**训练收尾自动生成**；也可独立跑
`python tools/avm/attribution.py --model output/avm/model.joblib --out-dir output/avm`）。

### 10.1 方法 = permutation importance，**不是 SHAP**（诚实标注）

- `shap` **未安装**（本项目零依赖风格，不引入新重依赖）；
- HistGBR 在 **quantile loss 下可能没有 `feature_importances_`**；
- permutation importance 模型无关、与 loss 无关：把测试集某特征列打乱
  `n_repeats` 次，测「neg MAPE 恶化多少」，恶化越多 = 该特征越重要。
  report `method` 字段为 `permutation_importance`，不冒充 SHAP。

### 10.2 评分口径与报告结构

评分：模型输出 log(单价)，评分时 `exp` 回**单价口径**再算 neg MAPE——总价 MAPE =
单价 MAPE（乘性误差），与 avm_report.json 总价口径一致；`scoring` 字段标注
`neg_mean_absolute_percentage_error`。

| 字段 | 含义 |
|---|---|
| version / generated_at | 模型版本 / 生成时间 |
| method | `permutation_importance`（诚实标注，非 SHAP） |
| n_repeats / seed | 打乱次数 / 随机种子（默认 5 / 42，可复现） |
| scoring / scoring_note | 评分口径与说明 |
| n_test | 评分测试集行数（CLI 从库重取、seed=42 复现切分） |
| top_features | 按 importance 降序，含中文名/说明（28 特征描述表复用 §4，映射不出留英文名） |
| full_importances | 全部 28 特征 mean/std（不随 top_n 截断） |

### 10.3 结果（canonical r11，测试集 8,041 行，neg MAPE 降幅口径）

| 排名 | 特征 | importance (MAPE pp) | 说明 |
|---|---|---|---|
| 1 | comm_mean | 11.31 | 小区目标编码均值（**位置信号主载体**） |
| 2 | comm_median | 10.31 | 小区目标编码中位数 |
| 3 | city_code | 7.12 | 城市编码 |
| 4 | floor_total | 2.43 | 总层数（楼层区位/总层数的粗代理） |
| 5 | area | 1.04 | 面积 |

解读：**定价信号几乎全部集中在目标编码的位置特征**（小区/城市），与 §9.3「城市间差异
85% 由数据属性决定」、§5「有小区无坐标段反而最好」互相印证；属性特征（面积/房型/楼龄）
的边际贡献远小于位置。归因报告不改变模型本身，只用于向业务侧解释「模型依据什么定价」。
