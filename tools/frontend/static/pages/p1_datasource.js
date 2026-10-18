/* P1 · 数据底座接入配置（设计评审 R-EVT-01 / 阶段2 L0，面向数据工程师）。
 *
 * 页面回答两件事：接了哪些源（登记与管理）、CDC 消费是否滞后（KPI 指标）。
 * 视觉与主框架一致：只用 style.css 已有的 .card/.table/.badge/.kpi 等类，
 * 本页特有的（分级徽章配色）走内联样式——插件页不改全局 css，
 * 否则多页并行开发会互相覆盖。
 */

(function () {
  const { api, esc } = window.spf;

  /* 页面级多语言字典（key = zh-CN 原文，译文只存此处）。
   * 数据键（source_type / data_level 等）保持中文原文用于匹配后端，仅展示层经 t() 翻译。 */
  registerI18n("en", {
    刷新: "Refresh",
    数据源登记: "Datasource Registry",
    数据库时间: "DB time",
    已登记数据源: "Registered Sources",
    "启用 CDC": "CDC Enabled",
    "PII / 敏感源": "PII / Sensitive Sources",
    消费滞后: "Consumption Lag",
    // ---- API 错误句整句注册（app.js api() 对 e.message 统一 t() 查表）----
    "field 只支持 cdc_enabled / enabled": "field must be cdc_enabled or enabled",
    "value 只支持 0 / 1": "value must be 0 or 1",
    "缺少 source_id": "source_id is required",
    "数据源不存在：{source_id}": "Data source not found: {source_id}",
    "{n} 条待消费": "{n} {n|record|records} pending",
    "实时 · 0 条积压": "Real-time · 0 backlog",
    开启: "On",
    // 开关的「关闭」（Off）与 p6 弹窗按钮的「关闭」（Close）同 key 异义，不上提 global。
    关闭: "Off",
    类型: "Type",
    分类分级: "Class / Level",
    "接入方式 / 说明": "Connection / Note",
    "落地表 / 管控单元": "Landing Table / Control Unit",
    负责人: "Owner",
    登记状态: "Registration Status",
    更新时间: "Updated At",
    "PII / 敏感级的数据导出必须脱敏，导出动作全程留痕可审计":
      "Exports of PII / sensitive-tier data must be masked; all export actions are fully audited",
    "分级口径，敏感度从低到高：": "Tiering — sensitivity from low to high:",
    "PII / 敏感级导出须脱敏 · 全程留痕":
      "PII / sensitive-tier exports must be masked · fully audited",
    当前角色只读: "read-only for the current role",
    业务库: "Business DB",
    爬虫: "Crawler",
    空间API: "Spatial API",
    日志: "Log",
    公开: "Public",
    内部: "Internal",
    敏感: "Sensitive",
    // 数据源登记表 · 各源的名称 / 接入方式 / 备注 / 管控单元 / 负责人（产品自带文案，随语言切换）
    "信贷业务源库 spacefin": "spacefin Core Lending DB",
    "MySQL binlog 变更捕获": "MySQL binlog Change Capture",
    "Kafka 事件总线 spacefin.cdc.log": "Kafka Event Bus spacefin.cdc.log",
    "安居客二手房挂牌采集（广东 21 城）": "Anjuke Second-hand Listing Crawler (21 cities, Guangdong)",
    "安居客租房挂牌采集": "Anjuke Rental Listing Crawler",
    "腾讯位置服务 · 地理编码": "Tencent Location Service · Geocoding",
    "直连信贷业务主库，覆盖客户/押品/贷款三张业务表":
      "Direct connection to the core lending DB, covering the customer / collateral / loan tables",
    "订阅业务主库 binlog，实时捕获行级变更":
      "Subscribes to the core DB binlog, capturing row-level changes in real time",
    "CDC 事件经 Kafka 事件总线对外分发": "CDC events distributed outward via the Kafka event bus",
    "分布式批量采集：调度中心 + Redis 队列 + 代理池":
      "Distributed batch crawl: scheduler + Redis queue + proxy pool",
    "宿主浏览器渲染采集，经代理池出网": "Collected via host-browser rendering and egress through a proxy pool",
    "腾讯位置服务地理编码开放接口": "Tencent Location Service geocoding open API",
    "客户征信分/月收入/负债率属个人金融信息，仅供风险计算，导出须脱敏":
      "Credit score / monthly income / debt ratio are personal financial data, used only for risk computation; exports must be masked",
    "变更前后的完整记录原样留存，内容等同源库，按源库同级管控":
      "Full before/after records are kept verbatim; content equals the source DB and is governed at the same tier",
    "消费进度实时对账，至少一次送达，事件不丢失":
      "Consumption progress is reconciled in real time; at-least-once delivery, no event loss",
    "公开挂牌信息，同一房源跨日自动去重；批量采集不适用实时链路":
      "Public listing data, auto-deduped per property across days; batch crawl not on the real-time path",
    "页面需真实浏览器渲染；容器内渲染已被对方反爬识别，改走宿主":
      "Page needs real browser rendering; in-container rendering was detected by the target's anti-bot, so it runs on the host",
    "调用配额有限，结果本地落库复用，避免重复消耗配额":
      "Limited API quota; results are stored locally and reused to avoid re-consuming quota",
    "业务主库（Docker 部署）": "Core business DB (Docker deployment)",
    "CDC 实时采集服务": "CDC real-time capture service",
    "流式事件生产服务": "Streaming event producer service",
    "每日 00:30 定时采集调度": "Scheduled daily crawl at 00:30",
    "宿主浏览器渲染服务": "Host-browser rendering service",
    "ETL 收尾异步回填任务": "ETL tail async backfill job",
    "数据工程": "Data Engineering",
    "算法/数据工程": "Algo / Data Engineering",
  }, "datasource");
  registerI18n("zh-HK", {
    刷新: "重新整理",
    数据源登记: "資料源登記",
    数据库时间: "數據庫時間",
    已登记数据源: "已登記資料源",
    "启用 CDC": "啟用 CDC",
    "PII / 敏感源": "PII / 敏感源",
    消费滞后: "消費滯後",
    // ---- API 错误句整句注册（与 en 块一一对应）----
    "field 只支持 cdc_enabled / enabled": "field 只支援 cdc_enabled / enabled",
    "value 只支持 0 / 1": "value 只支援 0 / 1",
    "缺少 source_id": "缺少 source_id",
    "数据源不存在：{source_id}": "數據源不存在：{source_id}",
    "{n} 条待消费": "{n} 條待消費",
    "实时 · 0 条积压": "即時 · 0 條積壓",
    开启: "開啟",
    关闭: "關閉",
    类型: "類型",
    分类分级: "分類分級",
    "接入方式 / 说明": "接入方式 / 說明",
    "落地表 / 管控单元": "落地表 / 管控單元",
    负责人: "負責人",
    登记状态: "登記狀態",
    更新时间: "更新時間",
    "PII / 敏感级的数据导出必须脱敏，导出动作全程留痕可审计":
      "PII / 敏感級資料匯出必須脫敏，匯出動作全程留痕可審計",
    "分级口径，敏感度从低到高：": "分級口徑，敏感度從低到高：",
    "PII / 敏感级导出须脱敏 · 全程留痕": "PII / 敏感級匯出須脫敏 · 全程留痕",
    当前角色只读: "目前角色唯讀",
    业务库: "業務庫",
    爬虫: "爬蟲",
    空间API: "空間API",
    日志: "日誌",
    公开: "公開",
    内部: "內部",
    敏感: "敏感",
    // 数据源登记表 · 各源的名称 / 接入方式 / 备注 / 管控单元 / 负责人（产品自带文案，随语言切换）
    "信贷业务源库 spacefin": "信貸業務源庫 spacefin",
    "MySQL binlog 变更捕获": "MySQL binlog 變更捕獲",
    "Kafka 事件总线 spacefin.cdc.log": "Kafka 事件匯流排 spacefin.cdc.log",
    "安居客二手房挂牌采集（广东 21 城）": "安居客二手屋牌價採集（廣東 21 城）",
    "安居客租房挂牌采集": "安居客租房牌價採集",
    "腾讯位置服务 · 地理编码": "騰訊位置服務 · 地理編碼",
    "直连信贷业务主库，覆盖客户/押品/贷款三张业务表":
      "直連信貸業務主庫，涵蓋客戶/押品/貸款三張業務表",
    "订阅业务主库 binlog，实时捕获行级变更": "訂閱業務主庫 binlog，即時捕獲列級變更",
    "CDC 事件经 Kafka 事件总线对外分发": "CDC 事件經 Kafka 事件匯流排對外分發",
    "分布式批量采集：调度中心 + Redis 队列 + 代理池":
      "分散式批次採集：排程中心 + Redis 隊列 + 代理池",
    "宿主浏览器渲染采集，经代理池出网": "宿主瀏覽器渲染採集，經代理池出網",
    "腾讯位置服务地理编码开放接口": "騰訊位置服務地理編碼開放介面",
    "客户征信分/月收入/负债率属个人金融信息，仅供风险计算，导出须脱敏":
      "客戶徵信分/月收入/負債率屬個人金融資訊，僅供風險計算，匯出須脫敏",
    "变更前后的完整记录原样留存，内容等同源库，按源库同级管控":
      "變更前後的完整記錄原樣留存，內容等同源庫，依源庫同級管控",
    "消费进度实时对账，至少一次送达，事件不丢失": "消費進度即時對帳，至少一次送達，事件不遺失",
    "公开挂牌信息，同一房源跨日自动去重；批量采集不适用实时链路":
      "公開牌價資訊，同一房源跨日自動去重；批次採集不適用即時鏈路",
    "页面需真实浏览器渲染；容器内渲染已被对方反爬识别，改走宿主":
      "頁面需真實瀏覽器渲染；容器內渲染已被對方反爬識別，改走宿主",
    "调用配额有限，结果本地落库复用，避免重复消耗配额":
      "呼叫配額有限，結果本地落庫複用，避免重複消耗配額",
    "业务主库（Docker 部署）": "業務主庫（Docker 部署）",
    "CDC 实时采集服务": "CDC 即時採集服務",
    "流式事件生产服务": "串流事件生產服務",
    "每日 00:30 定时采集调度": "每日 00:30 定時採集排程",
    "宿主浏览器渲染服务": "宿主瀏覽器渲染服務",
    "ETL 收尾异步回填任务": "ETL 收尾非同步回填任務",
    "数据工程": "資料工程",
    "算法/数据工程": "演算法/資料工程",
  }, "datasource");

  /* 分级标签配色：敏感度递增。PII 额外加边框描边，让它在一屏表格里第一眼就跳出来
   * （R-UNW-02 的意义就在于「谁都不能假装没看见」）。 */
  const LEVEL_STYLE = {
    公开: "background:#dcfce7;color:#15803d",
    内部: "background:#dbeafe;color:#1d4ed8",
    敏感: "background:#fef3c7;color:#b45309",
    PII: "background:#fee2e2;color:#b91c1c;box-shadow:inset 0 0 0 1px #b91c1c",
  };

  /* 消费滞后的量纲是「条」不是「秒」：lag = ODS 最大 id − 消费者 last_id
   * （见 pages/p1_datasource.py::_cdc_state，用 id 差而非时间差，避免同秒多事件漏算）。
   * 0 = 下游全部追平。 */
  function fmtLag(n) {
    const v = Number(n || 0);
    return v > 0 ? t("{n} 条待消费", { n: v }) : t("实时 · 0 条积压");
  }

  /* ---------------- 数据源登记表 ---------------- */

  function toggleBtn(row, field, on, canEdit) {
    const label = on ? t("开启") : t("关闭");
    if (!canEdit) return `<span class="badge ${on ? "ok" : "off"}">${label}</span>`;
    return (
      `<button class="btn btn-sm ${on ? "" : "btn-ghost"}" data-toggle="${esc(row.source_id)}" ` +
      `data-field="${field}" data-next="${on ? 0 : 1}">${label}</button>`
    );
  }

  function renderRegistry(el, data) {
    const rows = data.sources
      .map((s) => {
        const lv = LEVEL_STYLE[s.data_level] || LEVEL_STYLE["内部"];
        return (
          `<tr><td><div style="font-family:ui-monospace,Menlo,monospace;font-size:12px">${esc(
            s.source_id
          )}</div><div>${esc(t(s.name))}</div></td>` +
          `<td>${esc(t(s.source_type))}</td>` +
          `<td><span class="badge" style="${lv}">${esc(t(s.data_level))}</span></td>` +
          `<td style="max-width:280px"><div style="font-size:13px">${esc(
            t(s.endpoint)
          )}</div><div style="font-size:12px;color:#6b7280;word-break:break-all">${esc(t(s.remark || ""))}</div></td>` +
          `<td style="font-size:12px">${esc(s.target_table || "-")}<div style="color:#6b7280">${esc(
            t(s.service_unit || "-")
          )}</div></td>` +
          `<td>${esc(t(s.owner))}</td>` +
          `<td>${toggleBtn(s, "cdc_enabled", s.cdc_enabled, data.can_edit)}</td>` +
          `<td>${toggleBtn(s, "enabled", s.enabled, data.can_edit)}</td>` +
          `<td style="font-size:12px;color:#6b7280">${esc(s.updated_at)}</td></tr>`
        );
      })
      .join("");
    el.innerHTML =
      `<table class="table"><thead><tr>` +
      `<th>${t("数据源")}</th><th>${t("类型")}</th><th>${t("分类分级")}</th><th>${t(
        "接入方式 / 说明"
      )}</th><th>${t("落地表 / 管控单元")}</th>` +
      `<th>${t("负责人")}</th><th>CDC</th><th>${t("登记状态")}</th><th>${t("更新时间")}</th>` +
      `</tr></thead><tbody>${rows}</tbody></table>` +
      `<div class="check-line" title="${t(
        "PII / 敏感级的数据导出必须脱敏，导出动作全程留痕可审计"
      )}">${t("分级口径，敏感度从低到高：")}` +
      data.levels
        .map(
          (l) =>
            `<span class="badge" style="${LEVEL_STYLE[l]};margin-right:6px">${esc(t(l))}</span>`
        )
        .join("") +
      t("PII / 敏感级导出须脱敏 · 全程留痕") +
      (data.can_edit ? "" : " · " + t("当前角色只读")) +
      `</div>`;
  }

  /* ---------------- 页面装配 ---------------- */

  const HTML = function () {
    return (
      `<h1 class="page-title">` +
      t("数据底座接入配置") +
      `<button class="btn btn-ghost btn-sm" id="ds-refresh" style="margin-left:12px">${t(
        "刷新"
      )}</button>` +
      `<span id="ds-now" style="font-size:12px;color:#6b7280;margin-left:8px"></span></h1>` +
      `<div class="kpi-row" id="ds-kpi"></div>` +
      `<div class="card" style="margin-top:16px"><div class="card-title">${t(
        "数据源登记"
      )}</div><div id="ds-registry"></div></div>`
    );
  };

  async function render(_el) {
    const data = await api("/api/datasource");

    document.getElementById("ds-now").textContent = t("数据库时间") + " " + data.server_now;

    const kpis = [
      { label: t("已登记数据源"), value: data.sources.length },
      { label: t("启用 CDC"), value: data.sources.filter((s) => s.cdc_enabled).length },
      {
        label: t("PII / 敏感源"),
        value: data.sources.filter((s) => s.data_level === "PII" || s.data_level === "敏感").length,
      },
      { label: t("消费滞后"), value: fmtLag(data.cdc.verdict.max_lag) },
    ];
    document.getElementById("ds-kpi").innerHTML = kpis
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${esc(k.label)}</div>` +
          `<div class="kpi-value">${esc(k.value)}</div></div>`
      )
      .join("");

    renderRegistry(document.getElementById("ds-registry"), data);
  }

  /* 事件用委托绑在 section 上：render 每次都会重建内部 DOM，
   * 逐个按钮绑监听会在多次切页后累积重复回调。 */
  function bind(el) {
    if (el.dataset.bound) return;
    el.dataset.bound = "1";
    el.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-toggle]");
      if (btn) {
        btn.disabled = true;
        try {
          await api("/api/datasource/toggle", {
            method: "POST",
            body: JSON.stringify({
              source_id: btn.dataset.toggle,
              field: btn.dataset.field,
              value: Number(btn.dataset.next),
            }),
          });
          await render(el);
        } catch (e) {
          // api() 抛出的 message 已按当前语言翻译（错误句收口见 app.js api()）
          showToast(String((e && e.message) || e), false);
        } finally {
          btn.disabled = false;
        }
        return;
      }
      if (ev.target.id === "ds-refresh") render(el);
    });
  }

  window.registerPage("datasource", {
    html: HTML,
    render: async (el) => {
      bind(el);
      await render(el);
    },
  });
})();
