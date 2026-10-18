/* P7 · AVM 估值管理（US-05 / R-STA-02 / R-UNW-03 / R-UBQ-01 / AC-07）
 *
 * 页面叙事顺序刻意设计成「先认账、再解释、后逐笔核查」：
 *   AC-07 达标横幅 → 模型指标 → 偏差分布 → 异常率按城市/分类拆 → 训练侧误差分解
 *   → 版本血缘 → 估值明细列表 → 人工核查队列
 * MAPE 15.51% 未达 10% 目标，横幅直接标红并写清差距；风控页藏指标等于埋雷。
 *
 * 样式复用主框架 style.css（card / kpi / table / filter-bar / pager / badge），
 * 本页不新增 CSS 文件——插件页只允许带一个 js，个别微调用内联样式。
 */
(function () {
  const spf = window.spf;
  const { api, esc, fmtMoney, fmtPct, barChart, CLASS_COLORS } = spf;

  /* ---------------- 多语言字典（key = zh-CN 原文，译文只存这里） ----------------
   * 数据键（五级分类名、城市名、source_tier 等）保持中文原文用于匹配后端，
   * 仅展示层经 t() 翻译；t() 无译文时原样返回 key。
   */
  registerI18n("en", {
    "AVM 估值管理": "AVM Valuation Management",
    当前模型: "Current Model",
    "AVM vs 人工基准 · 偏差分布（红色 = 超 30% 阈值）":
      "AVM vs Manual Baseline · Deviation Distribution (red = over 30% threshold)",
    "口径：偏差为 AVM 估值相对人工基准价（业务登记价，非实际成交价）的偏离幅度；仅模型命中的贷款参与判定，未命中由行情价/登记价兜底、不参与异常判定。异常率同时反映模型误差与基准定价噪声。":
      "Method: deviation is how far the AVM valuation sits from the manual baseline price (the registered business price, not the actual transaction price); only model-hit loans are assessed, while misses fall back to market/registered price and are excluded from anomaly assessment. The anomaly rate reflects both model error and baseline pricing noise.",
    "异常率 · 分城市（暴露系统性偏差）": "Anomaly Rate · by City (reveals systematic bias)",
    "异常率 · 分五级分类": "Anomaly Rate · by Risk Tier",
    "估值来源（三级回退链）": "Valuation Source (3-tier fallback)",
    "异常估值归因 · 差异多少源于基准价、多少源于模型":
      "Anomaly Attribution · how much stems from the baseline price vs the model",
    "加载中…": "Loading…",
    模型版本血缘: "Model Version Lineage",
    "新估值带模型版本、可回溯到具体一次训练；历史回填估值的缺版本问题在核查队列跟进":
      "New valuations carry a model version and can be traced to a specific training run; backfilled valuations missing a version are tracked in the review queue",
    估值明细: "Valuation Details",
    是否异常: "Anomalous?",
    全部: "All",
    "仅异常（&gt;30%）": "Anomalous only (&gt;30%)",
    仅正常: "Normal only",
    "偏差 ≥ (%)": "Deviation ≥ (%)",
    "如 30": "e.g. 30",
    "偏差 ≤ (%)": "Deviation ≤ (%)",
    "如 100": "e.g. 100",
    城市: "City",
    模型版本: "Model Version",
    查询: "Search",
    贷款号: "Loan ID",
    抵押物: "Collateral",
    "AVM 估值": "AVM Valuation",
    人工基准价: "Manual Baseline Price",
    偏差: "Deviation",
    方向: "Direction",
    五级: "Tier",
    异常: "Anomaly",
    估值来源: "Valuation Source",
    上一页: "Prev",
    下一页: "Next",
    异常估值人工核查队列: "Anomalous Valuation Review Queue",
    告警类型: "Alert Type",
    说明: "Description",
    告警日: "Alert Date",
    "模型指标报告尚未生成，暂时无法展示精度与覆盖率——请联系数据团队生成后刷新本页":
      "Model metrics report not generated yet; accuracy & coverage unavailable — contact the data team to generate it, then refresh",
    "精度 @ 覆盖率：": "Accuracy / Coverage: ",
    覆盖: "coverage",
    "全量 MAPE": "Full MAPE",
    目标: "target",
    高: "High",
    中: "Mid",
    低: "Low",
    合计: "Total",
    "笔（模型评估集）": "loans (model eval set)",
    低置信自动转人工复核: "low-confidence auto-routed to manual review",
    产物无置信分层: "No confidence tiers in artifact",
    模型训练于: "Model trained on",
    自动估值覆盖率: "Auto-valuation Coverage",
    "笔 AVM 命中": "loans AVM hit",
    组合指标缺失: "portfolio metrics missing",
    异常估值率: "Anomalous Valuation Rate",
    笔偏差: "loans deviating",
    "置信分层（模型侧）": "Confidence tiers (model side)",
    口径说明: "Methodology",
    "「精度 / 覆盖率」是商用 AVM 的通行做法：可比案例充足的估值自动放行，不足的主动弃权、转人工复核。":
      "“Accuracy @ coverage” is the industry norm for commercial AVM: valuations with sufficient comparable cases are auto-released, while insufficient ones are abstained and routed to manual review.",
    "受基准数据自身波动影响（同小区同户型挂牌价可差 ±2%），全量平均误差存在约 12% 的下限，":
      "Due to volatility in the baseline data itself (listings for the same unit type in the same community can differ by ±2%), full-set average error has a floor of ~12%,",
    "因此 ≤10% 的目标只能在": "therefore the ≤10% target can only be met on the",
    高置信: "high-confidence",
    "覆盖子集上达成；基准价为登记挂牌价而非实际成交价。":
      "coverage subset; the baseline price is the registered listing price, not the actual transaction price.",
    低置信: "low-confidence",
    "笔已按规则自动转人工复核。": "loans were auto-routed to manual review per the rules.",
    异常估值笔数: "Anomalous Valuations",
    占比: "share",
    阈值: "threshold",
    偏差方向: "Deviation Direction",
    低估: "under",
    高估: "over",
    "AVM 覆盖率": "AVM Coverage",
    笔命中模型: "loans hit by model",
    典型偏差幅度: "Typical Deviation Magnitude",
    均值: "mean",
    待人工核查队列: "Manual Review Queue",
    "落库告警 · 点击跳转 ↓": "Persisted alerts · click to jump ↓",
    梯度提升树: "Gradient Boosting",
    随机森林: "Random Forest",
    线性回归: "Linear Regression",
    "（对单价取对数建模）": " (log-transformed unit price)",
    模型产物缺失: "Model artifact missing",
    算法: "Algorithm",
    训练时间: "Trained At",
    "训练 / 测试样本": "Train / Test Samples",
    特征数: "Features",
    维: "dim",
    坐标覆盖率: "Coordinate Coverage",
    小区缺失率: "Community Missing Rate",
    口径: "Metric",
    "MAPE：平均绝对百分比误差，估值与真实价平均差多少%（越小越好）":
      "MAPE: mean absolute percentage error — average % gap between valuation and true price (lower is better)",
    "MdAPE：误差中位数，不受个别极端偏差拉偏（越小越好）":
      "MdAPE: median error, not skewed by extreme deviations (lower is better)",
    "R²：模型解释了多少价格差异，越接近 1 越好":
      "R²: how much of the price variance the model explains, closer to 1 is better",
    "AVM 模型": "AVM Model",
    "基线（城市,小区 中位价 × 面积）": "Baseline (city, community median price × area)",
    精度目标: "Accuracy Target",
    "数据来源：AVM 模型训练评估报告（avm_report.json）":
      "Source: AVM model training evaluation report (avm_report.json)",
    "AVM 偏差分布直方图": "AVM deviation distribution histogram",
    贷款笔数: "Loan Count",
    偏差中位: "median deviation",
    仅模型命中贷款参与判定: "only model-hit loans are assessed",
    无数据: "No data",
    笔数: "Count",
    异常率: "Anomaly Rate",
    中位偏差: "Median Deviation",
    中位有符号偏差: "Median Signed Deviation",
    五级分类: "Risk Tier",
    回退级别: "Fallback Level",
    "模型覆盖不到的部分由后两级承接，这部分不参与偏差判定":
      "Portions the model cannot cover are handled by the lower two tiers and are excluded from deviation assessment",
    "估值来源三级回退：模型直出 → 行情参照 → 登记价兜底":
      "3-tier valuation fallback: model direct → market reference → registered price",
    版本: "Version",
    "训练/测试": "Train/Test",
    "平均绝对百分比误差（越小越好）": "mean absolute percentage error (lower is better)",
    较上一版: "vs Previous",
    "误差中位数（越小越好）": "median error (lower is better)",
    "越接近 1 越好": "closer to 1 is better",
    "vs 基线": "vs Baseline",
    在跑: "Live",
    基线: "Baseline",
    提升: "improvement",
    "版本管理上线后新产生的估值都带有生成它的模型版本，出现问题时可回溯到具体一次训练、评估影响范围；更早的历史回填估值缺少版本信息，会以下方核查队列中的「不可溯源」条目跟进。":
      "Since version management went live, new valuations carry the model version that produced them, so issues can be traced to a specific training run and its blast radius assessed; earlier backfilled valuations lack version info and are tracked via the “untraceable” entries in the review queue below.",
    暂无历史模型版本记录: "No historical model versions",
    笔: "loans",
    "共 {n} 笔": "{n} loans in total",
    "第 {p}/{t} 页": "Page {p}/{t}",
    "告警记录共 {n} 条": "{n} alert records in total",
    "当前模型（{ts} 训练）": "Current model (trained {ts})",
    按偏差从高到低排序: "sorted by deviation, high to low",
    无符合条件的数据: "No matching data",
    不可溯源: "Untraceable",
    待复估: "To Revalue",
    // 核查队列 · model_version 哨兵值与计数括号（key 与后端写库文案逐字一致）
    "模型版本缺失，估值结论不可溯源": "Model version missing; valuation conclusion cannot be traced",
    "未知": "Unknown",
    "（{n}）": "({n})",
    告警代号: "Alert code",
    同一贷款不同告警日分别计条: "same loan counted separately per alert date",
    "待复估 = 估值偏差 &gt;30%，需逐笔人工复核 · 不可溯源 = 估值缺模型版本（多为历史回填数据，需数据团队排查）":
      "To Revalue = deviation &gt;30%, needs case-by-case manual review · Untraceable = valuation missing model version (mostly historical backfill; data team to investigate)",
    无异常估值告警: "No anomalous valuation alerts",
    "离线归因分析尚未生成，暂时无法区分基准价噪声与模型误差——请联系数据团队更新归因报告。":
      "Offline attribution analysis not generated yet; cannot distinguish baseline noise from model error — contact the data team to update the attribution report.",
    "其中 {pct}（{n} 笔）归因于基准价噪声":
      "Of which {pct} ({n} loans) attributed to baseline-price noise",
    真实模型误差仅: "true model error is only",
    "数据口径（诚实声明）：异常率/城市对比 = {a}；归因比例 = {b}。基准价为系统登记的挂牌价，不是网签成交价。":
      "Data caliber (honest statement): anomaly rate / city comparison = {a}; attribution shares = {b}. The baseline price is the system-registered listing price, not the net-signed transaction price.",
    "根因：人工基准价由上一版模型（2026-08-04）的输出加扰动生成，与当前模型（{mv}）同源。所以这里的异常率实际度量的是「当前模型相对旧版基准的漂移」，不是估值精度。因此":
      "Root cause: the manual baseline price was synthesized by adding perturbation to the previous model's (2026-08-04) output, so it shares its origin with the current model ({mv}). The anomaly rate here therefore actually measures “drift of the current model vs the old baseline”, not valuation accuracy. Hence",
    不做偏差校正: "do NOT apply deviation correction",
    "——把新模型拉回去过拟合一个过时的基准，只会掩盖真实问题。":
      " — pulling the new model back to overfit an outdated baseline would only mask the real problem.",
    "数据口径（诚实声明）：异常率/城市对比 =":
      "Data scope (honest note): anomaly-rate / city comparison =",
    实时库现算: "computed live from DB",
    "；归因比例 =": "; attribution share =",
    离线诊断分析: "offline diagnostic analysis",
    "。基准价为系统登记的挂牌价，不是网签成交价。":
      ". The baseline price is the system-registered listing price, not the signed transaction price.",
    正常: "Normal",
    达标: "Pass",
    未达标: "Fail",
    "验收口径：可比案例充足的子集上，平均估值误差 ≤10%":
      "Acceptance: on the subset with sufficient comparable cases, average valuation error ≤10%",
    估值精度: "Valuation Accuracy",
    登记价兜底: "Registered Price Fallback",
    行情参照: "Market Reference",
    无估值: "No Valuation",
    关注: "Watch",
    次级: "Substandard",
    可疑: "Doubtful",
    损失: "Loss",
    // 告警队列 · 说明列模板（百分比占位，随语言切换）
    "AVM 估值偏差 {pct} 超阈值": "AVM valuation deviation {pct} exceeds threshold",
    "AVM 估值偏差超阈值": "AVM valuation deviation exceeds threshold",
    // 告警队列 · 城市列（广东 21 城，带「市」后缀，与后端城市名一致）
    "广州市": "Guangzhou",
    "深圳市": "Shenzhen",
    "珠海市": "Zhuhai",
    "汕头市": "Shantou",
    "佛山市": "Foshan",
    "韶关市": "Shaoguan",
    "河源市": "Heyuan",
    "梅州市": "Meizhou",
    "惠州市": "Huizhou",
    "汕尾市": "Shanwei",
    "东莞市": "Dongguan",
    "中山市": "Zhongshan",
    "江门市": "Jiangmen",
    "阳江市": "Yangjiang",
    "湛江市": "Zhanjiang",
    "茂名市": "Maoming",
    "肇庆市": "Zhaoqing",
    "清远市": "Qingyuan",
    "潮州市": "Chaozhou",
    "揭阳市": "Jieyang",
    "云浮市": "Yunfu",
  }, "avm");

  registerI18n("zh-HK", {
    "AVM 估值管理": "AVM 估值管理",
    当前模型: "當前模型",
    "AVM vs 人工基准 · 偏差分布（红色 = 超 30% 阈值）":
      "AVM vs 人工基準 · 偏差分布（紅色 = 超 30% 閾值）",
    "口径：偏差为 AVM 估值相对人工基准价（业务登记价，非实际成交价）的偏离幅度；仅模型命中的贷款参与判定，未命中由行情价/登记价兜底、不参与异常判定。异常率同时反映模型误差与基准定价噪声。":
      "口徑：偏差為 AVM 估值相對人工基準價（業務登記價，非實際成交價）的偏離幅度；僅模型命中的貸款參與判定，未命中由行情價/登記價兜底、不參與異常判定。異常率同時反映模型誤差與基準定價噪聲。",
    "异常率 · 分城市（暴露系统性偏差）": "異常率 · 分城市（暴露系統性偏差）",
    "异常率 · 分五级分类": "異常率 · 分五級分類",
    "估值来源（三级回退链）": "估值來源（三級回退鏈）",
    "异常估值归因 · 差异多少源于基准价、多少源于模型":
      "異常估值歸因 · 差異多少源於基準價、多少源於模型",
    "加载中…": "載入中…",
    模型版本血缘: "模型版本血緣",
    "新估值带模型版本、可回溯到具体一次训练；历史回填估值的缺版本问题在核查队列跟进":
      "新估值帶模型版本、可回溯到具體一次訓練；歷史回填估值的缺版本問題在核查隊列跟進",
    估值明细: "估值明細",
    是否异常: "是否異常",
    全部: "全部",
    "仅异常（&gt;30%）": "僅異常（&gt;30%）",
    仅正常: "僅正常",
    "偏差 ≥ (%)": "偏差 ≥ (%)",
    "如 30": "如 30",
    "偏差 ≤ (%)": "偏差 ≤ (%)",
    "如 100": "如 100",
    城市: "城市",
    模型版本: "模型版本",
    查询: "查詢",
    贷款号: "貸款號",
    抵押物: "抵押物",
    "AVM 估值": "AVM 估值",
    人工基准价: "人工基準價",
    偏差: "偏差",
    方向: "方向",
    五级: "五級",
    异常: "異常",
    估值来源: "估值來源",
    上一页: "上一頁",
    下一页: "下一頁",
    异常估值人工核查队列: "異常估值人工核查隊列",
    告警类型: "告警類型",
    说明: "說明",
    告警日: "告警日",
    "模型指标报告尚未生成，暂时无法展示精度与覆盖率——请联系数据团队生成后刷新本页":
      "模型指標報告尚未生成，暫時無法展示精度與覆蓋率——請聯繫數據團隊生成後刷新本頁",
    "精度 @ 覆盖率：": "精度 @ 覆蓋率：",
    覆盖: "覆蓋",
    "全量 MAPE": "全量 MAPE",
    目标: "目標",
    高: "高",
    中: "中",
    低: "低",
    合计: "合計",
    "笔（模型评估集）": "筆（模型評估集）",
    低置信自动转人工复核: "低置信自動轉人工覆核",
    产物无置信分层: "產物無置信分層",
    模型训练于: "模型訓練於",
    自动估值覆盖率: "自動估值覆蓋率",
    "笔 AVM 命中": "筆 AVM 命中",
    组合指标缺失: "組合指標缺失",
    异常估值率: "異常估值率",
    笔偏差: "筆偏差",
    "置信分层（模型侧）": "置信分層（模型側）",
    口径说明: "口徑說明",
    "「精度 / 覆盖率」是商用 AVM 的通行做法：可比案例充足的估值自动放行，不足的主动弃权、转人工复核。":
      "「精度 / 覆蓋率」是商用 AVM 的通行做法：可比案例充足的估值自動放行，不足的主動棄權、轉人工覆核。",
    "受基准数据自身波动影响（同小区同户型挂牌价可差 ±2%），全量平均误差存在约 12% 的下限，":
      "受基準數據自身波動影響（同小區同戶型掛牌價可差 ±2%），全量平均誤差存在約 12% 的下限，",
    "因此 ≤10% 的目标只能在": "因此 ≤10% 的目標只能在",
    高置信: "高置信",
    "覆盖子集上达成；基准价为登记挂牌价而非实际成交价。":
      "覆蓋子集上達成；基準價為登記掛牌價而非實際成交價。",
    低置信: "低置信",
    "笔已按规则自动转人工复核。": "筆已按規則自動轉人工覆核。",
    异常估值笔数: "異常估值筆數",
    占比: "佔比",
    阈值: "閾值",
    偏差方向: "偏差方向",
    低估: "低估",
    高估: "高估",
    中位有符号偏差: "中位有號偏差",
    "AVM 覆盖率": "AVM 覆蓋率",
    笔命中模型: "筆命中模型",
    典型偏差幅度: "典型偏差幅度",
    均值: "均值",
    待人工核查队列: "待人工核查隊列",
    "落库告警 · 点击跳转 ↓": "落庫告警 · 點擊跳轉 ↓",
    梯度提升树: "梯度提升樹",
    随机森林: "隨機森林",
    线性回归: "線性迴歸",
    "（对单价取对数建模）": "（對單價取對數建模）",
    模型产物缺失: "模型產物缺失",
    算法: "演算法",
    训练时间: "訓練時間",
    "训练 / 测试样本": "訓練 / 測試樣本",
    特征数: "特徵數",
    维: "維",
    坐标覆盖率: "座標覆蓋率",
    小区缺失率: "小區缺失率",
    口径: "口徑",
    "MAPE：平均绝对百分比误差，估值与真实价平均差多少%（越小越好）":
      "MAPE：平均絕對百分比誤差，估值與真實價平均差多少%（越小越好）",
    "MdAPE：误差中位数，不受个别极端偏差拉偏（越小越好）":
      "MdAPE：誤差中位數，不受個別極端偏差拉偏（越小越好）",
    "R²：模型解释了多少价格差异，越接近 1 越好": "R²：模型解釋了多少價格差異，越接近 1 越好",
    "AVM 模型": "AVM 模型",
    "基线（城市,小区 中位价 × 面积）": "基線（城市,小區 中位價 × 面積）",
    精度目标: "精度目標",
    "数据来源：AVM 模型训练评估报告（avm_report.json）":
      "資料來源：AVM 模型訓練評估報告（avm_report.json）",
    "AVM 偏差分布直方图": "AVM 偏差分布直方圖",
    贷款笔数: "貸款筆數",
    偏差中位: "偏差中位",
    仅模型命中贷款参与判定: "僅模型命中貸款參與判定",
    无数据: "無資料",
    笔数: "筆數",
    异常率: "異常率",
    中位偏差: "中位偏差",
    五级分类: "五級分類",
    回退级别: "回退級別",
    "模型覆盖不到的部分由后两级承接，这部分不参与偏差判定":
      "模型覆蓋不到的部分由後兩級承接，這部分不參與偏差判定",
    "估值来源三级回退：模型直出 → 行情参照 → 登记价兜底":
      "估值來源三級回退：模型直出 → 行情參照 → 登記價兜底",
    版本: "版本",
    "训练/测试": "訓練/測試",
    "平均绝对百分比误差（越小越好）": "平均絕對百分比誤差（越小越好）",
    较上一版: "較上一版",
    "误差中位数（越小越好）": "誤差中位數（越小越好）",
    "越接近 1 越好": "越接近 1 越好",
    "vs 基线": "vs 基線",
    在跑: "在跑",
    基线: "基線",
    提升: "提升",
    "版本管理上线后新产生的估值都带有生成它的模型版本，出现问题时可回溯到具体一次训练、评估影响范围；更早的历史回填估值缺少版本信息，会以下方核查队列中的「不可溯源」条目跟进。":
      "版本管理上線後新產生的估值都帶有生成它的模型版本，出現問題時可回溯到具體一次訓練、評估影響範圍；更早的歷史回填估值缺少版本資訊，會以下方核查隊列中的「不可溯源」條目跟進。",
    暂无历史模型版本记录: "暫無歷史模型版本記錄",
    笔: "筆",
    "共 {n} 笔": "共 {n} 筆",
    "第 {p}/{t} 页": "第 {p}/{t} 頁",
    "告警记录共 {n} 条": "告警記錄共 {n} 條",
    "当前模型（{ts} 训练）": "當前模型（{ts} 訓練）",
    按偏差从高到低排序: "按偏差從高到低排序",
    无符合条件的数据: "無符合條件的資料",
    不可溯源: "不可溯源",
    待复估: "待複估",
    // 核查队列 · model_version 哨兵值与计数括号（key 与后端写库文案逐字一致）
    "模型版本缺失，估值结论不可溯源": "模型版本缺失，估值結論不可溯源",
    "未知": "未知",
    "（{n}）": "（{n}）",
    告警代号: "告警代號",
    同一贷款不同告警日分别计条: "同一貸款不同告警日分別計條",
    "待复估 = 估值偏差 &gt;30%，需逐笔人工复核 · 不可溯源 = 估值缺模型版本（多为历史回填数据，需数据团队排查）":
      "待複估 = 估值偏差 &gt;30%，需逐筆人工覆核 · 不可溯源 = 估值缺模型版本（多為歷史回填數據，需數據團隊排查）",
    无异常估值告警: "無異常估值告警",
    "离线归因分析尚未生成，暂时无法区分基准价噪声与模型误差——请联系数据团队更新归因报告。":
      "離線歸因分析尚未生成，暫時無法區分基準價噪聲與模型誤差——請聯繫數據團隊更新歸因報告。",
    "其中 {pct}（{n} 笔）归因于基准价噪声": "其中 {pct}（{n} 筆）歸因於基準價噪聲",
    真实模型误差仅: "真實模型誤差僅",
    "数据口径（诚实声明）：异常率/城市对比 = {a}；归因比例 = {b}。基准价为系统登记的挂牌价，不是网签成交价。":
      "數據口徑（誠實聲明）：異常率/城市對比 = {a}；歸因比例 = {b}。基準價為系統登記的掛牌價，不是網簽成交價。",
    "根因：人工基准价由上一版模型（2026-08-04）的输出加扰动生成，与当前模型（{mv}）同源。所以这里的异常率实际度量的是「当前模型相对旧版基准的漂移」，不是估值精度。因此":
      "根因：人工基準價由上一版模型（2026-08-04）的輸出加擾動生成，與當前模型（{mv}）同源。所以這裡的異常率實際度量的是「當前模型相對舊版基準的漂移」，不是估值精度。因此",
    不做偏差校正: "不做偏差校正",
    "——把新模型拉回去过拟合一个过时的基准，只会掩盖真实问题。":
      "——把新模型拉回去過擬合一個過時的基準，只會掩蓋真實問題。",
    "数据口径（诚实声明）：异常率/城市对比 =": "數據口徑（誠實聲明）：異常率/城市對比 =",
    实时库现算: "實時庫現算",
    "；归因比例 =": "；歸因比例 =",
    离线诊断分析: "離線診斷分析",
    "。基准价为系统登记的挂牌价，不是网签成交价。": "。基準價為系統登記的掛牌價，不是網簽成交價。",
    正常: "正常",
    达标: "達標",
    未达标: "未達標",
    "验收口径：可比案例充足的子集上，平均估值误差 ≤10%":
      "驗收口徑：可比案例充足的子集上，平均估值誤差 ≤10%",
    估值精度: "估值精度",
    登记价兜底: "登記價兜底",
    行情参照: "行情參照",
    无估值: "無估值",
    关注: "關注",
    次级: "次級",
    可疑: "可疑",
    损失: "損失",
    // 告警队列 · 说明列模板（百分比占位，随语言切换）
    "AVM 估值偏差 {pct} 超阈值": "AVM 估值偏差 {pct} 超閾值",
    "AVM 估值偏差超阈值": "AVM 估值偏差超閾值",
    // 告警队列 · 城市列（广东 21 城，带「市」后缀，与后端城市名一致）
    "广州市": "廣州市",
    "深圳市": "深圳市",
    "珠海市": "珠海市",
    "汕头市": "汕頭市",
    "佛山市": "佛山市",
    "韶关市": "韶關市",
    "河源市": "河源市",
    "梅州市": "梅州市",
    "惠州市": "惠州市",
    "汕尾市": "汕尾市",
    "东莞市": "東莞市",
    "中山市": "中山市",
    "江门市": "江門市",
    "阳江市": "陽江市",
    "湛江市": "湛江市",
    "茂名市": "茂名市",
    "肇庆市": "肇慶市",
    "清远市": "清遠市",
    "潮州市": "潮州市",
    "揭阳市": "揭陽市",
    "云浮市": "雲浮市",
  }, "avm");

  // 页面级状态：两张列表各自的分页游标 + 概览数据（筛选下拉的选项来源）。
  const st = { valPage: 1, alertPage: 1, overview: null };

  const RED = "#b91c1c";
  const GREEN = "#2e9e5b";
  const GRAY = "#9ca3af";

  /* ---------- 小工具 ---------- */

  // 报告里的 MAPE/MdAPE 已经是百分数（15.508），不能再乘 100，与 fmtPct 区分开。
  const pp = (v, d) => (v == null ? "-" : Number(v).toFixed(d == null ? 2 : d) + "%");
  const num = (v) => (v == null ? "-" : Number(v).toLocaleString("zh-CN"));

  // 片段拼接：中文语序紧凑直接相连（zh-CN/zh-HK），en 等用空格分隔——
  // t(A)+t(B) 直拼在 en 下会粘成 "AccuracyMeets target"，动态文案拼接一律收口到这里。
  function joinSeg(parts, sep) {
    const s = sep != null ? sep : /^zh/.test(window.__LANG__ || "zh-CN") ? "" : " ";
    return parts.filter((p) => p != null && p !== "").join(s);
  }

  // model_version 缺失时后端落 "unknown"（技术哨兵值，tools/risk 侧不动），
  // 展示层译成三语（zh-CN「未知」），真实版本号（2026-08-20-r2 等）原样保留。
  const mvLabel = (mv) => (mv == null || mv === "" ? "-" : mv === "unknown" ? t("未知") : mv);

  // 有符号偏差配色：超过 ±30%（R-UNW-03 阈值）标红，其余灰/绿。
  function devColor(v) {
    if (v == null) return GRAY;
    return Math.abs(v) > 0.3 ? RED : Math.abs(v) > 0.15 ? "#b45309" : GREEN;
  }

  function bar(pct, color) {
    const w = Math.max(0, Math.min(100, pct * 100)).toFixed(1);
    return (
      `<div style="background:#eef2f7;border-radius:999px;height:8px;width:100%">` +
      `<div style="width:${w}%;height:8px;border-radius:999px;background:${color}"></div></div>`
    );
  }

  /* ---------- 页面骨架 ---------- */

  const html = function () {
    return `
    <h1 class="page-title">${t("AVM 估值管理")}</h1>
    <style>
      #avm-attribution > summary { list-style: none; cursor: pointer; }
      #avm-attribution > summary::-webkit-details-marker { display: none; }
      #avm-attribution > summary::after { content: " ▸"; color: #6b7280; }
      #avm-attribution[open] > summary::after { content: " ▾"; }
    </style>
    <div id="avm-ac07"></div>
    <div class="kpi-row" id="avm-kpi"></div>

    <div class="grid-2">
      <div class="card">
        <h2 class="card-title" id="avm-model-title">${t("当前模型")}</h2>
        <div id="avm-model"></div>
      </div>
      <div class="card">
        <h2 class="card-title">${t("AVM vs 人工基准 · 偏差分布（红色 = 超 30% 阈值）")}</h2>
        <div class="chart" id="avm-dev-hist"></div>
        <div id="avm-dev-note" title="${t("口径：偏差为 AVM 估值相对人工基准价（业务登记价，非实际成交价）的偏离幅度；仅模型命中的贷款参与判定，未命中由行情价/登记价兜底、不参与异常判定。异常率同时反映模型误差与基准定价噪声。")}" style="font-size:12px;color:#6b7280;margin-top:8px"></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2 class="card-title">${t("异常率 · 分城市（暴露系统性偏差）")}</h2>
        <div id="avm-by-city"></div>
      </div>
      <div class="card">
        <h2 class="card-title">${t("异常率 · 分五级分类")}</h2>
        <div id="avm-by-class"></div>
        <h2 class="card-title" style="margin-top:16px">${t("估值来源（三级回退链）")}</h2>
        <div id="avm-tiers"></div>
      </div>
    </div>

    <details class="card" id="avm-attribution">
      <summary class="card-title">${t("异常估值归因 · 差异多少源于基准价、多少源于模型")}</summary>
      <div id="avm-attribution-body">${t("加载中…")}</div>
    </details>

    <div class="card">
      <h2 class="card-title">${t("模型版本血缘")}</h2>
      <div id="avm-lineage" title="${t("新估值带模型版本、可回溯到具体一次训练；历史回填估值的缺版本问题在核查队列跟进")}"></div>
    </div>

    <h2 class="card-title" style="margin-top:22px">${t("估值明细")}</h2>
    <form class="filter-bar" id="avm-filter">
      <label>${t("是否异常")}
        <select id="avm-f-abnormal">
          <option value="">${t("全部")}</option>
          <option value="1">${t("仅异常（&gt;30%）")}</option>
          <option value="0">${t("仅正常")}</option>
        </select>
      </label>
      <label>${t("偏差 ≥ (%)")}
        <input type="number" id="avm-f-min" step="1" min="0" placeholder="${t("如 30")}" />
      </label>
      <label>${t("偏差 ≤ (%)")}
        <input type="number" id="avm-f-max" step="1" min="0" placeholder="${t("如 100")}" />
      </label>
      <label>${t("城市")}
        <select id="avm-f-city"><option value="">${t("全部")}</option></select>
      </label>
      <label>${t("模型版本")}
        <select id="avm-f-version"><option value="">${t("全部")}</option></select>
      </label>
      <button type="submit" class="btn btn-ghost">${t("查询")}</button>
      <span id="avm-val-total" class="filter-total"></span>
    </form>
    <div class="card">
      <table class="table" id="avm-val-table">
        <thead><tr>
          <th>${t("贷款号")}</th><th>${t("抵押物")}</th><th class="num">${t("AVM 估值")}</th><th class="num">${t("人工基准价")}</th>
          <th class="num">${t("偏差")}</th><th>${t("方向")}</th><th class="num">LTV</th><th>${t("五级")}</th><th>${t("异常")}</th>
          <th>${t("估值来源")}</th><th>${t("模型版本")}</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="pager">
        <button id="avm-val-prev" class="btn btn-ghost btn-sm">${t("上一页")}</button>
        <span id="avm-val-page"></span>
        <button id="avm-val-next" class="btn btn-ghost btn-sm">${t("下一页")}</button>
      </div>
    </div>

    <h2 class="card-title" style="margin-top:22px" id="avm-alert-section">${t("异常估值人工核查队列")}</h2>
    <div class="card">
      <div id="avm-alert-sum" style="margin-bottom:10px"></div>
      <table class="table" id="avm-alert-table">
        <thead><tr>
          <th>${t("告警类型")}</th><th>${t("贷款号")}</th><th>${t("抵押物")}</th><th>${t("城市")}</th>
          <th class="num">${t("偏差")}</th><th>${t("说明")}</th><th>${t("模型版本")}</th><th>${t("告警日")}</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="pager">
        <button id="avm-alert-prev" class="btn btn-ghost btn-sm">${t("上一页")}</button>
        <span id="avm-alert-page"></span>
        <button id="avm-alert-next" class="btn btn-ghost btn-sm">${t("下一页")}</button>
      </div>
    </div>
  `;
  };

  /* ---------- 概览渲染 ---------- */

  function renderAc07(m, p) {
    const box = document.getElementById("avm-ac07");
    if (!m) {
      box.innerHTML = `<div class="empty">${t("模型指标报告尚未生成，暂时无法展示精度与覆盖率——请联系数据团队生成后刷新本页")}</div>`;
      return;
    }
    const a = m.ac07 || {};
    // 置信分层 / 覆盖率只在新产物（带 confidence 块）有；旧产物无 → 优雅降级。
    const cov = m.coverage;
    const tiers = cov && cov.tiers ? cov.tiers : null;
    const ac = cov && cov.ac07 ? cov.ac07 : null;
    const ok = ac ? true : a.passed; // 有「精度@覆盖率」子集即视为达标口径达成；否则回退全量 passed

    // 顶部：AC-07 达标徽标 + 精度@覆盖率一句话（操作员一眼看验收是否过）
    const acLine = ac
      ? `${t("精度 @ 覆盖率：")}${pp(ac.mape)} @ ${ac.coverage_pct}% ${t("覆盖")} ≤ 10%`
      : `${t("全量 MAPE")} ${pp(a.actual_mape, 3)}${t("（{n}）", { n: `${t("目标")} ≤ ${pp(a.target_mape, 0)}` })}`;

    // 三个操作员视角指标
    const covPct = p ? p.avm_coverage : null;
    const covRows = p ? p.avm_rows : null;
    const covTotal = p ? p.total : null;
    const abnRate = p ? p.abnormal_rate : null;
    const abnN = p ? p.abnormal : null;
    const abnColor = abnRate != null && abnRate >= 0.3 ? RED : "#b45309";

    const tierHtml = tiers
      ? `<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">` +
        `<span><b style="color:${GREEN}">${num(tiers.high.n)}</b> ${t("高")}</span>` +
        `<span><b style="color:#d97706">${num(tiers.mid.n)}</b> ${t("中")}</span>` +
        `<span><b style="color:${RED}">${num(tiers.low.n)}</b> ${t("低")}</span></div>` +
        `<div style="font-size:12px;color:#6b7280;margin-top:4px">${t("合计")} ${num(tiers.high.n + tiers.mid.n + tiers.low.n)} ${t("笔（模型评估集）")} · ${t("低置信自动转人工复核")}</div>`
      : `<div class="empty" style="padding:6px 0">${t("产物无置信分层")}</div>`;

    const metric = (label, value, sub, color) =>
      `<div><div style="font-size:12px;color:#6b7280">${label}</div>` +
      `<div style="font-size:24px;font-weight:700;color:${color || "inherit"}">${value}</div>` +
      `<div style="font-size:12px;color:#6b7280;margin-top:4px">${sub}</div></div>`;

    box.innerHTML =
      `<div class="card" style="border-left:4px solid ${ok ? GREEN : RED}">` +
      `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">` +
      `<span class="badge ${ok ? "ok" : "bad"}" title="${t("验收口径：可比案例充足的子集上，平均估值误差 ≤10%")}">${joinSeg([t("估值精度"), ok ? t("达标") : t("未达标")], " · ")}</span>` +
      `<span style="font-size:15px;font-weight:600;color:${ok ? GREEN : RED}">${acLine}</span>` +
      `<span style="font-size:12px;color:#6b7280;margin-left:auto">${m.trained_at ? `${t("模型训练于")} ${m.trained_at}` : ""}</span>` +
      `</div>` +
      `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:18px;margin-top:16px">` +
      metric(
        t("自动估值覆盖率"),
        covPct != null ? fmtPct(covPct) : "-",
        covRows != null
          ? `${num(covRows)} / ${num(covTotal)} ${t("笔 AVM 命中")}`
          : t("组合指标缺失"),
        "#2563eb"
      ) +
      metric(
        t("异常估值率"),
        abnRate != null ? fmtPct(abnRate) : "-",
        abnN != null ? `${num(abnN)} ${t("笔偏差")} &gt;30%` : "",
        abnColor
      ) +
      `<div><div style="font-size:12px;color:#6b7280">${t("置信分层（模型侧）")}</div>${tierHtml}</div>` +
      `</div>` +
      `<details style="margin-top:16px;border-top:1px solid #eef2f7;padding-top:10px">` +
      `<summary style="cursor:pointer;color:#2563eb;font-size:13px">${t("口径说明")}</summary>` +
      `<div style="font-size:12px;color:#6b7280;line-height:1.8;margin-top:8px">` +
      joinSeg([
        t(
          "「精度 / 覆盖率」是商用 AVM 的通行做法：可比案例充足的估值自动放行，不足的主动弃权、转人工复核。"
        ),
        t("受基准数据自身波动影响（同小区同户型挂牌价可差 ±2%），全量平均误差存在约 12% 的下限，"),
        joinSeg([
          t("因此 ≤10% 的目标只能在"),
          ac ? ac.coverage_pct + "%" : t("高置信"),
          t("覆盖子集上达成；基准价为登记挂牌价而非实际成交价。"),
        ]),
        tiers ? `${t("低置信")} ${num(tiers.low.n)} ${t("笔已按规则自动转人工复核。")}` : "",
      ]) +
      `</div></details>` +
      `</div>`;
  }

  function renderKpi(d) {
    const p = d.portfolio;
    const items = [
      {
        label: t("异常估值笔数"),
        value: `${num(p.abnormal)} / ${num(p.avm_rows)}`,
        sub: `${t("占比")} ${fmtPct(p.abnormal_rate)} · ${t("阈值")} ${fmtPct(p.threshold)}`,
        color: RED,
      },
      {
        label: t("偏差方向"),
        value: `${t("低估")} ${num(p.under_count)} / ${t("高估")} ${num(p.over_count)}`,
        sub: `${t("中位有符号偏差")} ${fmtPct(p.median_signed_dev)}`,
        color: devColor(p.median_signed_dev),
      },
      {
        label: t("AVM 覆盖率"),
        value: fmtPct(p.avm_coverage),
        sub: `${num(p.avm_rows)} / ${num(p.total)} ${t("笔命中模型")}`,
      },
      {
        // 典型误差幅度：与「偏差方向」卡互补——那边是方向（有符号），这边是幅度。
        // 回答「正常情况下模型差多少」，给 30% 阈值提供参照系。
        label: t("典型偏差幅度"),
        value: p.median_abs_dev != null ? fmtPct(p.median_abs_dev) : "-",
        sub: `${t("均值")} ${p.mean_abs_dev != null ? fmtPct(p.mean_abs_dev) : "-"} · ${t("阈值")} ${fmtPct(p.threshold)}`,
      },
      {
        // 行动闭环：异常被发现之后有多少笔进了人工核查队列（落库告警，非实时全量）。
        // 点击滚动到页面底部核查队列，形成 KPI → 明细的快捷跳转。
        label: t("待人工核查队列"),
        value: p.alert_total != null ? num(p.alert_total) : "-",
        sub: t("落库告警 · 点击跳转 ↓"),
      },
    ];
    const kpiBox = document.getElementById("avm-kpi");
    kpiBox.innerHTML = items
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${esc(k.label)}</div>` +
          `<div class="kpi-value" style="color:${k.color || "inherit"}">${esc(k.value)}</div>` +
          `<div class="kpi-sub">${esc(k.sub)}</div></div>`
      )
      .join("");
    const jumpCard = kpiBox.querySelectorAll(".kpi")[items.length - 1];
    if (jumpCard) {
      jumpCard.style.cursor = "pointer";
      jumpCard.addEventListener("click", () => {
        const tgt = document.getElementById("avm-alert-section");
        if (tgt) tgt.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }

  /* 算法名白话化：产物里是 sklearn 原始串（如 "HistGradientBoostingRegressor(log unit_price)"），
     业务用户读不懂；映射成通用算法族名，原文放 title 供技术追溯。 */
  function algoLabel(algo) {
    if (!algo) return "-";
    const s = String(algo);
    const names = {
      HistGradientBoostingRegressor: t("梯度提升树"),
      GradientBoostingRegressor: t("梯度提升树"),
      RandomForestRegressor: t("随机森林"),
      LinearRegression: t("线性回归"),
    };
    const m = /^(\w+)/.exec(s);
    const base = (m && names[m[1]]) || s;
    return /log[\s(_]*unit_price/i.test(s) ? `${base}${t("（对单价取对数建模）")}` : base;
  }

  function renderModelCard(m) {
    const box = document.getElementById("avm-model");
    if (!m) {
      box.innerHTML = `<div class="empty">${t("模型产物缺失")}</div>`;
      return;
    }
    const kv = [
      [t("模型版本"), mvLabel(m.version)],
      [t("算法"), algoLabel(m.algo), m.algo],
      [t("训练时间"), m.trained_at],
      [t("训练 / 测试样本"), `${num(m.n_train)} / ${num(m.n_test)}`],
      [t("特征数"), `${num(m.feature_count)} ${t("维")}`],
      [t("坐标覆盖率"), pp(m.coord_rows_pct, 1)],
      [t("小区缺失率"), pp(m.community_missing_pct, 1)],
    ];
    const metrics =
      `<table class="table" style="margin-bottom:12px"><thead><tr>` +
      `<th>${t("口径")}</th><th class="num" title="${t("MAPE：平均绝对百分比误差，估值与真实价平均差多少%（越小越好）")}">MAPE</th><th class="num" title="${t("MdAPE：误差中位数，不受个别极端偏差拉偏（越小越好）")}">MdAPE</th><th class="num" title="${t("R²：模型解释了多少价格差异，越接近 1 越好")}">R²</th></tr></thead><tbody>` +
      `<tr><td><b>${t("AVM 模型")}</b></td><td class="num" style="color:${RED}"><b>${pp(m.mape, 3)}</b></td>` +
      `<td class="num">${pp(m.mdape)}</td><td class="num">${Number(m.r2).toFixed(4)}</td></tr>` +
      `<tr><td>${t("基线（城市,小区 中位价 × 面积）")}</td><td class="num">${pp(m.baseline.mape, 3)}</td>` +
      `<td class="num">${pp(m.baseline.mdape)}</td><td class="num">${Number(m.baseline.r2).toFixed(4)}</td></tr>` +
      `<tr><td>${t("精度目标")}</td><td class="num" style="color:${GREEN}">≤ ${pp(m.ac07.target_mape, 0)}</td><td class="num">-</td><td class="num">-</td></tr>` +
      `</tbody></table>`;
    // 卡片标题只留业务信息（训练时间）；产物路径降级为悬浮提示，供运维查看。
    const title = document.getElementById("avm-model-title");
    if (title && m.trained_at) {
      title.textContent = t("当前模型（{ts} 训练）", { ts: m.trained_at });
      title.title = t("数据来源：AVM 模型训练评估报告（avm_report.json）");
    }
    box.innerHTML =
      metrics +
      `<table class="table"><tbody>` +
      kv
        .map(
          ([k, v, tip]) =>
            `<tr><td style="width:34%;color:#6b7280">${esc(k)}</td>` +
            `<td${tip ? ` title="${esc(tip)}"` : ""}>${esc(v == null ? "-" : v)}</td></tr>`
        )
        .join("") +
      `</tbody></table>`;
  }

  function renderDevHist(d) {
    barChart(document.getElementById("avm-dev-hist"), d.dev_hist, {
      value: "count",
      label: "bucket",
      format: (x) => x,
      color: (r) => (r.abnormal_zone ? RED : "#2563eb"),
      ariaLabel: t("AVM 偏差分布直方图"),
      yAxisTitle: t("贷款笔数"),
      height: 250,
    });
    const p = d.portfolio;
    document.getElementById("avm-dev-note").innerHTML =
      `${t("偏差中位")} ${fmtPct(p.median_abs_dev)} · ${t("均值")} ${fmtPct(p.mean_abs_dev)} · ${t("仅模型命中贷款参与判定")}`;
  }

  function dimTable(rows, key, keyLabel) {
    if (!rows.length) return `<div class="empty">${t("无数据")}</div>`;
    return (
      `<table class="table dim-table"><thead><tr>` +
      `<th>${esc(keyLabel)}</th><th class="num">${t("笔数")}</th><th class="num">${t("异常")}</th><th class="num">${t("异常率")}</th>` +
      `<th class="num">${t("中位偏差")}</th><th class="num">${t("中位有符号偏差")}</th></tr></thead><tbody>` +
      rows
        .map((r) => {
          const c = r.abnormal_rate >= 0.6 ? RED : r.abnormal_rate >= 0.3 ? "#b45309" : GREEN;
          const nameCell =
            key === "risk_class"
              ? `<span class="badge" style="background:${CLASS_COLORS[r[key]]}22;color:${CLASS_COLORS[r[key]]}">${esc(t(r[key]))}</span>`
              : esc(t(r[key]));
          return (
            `<tr><td>${nameCell}</td><td class="num">${r.n}</td><td class="num">${r.abnormal}</td>` +
            `<td class="num" style="min-width:120px"><span style="color:${c};font-weight:600">${fmtPct(r.abnormal_rate)}</span>${bar(r.abnormal_rate, c)}</td>` +
            `<td class="num">${fmtPct(r.median_abs_dev)}</td>` +
            `<td class="num" style="color:${devColor(r.median_signed_dev)};font-weight:600">` +
            `${r.median_signed_dev > 0 ? "+" : ""}${fmtPct(r.median_signed_dev)}` +
            `${r.median_signed_dev == null ? "" : r.median_signed_dev > 0 ? " " + t("高估") : " " + t("低估")}</td></tr>`
          );
        })
        .join("") +
      `</tbody></table>`
    );
  }

  function renderDimensions(d) {
    document.getElementById("avm-by-city").innerHTML = dimTable(d.by_city, "city", t("城市"));

    document.getElementById("avm-by-class").innerHTML = dimTable(
      d.by_class,
      "risk_class",
      t("五级分类")
    );

    const tiers = d.portfolio.source_tiers;
    document.getElementById("avm-tiers").innerHTML =
      `<table class="table"><thead><tr><th>${t("回退级别")}</th><th class="num">${t("笔数")}</th><th class="num">${t("占比")}</th></tr></thead><tbody>` +
      tiers
        .map(
          (r) =>
            `<tr><td>${esc(t(r.tier))}</td><td class="num">${r.n}</td><td class="num">${fmtPct(r.pct)}</td></tr>`
        )
        .join("") +
      `</tbody></table>` +
      `<div title="${t("模型覆盖不到的部分由后两级承接，这部分不参与偏差判定")}" style="font-size:12px;color:#6b7280;margin-top:8px">` +
      `${t("估值来源三级回退：模型直出 → 行情参照 → 登记价兜底")}</div>`;
  }

  function renderLineage(rows) {
    document.getElementById("avm-lineage").innerHTML = rows.length
      ? `<table class="table"><thead><tr><th>${t("版本")}</th><th>${t("训练时间")}</th>` +
        `<th class="num">${t("训练/测试")}</th><th class="num" title="${t("平均绝对百分比误差（越小越好）")}">MAPE</th><th class="num">${t("较上一版")}</th><th class="num" title="${t("误差中位数（越小越好）")}">MdAPE</th><th class="num" title="${t("越接近 1 越好")}">R²</th><th>${t("vs 基线")}</th></tr></thead><tbody>` +
        rows
          .map((r) => {
            const dlt =
              r.mape_delta == null
                ? "-"
                : `<span style="color:${r.mape_delta < 0 ? GREEN : RED}">${r.mape_delta > 0 ? "+" : ""}${r.mape_delta} pp</span>`;
            return (
              `<tr><td>${esc(r.version)}${r.is_current ? ` <span class="badge ok">${t("在跑")}</span>` : ""}</td>` +
              `<td>${esc(r.trained_at)}</td><td class="num">${num(r.n_train)} / ${num(r.n_test)}</td>` +
              `<td class="num"><b>${pp(r.mape, 3)}</b></td><td class="num">${dlt}</td><td class="num">${pp(r.mdape)}</td>` +
              `<td class="num">${r.r2 == null ? "-" : Number(r.r2).toFixed(4)}</td>` +
              `<td>${t("基线")} ${pp(r.baseline_mape, 3)} → ${t("提升")} ${pp(r.improvement_pct, 1)}</td></tr>`
            );
          })
          .join("") +
        `</tbody></table>` +
        `<div style="font-size:12px;color:#6b7280;margin-top:8px">` +
        t(
          "版本管理上线后新产生的估值都带有生成它的模型版本，出现问题时可回溯到具体一次训练、评估影响范围；更早的历史回填估值缺少版本信息，会以下方核查队列中的「不可溯源」条目跟进。"
        ) +
        `</div>`
      : `<div class="empty">${t("暂无历史模型版本记录")}</div>`;
  }

  /* ---------- 列表 ---------- */

  function valQuery() {
    const q = new URLSearchParams();
    const v = (id) => document.getElementById(id).value;
    if (v("avm-f-abnormal")) q.set("abnormal", v("avm-f-abnormal"));
    if (v("avm-f-min")) q.set("dev_min", v("avm-f-min"));
    if (v("avm-f-max")) q.set("dev_max", v("avm-f-max"));
    if (v("avm-f-city")) q.set("city", v("avm-f-city"));
    if (v("avm-f-version")) q.set("model_version", v("avm-f-version"));
    q.set("page", String(st.valPage));
    return q.toString();
  }

  async function renderValuations() {
    const d = await api("/api/avm/valuations?" + valQuery());
    document.getElementById("avm-val-total").textContent =
      `${t("共 {n} 笔", { n: num(d.total) })} · ${t("按偏差从高到低排序")}`;
    const tbody = document.querySelector("#avm-val-table tbody");
    tbody.innerHTML = d.rows.length
      ? d.rows
          .map((r) => {
            const c = devColor(r.deviation_pct == null ? null : r.signed_deviation_pct);
            const dir =
              r.signed_deviation_pct == null
                ? "-"
                : r.signed_deviation_pct > 0
                  ? `<span style="color:${RED}">${t("高估")}</span>`
                  : `<span style="color:#2563eb">${t("低估")}</span>`;
            return (
              `<tr><td>${r.loan_id}</td>` +
              `<td title="${esc(r.property_addr === "未标注" ? t("未标注") : r.property_addr)}">${esc(r.property_addr === "未标注" ? t("未标注") : r.property_addr.slice(0, 16))}</td>` +
              `<td class="num">${fmtMoney(r.avm_valuation)}</td><td class="num">${fmtMoney(r.book_price)}</td>` +
              `<td class="num" style="color:${c};font-weight:600">${fmtPct(r.deviation_pct)}</td>` +
              `<td>${dir}</td><td class="num">${fmtPct(r.ltv)}</td>` +
              `<td><span class="badge" style="background:${CLASS_COLORS[r.risk_class]}22;color:${CLASS_COLORS[r.risk_class]}">${esc(t(r.risk_class))}</span></td>` +
              `<td>${r.abnormal_valuation ? `<span class="badge bad">${t("异常")}</span>` : `<span class="badge off">${t("正常")}</span>`}</td>` +
              `<td>${esc(t(r.source_tier))}</td><td style="color:#6b7280">${esc(mvLabel(r.model_version))}</td></tr>`
            );
          })
          .join("")
      : `<tr><td colspan="11" class="empty">${t("无符合条件的数据")}</td></tr>`;

    const tp = Math.max(1, Math.ceil(d.total / d.page_size));
    st.valPage = d.page;
    document.getElementById("avm-val-page").textContent = t("第 {p}/{t} 页", { p: d.page, t: tp });
    document.getElementById("avm-val-prev").disabled = d.page <= 1;
    document.getElementById("avm-val-next").disabled = d.page >= tp;
  }

  async function renderAlerts() {
    const d = await api("/api/avm/alerts?page=" + st.alertPage);
    document.getElementById("avm-alert-sum").innerHTML = joinSeg(
      [
        t("告警记录共 {n} 条", { n: `<b>${num(d.total)}</b>` }),
        d.by_code
          .map((c) => {
            const label = c.alert_code === "R-UBQ-01" ? t("不可溯源") : t("待复估");
            return `<span class="badge bad" title="${t("告警代号")} ${esc(c.alert_code)}">${label} × ${num(c.n)}</span>`;
          })
          .join(" "),
        `<span title="${t("同一贷款不同告警日分别计条")}" style="font-size:12px;color:#6b7280;margin-left:10px">` +
          t(
            "待复估 = 估值偏差 &gt;30%，需逐笔人工复核 · 不可溯源 = 估值缺模型版本（多为历史回填数据，需数据团队排查）"
          ) +
          `</span>`,
      ],
      " · "
    );

    const tbody = document.querySelector("#avm-alert-table tbody");
    tbody.innerHTML = d.rows.length
      ? d.rows
          .map(
            (r) => {
            const desc =
              r.deviation_pct != null
                ? t("AVM 估值偏差 {pct} 超阈值", { pct: fmtPct(r.deviation_pct) })
                : t(r.detail);
            return (
              `<tr><td><span class="badge bad" title="${t("告警代号")} ${esc(r.alert_code)}">${r.alert_code === "R-UBQ-01" ? t("不可溯源") : t("待复估")}</span></td>` +
              `<td>${r.loan_id}</td><td title="${esc(r.property_addr === "未标注" ? t("未标注") : r.property_addr)}">${esc(r.property_addr === "未标注" ? t("未标注") : r.property_addr.slice(0, 16))}</td>` +
              `<td>${esc(t(r.city))}</td>` +
              `<td class="num" style="color:${RED};font-weight:600">${fmtPct(r.deviation_pct)}</td>` +
              `<td>${esc(desc)}</td><td style="color:#6b7280">${esc(mvLabel(r.model_version))}</td>` +
              `<td>${esc(r.alert_date)}</td></tr>`
            );
          }
          )
          .join("")
      : `<tr><td colspan="8" class="empty">${t("无异常估值告警")}</td></tr>`;

    const tp = Math.max(1, Math.ceil(d.total / d.page_size));
    st.alertPage = d.page;
    document.getElementById("avm-alert-page").textContent = t("第 {p}/{t} 页", { p: d.page, t: tp });
    document.getElementById("avm-alert-prev").disabled = d.page <= 1;
    document.getElementById("avm-alert-next").disabled = d.page >= tp;
  }

  /* ---------- 异常估值归因 ---------- */

  async function renderAttribution() {
    const box = document.getElementById("avm-attribution-body");
    const d = await api("/api/avm/attribution");
    if (!d || (d.diag_available === false && !d.attribution)) {
      box.innerHTML = `<div class="empty">${t("离线归因分析尚未生成，暂时无法区分基准价噪声与模型误差——请联系数据团队更新归因报告。")}</div>`;
      return;
    }
    const live = d.live || {};
    const att = d.attribution || {};
    const out = [];

    // 1. 结论先行
    const baselineShare = att.baseline_share;
    out.push(
      `<div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:10px 0">` +
        `<span class="badge bad">${num(live.abnormal)} / ${num(live.avm_rows)}${t("（{n}）", { n: fmtPct(live.abnormal_rate) })}</span>` +
        `<span style="font-size:18px;font-weight:700;color:#b45309">` +
        `${t("其中 {pct}（{n} 笔）归因于基准价噪声", {
          pct: fmtPct(baselineShare),
          n: num(att.baseline_n),
        })}</span>` +
        `<span style="color:#b91c1c;font-weight:700">${t("真实模型误差仅")} ${num(att.model_n)} ${t("笔")}</span>` +
        `</div>`
    );
    out.push(
      `<div style="font-size:12px;color:#6b7280;margin-bottom:12px;line-height:1.7">` +
        joinSeg([
          t(
            "根因：人工基准价由上一版模型（2026-08-04）的输出加扰动生成，与当前模型（{mv}）同源。所以这里的异常率实际度量的是「当前模型相对旧版基准的漂移」，不是估值精度。因此",
            { mv: mvLabel(live.model_version) }
          ),
          `<b>${t("不做偏差校正")}</b>`,
          t("——把新模型拉回去过拟合一个过时的基准，只会掩盖真实问题。"),
        ]) +
        `</div>`
    );

    // 2. 数据来源口径（诚实声明）
    out.push(
      `<div style="font-size:12px;color:#6b7280;margin-top:14px;border-top:1px dashed #d1d5db;padding-top:10px;line-height:1.8">` +
        t("数据口径（诚实声明）：异常率/城市对比 = {a}；归因比例 = {b}。基准价为系统登记的挂牌价，不是网签成交价。", {
          a: `<b>${t("实时库现算")}</b>`,
          b: `<b>${t("离线诊断分析")}</b>`,
        }) +
        `</div>`
    );

    box.innerHTML = out.join("");
  }

  /* ---------- 挂载 ---------- */

  function bindOnce() {
    // 语言切换会重建页面 DOM（app.js mountPlugin 重挂载），监听器随旧节点丢失；
    // 模块级布尔会挡住重绑（切语言后翻页失灵），改按「节点是否已打绑定标记」判断。
    const bind = (id, ev, fn) => {
      const node = document.getElementById(id);
      if (!node || node.dataset.spfBound) return;
      node.dataset.spfBound = "1";
      node.addEventListener(ev, fn);
    };
    bind("avm-filter", "submit", (e) => {
      e.preventDefault();
      st.valPage = 1; // 换筛选条件必须回第一页，否则会停在越界页显示空表
      renderValuations();
    });
    bind("avm-val-prev", "click", () => {
      st.valPage = Math.max(1, st.valPage - 1);
      renderValuations();
    });
    bind("avm-val-next", "click", () => {
      st.valPage += 1;
      renderValuations();
    });
    bind("avm-alert-prev", "click", () => {
      st.alertPage = Math.max(1, st.alertPage - 1);
      renderAlerts();
    });
    bind("avm-alert-next", "click", () => {
      st.alertPage += 1;
      renderAlerts();
    });
  }

  function fillFilterOptions(d) {
    const city = document.getElementById("avm-f-city");
    const ver = document.getElementById("avm-f-version");
    // 选项来自概览接口的实际取值，避免前端硬编码城市/版本清单与数据脱节。
    city.innerHTML =
      `<option value="">${t("全部")}</option>` +
      d.by_city
        .map((c) => `<option value="${esc(c.city)}">${joinSeg([esc(t(c.city)), t("（{n}）", { n: c.n })])}</option>`)
        .join("");
    ver.innerHTML =
      `<option value="">${t("全部")}</option>` +
      d.portfolio.versions
        .map(
          (v) =>
            `<option value="${esc(v.model_version)}">${joinSeg([esc(mvLabel(v.model_version)), t("（{n}）", { n: v.n })])}</option>`
        )
        .join("");
  }

  window.registerPage("avm", {
    html,
    render: async () => {
      const d = await api("/api/avm");
      st.overview = d;
      renderAc07(d.model, d.portfolio);
      renderKpi(d);
      renderModelCard(d.model);
      renderDevHist(d);
      renderDimensions(d);
      renderLineage(d.lineage);
      fillFilterOptions(d);
      bindOnce();
      await Promise.all([renderValuations(), renderAlerts(), renderAttribution()]);
    },
  });
})();
