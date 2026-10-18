/* SpaceFin 前端驾驶舱 · 单页交互逻辑（vanilla JS，无框架无 CDN）。
 * RBAC 由服务端强制校验，这里仅根据 /api/me 返回的 pages/can_confirm/can_export
 * 裁剪导航与操作按钮（第二层防御，主要是 UX 层面）。 */

const state = { user: null, currentPage: "dashboard", alertsPage: 1 };

/* ---------------- 多语言字典（key = zh-CN 原文，zh-CN 默认返回原文） ----------------
 * 静态测试对 JS 源码做中文字面量断言，因此源码里一律写 t("中文原文")，译文只存字典。
 * 数据键（五级分类名、城市名、bucket 等）保持中文原文用于匹配后端，仅展示层经 t() 翻译。
 */
registerI18n("en", {
    离线预警: "Offline Alerts",
    风险宽表: "Risk Wide Table",
    房源挂牌: "Housing Listings",
  /* 导航分组 */
    资产总览: "Overview",
    风险监控与预警: "Monitoring & Alerts",
    策略推演: "Strategy Simulation",
    报送与合规: "Reporting & Compliance",
    数据底座: "Data Foundation",
  /* 侧栏页面标签：后端随用户权限下发中文 label，导航用 t(p.label) 翻译。
   * 这些是「全站可见」的文案——侧栏在任一页面都渲染，而各页字典是按页 ns 隔离的，
   * 放在页面 ns 里会导致只有该页激活时才译得出，故一律落在 global。 */
    数据底座接入配置: "Data Source Integration",
    迁徙与降级追踪: "Migration & Downgrade Tracking",
    空间风险画像: "Spatial Risk Profiling",
  // 与 P6 页内既有译法保持一致（该页的 Run Deduction / Deduction records 同词根）。
    策略推演中心: "Policy Deduction Center",
    "AVM 估值管理": "AVM Valuation Management",
    合规审计: "Compliance Audit",
  /* 登录 / 外壳 */
    "SpaceFin · 房产金融风险决策支持平台": "SpaceFin · Property Finance Risk Decision Platform",
    房产金融风险决策支持平台: "Property Finance Risk Decision Platform",
    账号: "Username",
    密码: "Password",
    "如 admin": "e.g. admin",
    "登 录": "Log In",
    展开导航: "Expand navigation",
    退出: "Logout",
    账号或密码错误: "Incorrect username or password",
    当前角色无此操作权限: "Your role does not have permission for this operation",
    "内部管理(风控/分析/管理员)": "Internal Management (Risk/Analysis/Admin)",
    柱状图: "Bar chart",
    横向条形图: "Horizontal bar chart",
    加载失败: "Failed to load",
    重试: "Retry",
  /* 驾驶舱 */
    资产质量驾驶舱: "Asset Quality Cockpit",
    数据底座可信度: "Data Foundation Trust",
    "以下指标反映支撑本页结论的数据规模、时效与质量":
      "These metrics reflect the data scale, freshness and quality behind this page's conclusions",
    预警处置闭环: "Alert Disposal Loop",
    五级分类分布: "Five-Tier Classification Distribution",
    "LTV 分布直方图": "LTV Distribution Histogram",
    区域贷款分布: "Regional Loan Distribution",
    "{city}：{count} 笔 · 高危 {hr}（{pct}%）": "{city}: {count} {count|loan|loans} · high-risk {hr} ({pct}%)",
    "颜色档位 = 高危网格贷款占比（青低 → 红高）":
      "Color = high-risk-grid loan share (cyan low → red high)",
    "LTV 预警概览（{n} 条）": "LTV Alert Overview ({n} {n|record|records})",
    // index.html 的 data-i18n="LTV 预警概览"（无 {n} 变体）整句注册
    "LTV 预警概览": "LTV Alert Overview",
    "总敞口（余额）": "Total Exposure (Balance)",
    预警贷款: "Alerted Loans",
    低置信笔数: "Low-Confidence Loans",
    位于高危网格贷款: "Loans in High-Risk Grids",
    五级分类分布柱状图: "Five-tier classification distribution bar chart",
    "贷款余额（元）": "Loan balance (CNY)",
    贷款笔数: "Loan count",
    强预警: "Strong alert",
    警示: "Warning",
    安全: "Safe",
    笔: "{n|loan|loans}",
    高危: "high-risk",
    条: "{n|record|records}",
    条挂牌: "{n|listing|listings}",
    在售: "For sale",
    覆盖: "covering",
    城: "{n|city|cities}",
    坐标点: "coordinate points",
    新鲜: "Fresh",
    略有延迟: "Slight delay",
    已滞后: "Stale",
    暂无数据: "No data",
    分钟前: "min ago",
    小时前: "h ago",
    天前: "d ago",
    更新于: "Updated",
    暂无分层数据: "No per-layer data",
    数据新鲜度: "Data Freshness",
    地址解析质量: "Address Geocoding Quality",
    "已解析中成功率（成功 {succ} · 失败 {fail}）": "Success rate among parsed (succeeded {succ} · failed {fail})",
    "；另有 {pending}% 待解析（排队中，非解析失败）": "; another {pending}% pending (queued, not a parse failure)",
    预警: "Alerts",
    确认: "Confirm",
    处置: "Dispose",
    解除: "Recover",
    未标注: "Unlabeled",
    缺失: "Missing",
  /* 预警列表 */
    "LTV 爆仓预警列表": "LTV Margin-Call Alert List",
    五级分类: "Risk Tier",
    全部: "All",
    正常: "Normal",
    "万": "×10K",
    "亿": "×100M",
    关注: "Watch",
    次级: "Substandard",
    可疑: "Doubtful",
    损失: "Loss",
    来源: "Source",
    "离线 T+1": "Offline T+1",
    实时流: "Real-time stream",
    城市: "City",
    "广州": "Guangzhou",
    "深圳": "Shenzhen",
    "惠州": "Huizhou",
    "东莞": "Dongguan",
    "佛山": "Foshan",
    "中山": "Zhongshan",
    "珠海": "Zhuhai",
    "江门": "Jiangmen",
    "肇庆": "Zhaoqing",
    "汕头": "Shantou",
    "汕尾": "Shanwei",
    "潮州": "Chaozhou",
    "揭阳": "Jieyang",
    "梅州": "Meizhou",
    "河源": "Heyuan",
    "清远": "Qingyuan",
    "韶关": "Shaoguan",
    "湛江": "Zhanjiang",
    "茂名": "Maoming",
    "阳江": "Yangjiang",
    "云浮": "Yunfu",
    日期从: "Date from",
    日期至: "Date to",
    搜索: "Search",
    "如 0.85": "e.g. 0.85",
    "如 2": "e.g. 2",
    "贷款号/客户号": "Loan ID / customer no.",
    查询: "Search",
    "导出 CSV": "Export CSV",
    "导出已脱敏（客户号 c****后4位）· 动作留痕": "Exports masked (customer c****+last4) · audited",
    "导出文件中客户号按分级脱敏（c****+后4位），并记录导出人/时间/筛选条件":
      "Customer numbers in exports are masked per tier (c****+last4); exporter/time/filters are audited",
    预警等级: "Alert Level",
    贷款号: "Loan ID",
    贷款余额: "Loan Balance",
    高危网格: "High-Risk Grid",
    闭环状态: "Loop Status",
    操作: "Action",
    详情: "Details",
    上一页: "Prev",
    下一页: "Next",
    强预警级: "Strong",
    警示级: "Warning",
    共: "Total",
    条预警: "{n|alert|alerts}",
    无符合条件的数据: "No matching data",
    低置信: "Low confidence",
    "抵押物空间特征严重缺失，估值置信度不足，该 LTV 仅供参考，需人工复核":
      "Collateral spatial features are severely missing; valuation confidence is low. This LTV is for reference only and needs manual review",
    未确认: "Unconfirmed",
    已确认待处置: "Confirmed, pending disposal",
    处置中: "Disposing",
    已恢复: "Recovered",
    恢复: "Recover",
    第: "Page",
    页: "of",
    收起: "Collapse",
    上一笔: "Previous",
    下一笔: "Next",
    贷款: "Loan",
    估值与余额: "Valuation & Balance",
    市场估值: "Market Valuation",
    缺口: "Gap",
    超额: "Over",
    抵押物信息: "Collateral Info",
    详细地址: "Address",
    抵押物号: "Collateral ID",
    客户号: "Customer ID",
    风险因子: "Risk Factors",
    空间特征低置信: "Low-confidence spatial features",
    估值偏差: "Valuation deviation",
    模型: "Model",
    确认与处置: "Confirm & Dispose",
    确认记录: "Confirmation",
    处置记录: "Disposal",
    预警日期: "Alert Date",
    警示线: "Warning line",
    强预警线: "Strong-alert line",
    "爆仓线 100%": "Margin-call line 100%",
    "100%爆仓": "100% margin call",
    处置建议: "Disposal Suggestion",
    依据: "Basis",
    "确认成功：预警已确认": "Confirmed: alert confirmed",
    确认失败: "Confirm failed",
    "处置成功：状态已更新": "Disposed: status updated",
    "恢复成功：状态已更新": "Recovered: status updated",
    处置失败: "Dispose failed",
    恢复失败: "Recover failed",
  /* 1104 报送 */
    "1104 G11 资产质量报送": "1104 G11 Asset Quality Report",
    业务日期: "Business Date",
    报送: "Submit",
    已报送: "Submitted",
    重新校验: "Re-check",
    更多: "More",
    重建快照: "Rebuild Snapshot",
    "仅在快照与风险明细口径漂移时使用：按明细重算并覆盖该业务日快照，覆盖后需重新报送。":
      "Use only when the snapshot drifts from risk details: recompute from details and overwrite this business day's snapshot; re-submit afterwards.",
    "客户号已脱敏（c****后4位）· 动作留痕": "Customer numbers masked (c****+last4) · audited",
    "报送与导出内容中客户号按分级脱敏（c****+后4位），操作全程留痕可审计":
      "Customer numbers in report/exports masked per tier (c****+last4); all operations audited",
    "G11 模板（五级 + 合计）": "G11 Template (five tiers + total)",
    风险类: "Risk Class",
    笔数: "Count",
    "余额（元）": "Balance (CNY)",
    余额占比: "Balance Share",
    口径一致性校验: "Consistency Check",
    阻断告警记录: "Blocking Alert History",
    待报送: "Pending",
    校验中: "Checking",
    口径一致: "Consistent",
    已阻断: "Blocked",
    该业务日尚无可报送快照: "No snapshot available for this business date",
    "正在实时复算口径一致性（只读，不落库）":
      "Recomputing consistency in real time (read-only, not persisted)",
    "与风险明细聚合口径一致，可提交报送": "Consistent with risk-detail aggregation; can be submitted",
    该业务日已报送: "This business date has been submitted",
    "口径不一致，报送已阻断": "Inconsistent; submission blocked",
    "口径已校验一致，可提交报送": "Consistency verified; ready to submit",
    已于: "Submitted at",
    由: "by",
    "口径不一致，报送已阻断 · 告警已发（": "Inconsistent; blocked · alerts sent (",
    曾于: "previously submitted at",
    "报送，快照已变更需重报": "; snapshot changed, re-submit required",
    "校验中：正在实时复算口径一致性…": "Checking: recomputing consistency…",
    "与风险明细聚合口径一致（笔数/余额均在容差内）":
      "Consistent with risk-detail aggregation (counts/balances within tolerance)",
    "以下口径与风险明细聚合不一致，报送已阻断：":
      "The following items are inconsistent with risk-detail aggregation; submission blocked:",
    分类: "Class",
    指标: "Metric",
    "1104 快照": "1104 snapshot",
    明细聚合: "Detail aggregation",
    "差（快照−明细）": "Diff (snapshot − detail)",
    "常见原因：快照生成后风险明细又发生变化（新增贷款或五级分类调整）。":
      "Common cause: risk details changed after the snapshot (new loans or re-tiering).",
    "以「明细聚合」为准重建快照即可恢复一致。":
      "Rebuild the snapshot from 'detail aggregation' to restore consistency.",
    按明细重建快照: "Rebuild snapshot from details",
    校验时间: "Checked at",
    尚无报送数据: "No report data",
    合计: "Total",
    暂无历史告警: "No historical alerts",
    本次校验: "This check",
    "口径不一致，已阻断本次报送": "Inconsistent; this submission blocked",
    请先选择业务日期: "Please select a business date first",
    校验完成: "Check complete",
    重新校验失败: "Re-check failed",
    报送成功: "Submitted",
    已标记为已报送: "has been marked as submitted",
    报送失败: "Submit failed",
    重建成功: "Rebuild succeeded",
    重建了: "rebuilt",
    行: "rows",
    重建快照失败: "Snapshot rebuild failed",
    "报送快照写入失败：字段内容超出长度限制，请点「重建快照」重试":
      "Snapshot write failed: field content exceeds length limit; click 'Rebuild Snapshot' to retry",
    "报送快照写入失败：数值超出字段范围，请点「重建快照」重试":
      "Snapshot write failed: value out of field range; click 'Rebuild Snapshot' to retry",
    "报送快照写入失败：与已有记录主键冲突，请点「重建快照」重试":
      "Snapshot write failed: primary key conflicts with an existing record; click 'Rebuild Snapshot' to retry",
    "报送快照写入异常，请点「重建快照」重试":
      "Snapshot write error; click 'Rebuild Snapshot' to retry",
    口径不一致: "Inconsistency",
    "「五级分类余额占比」之和不等于 100%（G11 恒等式校验不通过）":
      "Sum of 'five-tier balance shares' ≠ 100% (G11 identity check failed)",
    余额: "Balance",
    "「": "“",
    "」与「": "” vs “",
    "」在「": "” on “",
    "」类的「": "” tier, metric “",
    "」口径不一致": "” inconsistent",
  /* 数据底座信任卡 */
    房源数据规模: "Listing Data Scale",
  /* 确认/处置二次确认 */
    将对贷款: "Will perform on loan",
    "执行「": " execute “",
    "」，状态将变为「": "”. Status will become “",
    "」。确认？": "”. Confirm?",
    "确认该笔预警（贷款": "Confirm this alert (loan",
    "）？确认后状态将变为「": ")？ After confirmation the status will become “",
    "」。": "”.",
    将把: "Will mark",
    "的 G11 报表标记为「": "'s G11 report as “",
    "」，确认？": "”. Confirm?",
    "将用风险明细重建该日 G11 快照并覆盖，不可撤销；覆盖后报送态回退，需重新报送。确认？":
      "Rebuild this business day's G11 snapshot from risk details and overwrite; irreversible. The submission status will reset and needs re-submission. Confirm?",
  /* 口径对照业务名 */
    "1104 报送快照": "1104 report snapshot",
    风险明细聚合: "risk-detail aggregation",
    "1104 模板行": "1104 template row",
    内部累计口径: "internal cumulative scope",
  /* 报送时间线 */
    "实时复算结论并入时间线顶部；历史告警由报送流程写入":
      "Real-time check result merged at the top of the timeline; historical alerts are written by the submission flow",
  /* 公共术语层：策略动作（p6 规则/推演、p9 审计共用）与行业 15 门类
   * （tools/risk/config.py INDUSTRY_CATEGORIES 的展示名；城市 21 城见上）。 */
    行业: "Industry",
    网格: "Grid",
    拒贷: "Reject",
    加严审批: "Tightened Approval",
    仅提示: "Notify Only",
    "交通运输、仓储和邮政业": "Transport & Logistics",
    "住宿和餐饮业": "Accommodation & Food",
    "信息传输、软件和信息技术服务业": "IT & Software",
    // 行业门类短名（INDUSTRY_CODES 的 value）：会作为数据值出现在组合压力 scope/group
    // 说明的 params 里，经 t() 回落 global 翻译，避免「Only count 制造业 borrowers」式混杂。
    "制造业": "Manufacturing",
    "建筑业": "Construction",
    "批发和零售业": "Wholesale & Retail",
    "指定范围": "specified scope",
    金融业: "Finance",
    房地产业: "Real Estate",
    "租赁和商务服务业": "Business Services",
    "科学研究和技术服务业": "Sci & Tech Services",
    教育: "Education",
    "卫生和社会工作": "Health & Social Work",
    "文化、体育和娱乐业": "Culture & Sports",
    "农、林、牧、渔业": "Agriculture, Forestry, Animal Husbandry & Fishery",
    "公共管理、社会保障和社会组织": "Public Administration & Social Security",
  /* 跨页公共术语上提：原 p3/p5/p6/p7/p9 各自注册的同 key 词条收敛进 global
   * （五级分类四页、分页/表格表头三页重复等）；en 取各页统一后的规范译法。
   * 场景特化译法（p6 表头「操作」= Actions、p6/p7「覆盖」= overwrite/coverage、
   * p6「启用/停用」动作义 vs p9 状态义）仍留在各页 ns，页 ns 优先于 global。 */
    五级: "Tier",
    "LTV 上限": "LTV Cap",
    "LTV 网格": "LTV Grid",
    "LTV 集中": "LTV Concentration",
    价格洼地: "Price Trough",
    价格网格: "Price Grid",
    作用域: "Scope",
    "共 ": "Total ",
    判定: "Verdict",
    动作: "Action",
    状态: "Status",
    是: "Yes",
    否: "No",
    数据源: "Source",
    抵押物: "Collateral",
    抵押物地址: "Collateral Address",
    命中规则: "Matched Rule",
    命中笔数: "Hit Count",
    命中余额: "Hit Balance",
    无数据: "No data",
    "加载中…": "Loading…",
    达标: "Pass",
    未达标: "Fail",
    "规则：": "Rule: ",
    "：": ": ",
});
registerI18n("zh-HK", {
  离线预警: "離線預警",
  风险宽表: "風險寬表",
  房源挂牌: "房源掛牌",
    资产总览: "資產總覽",
    风险监控与预警: "風險監控與預警",
    策略推演: "策略推演",
    报送与合规: "報送與合規",
    数据底座: "數據底座",
  /* 侧栏页面标签（与 en 块同 key 集，落在 global 供全站导航使用） */
    数据底座接入配置: "數據底座接入配置",
    迁徙与降级追踪: "遷徙與降級追蹤",
    空间风险画像: "空間風險畫像",
    策略推演中心: "策略推演中心",
    "AVM 估值管理": "AVM 估值管理",
    合规审计: "合規審計",
    "SpaceFin · 房产金融风险决策支持平台": "SpaceFin · 房產金融風險決策支持平台",
    房产金融风险决策支持平台: "房產金融風險決策支持平台",
    账号: "帳號",
    密码: "密碼",
    "如 admin": "如 admin",
    "登 录": "登 錄",
    展开导航: "展開導航",
    退出: "退出",
    账号或密码错误: "帳號或密碼錯誤",
    当前角色无此操作权限: "當前角色無此操作權限",
    "内部管理(风控/分析/管理员)": "內部管理(風控/分析/管理員)",
    柱状图: "柱狀圖",
    横向条形图: "橫向條形圖",
    加载失败: "載入失敗",
    重试: "重試",
    资产质量驾驶舱: "資產質量駕駛艙",
    数据底座可信度: "數據底座可信度",
    "以下指标反映支撑本页结论的数据规模、时效与质量":
      "以下指標反映支撐本頁結論的數據規模、時效與質量",
    预警处置闭环: "預警處置閉環",
    五级分类分布: "五級分類分佈",
    "LTV 分布直方图": "LTV 分佈直方圖",
    区域贷款分布: "區域貸款分佈",
    "{city}：{count} 笔 · 高危 {hr}（{pct}%）": "{city}：{count} 筆 · 高危 {hr}（{pct}%）",
    "颜色档位 = 高危网格贷款占比（青低 → 红高）": "顏色檔位 = 高危網格貸款佔比（青低 → 紅高）",
    "LTV 预警概览（{n} 条）": "LTV 預警概覽（{n} 條）",
    "LTV 预警概览": "LTV 預警概覽",
    "总敞口（余额）": "總敞口（餘額）",
    预警贷款: "預警貸款",
    低置信笔数: "低置信筆數",
    位于高危网格贷款: "位於高危網格貸款",
    五级分类分布柱状图: "五級分類分佈柱狀圖",
    "贷款余额（元）": "貸款餘額（元）",
    贷款笔数: "貸款筆數",
    强预警: "強預警",
    警示: "警示",
    安全: "安全",
    笔: "筆",
    高危: "高危",
    条: "條",
    亿: "億",
    万: "萬",
    条挂牌: "條掛牌",
    在售: "在售",
    覆盖: "覆蓋",
    城: "城",
    坐标点: "座標點",
    新鲜: "新鮮",
    略有延迟: "略有延遲",
    已滞后: "已滯後",
    暂无数据: "暫無數據",
    分钟前: "分鐘前",
    小时前: "小時前",
    天前: "天前",
    更新于: "更新於",
    暂无分层数据: "暫無分層數據",
    数据新鲜度: "數據新鮮度",
    地址解析质量: "地址解析質量",
    "已解析中成功率（成功 {succ} · 失败 {fail}）": "已解析中成功率（成功 {succ} · 失敗 {fail}）",
    "；另有 {pending}% 待解析（排队中，非解析失败）": "；另有 {pending}% 待解析（排隊中，非解析失敗）",
    预警: "預警",
    确认: "確認",
    处置: "處置",
    解除: "解除",
    未标注: "未標註",
    缺失: "缺失",
    "LTV 爆仓预警列表": "LTV 爆倉預警列表",
    五级分类: "五級分類",
    全部: "全部",
    正常: "正常",
    关注: "關注",
    次级: "次級",
    可疑: "可疑",
    损失: "損失",
    来源: "來源",
    "离线 T+1": "離線 T+1",
    实时流: "實時流",
    城市: "城市",
  // 城市名（广东 21 城，无「市」后缀，与后端 city_name 一致；en 字典已含，zh-HK 补齐）
    "广州": "廣州",
    "深圳": "深圳",
    "珠海": "珠海",
    "汕头": "汕頭",
    "佛山": "佛山",
    "韶关": "韶關",
    "河源": "河源",
    "梅州": "梅州",
    "惠州": "惠州",
    "汕尾": "汕尾",
    "东莞": "東莞",
    "中山": "中山",
    "江门": "江門",
    "阳江": "陽江",
    "湛江": "湛江",
    "茂名": "茂名",
    "肇庆": "肇慶",
    "清远": "清遠",
    "潮州": "潮州",
    "揭阳": "揭陽",
    "云浮": "雲浮",
    日期从: "日期從",
    日期至: "日期至",
    搜索: "搜尋",
    "如 0.85": "如 0.85",
    "如 2": "如 2",
    "贷款号/客户号": "貸款號/客戶號",
    查询: "查詢",
    "导出 CSV": "匯出 CSV",
    "导出已脱敏（客户号 c****后4位）· 动作留痕": "匯出已脫敏（客戶號 c****後4位）· 動作留痕",
    "导出文件中客户号按分级脱敏（c****+后4位），并记录导出人/时间/筛选条件":
      "匯出檔案中客戶號按分級脫敏（c****+後4位），並記錄匯出人/時間/篩選條件",
    预警等级: "預警等級",
    贷款号: "貸款號",
    贷款余额: "貸款餘額",
    高危网格: "高危網格",
    闭环状态: "閉環狀態",
    操作: "操作",
    详情: "詳情",
    上一页: "上一頁",
    下一页: "下一頁",
    强预警级: "強預警級",
    警示级: "警示級",
    共: "共",
    条预警: "條預警",
    无符合条件的数据: "無符合條件的數據",
    低置信: "低置信",
    "抵押物空间特征严重缺失，估值置信度不足，该 LTV 仅供参考，需人工复核":
      "抵押物空間特徵嚴重缺失，估值置信度不足，該 LTV 僅供參考，需人工覆核",
    未确认: "未確認",
    已确认待处置: "已確認待處置",
    处置中: "處置中",
    已恢复: "已恢復",
    恢复: "恢復",
    第: "第",
    页: "頁",
    收起: "收起",
    上一笔: "上一筆",
    下一笔: "下一筆",
    贷款: "貸款",
    估值与余额: "估值與餘額",
    市场估值: "市場估值",
    缺口: "缺口",
    超额: "超額",
    抵押物信息: "抵押物資訊",
    详细地址: "詳細地址",
    抵押物号: "抵押物號",
    客户号: "客戶號",
    风险因子: "風險因子",
    空间特征低置信: "空間特徵低置信",
    估值偏差: "估值偏差",
    模型: "模型",
    确认与处置: "確認與處置",
    确认记录: "確認記錄",
    处置记录: "處置記錄",
    预警日期: "預警日期",
    警示线: "警示線",
    强预警线: "強預警線",
    "爆仓线 100%": "爆倉線 100%",
    "100%爆仓": "100%爆倉",
    处置建议: "處置建議",
    依据: "依據",
    "确认成功：预警已确认": "確認成功：預警已確認",
    确认失败: "確認失敗",
    "处置成功：状态已更新": "處置成功：狀態已更新",
    "恢复成功：状态已更新": "恢復成功：狀態已更新",
    处置失败: "處置失敗",
    恢复失败: "恢復失敗",
    "1104 G11 资产质量报送": "1104 G11 資產質量報送",
    业务日期: "業務日期",
    报送: "報送",
    已报送: "已報送",
    重新校验: "重新校驗",
    更多: "更多",
    重建快照: "重建快照",
    "仅在快照与风险明细口径漂移时使用：按明细重算并覆盖该业务日快照，覆盖后需重新报送。":
      "僅在快照與風險明細口徑漂移時使用：按明細重算並覆蓋該業務日快照，覆蓋後需重新報送。",
    "客户号已脱敏（c****后4位）· 动作留痕": "客戶號已脫敏（c****後4位）· 動作留痕",
    "报送与导出内容中客户号按分级脱敏（c****+后4位），操作全程留痕可审计":
      "報送與匯出內容中客戶號按分級脫敏（c****+後4位），操作全程留痕可審計",
    "G11 模板（五级 + 合计）": "G11 模板（五級 + 合計）",
    风险类: "風險類",
    笔数: "筆數",
    "余额（元）": "餘額（元）",
    余额占比: "餘額佔比",
    口径一致性校验: "口徑一致性校驗",
    阻断告警记录: "阻斷告警記錄",
    待报送: "待報送",
    校验中: "校驗中",
    口径一致: "口徑一致",
    已阻断: "已阻斷",
    该业务日尚无可报送快照: "該業務日尚無可報送快照",
    "正在实时复算口径一致性（只读，不落库）": "正在實時複算口徑一致性（唯讀，不落庫）",
    "与风险明细聚合口径一致，可提交报送": "與風險明細聚合口徑一致，可提交報送",
    该业务日已报送: "該業務日已報送",
    "口径不一致，报送已阻断": "口徑不一致，報送已阻斷",
    "口径已校验一致，可提交报送": "口徑已校驗一致，可提交報送",
    已于: "已於",
    由: "由",
    "口径不一致，报送已阻断 · 告警已发（": "口徑不一致，報送已阻斷 · 告警已發（",
    曾于: "曾於",
    "报送，快照已变更需重报": "報送，快照已變更需重報",
    "校验中：正在实时复算口径一致性…": "校驗中：正在實時複算口徑一致性…",
    "与风险明细聚合口径一致（笔数/余额均在容差内）": "與風險明細聚合口徑一致（筆數/餘額均在容差內）",
    "以下口径与风险明细聚合不一致，报送已阻断：": "以下口徑與風險明細聚合不一致，報送已阻斷：",
    分类: "分類",
    指标: "指標",
    "1104 快照": "1104 快照",
    明细聚合: "明細聚合",
    "差（快照−明细）": "差（快照−明細）",
    "常见原因：快照生成后风险明细又发生变化（新增贷款或五级分类调整）。":
      "常見原因：快照生成後風險明細又發生變化（新增貸款或五級分類調整）。",
    "以「明细聚合」为准重建快照即可恢复一致。": "以「明細聚合」為準重建快照即可恢復一致。",
    按明细重建快照: "按明細重建快照",
    校验时间: "校驗時間",
    尚无报送数据: "尚無報送數據",
    合计: "合計",
    暂无历史告警: "暫無歷史告警",
    本次校验: "本次校驗",
    "口径不一致，已阻断本次报送": "口徑不一致，已阻斷本次報送",
    请先选择业务日期: "請先選擇業務日期",
    校验完成: "校驗完成",
    重新校验失败: "重新校驗失敗",
    报送成功: "報送成功",
    已标记为已报送: "已標記為已報送",
    报送失败: "報送失敗",
    重建成功: "重建成功",
    重建了: "重建了",
    行: "行",
    重建快照失败: "重建快照失敗",
    "报送快照写入失败：字段内容超出长度限制，请点「重建快照」重试":
      "報送快照寫入失敗：欄位內容超出長度限制，請點「重建快照」重試",
    "报送快照写入失败：数值超出字段范围，请点「重建快照」重试":
      "報送快照寫入失敗：數值超出欄位範圍，請點「重建快照」重試",
    "报送快照写入失败：与已有记录主键冲突，请点「重建快照」重试":
      "報送快照寫入失敗：與既有記錄主鍵衝突，請點「重建快照」重試",
    "报送快照写入异常，请点「重建快照」重试": "報送快照寫入異常，請點「重建快照」重試",
    口径不一致: "口徑不一致",
    "「五级分类余额占比」之和不等于 100%（G11 恒等式校验不通过）":
      "「五級分類餘額佔比」之和不等于 100%（G11 恆等式校驗不通過）",
    余额: "餘額",
    "「": "「",
    "」与「": "」與「",
    "」在「": "」在「",
    "」类的「": "」類的「",
    "」口径不一致": "」口徑不一致",
  /* 数据底座信任卡 */
    房源数据规模: "房源數據規模",
  /* 确认/处置二次确认 */
    将对贷款: "將對貸款",
    "执行「": "執行「",
    "」，状态将变为「": "」，狀態將變為「",
    "」。确认？": "」。確認？",
    "确认该笔预警（贷款": "確認該筆預警（貸款",
    "）？确认后状态将变为「": "）？確認後狀態將變為「",
    "」。": "」。",
    将把: "將把",
    "的 G11 报表标记为「": "的 G11 報表標記為「",
    "」，确认？": "」，確認？",
    "将用风险明细重建该日 G11 快照并覆盖，不可撤销；覆盖后报送态回退，需重新报送。确认？":
      "將用風險明細重建該日 G11 快照並覆蓋，不可撤銷；覆蓋後報送態回退，需重新報送。確認？",
  /* 口径对照业务名 */
    "1104 报送快照": "1104 報送快照",
    风险明细聚合: "風險明細聚合",
    "1104 模板行": "1104 模板行",
    内部累计口径: "內部累計口徑",
  /* 报送时间线 */
    "实时复算结论并入时间线顶部；历史告警由报送流程写入":
      "實時複算結論併入時間線頂部；歷史告警由報送流程寫入",
  /* 公共术语层：策略动作与行业 15 门类（与 en 块对齐；城市 21 城见上）。 */
    行业: "行業",
    网格: "網格",
    拒贷: "拒貸",
    加严审批: "加嚴審批",
    仅提示: "僅提示",
    "交通运输、仓储和邮政业": "交通運輸、倉儲和郵政業",
    "住宿和餐饮业": "住宿和餐飲業",
    "信息传输、软件和信息技术服务业": "資訊傳輸、軟件和資訊技術服務業",
    // 行业门类短名（INDUSTRY_CODES 的 value，香港繁体；与 en 同款回落 global 翻译）
    "制造业": "製造業",
    "建筑业": "建築業",
    "批发和零售业": "批發和零售業",
    "指定范围": "指定範圍",
    金融业: "金融業",
    房地产业: "房地產業",
    "租赁和商务服务业": "租賃和商務服務業",
    "科学研究和技术服务业": "科學研究和技術服務業",
    教育: "教育",
    "卫生和社会工作": "衛生和社會工作",
    "文化、体育和娱乐业": "文化、體育和娛樂業",
    "农、林、牧、渔业": "農、林、牧、漁業",
    "公共管理、社会保障和社会组织": "公共管理、社會保障和社會組織",
  /* 跨页公共术语上提（与 en 块同批；zh-HK 取各页既有繁体译法） */
    五级: "五級",
    "LTV 上限": "LTV 上限",
    "LTV 网格": "LTV 網格",
    "LTV 集中": "LTV 集中",
    价格洼地: "價格窪地",
    价格网格: "價格網格",
    作用域: "作用域",
    "共 ": "共 ",
    判定: "判定",
    动作: "動作",
    状态: "狀態",
    是: "是",
    否: "否",
    数据源: "資料源",
    抵押物: "抵押物",
    抵押物地址: "抵押物地址",
    命中规则: "命中規則",
    命中笔数: "命中筆數",
    命中余额: "命中餘額",
    无数据: "無資料",
    "加载中…": "載入中…",
    达标: "達標",
    未达标: "未達標",
    "规则：": "規則：",
    "：": "：",
});

/* 五级分类展示名：后端返回的中文类名是数据键（CLASS_COLORS 也按中文键取色），
 * 展示层经 t() 译为当前语言，取值/匹配仍用原始中文。 */
function clsName(cls) {
  return t(String(cls == null ? "—" : cls));
}

const CLASS_COLORS = {
    正常: "#2e9e5b",
    关注: "#d9a520",
  // 三档预警色用完全不同的色相 + 明度拉开区分度（不再是暖色系内部渐变）：
  // 次级=金黄(亮)、可疑=红(中)、损失=紫(深)——三级之间人眼可立即分辨。
    次级: "#f59e0b",
    可疑: "#dc2626",
    损失: "#6d28d9",
};

/* LTV 预警带口径：与 tools/risk/config.py 的 LTV_WARN_LINE=0.75 / LTV_RED_LINE=0.85 一致
 * （AC-03 两档，均为「严格大于」触发）。直方图图例、柱体着色、详情仪表刻度三处只认这一份常量，
 * 之前图例写 0.80/0.85 而引擎按 0.75 判警示级，是两套尺子。config 侧改阈值时这里要同步。 */
const LTV_WARN_LINE = 0.75;
const LTV_RED_LINE = 0.85;
const LTV_COLORS = {
  strong: "#b91c1c", // 强预警（LTV > 0.85）
  warn: "#b45309", // 警示（0.75 < LTV ≤ 0.85）
  safe: "#2563eb", // 安全（LTV ≤ 0.75）
  missing: "#9ca3af", // LTV 缺失
};

/* 0.75 → "75%"：仪表刻度线与刻度文字共用，避免两处数字各写一遍。 */
function ltvPctTick(v) {
  return Math.round(v * 100) + "%";
}

/* ---------------- 工具函数 ---------------- */

function fmtMoney(v) {
  if (v == null) return "-";
  const n = Number(v);
  // 英文用 billion/million/thousand 等自然单位，避免把中文「亿/万」逐字译成
  // "hundred million / ten thousand"（如 15.40 亿 → "1.54 billion"）；
  // 中文（zh-CN/zh-HK）沿用「亿/万」口径，符合本地阅读习惯。
  if ((window.__LANG__ || "zh-CN") === "en") {
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(2) + " billion";
    if (a >= 1e6) return (n / 1e6).toFixed(2) + " million";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + " thousand";
    return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + " " + t("亿");
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + " " + t("万");
  return n.toLocaleString("zh-CN");
}

function fmtPct(v) {
  if (v == null) return "-";
  return (Number(v) * 100).toFixed(2) + "%";
}

function fmtLtv(v) {
  if (v == null) return "-";
  return (Number(v) * 100).toFixed(2) + "%";
}

/* 全局请求计数：任意请求进行中时 body 加 .loading（顶部细条指示），全部完成移除。 */
let _pendingRequests = 0;

function setGlobalLoading(delta) {
  _pendingRequests += delta;
  document.body.classList.toggle("loading", _pendingRequests > 0);
}

async function api(path, opts = {}) {
  const init = {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  };
  setGlobalLoading(1);
  try {
    // 仅对 fetch 的网络层失败做重试：3 次、指数退避（~300/600/1200ms），全程静默——
    // 首拉竞态窗口（服务重启/首屏并发高峰）可能超过 1s，重试必须盖过它；
    // 重试期间不留任何 console 输出，只有最终失败才抛给页面自己的错误条降级。
    // 一旦 fetch 成功（无论后续 HTTP 状态）即跳出循环，401/403/4xx/5xx 绝不重试。
    let res;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        res = await fetch(path, init);
        break;
      } catch (netErr) {
        if (attempt === 2) throw netErr; // 已重试 2 次仍网络层失败，原样抛出
        await new Promise((r) => setTimeout(r, 300 * Math.pow(2, attempt)));
      }
    }
    if (res.status === 401) {
      state.user = null;
      showLogin();
      throw new Error("unauthorized");
    }
    if (res.status === 403) {
      const data403 = await res.json().catch(() => ({}));
      alert(data403.error_key ? t(data403.error_key, data403.error_params || {}) : t("当前角色无此操作权限"));
      throw new Error("forbidden");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // 错误文案 i18n 收口：后端带 error_key/error_params 双轨时整句查表；
      // 否则把 zh-CN 原句当 key 试查（已注册的错误句译出、未注册的原样返回，
      // zh-CN 下两者都与原文逐字一致）。
      const rawMsg = data.error != null ? String(data.error) : res.statusText;
      throw new Error(data.error_key ? t(data.error_key, data.error_params || {}) : t(rawMsg));
    }
    return data;
  } finally {
    setGlobalLoading(-1);
  }
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ---------------- toast 反馈（右上角淡入淡出，绿=成功，红=失败） ---------------- */

function showToast(msg, ok = true) {
  let wrap = document.getElementById("toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.id = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const t = document.createElement("div");
  t.className = "toast " + (ok ? "toast-ok" : "toast-err");
  t.textContent = msg;
  wrap.appendChild(t);
  // 2.6s 后淡出并移除，避免堆叠。
  setTimeout(() => {
    t.classList.add("toast-out");
    setTimeout(() => t.remove(), 400);
  }, 2600);
}

/* ---------------- SVG 柱状图 ---------------- */

function barChart(container, data, opts) {
  const { value, label, sub, format, color, labelFn } = opts;
  const _label = labelFn || ((x) => x);
  // 柱体 hover（原生 SVG tooltip）：可选，与 sub 同套路——可以是数据键，也可以是回调
  // （P6 敏感性阶梯用它挂「跌 10% · 命中 5 笔 · 余额 … · 跌后 LTV 中位数 …」）。
  // 不传时柱体仍是原来的自闭合 <rect>，形状一个字符都不变——这是全站共用组件，
  // P2 的「五级分类分布 / LTV 分布直方图」与 P5 的「区域贷款分布」都走同一条路径。
  const _title = typeof opts.title === "function" ? opts.title : opts.title != null ? ((d) => d[opts.title]) : null;
  const height = opts.height || 230;
  const width = container.clientWidth || 640;
  const padT = 26;
  const padB = 52;
  // 有数值轴标题时在左侧留出一条竖排文字的宽度，否则沿用原来的 4px 内缩。
  const padL = opts.yAxisTitle ? 22 : 4;
  const padR = 4;
  const plotH = height - padT - padB;
  const plotW = width - padL - padR;
  const max = Math.max(1, ...data.map((d) => Number(d[value]) || 0));
  const slot = plotW / data.length;
  const barW = Math.max(8, Math.min(46, slot * 0.56));
  const fmt = format || ((x) => x);

  // 无障碍：svg 加 role="img" + aria-label；柱体可聚焦以便键盘钻取（tabindex）。
  let svg = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="${esc(opts.ariaLabel || t("柱状图"))}">`;
  for (let i = 0; i <= 4; i++) {
    const y = padT + plotH - (plotH * i) / 4;
    svg += `<line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="#eef2f7"/>`;
  }
  // 数值轴标题：绕自身中心旋转 -90°，说明纵轴量纲（笔数 / 余额…）。
  if (opts.yAxisTitle) {
    const cy = padT + plotH / 2;
    svg +=
      `<text x="11" y="${cy.toFixed(1)}" transform="rotate(-90 11 ${cy.toFixed(1)})" ` +
      `text-anchor="middle" font-size="11" fill="#6b7280">${esc(opts.yAxisTitle)}</text>`;
  }
  data.forEach((d, i) => {
    const v = Number(d[value]) || 0;
    const h = (v / max) * plotH;
    const x = padL + slot * i + (slot - barW) / 2;
    const y = padT + plotH - h;
    const fill = color ? color(d) : "#2563eb";
    // 柱体属性（class / data-i / tabindex）一个都不能动：全站点击钻取
    // 依赖 .bar-rect[data-i]（见下方 bar-rect 的委托 handler）。
    const rectAttrs = `class="bar-rect" data-i="${i}" tabindex="0" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="3" fill="${fill}"`;
    const tip = _title ? esc(_title(d)) : "";
    svg += tip
      ? `<rect ${rectAttrs}><title>${tip}</title></rect>`
      : `<rect ${rectAttrs}/>`;
    svg += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle" font-size="11" fill="#374151">${fmt(v)}</text>`;
    const lx = padL + slot * i + slot / 2;
    svg += `<text x="${lx.toFixed(1)}" y="${height - 28}" text-anchor="middle" font-size="11" fill="#6b7280">${esc(_label(d[label]))}</text>`;
    if (sub != null) {
      // sub 可以是数据键，也可以是回调（如把余额格式化后当副标签）。
      const subTxt = typeof sub === "function" ? sub(d) : d[sub];
      svg += `<text x="${lx.toFixed(1)}" y="${height - 10}" text-anchor="middle" font-size="10" fill="#9ca3af">${esc(subTxt)}</text>`;
    }
  });
  svg += "</svg>";
  container.innerHTML = svg;
}

function hbarList(container, items, opts) {
  const { value, label, format, color } = opts;
  const max = Math.max(1, ...items.map((d) => Number(d[value]) || 0));
  const fmt = format || ((d) => d[value]);
  // 无障碍：容器标注 role="img" + aria-label；行可聚焦以便键盘钻取。
  container.setAttribute("role", "img");
  container.setAttribute("aria-label", opts.ariaLabel || t("横向条形图"));
  let html = "";
  items.forEach((d, i) => {
    const w = ((Number(d[value]) || 0) / max) * 100;
    // 颜色回调返回 #hex / rgb() 时作为内联背景色（支持连续色阶），否则作为 CSS class 名。
    const c = color ? color(d) : "";
    const isInline = typeof c === "string" && (c.startsWith("#") || c.startsWith("rgb"));
    // 可选 title 回调：为每行生成悬浮提示（如 hover 显示占比）。
    const tip = opts.title ? opts.title(d) : "";
    html +=
      `<div class="hbar-row" data-i="${i}" tabindex="0"${tip ? ` title="${esc(tip)}"` : ""}>` +
      `<div class="hbar-label">${esc(t(d[label]))}</div>` +
      `<div class="hbar-track"><div class="hbar-fill ${isInline ? "" : c}" style="width:${w.toFixed(1)}%${isInline ? `;background:${c}` : ""}"></div></div>` +
      `<div class="hbar-num">${esc(fmt(d))}</div>` +
      `</div>`;
  });
  container.innerHTML = html;
}

/* ---------------- 登录 / 会话 ---------------- */

/* 多语言：向 .login-lang / .side-lang 注入切换器，并注册语言切换后的全局重渲染钩子。
 * 静态文本（data-i18n）由 applyI18nText 处理；动态页（驾驶舱/预警/报送/插件页）按
 * 当前页重跑 render，保证 KPI、表格、图表、按钮等全部随语言即时刷新。 */
function initLangSwitchers() {
  [document.querySelector(".login-lang"), document.querySelector(".side-lang")].forEach((m) => {
    if (m && !m.querySelector(".lang-switcher")) m.appendChild(buildLangSwitcher());
  });
  window.__onLangChange = async (_code) => {
    applyI18nText(document);
    renderNav();
    // 侧栏角色标签随语言刷新（去掉 dev 后缀后翻译主体再拼回）。
    const userEl = document.getElementById("user-role");
    if (userEl && state.user) {
      const base = state.user.role_label || "";
      const devSuffix = base.includes("[DEV-RBAC-OFF]") ? " [DEV-RBAC-OFF]" : "";
      userEl.textContent = t(base.replace("[DEV-RBAC-OFF]", "").trim()) + devSuffix;
    }
    const id = state.currentPage;
    if (id === "dashboard") renderDashboard();
    else if (id === "alerts") renderAlerts();
    else if (id === "report") renderReport();
    else if (PLUGIN_PAGES[id]) {
      // 插件页骨架（html 函数生成的 data-i18n 静态文案）也要随语言重生成。
      // 必须清空整个 _mounted 而不是只删当前页：本会话已打开过的其它插件页，
      // 其 DOM 是用切换前的语言生成的，若保留挂载标记，下次切过去会跳过骨架
      // 重建，整页停留在旧语言（跨页语言混杂）。清空后各页在下次进入时重建。
      _mounted.clear();
      mountPlugin(id);
    }
  };
}

function showLogin() {
  document.getElementById("login-view").classList.remove("hidden");
  document.getElementById("app-view").classList.add("hidden");
}

async function showApp() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("app-view").classList.remove("hidden");
  document.getElementById("user-label").textContent = state.user.user;
  // 角色标签按 role 翻译（dev 模式 label 带 "[DEV-RBAC-OFF]" 后缀，去掉后翻译主体再拼回）。
  const base = state.user.role_label || "";
  const devSuffix = base.includes("[DEV-RBAC-OFF]") ? " [DEV-RBAC-OFF]" : "";
  const roleLabel = t(base.replace("[DEV-RBAC-OFF]", "").trim()) + devSuffix;
  document.getElementById("user-role").textContent = roleLabel;
  // 插件页必须先加载并建好 section，renderNav/showPage 才能正确切换。
  await loadPlugins(state.user.pages || []);
  renderNav();
  const visible = (state.user.pages || []).map((p) => p.id);
  if (!visible.includes(state.currentPage)) state.currentPage = visible[0] || "dashboard";
  showPage(state.currentPage);
}

/* ---------------- 侧栏窄屏抽屉 ---------------- */

/* 窄屏（CSS @media max-width:960px）下侧栏默认移出视口，靠 .open 滑入。
   宽屏时按钮/遮罩被 CSS 隐藏，切类不产生任何视觉影响。 */
function setSidebarOpen(open) {
  const sidebar = document.getElementById("sidebar");
  const mask = document.getElementById("sidebar-mask");
  const btn = document.getElementById("sidebar-toggle");
  if (!sidebar) return;
  sidebar.classList.toggle("open", open);
  if (mask) mask.classList.toggle("open", open);
  if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function bindSidebarToggle() {
  const btn = document.getElementById("sidebar-toggle");
  const mask = document.getElementById("sidebar-mask");
  const sidebar = document.getElementById("sidebar");
  if (btn)
    btn.addEventListener("click", () =>
      setSidebarOpen(!(sidebar && sidebar.classList.contains("open")))
    );
  if (mask) mask.addEventListener("click", () => setSidebarOpen(false));
}

function renderNav() {
  const nav = document.getElementById("nav");
  // 功能导向分组：与 pages/__init__.py 的 _PAGE_GROUP 对应（键名两处必须一致）。
  const groupTitle = {
    overview: t("资产总览"),
    monitoring: t("风险监控与预警"),
    strategy: t("策略推演"),
    governance: t("报送与合规"),
    data: t("数据底座"),
  };
  // 分组之间的先后（组内条目序由各页 PAGE.order 决定，两边互不影响）。
  // 按数据链路主线排：总览 → 数据进来 → 识别与预警 → 推演处置 → 报送留痕。
  const order = ["overview", "data", "monitoring", "strategy", "governance"];
  let html = "";
  for (const g of order) {
    const items = (state.user.pages || []).filter((p) => (p.group || "overview") === g);
    if (!items.length) continue;
    // 分组标题一律常显、不折叠：形状不能随组内页面数变化，否则加一个页面就会让该组
    // 从「独立链接」变成「文件夹」，单项组的组名还会整个消失。
    html +=
      `<div class="nav-group">` +
      `<div class="nav-group-title">${esc(groupTitle[g] || g)}</div>` +
      `<div class="nav-group-items">` +
      items
        .map(
          (p) =>
            `<a data-page="${p.id}" class="${p.id === state.currentPage ? "active" : ""}">${esc(t(p.label))}</a>`
        )
        .join("") +
      `</div></div>`;
  }
  nav.innerHTML = html;
  nav.querySelectorAll("a").forEach((a) =>
    a.addEventListener("click", () => {
      showPage(a.dataset.page);
      setSidebarOpen(false); // 窄屏抽屉：选完页面即收起（宽屏无副作用）
    })
  );
}

/* ---------------- 页面路由 ---------------- */

function showPage(id) {
  state.currentPage = id;
  // 命名空间随页切换：t() 先查当前页的 ns 字典再回退 global——
  // 各页 registerI18n(..., ns) 已按页隔离，这里不切 ns 的话页面译文永远查不到。
  setNamespace(id);
  document.querySelectorAll(".page").forEach((el) => {
    el.classList.toggle("hidden", el.id !== "page-" + id);
  });
  document.querySelectorAll("#nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === id);
  });
  // 内置三页切换瞬间加顶部细条 loading，渲染函数 finally 里移除，避免白屏。
  // 插件页同样加加载态，由 mountPlugin 渲染结束后清除（避免先白一下再出内容）。
  if (id === "dashboard" || id === "alerts" || id === "report" || PLUGIN_PAGES[id])
    setPageLoading(id, true);
  if (id === "dashboard") renderDashboard();
  if (id === "alerts") {
    applyAlertUrl(); // 先应用 URL 深链参数；renderAlerts 里 preset 后应用（preset 优先）。
    renderAlerts();
  }
  if (id === "report") renderReport();
  if (PLUGIN_PAGES[id]) mountPlugin(id);
}

/* ---------------- 页面级 loading 与错误兜底 ---------------- */

function setPageLoading(id, on) {
  const el = document.getElementById("page-" + id);
  if (el) el.classList.toggle("page-loading", on);
}

function pageErrBox(pageId) {
  let box = document.getElementById(pageId + "-err");
  if (!box) {
    box = document.createElement("div");
    box.id = pageId + "-err";
    box.className = "page-err";
    const page = document.getElementById(pageId);
    if (page) page.insertBefore(box, page.firstChild);
  }
  return box;
}

function clearPageErr(pageId) {
  const box = document.getElementById(pageId + "-err");
  if (box) box.remove();
}

/* 主页面 API 失败时渲染错误卡（错误信息 + 重试按钮），重试重新调对应 render。 */
function showPageErr(pageId, err, retry) {
  const box = pageErrBox(pageId);
  box.innerHTML =
    `<div class="err-card"><span class="err-msg">${t("加载失败")}：${esc(String((err && err.message) || err))}</span>` +
    `<button class="btn btn-ghost btn-sm">${t("重试")}</button></div>`;
  box.querySelector("button").addEventListener("click", () => {
    clearPageErr(pageId);
    retry();
  });
}

/* ---------------- 插件页面（pages/ 下的 P1~P10） ----------------
 * 每个插件页只需在自己的 js 里调用 registerPage(id, {html, render})：
 *   html   —— 页面骨架字符串，首次挂载时注入 <section id="page-{id}">
 *   render —— 每次切到该页时调用，负责拉数据并填充 DOM
 * 由后端 /api/me 下发的 pages[].js 决定加载哪些模块，未授权角色根本拿不到文件名。
 */

const PLUGIN_PAGES = {};
const _mounted = new Set();

function registerPage(id, def) {
  PLUGIN_PAGES[id] = def;
}
window.registerPage = registerPage;
/* 跨页复用的枚举 → i18n key 映射（与 tools/risk/config.py 的 CITY_MAP /
 * INDUSTRY_CATEGORIES / p6_policy.ACTIONS 同源）。后端下发 code、前端查这里的
 * 中文 key 再 t() 译出——显示名单点在 global 层，p6/p9 共用，页面内不再各自复制。
 * 注意 key 是 zh-CN 原文：zh-CN 下 t() 原样返回，en/zh-HK 译文在上面字典里。 */
const CITY_CODES = {
  gz: "广州",
  sz: "深圳",
  fs: "佛山",
  dg: "东莞",
  zh: "珠海",
  zs: "中山",
  hui: "惠州",
  jm: "江门",
  zq: "肇庆",
  qy: "清远",
  sg: "韶关",
  st: "汕头",
  sw: "汕尾",
  jy: "揭阳",
  cz: "潮州",
  mz: "梅州",
  hy: "河源",
  yj: "阳江",
  mm: "茂名",
  zj: "湛江",
  yf: "云浮",
};
const INDUSTRY_CODES = {
  manufacturing: "制造业",
  construction: "建筑业",
  wholesale_retail: "批发和零售业",
  transport: "交通运输、仓储和邮政业",
  accommodation_food: "住宿和餐饮业",
  it_software: "信息传输、软件和信息技术服务业",
  finance: "金融业",
  real_estate: "房地产业",
  business_services: "租赁和商务服务业",
  sci_tech: "科学研究和技术服务业",
  education: "教育",
  health: "卫生和社会工作",
  culture: "文化、体育和娱乐业",
  agriculture: "农、林、牧、渔业",
  public_admin: "公共管理、社会保障和社会组织",
};
// 策略惩罚动作三档（p6 规则表/推演结果与 p9 审计留痕共用一套措辞）。
const POLICY_ACTIONS = { reject: "拒贷", strict: "加严审批", notice: "仅提示" };
// 供插件页复用主框架的工具函数，避免每个页面重复实现。
window.spf = {
  api,
  esc,
  fmtMoney,
  fmtPct,
  fmtLtv,
  barChart,
  hbarList,
  CLASS_COLORS,
  CITY_CODES,
  INDUSTRY_CODES,
  POLICY_ACTIONS,
};

function loadPluginScript(src) {
  return new Promise((resolve) => {
    let attempt = 0;
    const inject = () => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      // 首屏偶发网络竞态（服务重启/首屏并发高峰）可能让脚本加载失败；
      // 指数退避静默重试 5 次（总窗口 ~9s），重试期间不打任何 console——
      // preserve-log 会把中途失败常驻控制台，被当成代码缺陷的假报警。
      // 全部耗尽才 error 一次；HTTP 200 但脚本逻辑错误不靠重试解决。
      s.onerror = () => {
        if (attempt < 4) {
          attempt++;
          setTimeout(inject, 300 * Math.pow(2, attempt - 1));
          return;
        }
        console.error("[spf] 页面模块加载失败:", src);
        resolve();
      };
      document.body.appendChild(s);
    };
    inject();
  });
}

async function loadPlugins(pages) {
  await Promise.all(pages.filter((p) => p.js).map((p) => loadPluginScript("/pages/" + p.js)));
  // 为每个已注册插件页建好空 section，showPage 的 hidden 切换才能命中。
  const main = document.querySelector(".main") || document.getElementById("app-view");
  pages
    .filter((p) => p.js && PLUGIN_PAGES[p.id])
    .forEach((p) => {
      if (document.getElementById("page-" + p.id)) return;
      const sec = document.createElement("section");
      sec.id = "page-" + p.id;
      sec.className = "page hidden";
      main.appendChild(sec);
    });
}

async function mountPlugin(id) {
  const def = PLUGIN_PAGES[id];
  const el = document.getElementById("page-" + id);
  if (!def || !el) return;
  if (!_mounted.has(id)) {
    el.innerHTML = typeof def.html === "function" ? def.html() : def.html || "";
    applyI18nText(el); // 插件页模板里的 data-i18n 静态文案随语言刷新
    _mounted.add(id);
  }
  if (def.render) {
    try {
      await def.render(el);
    } catch (e) {
      console.error("[spf] 页面渲染失败:", id, e);
      el.innerHTML = `<div class="empty">${t("加载失败")}：${esc(String(e.message || e))}</div>`;
    } finally {
      // 无论渲染成功/失败都清除加载态，保证细条不残留。
      setPageLoading(id, false);
    }
  } else {
    setPageLoading(id, false);
  }
}

/* ---------------- 驾驶舱 ---------------- */

async function renderDashboard() {
  const pageId = "page-dashboard";
  clearPageErr(pageId);
  try {
    const data = await api("/api/dashboard");
    state.dashboardData = data;
    const kpi = data.kpi;
    const kpis = [
      { label: t("贷款笔数"), value: kpi.loan_count },
      { label: t("总敞口（余额）"), value: fmtMoney(kpi.total_balance) },
      { label: t("预警贷款"), value: kpi.alert_loans },
      { label: t("低置信笔数"), value: kpi.low_confidence },
      { label: t("位于高危网格贷款"), value: kpi.high_risk_zone_loans },
    ];
    document.getElementById("kpi-cards").innerHTML = kpis
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${k.label}</div>` +
          `<div class="kpi-value">${k.value}</div>` +
          (k.sub ? `<div class="kpi-sub">${k.sub}</div>` : "") +
          `</div>`
      )
      .join("");

    renderTrustCards(data.trust_cards);

    barChart(document.getElementById("five-class-chart"), data.five_class, {
      value: "balance_total",
      label: "risk_class",
      labelFn: clsName,
      sub: (d) => `${d.loan_count}${t("笔", { n: d.loan_count })} · ${fmtPct(d.balance_pct)}`,
      format: fmtMoney,
      color: (d) => CLASS_COLORS[d.risk_class] || "#2563eb",
      ariaLabel: t("五级分类分布柱状图"),
      yAxisTitle: t("贷款余额（元）"),
      height: 250,
    });

    barChart(document.getElementById("ltv-hist-chart"), data.ltv_hist, {
      value: "count",
      label: "bucket",
      // 桶标签走 t() 翻译（如「缺失」→ Missing），保证图例与坐标轴随语言切换。
      labelFn: (b) => t(b),
      format: (x) => x,
      color: (d) => ltvColor(d.bucket),
      ariaLabel: t("LTV 分布直方图"),
      yAxisTitle: t("贷款笔数"),
      height: 250,
    });
    document
      .getElementById("ltv-hist-chart")
      .insertAdjacentHTML(
        "beforeend",
        `<div class="legend"><span><i style="background:${LTV_COLORS.strong}"></i>LTV&gt;${LTV_RED_LINE} ${t("强预警")}</span>` +
          `<span><i style="background:${LTV_COLORS.warn}"></i>${LTV_WARN_LINE}-${LTV_RED_LINE} ${t("警示")}</span>` +
          `<span><i style="background:${LTV_COLORS.safe}"></i>&lt;${LTV_WARN_LINE} ${t("安全")}</span></div>`
      );

    hbarList(document.getElementById("city-dist"), data.city_dist, {
      value: "loan_count",
      label: "city",
      format: (d) =>
        `${d.loan_count} ${t("笔", { n: d.loan_count })}${
          d.high_risk_loans ? " · " + t("高危") + " " + d.high_risk_loans : ""
        }`,
      // 离散风险档：按「高危贷款占比」分 6 档、跨色相着色（蓝/青/绿/黄/橙/红）。
      // 为什么不继续用连续渐变：本项目占比高度集中在 11%~17%，连续色阶会让
      // 绝大多数城市挤在红紫段、肉眼难分（实测最小色距≈1）；分档后档与档色相
      // 跨度大（青 190°→绿 142°→黄 55°→橙 25°→红 0°），一眼可辨风险档位。
      color: (d) => {
        const total = Number(d.loan_count) || 0;
        if (!total) return "#2563eb";
        const ratio = (Number(d.high_risk_loans) || 0) / total;
        if (ratio <= 0.05) return "#0891b2"; // 青：低危
        if (ratio <= 0.1) return "#16a34a"; // 绿
        if (ratio <= 0.13) return "#eab308"; // 黄
        if (ratio <= 0.16) return "#f97316"; // 橙
        return "#dc2626"; // 红：高危
      },
      // hover 悬浮显示高危占比比例（替代常驻标题说明，不占空间）。
      title: (d) => {
        const total = Number(d.loan_count) || 0;
        const hr = Number(d.high_risk_loans) || 0;
        const pct = total ? ((hr / total) * 100).toFixed(1) : "0.0";
        return t("{city}：{count} 笔 · 高危 {hr}（{pct}%）", { city: t(d.city), count: d.loan_count, hr, pct });
      },
      ariaLabel: t("区域贷款分布"),
    });

    const alertTotal = renderAlertOverview(data.alert_breakdown);
    const alertTitleEl = document.getElementById("alert-overview-title");
    if (alertTitleEl && alertTotal != null)
      alertTitleEl.textContent = t("LTV 预警概览（{n} 条）", {
        n: alertTotal.toLocaleString("zh-CN"),
      });
    renderFunnel(data.alert_funnel);
    fillCityOptions(data.city_dist);
  } catch (e) {
    console.error("[spf] 驾驶舱渲染失败:", e);
    showPageErr(pageId, e, renderDashboard);
  } finally {
    setPageLoading("dashboard", false);
  }
}

/* ---------------- 数据底座信任卡（规模 / 时效 / 解析质量） ---------------- */

const TRUST_STATUS = {
  ok: { cls: "ok", text: "新鲜" },
  warn: { cls: "warn", text: "略有延迟" },
  bad: { cls: "bad", text: "已滞后" },
  empty: { cls: "off", text: "暂无数据" },
};

function fmtAge(sec) {
  if (sec == null) return "-";
  const h = Math.floor(sec / 3600);
  if (h < 1) return Math.max(1, Math.floor(sec / 60)) + " " + t("分钟前");
  if (h < 48) return h + " " + t("小时前");
  return (h / 24).toFixed(1) + " " + t("天前");
}

function renderTrustCards(tc) {
  const wrap = document.getElementById("trust-card-wrap");
  const el = document.getElementById("trust-cards");
  if (!wrap || !el) return;
  if (!tc) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "";
  const cs = tc.crawl_scale || {};
  const fr = tc.freshness || { layers: [], overall: "empty" };
  const ps = tc.parse_success || {};

  const listings = (Number(cs.sale) || 0) + (Number(cs.rent) || 0);
  const scaleHtml =
    `<div class="trust-card">` +
    `<div class="trust-label">${t("房源数据规模")}</div>` +
    `<div class="trust-value">${listings.toLocaleString("zh-CN")} ${t("条挂牌", { n: listings })}</div>` +
    `<div class="trust-sub">${t("在售")} ${Number(cs.sale || 0).toLocaleString("zh-CN")} · ${t("覆盖")} ${cs.cities ?? "-"} ${t("城", { n: cs.cities })} · ${t("坐标点")} ${Number(cs.coords || 0).toLocaleString("zh-CN")}</div>` +
    `</div>`;

  const st = TRUST_STATUS[fr.overall] || TRUST_STATUS.empty;
  const rank = { bad: 0, warn: 1, empty: 2, ok: 3 };
  const layers = (fr.layers || [])
    .slice()
    .sort((a, b) => (rank[a.status] ?? 4) - (rank[b.status] ?? 4));
  const layerLines = layers.length
    ? layers
        .map((l) => {
          const s = TRUST_STATUS[l.status] || TRUST_STATUS.empty;
          return (
            `<div class="tf-fresh-line"><span class="badge ${s.cls}">${t(s.text)}</span>` +
            `<span class="tf-layer">${esc(l.layer)}</span>` +
            `<span class="tf-note" title="${esc(t(l.note) || "")}">${esc(t(l.note) || l.layer)}</span>` +
            `<span class="trust-dim">${t("更新于")} ${fmtAge(l.age_seconds)}</span></div>`
          );
        })
        .join("")
    : `<div class="tf-fresh-line"><span class="trust-dim">${t("暂无分层数据")}</span></div>`;
  const freshHtml =
    `<div class="trust-card">` +
    `<div class="trust-label">${t("数据新鲜度")} <span class="badge ${st.cls}">${t(st.text)}</span></div>` +
    `<div class="tf-fresh">${layerLines}</div>` +
    `</div>`;

  const rate = ps.rate == null ? null : Number(ps.rate);
  const pendingPct = ps.pending_pct == null ? null : Number(ps.pending_pct);
  const parseHtml =
    `<div class="trust-card">` +
    `<div class="trust-label">${t("地址解析质量")}</div>` +
    `<div class="trust-value">${rate == null ? "-" : rate.toFixed(2) + "%"}</div>` +
    `<div class="trust-sub">` +
    t(
      "已解析中成功率（成功 {succ} · 失败 {fail}）",
      {
        succ: Number(ps.success || 0).toLocaleString("zh-CN"),
        fail: Number(ps.failed || 0).toLocaleString("zh-CN"),
      }
    ) +
    (pendingPct == null
      ? ""
      : t("；另有 {pending}% 待解析（排队中，非解析失败）", {
          pending: pendingPct.toFixed(1),
        })) +
    `</div>` +
    `</div>`;

  el.innerHTML = scaleHtml + freshHtml + parseHtml;
}

/* ---------------- 预警处置闭环漏斗（与 7 天剧本呼应） ---------------- */

function renderFunnel(f) {
  const stages = [
    { key: "alert", label: t("预警"), color: "#d14a3a" },
    { key: "confirmed", label: t("确认"), color: "#e07b1f" },
    { key: "disposed", label: t("处置"), color: "#2563eb" },
    { key: "recovered", label: t("解除"), color: "#2e9e5b" },
  ];
  const max = Math.max(1, f.alert, f.confirmed, f.disposed, f.recovered);
  document.getElementById("alert-funnel").innerHTML =
    `<div class="funnel-row">` +
    stages
      .map(
        (s) =>
          `<div class="funnel-stage">` +
          `<div class="funnel-label">${s.label}</div>` +
          `<div class="funnel-track"><div class="funnel-fill" style="width:${((f[s.key] / max) * 100).toFixed(1)}%;background:${s.color}"></div></div>` +
          `<div class="funnel-num"><b>${f[s.key]}</b> ${t("条", { n: f[s.key] })}</div>` +
          `</div>`
      )
      .join("") +
    `</div>`;
}

/* ---------------- 预警列表城市下拉 ---------------- */

function fillCityOptions(cityDist) {
  state.cityOptions = cityDist || [];
  const sel = document.getElementById("f-city");
  if (!sel) return;
  const cur = sel.value;
  const cities = (cityDist || []).filter((c) => c.city && c.city !== "未标注").map((c) => c.city);
  sel.innerHTML =
    `<option value="">${t("全部")}</option>` +
    cities.map((c) => `<option>${esc(t(c))}</option>`).join("");
  if (cities.includes(cur)) sel.value = cur;
}

/* ---------------- 二级钻取：图表点击 → 预警列表预置筛选 ---------------- */

function ltvBucketRange(bucket) {
  const map = {
    "<0.50": [null, 0.5],
    "0.50-0.60": [0.5, 0.6],
    "0.60-0.70": [0.6, 0.7],
    "0.70-0.80": [0.7, 0.8],
    "0.80-0.85": [0.8, 0.85],
    "0.85-0.90": [0.85, 0.9],
    "0.90-1.00": [0.9, 1.0],
    ">1.00": [1.0, null],
  };
  return map[bucket] || null;
}

function drillToAlerts(filters) {
  state.alertPreset = filters || {};
  state.alertsPage = 1;
  showPage("alerts");
}

function bindChartDrill() {
  // 通用钻取触发器：click 与键盘 Enter 共用同一份解析逻辑（无障碍）。
  const bind = (el, listKey, resolve) => {
    const fire = (e) => {
      const bar = e.target.closest(".bar-rect, .hbar-row");
      if (!bar) return;
      const list = state.dashboardData && state.dashboardData[listKey];
      const item = list && list[Number(bar.dataset.i)];
      if (!item) return;
      const filters = resolve(item);
      if (filters) drillToAlerts(filters);
    };
    el.addEventListener("click", fire);
    // 柱体/行已带 tabindex，Enter 键也能触发钻取。
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") fire(e);
    });
  };

  const ltvEl = document.getElementById("ltv-hist-chart");
  ltvEl.classList.add("clickable");
  bind(ltvEl, "ltv_hist", (item) => {
    const range = ltvBucketRange(item.bucket);
    return range ? { ltv_min: range[0], ltv_max: range[1] } : null; // 「缺失」桶没有 LTV 区间，不钻取
  });

  const fiveEl = document.getElementById("five-class-chart");
  fiveEl.classList.add("clickable");
  bind(fiveEl, "five_class", (item) =>
    item && item.risk_class ? { risk_class: item.risk_class } : null
  );

  const cityEl = document.getElementById("city-dist");
  cityEl.classList.add("clickable");
  bind(
    cityEl,
    "city_dist",
    (item) => (item && item.city && item.city !== "未标注" ? { city: item.city } : null) // 未标注无城市维度，不钻取
  );
}

function applyAlertPreset() {
  const p = state.alertPreset || {};
  const set = (id, v) => {
    if (v !== undefined && v !== null && v !== "") document.getElementById(id).value = v;
  };
  set("f-risk-class", p.risk_class);
  set("f-city", p.city);
  set("f-ltv-min", p.ltv_min);
  set("f-ltv-max", p.ltv_max);
  state.alertPreset = null;
}

function ltvColor(bucket) {
  return LTV_COLORS[ltvBand(bucket)];
}

/* 桶 → 预警带。分桶边界（0.70/0.80/0.85）与阈值线不重合：一个桶只要区间够到警示带
 * 就按警示色渲染（如 0.70-0.80 含 0.75 以上那段），否则图例会把真实警示染成安全色。 */
function ltvBand(bucket) {
  if (bucket === "缺失") return "missing";
  const range = ltvBucketRange(bucket);
  if (!range) return "safe";
  const [lo, hi] = range;
  if (lo != null && lo >= LTV_RED_LINE) return "strong";
  if (hi != null && hi > LTV_WARN_LINE) return "warn";
  return "safe";
}

function renderAlertOverview(ab) {
  const srcs = [{ key: "offline", total: ab.offline_total }];
  let html = "";
  for (const src of srcs) {
    const rows = Object.entries(ab[src.key])
      .filter(([k]) => k !== "total")
      .sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...rows.map(([, c]) => c));
    html += `<div class="alert-src"><div class="alert-bars">`;
    for (const [cls, cnt] of rows) {
      html +=
        `<div class="alert-bar-row"><span>${esc(t(cls))}</span>` +
        `<div class="alert-bar-track"><div class="alert-bar-fill" style="width:${((cnt / max) * 100).toFixed(1)}%;background:${CLASS_COLORS[cls] || "#d14a3a"}"></div></div>` +
        `<span class="alert-bar-num">${cnt} ${t("条", { n: cnt })}</span></div>`;
    }
    html += `</div></div>`;
  }
  document.getElementById("alert-overview").innerHTML = html;
  return srcs[0].total;
}

/* ---------------- LTV 预警列表 ---------------- */

/* 两档预警等级标签（与 tools/alerting/main.py 的 alert_level_label 口径一致）：
 * strong→强预警级、warn→警示级、缺失/NULL→「—」。 */
function alertLevelLabel(lv) {
  const level = (lv == null ? "" : String(lv)).toLowerCase();
  if (level === "strong") return `<span class="badge bad">${t("强预警级")}</span>`;
  if (level === "warn") return `<span class="badge warn">${t("警示级")}</span>`;
  return "—";
}

function buildAlertQuery(page) {
  const q = new URLSearchParams();
  const v = (id) => document.getElementById(id).value;
  if (v("f-risk-class")) q.set("risk_class", v("f-risk-class"));
  if (v("f-source")) q.set("source", v("f-source"));
  if (v("f-city")) q.set("city", v("f-city"));
  if (v("f-ltv-min")) q.set("ltv_min", v("f-ltv-min"));
  if (v("f-ltv-max")) q.set("ltv_max", v("f-ltv-max"));
  if (v("f-date-from")) q.set("date_from", v("f-date-from"));
  if (v("f-date-to")) q.set("date_to", v("f-date-to"));
  if (v("f-q")) q.set("q", v("f-q"));
  q.set("page", String(page || state.alertsPage));
  return q.toString();
}

/* ---------------- 预警筛选 URL 深链 ---------------- */

const ALERT_URL_FIELDS = [
  ["risk_class", "f-risk-class"],
  ["source", "f-source"],
  ["city", "f-city"],
  ["ltv_min", "f-ltv-min"],
  ["ltv_max", "f-ltv-max"],
  ["date_from", "f-date-from"],
  ["date_to", "f-date-to"],
  ["q", "f-q"],
];

function applyAlertUrl() {
  // 进入 alerts 页时从 location.search 回填筛选表单；preset 会在 renderAlerts 里后应用（preset 优先）。
  const sp = new URLSearchParams(location.search);
  for (const [key, id] of ALERT_URL_FIELDS) {
    const val = sp.get(key);
    if (val !== null && val !== "") document.getElementById(id).value = val;
  }
  // 页码仅在无钻取 preset 时从 URL 恢复（preset 已把 page 重置为 1，避免覆盖钻取行为）。
  if (!state.alertPreset) {
    const page = parseInt(sp.get("page") || "", 10);
    if (page > 0) state.alertsPage = page;
  }
}

function syncAlertUrl() {
  // 筛选提交 / 分页后把当前筛选写回 URL（history.replaceState，不整页刷新）。
  const sp = new URLSearchParams();
  const v = (id) => document.getElementById(id).value;
  for (const [key, id] of ALERT_URL_FIELDS) {
    if (v(id)) sp.set(key, v(id));
  }
  if (state.alertsPage > 1) sp.set("page", String(state.alertsPage));
  const qs = sp.toString();
  history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
}

async function renderAlerts() {
  const pageId = "page-alerts";
  clearPageErr(pageId);
  try {
    applyAlertPreset();
    // 用缓存的 city_dist 重填城市下拉。这里不能只在「下拉为空」时补填：
    // 切语言走 __onLangChange → renderAlerts，下拉里已有 option，若跳过重填，
    // 城市名就会停在切换前的语言上（下拉文本在填充时就写死了 t(c) 的结果）。
    // 重填会保留当前选中项（fillCityOptions 里按 cities.includes(cur) 回填）。
    const citySel = document.getElementById("f-city");
    if (citySel && state.cityOptions && state.cityOptions.length) {
      fillCityOptions(state.cityOptions);
    }
    const qs = buildAlertQuery();
    const data = await api("/api/alerts?" + qs);
    document.getElementById("export-btn").href = "/api/alerts/export?" + qs;
    document.getElementById("alert-total").textContent = `${t("共")} ${data.total} ${t("条预警", { n: data.total })}`;

    const tbody = document.querySelector("#alert-table tbody");
    if (!data.rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">${t("无符合条件的数据")}</td></tr>`;
    } else {
      tbody.innerHTML = data.rows
        .map((r) => {
          return (
            `<tr>` +
            `<td>${alertLevelLabel(r.alert_level)}</td>` +
            `<td>${r.loan_id}</td>` +
            // 低置信徽标就地挂在 LTV 单元格内：空间特征严重缺失时该 LTV 仅供参考，
            // 不另开一列（列数不变，避免详情行 colspan 联动）。
            `<td class="num"><b>${fmtLtv(r.ltv)}</b>${r.low_confidence ? ` <span class="badge warn" title="${t("抵押物空间特征严重缺失，估值置信度不足，该 LTV 仅供参考，需人工复核")}">${t("低置信")}</span>` : ""}</td>` +
            `<td class="num">${fmtMoney(r.loan_balance)}</td>` +
            `<td><span class="badge" style="background:${CLASS_COLORS[r.risk_class]}22;color:${CLASS_COLORS[r.risk_class]}">${esc(clsName(r.risk_class))}</span></td>` +
            `<td>${r.is_high_risk_zone ? `<span class="badge bad">${t("高危网格")}</span>` : "-"}</td>` +
            `<td>${dispositionCell(r)}</td>` +
            `<td>${alertActionCell(r)}</td>` +
            `<td><button class="btn btn-sm btn-ghost btn-detail" data-loan="${r.loan_id}" data-date="${r.alert_date}" data-src="${r.src}">${esc(t("详情"))}</button></td>` +
            `</tr>`
          );
        })
        .join("");
    }

    // 记录当前筛选结果中的贷款 ID 列表（跨页重置），用于详情内 prev/next 导航。
    _alertLoanIds = data.rows.map((r) => String(r.loan_id));
    _alertCursor = -1;

    const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
    state.alertsPage = data.page;
    document.getElementById("page-info").textContent =
      `${t("第")} ${data.page}/${totalPages} ${t("页")}`;
    document.getElementById("prev-page").disabled = data.page <= 1;
    document.getElementById("next-page").disabled = data.page >= totalPages;
    syncAlertUrl(); // 渲染成功后把当前筛选写回 URL，刷新/复制链接可还原视图。
  } catch (e) {
    console.error("[spf] 预警列表渲染失败:", e);
    showPageErr(pageId, e, renderAlerts);
  } finally {
    setPageLoading("alerts", false);
  }
}

/* 闭环状态列：只放徽标 + 当前状态的操作人，动作按钮全部收敛到「操作」列（见 alertActionCell）。
 * 状态机：pending(未确认) → 确认 → confirmed(已确认待处置) → 处置 → disposed(处置中)
 * → 恢复 → recovered(已恢复)。处置动作隐含确认（后端落 confirm 记录）。 */
const DISP_LABEL = {
  pending: "未确认",
  confirmed: "已确认待处置",
  disposed: "处置中",
  recovered: "已恢复",
};

const DISP_BADGE = {
  pending: "off",
  confirmed: "warn",
  disposed: "warn",
  recovered: "ok",
};

/* 状态归一：后端缺 disposition_status 列时只回 confirmed 记录，此时按 confirmed 处理。 */
function dispStatus(r) {
  const st = (r.disposition && r.disposition.status) || "pending";
  if (st === "pending" && r.confirmed) return "confirmed";
  return st;
}

function dispositionCell(r) {
  const st = dispStatus(r);
  const disp = r.disposition || {};
  const actor = disp.by || (r.confirmed ? r.confirmed.confirmed_by : null);
  const who = actor ? `<span class="confirmed-tag">${esc(actor)}</span>` : "";
  return `<div class="disp-cell"><span class="badge ${DISP_BADGE[st] || "off"}">${t(DISP_LABEL[st]) || esc(st)}</span>${who}</div>`;
}

/* 单一状态推进动作：一行只给下一步该做的那一个按钮，行内列表与详情面板共用同一判定。
 * recovered 是终态、无下一步。cls 沿用原事件委托选择器（btn-confirm / btn-dispose）。 */
function nextStateAction(status) {
  if (status === "pending")
    return { label: t("确认"), cls: "btn-confirm", action: "confirm", next: null, primary: false };
  if (status === "confirmed")
    return {
      label: t("处置"),
      cls: "btn-dispose",
      action: "dispose",
      next: "disposed",
      primary: true,
    };
  if (status === "disposed")
    return {
      label: t("恢复"),
      cls: "btn-dispose",
      action: "dispose",
      next: "recovered",
      primary: false,
    };
  return null;
}

/* 操作列：无权限（非 admin/risk）时不给按钮，状态本身在「闭环状态」列已可读。 */
function alertActionCell(r) {
  const act = state.user.can_confirm ? nextStateAction(dispStatus(r)) : null;
  if (!act) return `<span class="disp-who">—</span>`;
  return (
    `<div class="disp-actions"><button class="btn btn-sm btn-ghost ${act.cls}"` +
    ` data-loan="${r.loan_id}" data-date="${r.alert_date}" data-src="${r.src}"` +
    `${act.next ? ` data-status="${act.next}"` : ""}>${act.label}</button></div>`
  );
}

/* ---------------- 行内详情展开（二级钻取） ---------------- */

// 当前筛选结果中的贷款序号，用于同行筛选下 prev/next 导航。
let _alertCursor = -1;
let _alertLoanIds = [];

async function toggleDetail(btn) {
  const tr = btn.closest("tr");
  const loan = btn.dataset.loan;
  const date = btn.dataset.date;
  const src = btn.dataset.src;
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains("detail-row")) {
    existing.remove();
    btn.textContent = t("详情");
    return;
  }
  // 同一时刻只展开一行。
  document.querySelectorAll("#alert-table tr.detail-row").forEach((el) => el.remove());
  document.querySelectorAll("#alert-table .btn-detail").forEach((b) => {
    if (b !== btn) b.textContent = t("详情");
  });
  const data = await api(
    `/api/alerts/detail?loan_id=${loan}&alert_date=${encodeURIComponent(date)}&source=${src}`
  );
  // 更新游标：在当前贷款列表中找位置。
  const idx = _alertLoanIds.indexOf(loan);
  _alertCursor = idx >= 0 ? idx : -1;
  const dtr = document.createElement("tr");
  dtr.className = "detail-row";
  dtr.innerHTML = `<td colspan="9" class="detail-cell">${buildDetailHtml(data, loan)}</td>`;
  tr.after(dtr);
  btn.textContent = t("收起");
}

/* 从地址解析城市：与 db._city_from_addr 口径一致（按 config.CITY_MAP 前缀匹配）。*/
function _parseCity(addr) {
  if (!addr) return null;
  const cities = [
    "广州",
    "深圳",
    "惠州",
    "东莞",
    "佛山",
    "中山",
    "珠海",
    "江门",
    "肇庆",
    "汕头",
    "汕尾",
    "潮州",
    "揭阳",
    "梅州",
    "河源",
    "清远",
    "韶关",
    "湛江",
    "茂名",
    "阳江",
    "云浮",
  ];
  for (const c of cities) {
    if (addr.startsWith(c)) return c;
  }
  return null;
}

function buildDetailHtml(d, _currentLoanId) {
  const a = d.alert;
  const rf = d.risk_factors;
  const cf = d.confirm;
  const disp = cf && cf.disposition ? cf.disposition : { status: "pending", by: null, ts: null };
  const fmtDt = (v) => (v ? esc(String(v).replace("T", " ").slice(0, 19)) : "—");
  const lv = (a.alert_level || "").toLowerCase();
  const lvLabel = lv === "strong" ? t("强预警级") : lv === "warn" ? t("警示级") : "—";
  const lvColor =
    lv === "strong" ? LTV_COLORS.strong : lv === "warn" ? LTV_COLORS.warn : LTV_COLORS.missing;
  const riskCls = a.risk_class || "—";
  const riskColor = CLASS_COLORS[riskCls] || "#6b7280";
  const ltvPct = a.ltv != null ? (a.ltv * 100).toFixed(1) : "—";
  const ltvNum = a.ltv || 0;
  // 与 config 同为「严格大于」触发：=0.85 归警示，>0.85 才走强预警色。
  const ltvBarColor =
    ltvNum > LTV_RED_LINE
      ? LTV_COLORS.strong
      : ltvNum > LTV_WARN_LINE
        ? LTV_COLORS.warn
        : "#2e9e5b";
  const bal = a.loan_balance || 0;
  const val = a.market_valuation || 0;
  const gapPct = val > 0 ? (((bal - val) / val) * 100).toFixed(1) : null;
  const gapNegative = bal > val;

  // 抵押物地址：只取城市（badge）+ 完整地址原文。
  // 不再做「区县」正则解析——种子地址是「城市+小区名+门牌」拼出来的，
  // 用 /[^区]+区/ 切出来的往往是小区名（如「中信凯旋城区」），是伪信息。
  const addr = d.property_addr || "—";
  const city = _parseCity(addr);
  // 风险因子卡片
  const riskCards = [];
  if (rf) {
    if (rf.low_confidence) riskCards.push({ label: t("空间特征低置信"), color: "#b45309" });
    if (rf.abnormal_valuation)
      riskCards.push({
        label: `${t("估值偏差")} ${fmtPct(rf.valuation_deviation_pct)}`,
        color: "#b91c1c",
      });
    else if (rf.valuation_deviation_pct != null)
      riskCards.push({
        label: `${t("估值偏差")} ${fmtPct(rf.valuation_deviation_pct)}`,
        color: "#2563eb",
      });
    if (rf.is_high_risk_zone) riskCards.push({ label: t("高危网格"), color: "#b91c1c" });
    if (rf.model_version)
      riskCards.push({ label: `${t("模型")} ${esc(rf.model_version)}`, color: "#6b7280" });
  }
  const riskCardsHtml = riskCards.length
    ? riskCards
        .map(
          (rc) =>
            `<div class="rf-card" style="border-left-color:${rc.color}"><span class="rf-label">${esc(rc.label)}</span></div>`
        )
        .join("")
    : `<div class="rf-card"><span class="rf-label">—</span></div>`;
  // 闭环状态归一后同时供「处置记录」行与操作按钮使用，保证详情与列表行徽标同一口径。
  const detailStatus = disp.status === "pending" && cf ? "confirmed" : disp.status;
  const detailActor = disp.by || (cf ? cf.confirmed_by : null);
  // 确认/处置行
  const confirmLine = cf ? `<b>${esc(cf.confirmed_by)}</b> @ ${fmtDt(cf.confirmed_ts)}` : "—";
  const disposeLine =
    detailStatus === "pending"
      ? "—"
      : `${DISP_LABEL[detailStatus] ? t(DISP_LABEL[detailStatus]) : esc(detailStatus)}${detailActor ? ` · <b>${esc(detailActor)}</b>${disp.ts ? ` @ ${fmtDt(disp.ts)}` : ""}` : ""}`;
  // 操作按钮（仅 admin/risk 可见）：与列表行共用 nextStateAction，一行只给下一步一个动作。
  const canAction = state.user && state.user.can_confirm;
  const nextAct = canAction ? nextStateAction(detailStatus) : null;
  const actionsHtml = nextAct
    ? `<button class="btn btn-sm ${nextAct.primary ? "btn-primary" : "btn-ghost"}"` +
      ` data-action="${nextAct.action}" data-loan="${a.loan_id}" data-date="${a.alert_date}" data-src="${a.src}"` +
      `${nextAct.next ? ` data-status="${nextAct.next}"` : ""}>${nextAct.label}</button>`
    : "";
  const actionsRow = actionsHtml ? `<div class="detail-actions">${actionsHtml}</div>` : "";
  // prev/next 导航
  const prevLoan = _alertCursor > 0 ? _alertLoanIds[_alertCursor - 1] : null;
  const nextLoan =
    _alertCursor >= 0 && _alertCursor < _alertLoanIds.length - 1
      ? _alertLoanIds[_alertCursor + 1]
      : null;
  const navHtml =
    prevLoan || nextLoan
      ? `<div class="detail-nav">
        ${prevLoan ? `<button class="btn btn-ghost btn-sm detail-nav-btn" data-prev="${prevLoan}">${t("上一笔")}</button>` : `<span></span>`}
        ${nextLoan ? `<button class="btn btn-ghost btn-sm detail-nav-btn" data-next="${nextLoan}">${t("下一笔")}</button>` : `<span></span>`}
      </div>`
      : "";
  const thresholds = [
    {
      val: LTV_WARN_LINE,
      label: `${t("警示线")} ${ltvPctTick(LTV_WARN_LINE)}`,
      color: LTV_COLORS.warn,
    },
    {
      val: LTV_RED_LINE,
      label: `${t("强预警线")} ${ltvPctTick(LTV_RED_LINE)}`,
      color: LTV_COLORS.strong,
    },
    { val: 1.0, label: t("爆仓线 100%"), color: "#374151" },
  ];
  const threshHtml = thresholds
    .map(
      (t) =>
        `<div class="ltv-threshold" style="left:${(t.val * 100).toFixed(0)}%;background:${t.color}" title="${t.label}"></div>`
    )
    .join("");
  // 处置建议（后端按 risk_factors + config 阈值规则派生，见 db.derive_suggestion）：
  // 只给一条主建议，避免风控同时看到互相矛盾的动作。风险宽表无该笔时 sg=null，整区不渲染。
  const sg = d.suggestion;
  const sgHtml = sg
    ? `<div class="detail-section">
        <div class="section-title">${t("处置建议")}</div>
        <div class="sg-box sg-${esc(sg.level || "info")}">
          <div class="sg-headline">${esc(sg.headline)}</div>
          <ol class="sg-actions">${(sg.actions || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
          ${(sg.basis || []).length ? `<div class="sg-basis">${t("依据")}：${(sg.basis || []).map((x) => esc(x)).join("；")}</div>` : ""}
        </div>
      </div>`
    : "";
  return `<div class="detail-panel">
      <div class="detail-header">
        <div class="detail-header-left">
          <span class="detail-lv-badge" style="background:${lvColor}22;color:${lvColor}">${lvLabel}</span>
          <span class="detail-risk-badge" style="background:${riskColor}22;color:${riskColor}">${esc(clsName(riskCls))}</span>
          <span class="detail-loan-id">${t("贷款")} ${a.loan_id}</span>
        </div>
        <div class="detail-header-right">
          <span class="detail-ltv-big">${ltvPct}%</span>
          <span class="detail-ltv-label">LTV</span>
        </div>
      </div>
      <div class="ltv-gauge-wrap">
        <div class="ltv-gauge-track">
          <div class="ltv-gauge-fill" style="width:${Math.min(ltvNum * 100, 100).toFixed(1)}%;background:${ltvBarColor}"></div>
          ${threshHtml}
        </div>
        <div class="ltv-gauge-labels">
          <span>0%</span><span>${ltvPctTick(LTV_WARN_LINE)}</span><span>${ltvPctTick(LTV_RED_LINE)}</span><span>${t("100%爆仓")}</span>
        </div>
      </div>
      <div class="detail-section">
        <div class="section-title">${t("估值与余额")}</div>
        <div class="detail-grid detail-grid-2">
          <div class="detail-item"><span>${t("贷款余额")}</span><b>${fmtMoney(bal)}</b></div>
          <div class="detail-item"><span>${t("市场估值")}</span><b>${fmtMoney(val)}</b></div>
          <div class="detail-item">
            <span>${t("缺口")}</span>
            <b style="color:${gapNegative ? "#b91c1c" : "#2e9e5b"}">${gapPct != null ? (gapNegative ? "-" : "+") + fmtMoney(Math.abs(bal - val)) : "—"}</b>
            ${gapPct != null ? `<span class="gap-tag ${gapNegative ? "gap-tag-bad" : "gap-tag-ok"}">${gapNegative ? t("超额") : t("安全")} ${fmtPct(gapPct / 100)}</span>` : ""}
          </div>
        </div>
      </div>
      <div class="detail-section">
        <div class="section-title">${t("抵押物信息")}</div>
        <div class="detail-grid detail-grid-2">
          <div class="detail-item" style="grid-column:1/-1"><span>${t("详细地址")}</span><b>${city ? `<span class="badge ok">${esc(city)}</span> ` : ""}${esc(addr)}</b></div>
          <div class="detail-item"><span>${t("抵押物号")}</span><b>${a.collateral_id ?? "—"}</b></div>
          <div class="detail-item"><span>${t("客户号")}</span><b>${esc(a.customer_id ?? "—")}</b></div>
        </div>
      </div>
      <div class="detail-section">
        <div class="section-title">${t("风险因子")}</div>
        <div class="rf-cards">${riskCardsHtml}</div>
      </div>
      ${sgHtml}
      <div class="detail-section">
        <div class="section-title">${t("确认与处置")}</div>
        <div class="detail-grid detail-grid-2">
          <div class="detail-item"><span>${t("确认记录")}</span><b>${confirmLine}</b></div>
          <div class="detail-item"><span>${t("处置记录")}</span><b>${disposeLine}</b></div>
          <div class="detail-item"><span>${t("来源")}</span><b>${a.src === "offline" ? t("离线 T+1") : t("实时流")}</b></div>
          <div class="detail-item"><span>${t("预警日期")}</span><b>${a.alert_date}</b></div>
        </div>
      </div>
      ${actionsRow}
      ${navHtml}
    </div>`;
}

async function disposeAlert(loanId, alertDate, source, status) {
  const isRecover = status === "recovered";
  // 处置是状态变更，二次确认并写清将改变为什么状态。
  if (
    !window.confirm(
      `${t("将对贷款")} ${loanId} · ${alertDate} ${t("执行「")}${isRecover ? t("恢复") : t("处置")}${t("」，状态将变为「")}${isRecover ? t("已恢复") : t("处置中")}${t("」。确认？")}`
    )
  )
    return;
  try {
    await api("/api/alerts/dispose", {
      method: "POST",
      body: JSON.stringify({ loan_id: loanId, alert_date: alertDate, source, status }),
    });
    showToast(isRecover ? t("恢复成功：状态已更新") : t("处置成功：状态已更新"), true);
    renderAlerts();
  } catch (e) {
    showToast(
      `${isRecover ? t("恢复失败") : t("处置失败")}：${esc(String((e && e.message) || e))}`,
      false
    );
  }
}

async function confirmAlert(loanId, alertDate, source) {
  if (
    !window.confirm(
      `${t("确认该笔预警（贷款")} ${loanId} · ${alertDate}${t("）？确认后状态将变为「")}${t("已确认待处置")}${t("」。")}`
    )
  )
    return;
  try {
    await api("/api/alerts/confirm", {
      method: "POST",
      body: JSON.stringify({ loan_id: loanId, alert_date: alertDate, source }),
    });
    showToast(t("确认成功：预警已确认"), true);
    renderAlerts();
  } catch (e) {
    showToast(`${t("确认失败")}：${esc(String((e && e.message) || e))}`, false);
  }
}

/* ---------------- 1104 报送 ---------------- */

/* 三个口径出口对照：出口① ads_risk_class（内部累计）、出口② dws_risk_class（风险明细聚合裁判）、
   出口③ 1104 模板行（由出口①生成）。后端 mismatch / 告警 detail 形如 <scope>:<类>:<字段>，
   统一转成业务能读懂的中文，避免 g11_vs_dws / dws_vs_internal 等技术 key 直接暴露给业务用户。 */
function businessMismatch(key) {
  const k = String(key || "").trim();
  // 特殊项：五级占比恒等式。
  if (/^g11:占比之和:balance_pct$/.test(k)) {
    return t("「五级分类余额占比」之和不等于 100%（G11 恒等式校验不通过）");
  }
  const m =
    /^(g11_vs_dws|dws_vs_internal|1104_vs_dws|1104_vs_internal):(.+?):(loan_count|balance|balance_pct)$/.exec(
      k
    );
  if (!m) return key;
  const scope = m[1],
    cls = m[2],
    field = m[3];
  const clsName =
    {
      正常: t("正常"),
      关注: t("关注"),
      次级: t("次级"),
      可疑: t("可疑"),
      损失: t("损失"),
      总额: t("合计"),
    }[cls] || cls;
  const fldName =
    { loan_count: t("笔数"), balance: t("余额"), balance_pct: t("余额占比") }[field] || field;
  const pair = {
    g11_vs_dws: [t("1104 报送快照"), t("风险明细聚合")],
    dws_vs_internal: [t("风险明细聚合"), t("内部累计口径")],
    "1104_vs_dws": [t("1104 模板行"), t("风险明细聚合")],
    "1104_vs_internal": [t("1104 模板行"), t("内部累计口径")],
  }[scope];
  return `${t("「")}${pair[0]}${t("」与「")}${pair[1]}${t("」在「")}${clsName}${t("」类的「")}${fldName}${t("」口径不一致")}`;
}

/* 把「阻断告警历史」里的内容转成业务语言，避免技术泄漏：
   - SQL 写入异常 → 可操作中文提示；
   - 口径不一致告警 detail 形如「口径不一致: <key>」→ 转业务中文。 */
function businessAlertDetail(detail) {
  const d = String(detail || "");
  if (/Data too long for column/i.test(d))
    return t("报送快照写入失败：字段内容超出长度限制，请点「重建快照」重试");
  if (/Out of range value for column/i.test(d))
    return t("报送快照写入失败：数值超出字段范围，请点「重建快照」重试");
  if (/Duplicate entry/i.test(d))
    return t("报送快照写入失败：与已有记录主键冲突，请点「重建快照」重试");
  if (/errno|SQLSTATE/i.test(d)) return t("报送快照写入异常，请点「重建快照」重试");
  const mm = /口径不一致[:：]\s*(.+)$/.exec(d);
  if (mm) return `${t("口径不一致")}：${businessMismatch(mm[1].trim())}`;
  return d;
}

/* 报送五态（与后端 db.REPORT_STATUSES 一一对应）：
   pending=尚无可报送快照 / consistent=口径一致可报送 / submitted=已报送 /
   blocked=口径不一致已阻断 / checking=正在实时复算（前端瞬时态，永不落库）。
   徽标做「颜色 + 图标 + 文字」三重编码：五色互斥（灰/橙/绿/蓝/红），
   consistent 与 submitted 不再同绿——色弱用户也能靠图标与文字区分。 */
const REPORT_STATUS = {
  pending: {
    cls: "badge status-pending",
    icon: "✎",
    text: "待报送",
    hint: "该业务日尚无可报送快照",
  },
  checking: {
    cls: "badge status-checking",
    icon: "⏳",
    text: "校验中",
    hint: "正在实时复算口径一致性（只读，不落库）",
  },
  consistent: {
    cls: "badge status-consistent",
    icon: "✔",
    text: "口径一致",
    hint: "与风险明细聚合口径一致，可提交报送",
  },
  submitted: { cls: "badge status-submitted", icon: "▤", text: "已报送", hint: "该业务日已报送" },
  blocked: {
    cls: "badge status-blocked",
    icon: "⛔",
    text: "已阻断",
    hint: "口径不一致，报送已阻断",
  },
};

function setReportStatusBadge(status) {
  const el = document.getElementById("report-status");
  if (!el) return;
  const s = REPORT_STATUS[status] || REPORT_STATUS.pending;
  el.className = s.cls;
  el.dataset.status = status;
  // 图标只是文字的冗余编码，对读屏无意义 → aria-hidden；完整解释放 title。
  el.innerHTML = `<span class="badge-icon" aria-hidden="true">${s.icon}</span>${esc(t(s.text))}`;
  el.title = t(s.hint);
}

/* 报送按钮按态禁用（D-07：禁用必须可见 + 说明为什么不能点）。
   注意 blocked 态下即使之前报过也要提示重报——快照已经变了。 */
function updateSubmitBtn(status, data) {
  const btn = document.getElementById("btn-submit");
  const hint = document.getElementById("submit-hint");
  if (!btn) return;
  const sub = (data && data.submission) || null;
  const sentCount =
    data && data.alert_sent_count != null
      ? data.alert_sent_count
      : data && data.alerts
        ? data.alerts.filter((a) => a.alert_level === "block").length
        : 0;
  const firstAt =
    sub && sub.submitted_at ? String(sub.submitted_at).replace("T", " ").slice(0, 16) : null;
  let hintText = "",
    hintCls = "submit-hint";
  if (status === "consistent") {
    btn.disabled = false;
    btn.textContent = t("报送");
    hintText = t("口径已校验一致，可提交报送");
    hintCls += " ok";
  } else if (status === "submitted") {
    btn.disabled = true;
    btn.textContent = t("已报送");
    hintText = firstAt
      ? `${t("已于")} ${firstAt} ${t("由")} ${sub.submitted_by || "—"} ${t("报送")}`
      : t("该业务日已报送");
    hintCls += " ok";
  } else if (status === "blocked") {
    btn.disabled = true;
    btn.textContent = t("报送");
    hintText = sentCount
      ? `${t("口径不一致，报送已阻断 · 告警已发（")}${sentCount} ${t("条", { n: sentCount })}）`
      : t("口径不一致，报送已阻断");
    if (firstAt) hintText += ` · ${t("曾于")} ${firstAt} ${t("报送，快照已变更需重报")}`;
    hintCls += " bad";
  } else if (status === "checking") {
    btn.disabled = true;
    hintText = t("校验中：正在实时复算口径一致性…");
  } else {
    btn.disabled = true;
    btn.textContent = t("报送");
    hintText = t("该业务日尚无可报送快照");
  }
  if (hint) {
    hint.textContent = hintText;
    hint.className = hintCls;
  }
}

/* 渲染口径一致性校验区：五态徽标 + mismatch 对照矩阵 + 归因提示 + 修复入口。
   status 由后端派生；旧口径 consistent 作为兜底（老接口/老缓存响应仍可渲染）。
   mismatch_details 携带双方取值与差值 → 渲染「差在哪、差多少」矩阵，一眼定位；
   老响应无 details 时回退纯文本列表。 */
function renderReportCheck(data, checkedAt) {
  const status = data.status || (data.consistent ? "consistent" : "blocked");
  setReportStatusBadge(status);
  updateSubmitBtn(status, data);
  const checkEl = document.getElementById("report-check");
  if (data.consistent) {
    checkEl.innerHTML = `<div class="check-line">${t("与风险明细聚合口径一致（笔数/余额均在容差内）")}</div>`;
  } else {
    const details = data.mismatch_details || [];
    const fldName = { loan_count: t("笔数"), balance: t("余额") };
    // 笔数直接展示；余额用短格式（万/亿）保持与 G11 表一致，精确值放悬浮提示。
    const fmtCell = (d, v) =>
      v == null
        ? "-"
        : d.field === "balance"
          ? `<span title="${Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}">${fmtMoney(v)}</span>`
          : Number(v).toLocaleString("zh-CN");
    let html = `<div class="check-line check-bad">${t("以下口径与风险明细聚合不一致，报送已阻断：")}</div>`;
    if (details.length) {
      // 对照矩阵：行=分类×指标，列=双方取值与差值。差值带符号（快照−明细）。
      html +=
        `<table class="mismatch-table"><thead><tr>` +
        `<th>${t("分类")}</th><th>${t("指标")}</th><th class="num">${t("1104 快照")}</th>` +
        `<th class="num">${t("明细聚合")}</th><th class="num">${t("差（快照−明细）")}</th>` +
        `</tr></thead><tbody>` +
        details
          .map((d) => {
            const sign = d.diff > 0 ? "+" : d.diff < 0 ? "−" : "";
            return (
              `<tr><td>${esc(clsName(d.risk_class))}</td>` +
              `<td>${esc(fldName[d.field] || d.field)}</td>` +
              `<td class="num">${fmtCell(d, d.snapshot)}</td>` +
              `<td class="num">${fmtCell(d, d.dws)}</td>` +
              `<td class="num mismatch-diff" title="${sign}${Number(d.diff).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}">${sign}${fmtCell(d, Math.abs(d.diff))}</td></tr>`
            );
          })
          .join("") +
        `</tbody></table>`;
    } else {
      // 老响应兜底：无 details 时退回纯文本列表。
      html += `<div class="mismatch-list">${(data.mismatches || [])
        .map((m) => `<div class="mismatch-item">· ${esc(businessMismatch(m))}</div>`)
        .join("")}</div>`;
    }
    // 归因提示：这类差异几乎都源于快照生成后明细又变了，直接给修复路径。
    html +=
      `<div class="check-line mismatch-hint">${t("常见原因：快照生成后风险明细又发生变化（新增贷款或五级分类调整）。")}` +
      `${t("以「明细聚合」为准重建快照即可恢复一致。")}</div>`;
    // 修复入口就近放：不用再去「更多」菜单找（写操作仍由服务端按权限校验）。
    if (state.user && state.user.can_confirm) {
      html += `<button id="btn-rebuild-inline" class="btn btn-ghost btn-sm" style="margin-top:8px">${t("按明细重建快照")}</button>`;
    }
    checkEl.innerHTML = html;
    const inlineBtn = document.getElementById("btn-rebuild-inline");
    if (inlineBtn) inlineBtn.addEventListener("click", rebuildReport);
  }
  const checkedEl = document.getElementById("report-checked-at");
  if (checkedEl) checkedEl.textContent = checkedAt ? `${t("校验时间")} ${checkedAt}` : "";
}

async function renderReport() {
  const pageId = "page-report";
  clearPageErr(pageId);
  try {
    const dates = (await api("/api/report/dates")).dates;
    const sel = document.getElementById("report-date");
    const prev = sel.value;
    sel.innerHTML = dates.map((d) => `<option>${d}</option>`).join("");
    if (prev && dates.includes(prev)) sel.value = prev;

    const date = sel.value;
    if (!date) {
      setReportStatusBadge("pending");
      document.getElementById("report-checked-at").textContent = "";
      document.getElementById("report-check").innerHTML = "";
      document.querySelector("#report-table tbody").innerHTML =
        `<tr><td colspan="4" class="empty">${t("尚无报送数据")}</td></tr>`;
      updateSubmitBtn("pending", {});
      return;
    }
    const data = await api("/api/report?date=" + encodeURIComponent(date));

    renderReportCheck(data, null);

    const tbody = document.querySelector("#report-table tbody");
    tbody.innerHTML = data.rows
      .map(
        (r) =>
          `<tr style="${r.is_total ? "font-weight:700;background:#f8fafc" : ""}">` +
          `<td>${r.is_total ? t("合计") : esc(clsName(r.risk_class))}</td>` +
          `<td class="num">${r.loan_count}</td>` +
          `<td class="num">${fmtMoney(r.balance_total)}</td>` +
          `<td class="num">${fmtPct(r.balance_pct)}</td>` +
          `</tr>`
      )
      .join("");

    const alertEl = document.getElementById("report-alerts");
    // 本次实时校验结论并入时间线顶部：blocked 态下历史可能为空（告警由报送 CLI 落库，
    // 页面校验只读不落库），不并入的话「已阻断」与「无阻断告警」并列会自相矛盾。
    const nowLine =
      data.status === "blocked"
        ? `<div class="check-line alert-now" title="${t("实时复算结论并入时间线顶部；历史告警由报送流程写入")}"><span class="tag-now">${t("本次校验")}</span>${t("口径不一致，已阻断本次报送")}</div>`
        : "";
    const histLines = data.alerts
      .map(
        (a) =>
          `<div class="check-line">#${a.id} [${a.alert_level}] ${esc(businessAlertDetail(a.detail))}` +
          `<span class="alert-ts">${esc(String(a.etl_ts).replace("T", " ").slice(0, 16))}</span></div>`
      )
      .join("");
    alertEl.innerHTML =
      nowLine + (histLines ? histLines : `<div class="check-line">${t("暂无历史告警")}</div>`);
  } catch (e) {
    console.error("[spf] 1104 报送页渲染失败:", e);
    showPageErr(pageId, e, renderReport);
  } finally {
    setPageLoading("report", false);
  }
}

/* 「重新校验」：实时复算口径一致性，只读不落库，所有人可点。 */
async function recheckReport() {
  const date = document.getElementById("report-date").value;
  if (!date) {
    showToast(t("请先选择业务日期"), false);
    return;
  }
  const btn = document.getElementById("btn-recheck");
  btn.disabled = true; // 防重复点击
  // checking 是纯前端瞬时态：只改徽标，绝不落库。
  setReportStatusBadge("checking");
  updateSubmitBtn("checking", {});
  try {
    const data = await api("/api/report/recheck", {
      method: "POST",
      body: JSON.stringify({ date }),
    });
    renderReportCheck(data, data.checked_at);
    showToast(t("校验完成"), true);
  } catch (e) {
    showToast(`${t("重新校验失败")}：${esc(String((e && e.message) || e))}`, false);
  } finally {
    btn.disabled = false;
  }
}

/* 「报送」：把该业务日标记为已报送（落库，刷新/重启后保留）。
   后端会自复算口径，不一致直接 409 且不落库——所以这里不做前端判断，只负责二次确认。 */
async function submitReport() {
  const date = document.getElementById("report-date").value;
  if (!date) {
    showToast(t("请先选择业务日期"), false);
    return;
  }
  if (
    !window.confirm(
      `${t("将把")} ${date} ${t("的 G11 报表标记为「")}${t("已报送")}${t("」，确认？")}`
    )
  )
    return;
  const btn = document.getElementById("btn-submit");
  btn.disabled = true;
  try {
    await api("/api/report/submit", { method: "POST", body: JSON.stringify({ date }) });
    showToast(`${t("报送成功")}：${date} ${t("已标记为已报送")}`, true);
  } catch (e) {
    showToast(`${t("报送失败")}：${esc(String((e && e.message) || e))}`, false);
  } finally {
    // 无论成功或被阻断都重拉一次，按服务端真实状态刷新徽标与按钮。
    await renderReport();
  }
}

/* 「重建快照」：从 dws_risk_class 重建该日 G11 快照写回（修复漂移），写操作需二次确认。 */
async function rebuildReport() {
  const date = document.getElementById("report-date").value;
  if (!date) {
    showToast(t("请先选择业务日期"), false);
    return;
  }
  if (
    !window.confirm(
      t("将用风险明细重建该日 G11 快照并覆盖，不可撤销；覆盖后报送态回退，需重新报送。确认？")
    )
  )
    return;
  const btn = document.getElementById("btn-rebuild");
  btn.disabled = true;
  try {
    const data = await api("/api/report/rebuild", {
      method: "POST",
      body: JSON.stringify({ date }),
    });
    showToast(`${t("重建成功")}：${t("重建了")} ${data.rebuilt_rows} ${t("行")}`, true);
    await renderReport(); // 重建后重新拉 /api/report 渲染表格与状态。
  } catch (e) {
    showToast(`${t("重建快照失败")}：${esc(String((e && e.message) || e))}`, false);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 初始化 ---------------- */

async function init() {
  initLangSwitchers();
  // 启动即应用 localStorage 里保存的语言（如上次选了 zh-HK/English），
  // 否则静态文本会停留 zh-CN 直到用户手动切换一次。
  applyI18nText(document);
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-err");
    err.textContent = "";
    try {
      await api("/api/login", {
        method: "POST",
        body: JSON.stringify({
          username: document.getElementById("login-user").value.trim(),
          password: document.getElementById("login-pass").value,
        }),
      });
      const me = await api("/api/me");
      state.user = me;
      await showApp();
    } catch {
      err.textContent = t("账号或密码错误");
    }
  });

  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    state.user = null;
    showLogin();
  });

  document.getElementById("alert-filter").addEventListener("submit", (e) => {
    e.preventDefault();
    state.alertsPage = 1;
    renderAlerts();
  });
  document.getElementById("prev-page").addEventListener("click", () => {
    state.alertsPage = Math.max(1, state.alertsPage - 1);
    renderAlerts();
  });
  document.getElementById("next-page").addEventListener("click", () => {
    state.alertsPage += 1;
    renderAlerts();
  });
  document.getElementById("report-date").addEventListener("change", renderReport);

  // 1104 可操作闭环：报送（写，按态禁用）+ 重新校验（所有人）+ 重建快照（写，按权限裁剪）。
  document.getElementById("btn-submit").addEventListener("click", submitReport);
  document.getElementById("btn-recheck").addEventListener("click", recheckReport);
  document.getElementById("btn-rebuild").addEventListener("click", rebuildReport);

  // 图表钻取：绑定一次（dashboard 容器常驻，事件委托无需每次重绑）。
  bindChartDrill();

  // 侧栏窄屏抽屉开关（宽屏下按钮被 CSS 隐藏，行为不变）。
  bindSidebarToggle();

  // 预警列表操作按钮用事件委托，避免整表重绑。
  document.querySelector("#alert-table tbody").addEventListener("click", (e) => {
    const confBtn = e.target.closest(".btn-confirm");
    if (confBtn) {
      confirmAlert(confBtn.dataset.loan, confBtn.dataset.date, confBtn.dataset.src);
      return;
    }
    const dispBtn = e.target.closest(".btn-dispose");
    if (dispBtn) {
      disposeAlert(
        dispBtn.dataset.loan,
        dispBtn.dataset.date,
        dispBtn.dataset.src,
        dispBtn.dataset.status
      );
      return;
    }
    const detailBtn = e.target.closest(".btn-detail");
    if (detailBtn) {
      toggleDetail(detailBtn);
      return;
    }
    // 详情面板内的操作按钮（确认/处置/恢复）
    const actionBtn = e.target.closest("[data-action]");
    if (actionBtn) {
      const action = actionBtn.dataset.action;
      if (action === "confirm") {
        confirmAlert(actionBtn.dataset.loan, actionBtn.dataset.date, actionBtn.dataset.src);
      } else if (action === "dispose") {
        disposeAlert(
          actionBtn.dataset.loan,
          actionBtn.dataset.date,
          actionBtn.dataset.src,
          actionBtn.dataset.status
        );
      }
      return;
    }
    // 详情面板内 prev/next 导航
    const navBtn = e.target.closest(".detail-nav-btn");
    if (navBtn) {
      const targetLoan = navBtn.dataset.prev || navBtn.dataset.next;
      const nextBtn = document.querySelector(`.btn-detail[data-loan="${targetLoan}"]`);
      if (nextBtn) nextBtn.click();
      return;
    }
  });

  const me = await api("/api/me");
  if (me.logged_in) {
    state.user = me;
    // 「报送」「重建快照」都是写操作，仅 admin/risk（can_confirm）可见；服务端仍会强制校验。
    if (!me.can_confirm) {
      for (const id of ["btn-submit", "btn-rebuild"]) {
        const el = document.getElementById(id);
        if (el) el.style.display = "none";
      }
    }
    await showApp();
  } else {
    showLogin();
  }
}

init();

// ---- 热重载轮询：与服务端 /api/reload-version 令牌比对，不一致即刷新（无需手动 F5）----
(function hotReloadPoll() {
  let last = null;
  const tick = async () => {
    try {
      const r = await fetch("/api/reload-version", { cache: "no-store" });
      if (!r.ok) return;
      const j = await r.json();
      if (last !== null && j.version !== last) {
        location.reload();
        return;
      }
      last = j.version;
    } catch (_) {
      /* 服务重启瞬间可能 502/ECONNRESET，忽略后下次窗口再试 */
    }
  };
  tick();
  setInterval(tick, 1000);
})();
