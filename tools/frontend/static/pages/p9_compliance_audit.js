/* P9 · 合规审计（面向合规官）。
 *
 * 导出审计：谁在什么时候导出/确认/配置了什么，支持筛选和导出。
 *
 * 特征归因已移至 P7 AVM 估值管理页。
 * 原「报送阻断告警」Tab 与 P4 报送页读同一张 ads_report_alert，属跨页重复，
 * 已下掉本页该 Tab，仅保留 P4 一处（R-UBQ-02 归属）。
 */

(function () {
  const { api, esc, fmtMoney } = window.spf;

  /* 页面级多语言字典（key = zh-CN 原文，译文只存此处）。
   * 数据键（action / result / alert_level / detail 里的键）保持原文用于匹配后端，
   * 仅展示层经 t() 翻译；t() 无译文时原样返回 key。 */
  registerI18n("en", {
    合规审计: "Compliance Audit",
    ID: "ID",
    日期从: "From",
    日期至: "To",
    操作类型: "Action Type",
    全部: "All",
    "导出预警 CSV": "Export Alert CSV",
    预警确认: "Alert Confirmation",
    预警处置: "Alert Disposal",
    数据源开关变更: "Datasource Toggle Change",
    策略保存: "Policy Save",
    策略删除: "Policy Delete",
    策略预览: "Policy Preview",
    校准重算: "Calibration Refresh",
    归因重建: "Attribution Refresh",
    "1104 报送": "1104 Submission",
    重建报送快照: "Rebuild Submission Snapshot",
    操作人: "Operator",
    搜索用户名: "Search username",
    查询: "Search",
    "导出 CSV": "Export CSV",
    动作: "Action",
    角色: "Role",
    行数: "Rows",
    结果: "Result",
    "来源 IP": "Source IP",
    来源IP: "Source IP",
    时间: "Time",
    明细: "Details",
    成功: "Success",
    已确认: "Confirmed",
    离线: "Offline",
    实时: "Realtime",
    失败: "Failed",
    被阻断: "Blocked",
    贷款: "Loan",
    预警日: "Alert Date",
    预警来源: "Alert Source",
    状态: "Status",
    五级分类: "Risk Tier",
    "LTV 下限": "LTV Floor",
    "LTV 上限": "LTV Cap",
    规则编号: "Policy ID",
    作用域: "Scope",
    启用: "Enabled",
    停用: "Disabled",
    理由: "Reason",
    命中笔数: "Hit Count",
    命中余额: "Hit Balance",
    新增受限: "Newly Restricted",
    "规则：": "Rule: ",
    "命中 {n} 笔": "Hit {n} {n|loan|loans}",
    "新增受限 {n} 笔": "Newly Restricted {n}",
    数据源: "Source",
    字段: "Field",
    值: "Value",
    错误: "Error",
    越权角色: "Unauthorized Role",
    请求体: "Request Body",
    处置中: "In Progress",
    已恢复: "Recovered",
    // 拒贷/加严审批/仅提示 已上提 app.js global 公共层（en 译法以审计页为准）。
    广州: "Guangzhou",
    深圳: "Shenzhen",
    佛山: "Foshan",
    东莞: "Dongguan",
    珠海: "Zhuhai",
    中山: "Zhongshan",
    惠州: "Huizhou",
    江门: "Jiangmen",
    肇庆: "Zhaoqing",
    清远: "Qingyuan",
    韶关: "Shaoguan",
    汕头: "Shantou",
    汕尾: "Shanwei",
    揭阳: "Jieyang",
    潮州: "Chaozhou",
    梅州: "Meizhou",
    河源: "Heyuan",
    阳江: "Yangjiang",
    茂名: "Maoming",
    湛江: "Zhanjiang",
    云浮: "Yunfu",
    "（全市）": " (citywide)",
    "LTV 网格": "LTV Grid",
    价格网格: "Price Grid",
    行业: "Industry",
    重建: "Rebuild",
    报送: "Submit",
    模式: "Mode",
    按指纹: "By Fingerprint",
    强制: "Force",
    "（按指纹）": " (by fingerprint)",
    "（强制）": " (forced)",
    "(按指纹)": " (by fingerprint)",
    "(强制)": " (forced)",
    摘要: "Summary",
    脚本路径: "Script Path",
    // 历史 audit detail 中文短语（app.py 改造前留下的非结构化字符串，新数据走 JSON 不再产生）
    "报送 G11 快照": "Submitted G11 snapshot",
    "报送 G11": "Submitted G11",
    "重建 G11 快照": "Rebuilt G11 snapshot",
    "重建 G11": "Rebuilt G11",
    "重建校准报告缺少脚本": "Rebuild calibration report: missing script",
    "重建校准报告执行失败": "Rebuild calibration report: execution failed",
    "重建校准报告": "Rebuild calibration report",
    "校准报告重建": "Calibration report refresh",
    "重建归因产物缺少脚本": "Rebuild attribution: missing script",
    "重建归因产物执行失败": "Rebuild attribution: execution failed",
    "重建归因产物": "Rebuild attribution",
    "归因产物重建": "Attribution refresh",
    // 带左括号：en 排版需 "Attribution refresh ("，与 "Guangzhou (citywide)" 一致
    "归因产物重建(": "Attribution refresh (",
    // 尾部不带空格：原文 "被阻断: 口径不一致" 里的空格已由原串提供，
    // 译文再带尾空格会变成 "Blocked:  Inconsistent"（双空格）。
    "被阻断：": "Blocked:",
    "被阻断:": "Blocked:",
    "执行失败：": "Execution failed:",
    "执行失败:": "Execution failed:",
    "缺少脚本": "Missing script",
    "参数不合法": "Invalid parameter",
    // RESULT_LABELS 的裸串（后端 result 值 → 展示词，resultBadge 经 t() 渲染）
    "日期不合法": "Invalid date",
    "校验失败": "Validation failed",
    "执行失败": "Execution failed",
    "脚本退出非零": "Script exited non-zero",
    "口径不一致": "Inconsistent calibration",
    // 带全角逗号的整段：原文 "口径不一致，报送已阻断" 的逗号是全角「，」，
    // en 下必须一并替换成半角；"报送已阻断" 处于句中，首字母小写。
    "，报送已阻断": ", submission blocked",
    "报送已阻断": "Submission blocked",
    "必须落在": "must be within",
    " 区间": " range",
    " 快照 ": " snapshot ",
    "快照 ": "snapshot ",
    " 行": " rows",
    // 建议 reason 模板骨架（p6_policy.handle_suggest 生成的系统 reason，见 reasonText）。
    "高危网格自动建议": "High-risk grid auto-suggestion",
    "（规则 ": " (Rule ",
    "，样本 ": ", Sample ",
    "）：": "): ",
    "LTV 上限压至 ": "LTV Cap lowered to ",
    // 动作/行业/城市词条已上提 app.js global 公共层，不再在此重复注册。
    是: "Yes",
    否: "No",
    "共 ": "Total ",
    " 条审计记录": " audit records",
    暂无审计记录: "No audit records",
    当前没有可导出的审计记录: "No audit records available to export",
    合规审计_导出记录_: "ComplianceAudit_ExportRecords_",
  }, "compliance_audit");
  registerI18n("zh-HK", {
    合规审计: "合規審計",
    ID: "ID",
    日期从: "日期從",
    日期至: "日期至",
    操作类型: "操作類型",
    全部: "全部",
    "导出预警 CSV": "匯出預警 CSV",
    预警确认: "預警確認",
    预警处置: "預警處置",
    数据源开关变更: "資料源開關變更",
    策略保存: "策略儲存",
    策略删除: "策略刪除",
    策略预览: "策略預覽",
    校准重算: "校準重算",
    归因重建: "歸因重建",
    "1104 报送": "1104 報送",
    重建报送快照: "重建報送快照",
    操作人: "操作人",
    搜索用户名: "搜尋使用者名稱",
    查询: "查詢",
    "导出 CSV": "匯出 CSV",
    动作: "動作",
    角色: "角色",
    行数: "行數",
    结果: "結果",
    "来源 IP": "來源 IP",
    来源IP: "來源 IP",
    时间: "時間",
    明细: "明細",
    成功: "成功",
    已确认: "已確認",
    离线: "離線",
    实时: "即時",
    失败: "失敗",
    被阻断: "被阻斷",
    贷款: "貸款",
    预警日: "預警日",
    预警来源: "預警來源",
    状态: "狀態",
    五级分类: "五級分類",
    "LTV 下限": "LTV 下限",
    "LTV 上限": "LTV 上限",
    规则编号: "規則編號",
    作用域: "作用域",
    启用: "啟用",
    停用: "停用",
    理由: "理由",
    命中笔数: "命中筆數",
    命中余额: "命中餘額",
    新增受限: "新增受限",
    "规则：": "規則：",
    "命中 {n} 笔": "命中 {n} 筆",
    "新增受限 {n} 笔": "新增受限 {n} 筆",
    数据源: "資料源",
    字段: "欄位",
    值: "值",
    错误: "錯誤",
    越权角色: "越權角色",
    请求体: "請求體",
    处置中: "處置中",
    已恢复: "已恢復",
    // 动作词条已上提 app.js global 公共层。
    广州: "廣州",
    深圳: "深圳",
    佛山: "佛山",
    东莞: "東莞",
    珠海: "珠海",
    中山: "中山",
    惠州: "惠州",
    江门: "江門",
    肇庆: "肇慶",
    清远: "清遠",
    韶关: "韶關",
    汕头: "汕頭",
    汕尾: "汕尾",
    揭阳: "揭陽",
    潮州: "潮州",
    梅州: "梅州",
    河源: "河源",
    阳江: "陽江",
    茂名: "茂名",
    湛江: "湛江",
    云浮: "雲浮",
    "（全市）": "（全市）",
    "LTV 网格": "LTV 網格",
    价格网格: "價格網格",
    行业: "行業",
    重建: "重建",
    报送: "報送",
    模式: "模式",
    按指纹: "按指紋",
    强制: "強制",
    "（按指纹）": "（按指紋）",
    "（强制）": "（強制）",
    "(按指纹)": "(按指紋)",
    "(强制)": "(強制)",
    摘要: "摘要",
    脚本路径: "腳本路徑",
    // 历史 detail 中文短语 → 繁体
    "报送 G11 快照": "報送 G11 快照",
    "报送 G11": "報送 G11",
    "重建 G11 快照": "重建 G11 快照",
    "重建 G11": "重建 G11",
    "重建校准报告缺少脚本": "重建校準報告缺少腳本",
    "重建校准报告执行失败": "重建校準報告執行失敗",
    "重建校准报告": "重建校準報告",
    "校准报告重建": "校準報告重建",
    "重建归因产物缺少脚本": "重建歸因產物缺少腳本",
    "重建归因产物执行失败": "重建歸因產物執行失敗",
    "重建归因产物": "重建歸因產物",
    "归因产物重建": "歸因產物重建",
    "归因产物重建(": "歸因產物重建(",
    "被阻断：": "被阻斷：",
    "被阻断:": "被阻斷:",
    "执行失败：": "執行失敗：",
    "执行失败:": "執行失敗:",
    "缺少脚本": "缺少腳本",
    "参数不合法": "參數不合法",
    // 與 en 同批：RESULT_LABELS 的裸串
    "日期不合法": "日期不合法",
    "校验失败": "校驗失敗",
    "执行失败": "執行失敗",
    "脚本退出非零": "腳本退出非零",
    "口径不一致": "口徑不一致",
    "，报送已阻断": "，報送已阻斷",
    "报送已阻断": "報送已阻斷",
    "必须落在": "必須落在",
    " 区间": " 區間",
    " 快照 ": " 快照 ",
    "快照 ": "快照 ",
    " 行": " 行",
    // 建议 reason 模板骨架（p6_policy.handle_suggest 生成的系统 reason，见 reasonText）。
    "高危网格自动建议": "高危網格自動建議",
    "（规则 ": "（規則 ",
    "，样本 ": "，樣本 ",
    "）：": "）：",
    "LTV 上限压至 ": "LTV 上限壓至 ",
    // 动作/行业词条已上提 app.js global 公共层，不再在此重复注册。
    是: "是",
    否: "否",
    "共 ": "共 ",
    " 条审计记录": " 條審計記錄",
    暂无审计记录: "暫無審計記錄",
    当前没有可导出的审计记录: "目前沒有可匯出的審計記錄",
    合规审计_导出记录_: "合規審計_匯出記錄_",
  }, "compliance_audit");

  /* ---------------- 工具函数 ---------------- */

  function resultBadge(result) {
    const r = String(result || "");
    if (r === "success") return '<span class="badge ok">' + t("成功") + "</span>";
    if (r === "failure") return '<span class="badge bad">' + t("失败") + "</span>";
    if (r === "blocked") return '<span class="badge warn">' + t("被阻断") + "</span>";
    return '<span class="badge off">' + esc(r || "-") + "</span>";
  }

  /* ---------------- 页面骨架 ---------------- */

  const HTML = function () {
    return (
      '<h1 class="page-title">' +
      t("合规审计") +
      "</h1>" +
      "<style>" +
      ".audit-detail summary { cursor: pointer; color: var(--muted); list-style: none; }" +
      ".audit-detail summary::-webkit-details-marker { display: none; }" +
      '.audit-detail summary::before { content: "▸ "; color: #94a3b8; }' +
      '.audit-detail[open] summary::before { content: "▾ "; }' +
      ".audit-detail .audit-raw { margin-top: 6px; padding: 8px; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; font-family: monospace; font-size: 11px; word-break: break-all; color: #6b7280; }" +
      "</style>" +
      '<form class="filter-bar" id="p9-export-filter">' +
      '<label data-i18n="日期从">日期从<span class="date-input-wrap"><input type="text" class="date-display" id="p9-exp-from-display" readonly data-i18n-ph="日期从" /><input type="date" id="p9-exp-from" data-i18n-title="日期从" data-i18n-aria="日期从" /></span></label>' +
      '<label data-i18n="日期至">日期至<span class="date-input-wrap"><input type="text" class="date-display" id="p9-exp-to-display" readonly data-i18n-ph="日期至" /><input type="date" id="p9-exp-to" data-i18n-title="日期至" data-i18n-aria="日期至" /></span></label>' +
      "<label>" +
      t("操作类型") +
      '<select id="p9-exp-action"><option value="">' +
      t("全部") +
      "</option>" +
      '<option value="export">' +
      t("导出预警 CSV") +
      "</option>" +
      '<option value="confirm">' +
      t("预警确认") +
      "</option>" +
      '<option value="dispose">' +
      t("预警处置") +
      "</option>" +
      '<option value="datasource_toggle">' +
      t("数据源开关变更") +
      "</option>" +
      '<option value="policy_preview">' +
      t("策略预览") +
      "</option>" +
      '<option value="policy_save">' +
      t("策略保存") +
      "</option>" +
      '<option value="policy_delete">' +
      t("策略删除") +
      "</option>" +
      '<option value="persona_refresh">' +
      t("校准重算") +
      "</option>" +
      '<option value="avm_attr_refresh">' +
      t("归因重建") +
      "</option>" +
      '<option value="report_submit">' +
      t("1104 报送") +
      "</option>" +
      '<option value="report_rebuild">' +
      t("重建报送快照") +
      "</option>" +
      "</select></label>" +
      "<label>" +
      t("操作人") +
      '<input id="p9-exp-user" placeholder="' +
      t("搜索用户名") +
      '" style="width:120px" /></label>' +
      '<button type="submit" class="btn btn-ghost btn-sm">' +
      t("查询") +
      "</button>" +
      '<button type="button" id="p9-export-csv" class="btn btn-ghost btn-sm" style="margin-left:auto">' +
      t("导出 CSV") +
      "</button>" +
      '<span id="p9-exp-total" class="filter-total"></span>' +
      "</form>" +
      '<div class="card" style="margin-top:8px;overflow-x:auto">' +
      '<table class="table" id="p9-export-table"><thead><tr>' +
      "<th>" +
      t("ID") +
      "</th><th>" +
      t("动作") +
      "</th><th>" +
      t("操作人") +
      "</th><th>" +
      t("角色") +
      "</th>" +
      '<th class="num">' +
      t("行数") +
      "</th><th>" +
      t("结果") +
      "</th><th>" +
      t("来源 IP") +
      "</th><th>" +
      t("时间") +
      "</th><th>" +
      t("明细") +
      "</th>" +
      "</tr></thead><tbody></tbody></table></div>"
    );
  };

  /* ---------------- 明细可读化 ---------------- */

  // 审计 detail 的 JSON 键 → 业务语言；未知键原样展示（展示时经 t() 译出）。
  const DETAIL_KEY_LABELS = {
    loan_id: "贷款",
    alert_date: "预警日",
    source: "预警来源",
    status: "状态",
    risk_class: "五级分类",
    ltv_min: "LTV 下限",
    ltv_max: "LTV 上限",
    date_from: "日期从",
    date_to: "日期至",
    rows: "行数",
    policy_id: "规则编号",
    scope: "作用域",
    action: "动作",
    ltv_cap: "LTV 上限",
    enabled: "启用",
    reason: "理由",
    hit_loans: "命中笔数",
    hit_balance: "命中余额",
    newly_hit_loans: "新增受限",
    source_id: "数据源",
    field: "字段",
    value: "值",
    error: "错误",
    denied_role: "越权角色",
    body: "请求体",
    path: "脚本路径",
    summary: "摘要",
    mode: "模式",
    stderr: "错误",
  };
  // dispose 的 status 取值：disposed/recovered 来自 P4 处置，confirmed 来自确认动作留痕。
  const DISPOSE_STATUS = { disposed: "处置中", recovered: "已恢复", confirmed: "已确认" };
  // POLICY_ACTIONS / CITY_CODES / INDUSTRY_CODES 已上提为全局（app.js，含 en/zh-HK
  // 字典与 21 城/15 行业公共术语层），本页直接引用，不再各留一份导致漂移。
  // 预警来源枚举（与 db.ALERT_SOURCES 对齐）：离线 T+1 批跑 / 实时流。
  const ALERT_SOURCE_LABELS = { offline: "离线", stream: "实时" };

  // 报送/重建/归因/校准 类 audit detail（后端写结构化 JSON，kind 区分事件类型）。
  const KIND_LABELS = {
    rebuild: "重建",
    submit: "报送",
    attr_refresh: "归因重建",
    persona_refresh: "校准重算",
  };
  // 后端 result 字段值（"ok" / "bad_date" / "blocked" / ...）→ i18n 展示词。
  // "ok" 不展示（外层 resultBadge 已显示 success）。
  const RESULT_LABELS = {
    bad_date: "日期不合法",
    value_error: "校验失败",
    blocked: "被阻断",
    missing_script: "缺少脚本",
    exec_error: "执行失败",
    script_failed: "脚本退出非零",
  };
  const MODE_LABELS = {
    force: "强制",
    fingerprint: "按指纹",
  };

  // "city:gz" → 「广州（全市）」；"zone:price-hui-114.40-22.74" → 「惠州 · 价格网格 114.40, 22.74」；
  // "industry:agriculture" → 「行业 · 农、林、牧、渔业」。
  function scopeText(raw) {
    const s = String(raw || "");
    const m = /^(city|zone|industry):(.+)$/.exec(s);
    if (!m) return s;
    if (m[1] === "city") return t(CITY_CODES[m[2]] || m[2]) + t("（全市）");
    if (m[1] === "industry") return t("行业") + " · " + t(INDUSTRY_CODES[m[2]] || m[2]);
    const z = /^([a-z]+)(?:-([a-z]+))?-(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$/.exec(m[2]);
    if (!z) return s;
    const grid = `${z[1] === "ltv" ? t("LTV 网格") : t("价格网格")} ${z[3]}, ${z[4]}`;
    return (z[2] ? t(CITY_CODES[z[2]] || z[2]) + " · " : "") + grid;
  }

  // 审计时间：MySQL datetime → 分钟级本地化（en 用 12h 制，中/繁用 24h）。
  // MySQL 出来的字符串无 T/Z，replace 一下让 new Date 按服务器时区（Asia/Shanghai）正确解析。
  function fmtTime(s) {
    if (!s) return "-";
    const d = new Date(String(s).replace(" ", "T"));
    if (isNaN(d.getTime())) return String(s).slice(0, 16);
    const lang = window.__LANG__ || "zh-CN";
    const locale = lang === "en" ? "en-US" : lang === "zh-HK" ? "zh-HK" : "zh-CN";
    return d.toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: lang === "en",
    });
  }

  // 历史 audit detail 的后端拼接中文短语（app.py 改造前留下的留痕，新数据走结构化 JSON，
  // 不再产生这些字面量；保留此表只为把老数据原样翻译给 en/zh-HK 用户看）。
  // 顺序很关键：先匹配长串再匹配短串，避免 "报送 G11" 抢在 "报送 G11 快照" 之前替换。
  const LEGACY_PHRASES = [
    "报送 G11 快照",
    "报送 G11",
    "重建 G11 快照",
    "重建 G11",
    "重建校准报告缺少脚本",
    "重建校准报告执行失败",
    "重建校准报告",
    "校准报告重建",
    "重建归因产物缺少脚本",
    "重建归因产物执行失败",
    "重建归因产物",
    // 带左括号的整段排在裸 "归因产物重建" 之前：原文 "归因产物重建(按指纹)" 括号紧贴
    // 无空格，en 排版需在括号前补一个空格（与同页 "Guangzhou (citywide)" 一致）。
    "归因产物重建(",
    "归因产物重建",
    "被阻断：",
    "被阻断:",
    "执行失败：",
    "执行失败:",
    "缺少脚本",
    "参数不合法",
    "口径不一致",
    // 带全角逗号的整段必须排在裸 "报送已阻断" 之前（长串优先），
    // 否则 "报送已阻断" 先被替换掉，全角逗号就留在 en 文案里了。
    "，报送已阻断",
    "报送已阻断",
    "（按指纹）",
    "（强制）",
    "(按指纹)",
    "(强制)",
    "按指纹",
    "强制",
    "必须落在",
    " 区间",
    " 快照 ",
    "快照 ",
    " 行",
  ];

  // 历史 detail 翻译兜底：把后端写入的中文短语按 i18n 字典替换为当前语言。
  // 仅非 JSON 分支会走这里；新数据写的是结构化 JSON，走通用循环里的 KIND_LABELS。
  function translateLegacy(raw) {
    let s = String(raw);
    for (const k of LEGACY_PHRASES) {
      const replaced = t(k);
      if (replaced !== k) s = s.split(k).join(replaced);
    }
    return s;
  }

  // 系统生成的「建议」reason（p6_policy.handle_suggest 模板，经 P6 策略保存进入审计）：
  //   "高危网格自动建议（规则 {rule}，样本 {n}）：LTV 上限压至 {cap}"
  // 半结构化——固定骨架需翻译，{rule}/{n}/{cap} 是动态值（rule 为内部枚举编码、
  // n/cap 为数字）原样保留，避免把这些不可穷举的内容塞进 t() 字典。
  // 命中则结构化翻译；不中（真正的用户自由录入、或历史其他文案）走 translateLegacy 兜底，
  // 仍不中则原样返回——符合「仅真正用户自由录入文本保留原文」的 i18n 政策。
  const REASON_SUGGEST_RE =
    /^高危网格自动建议（规则 (\w+)，样本 (\d+)）：LTV 上限压至 ([\d.]+)$/;
  function reasonText(v) {
    const s = String(v);
    const m = REASON_SUGGEST_RE.exec(s);
    if (m) {
      return (
        t("高危网格自动建议") +
        t("（规则 ") + m[1] + t("，样本 ") + m[2] + t("）：") +
        t("LTV 上限压至 ") + m[3]
      );
    }
    return translateLegacy(s);
  }

  function detailCell(detail) {
    const base = "max-width:340px;font-size:12px";
    if (!detail) return `<td style='${base};color:var(--muted)'>-</td>`;
    let parsed = null;
    try {
      parsed = JSON.parse(detail);
    } catch (_e) {
      parsed = null; /* 非 JSON（如报送/重建动作的自然语言留痕）直接展示 */
    }
    if (!parsed || typeof parsed !== "object") {
      // 非 JSON 留痕（如脚本 stdout 摘要）：可能带 ==== 横幅 / 日志噪声。
      // 折叠态显示去噪后的首段；展开态保留原文（与 JSON 明细一致）。
      // 历史数据按 LEGACY_PHRASES 做 i18n 兜底翻译（新数据走结构化 JSON 不进此分支）。
      const raw = translateLegacy(String(detail));
      const stripEdge = (s) => s.replace(/^[\s=*#\-_]+|[\s=*#\-_·]+$/g, "");
      const lines = raw.split("\n").map((s) => s.trim());
      const isSep = (s) => s === "" || /^[\s=*#\-_·]+$/.test(s);
      const meaningful = lines.filter((s) => !isSep(s)).map(stripEdge);
      // summary：取首条有意义行，再按 "·" 或 "[" 切首段（去掉尾随 banner / log tag），
      // 最后截到 60 字内——persona_refresh 的"校准报告重建· ===="、avm_attr_refresh 的
      // "归因产物重建(按指纹)· [gen_attribution] ..." 都依赖这一步降噪。
      const first = meaningful.length ? meaningful[0] : lines.find((s) => s) || "-";
      const head = first.split(/[·[]/)[0].trim() || first;
      // 上限 120：en 译文字数比中文长约 1.6 倍，60 会把
      // "Submitted G11 2026-08-19 Blocked: Inconsistent calibration, submission blocked"
      // 这种正常长度的历史留痕切成 "…Inconsistent calibration，…"，反而看不懂。
      // detail 列有 max-width:340px 兜底换行，放宽上限不会撑破表格。
      const summary = head.length > 120 ? head.slice(0, 120) + "…" : head;
      return (
        `<td style='${base}'>` +
        `<details class="audit-detail"><summary>${esc(summary)}</summary>` +
        `<div class="audit-raw">${esc(raw)}</div></details></td>`
      );
    }
    // 折叠态展示自然语言摘要；原始 JSON 进 .audit-raw，仅展开时显示。
    const parts = [];
    // 推演规则对象（policy_preview 的 rule 子对象）→ 自然语言短语。
    if (parsed.rule && typeof parsed.rule === "object") {
      const r = parsed.rule;
      const segs = [];
      if (r.scope_type) segs.push(scopeText(r.scope_type + ":" + r.scope_value));
      if (r.action) segs.push(t(POLICY_ACTIONS[r.action] || r.action));
      if (r.ltv_cap != null) segs.push(t("LTV 上限") + " " + r.ltv_cap);
      if (segs.length) parts.push(t("规则：") + segs.join(" · "));
    }
    // 命中指标：带业务单位的自然语言，余额走 fmtMoney 随语言用 亿/万 或 billion/million。
    if (parsed.hit_loans != null) parts.push(t("命中 {n} 笔", { n: parsed.hit_loans }));
    if (parsed.newly_hit_loans != null) parts.push(t("新增受限 {n} 笔", { n: parsed.newly_hit_loans }));
    if (parsed.hit_balance != null) parts.push(t("命中余额") + " " + fmtMoney(parsed.hit_balance));
    // 其余通用键值（已知键已上移，避免与上面重复）；请求体冗长只进原始记录。
    // 顶层 ltv_cap/ltv_min/ltv_max 通常是后端把 rule 子对象拍平的结果，跟用户填的
    // reason 文本（"LTV 上限压至 0.70"）会在同一行 summary 里同时出现，en 模式
    // 下变成"LTV Cap 0.7 · ... · Reason ... LTV 上限 0.70"中英混杂。policy_id/scope/action
    // 三要素已表达足够信息，故顶层 ltv_* 一律不让它再独立 push。
    const handled = new Set(["rule", "ltv_cap", "ltv_min", "ltv_max", "hit_loans", "newly_hit_loans", "hit_balance", "hyp_text", "body"]);
    Object.entries(parsed).forEach(([k, v]) => {
      if (handled.has(k) || v == null || v === "" || typeof v === "object") return;
      const label = t(DETAIL_KEY_LABELS[k] || k);
      let text;
      if (k === "enabled") text = v === 1 || v === true || v === "1" ? t("启用") : t("停用");
      else if (k === "status") text = t(DISPOSE_STATUS[v] || String(v));
      else if (k === "source") text = t(ALERT_SOURCE_LABELS[v] || String(v));
      // error 是后端抛的异常文案（如 p6_policy 的 "ltv_cap 必须落在 [0.01, 2] 区间"），
      // 中文硬编码在后端、无法 t() 命中字典，用历史短语表兜底翻译。
      else if (k === "error") text = translateLegacy(String(v));
      // reason 有三种来源：① p6 策略保存时用户自由录入的「理由」文本（命不中字典、应原样
      // 保留）；② P6 高危网格建议生成的半结构化系统文案（"高危网格自动建议（规则 …）：LTV
      // 上限压至 …"），由 reasonText 结构化译出；③ 其他后端系统文案（如 "口径不一致，
      // 报送已阻断"、异常 str(exc)），reasonText 不中则走 translateLegacy 兜底。
      else if (k === "reason") text = reasonText(String(v));
      else if (k === "action") text = t(POLICY_ACTIONS[v] || String(v));
      else if (k === "scope") text = scopeText(v);
      else if (k === "kind") text = t(KIND_LABELS[v] || String(v));
      else if (k === "result" && v !== "ok") text = t(RESULT_LABELS[v] || String(v));
      else if (k === "mode") text = t(MODE_LABELS[v] || String(v));
      else if (k === "snapshot") return; // 仅作标记，无展示价值
      else if (typeof v === "boolean") text = v ? t("是") : t("否");
      else text = t(String(v));
      // enabled 已是自解释词（启用/停用 / Enabled/Disabled），拼上 label 会重复
      // "启用 启用" / "Enabled Enabled"，单独处理；其他键保留 "label value" 形式。
      // result === "ok" 也不展示（外层 resultBadge 已显示 success 状态）。
      if (!text) return;
      parts.push(k === "enabled" ? text : `${label} ${text}`);
    });
    const summary = parts.length ? parts.join(" · ") : "-";
    return (
      `<td style='${base}'>` +
      `<details class="audit-detail"><summary>${esc(summary)}</summary>` +
      `<div class="audit-raw">${esc(detail)}</div></details></td>`
    );
  }

  /* ---------------- 导出审计 Tab ---------------- */

  function buildExportQuery() {
    const q = new URLSearchParams();
    const v = (id) => document.getElementById(id).value;
    if (v("p9-exp-from")) q.set("date_from", v("p9-exp-from"));
    if (v("p9-exp-to")) q.set("date_to", v("p9-exp-to"));
    if (v("p9-exp-action")) q.set("action", v("p9-exp-action"));
    if (v("p9-exp-user")) q.set("user", v("p9-exp-user"));
    return q.toString();
  }

  async function renderExportAudit() {
    const data = await api("/api/compliance_audit?" + buildExportQuery());
    document.getElementById("p9-exp-total").textContent =
      t("共 ") + data.export_total + t(" 条审计记录");

    const tbody = document.querySelector("#p9-export-table tbody");
    if (!data.export_rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty">' + t("暂无审计记录") + "</td></tr>";
      return;
    }
    tbody.innerHTML = data.export_rows
      .map(
        (r) =>
          "<tr>" +
          "<td>" +
          r.id +
          "</td>" +
          "<td>" +
          esc(t(r.action_label)) +
          "</td>" +
          "<td>" +
          esc(r.username) +
          "</td>" +
          "<td>" +
          esc(r.role) +
          "</td>" +
          '<td class="num">' +
          (r.rows == null ? "-" : r.rows) +
          "</td>" +
          "<td>" +
          resultBadge(r.result) +
          "</td>" +
          "<td style='font-family:monospace;font-size:12px'>" +
          esc(r.ip || "-") +
          "</td>" +
          "<td style='font-size:12px;white-space:nowrap'>" +
          esc(fmtTime(r.created_at)) +
          "</td>" +
          detailCell(r.detail) +
          "</tr>"
      )
      .join("");
  }

  /* ---------------- 导出 CSV ---------------- */

  function downloadExportCSV() {
    const tbody = document.querySelectorAll("#p9-export-table tbody tr");
    if (!tbody.length || tbody[0].querySelector(".empty")) {
      alert(t("当前没有可导出的审计记录"));
      return;
    }
    const rows = [];
    // 表头
    const headers = [
      t("ID"),
      t("动作"),
      t("操作人"),
      t("角色"),
      t("行数"),
      t("结果"),
      t("来源IP"),
      t("时间"),
      t("明细"),
    ];
    rows.push(headers.join(","));

    document.querySelectorAll("#p9-export-table tbody tr").forEach((tr) => {
      const tds = tr.querySelectorAll("td");
      if (tds.length === 1) return; // 空态行跳过
      const vals = Array.from(tds).map((td) => {
        const clone = td.cloneNode(true);
        clone.querySelectorAll(".audit-raw").forEach((n) => n.remove());
        const text = clone.textContent.trim().replace(/,/g, "，").replace(/"/g, '""');
        return '"' + text + '"';
      });
      rows.push(vals.join(","));
    });

    const csv = "\ufeff" + rows.join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = t("合规审计_导出记录_") + new Date().toISOString().slice(0, 10) + ".csv";
    a.click();
    URL.revokeObjectURL(a);
  }

  /* ---------------- 主渲染 ---------------- */

  async function loadExportTab() {
    await renderExportAudit();
  }

  async function render(_root) {
    await loadExportTab();
  }

  /* ---------------- 事件绑定 ---------------- */

  function bind(root) {
    if (root.dataset.p9Bound) return;
    root.dataset.p9Bound = "1";

    // 导出审计筛选
    root.querySelector("#p9-export-filter").addEventListener("submit", (e) => {
      e.preventDefault();
      loadExportTab();
    });

    // 导出 CSV
    root.querySelector("#p9-export-csv").addEventListener("click", downloadExportCSV);
  }

  /* 切语言时随当前语言重渲染导出表：审计明细列是动态生成（非 data-i18n 静态文案），
   * 仅凭全局 __onLangChange 的插件重挂载在部分路径下不触发本页 render，导致明细冻结在
   * 渲染时的语言。这里显式挂一层——只要导出表在 DOM 中就重拉，保证明细随语言即时刷新。 */
  const _origOnLangChange = window.__onLangChange;
  window.__onLangChange = async (code) => {
    if (typeof _origOnLangChange === "function") {
      try {
        await _origOnLangChange(code);
      } catch (e) {
        console.error("[p9] 语言切换原钩子异常:", e);
      }
    }
    const page = document.getElementById("page-compliance_audit");
    const visible = page && (!page.classList || typeof page.classList.contains !== "function" || !page.classList.contains("hidden"));
    if (visible) loadExportTab();
  };

  window.registerPage("compliance_audit", {
    html: HTML,
    render: async (root) => {
      bind(root);
      await render(root);
    },
  });
})();
