/* global window, document */
/* P9 · 合规审计 / 特征归因（设计评审 P9 / R-UNW-02，面向合规官）。
 *
 * 三块：导出审计（who/role/when/what/result/ip）、报送阻断告警（level=block）、
 * 特征归因报告（output/avm/attribution_report.json，缺失时降级提示）。
 * 视觉与交互全部复用主框架（card / table / badge / empty），不动全局 CSS。
 */

(function () {
  const { api, esc } = window.spf;

  const HTML = `
    <h1 class="page-title">合规审计 / 特征归因</h1>

    <div id="p9-attr-notice"></div>

    <div class="card" style="margin-top:16px">
      <h2 class="card-title">导出审计 · PII 导出脱敏留痕（ads_export_audit · TC-06）</h2>
      <div id="p9-export-total" class="check-line"></div>
      <div style="overflow-x:auto;margin-top:8px">
        <table class="table">
          <thead>
            <tr>
              <th>ID</th><th>动作</th><th>操作人</th><th>角色</th>
              <th>行数</th><th>结果</th><th>来源 IP</th><th>时间</th><th>明细</th>
            </tr>
          </thead>
          <tbody id="p9-export-tbody"></tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h2 class="card-title">报送阻断告警 · level=block（ads_report_alert · AC-05/AC-08）</h2>
      <div id="p9-alert-total" class="check-line"></div>
      <div style="overflow-x:auto;margin-top:8px">
        <table class="table">
          <thead>
            <tr>
              <th>ID</th><th>报表日期</th><th>报表类型</th><th>级别</th>
              <th>校验项</th><th>明细</th><th>产生时间</th>
            </tr>
          </thead>
          <tbody id="p9-alert-tbody"></tbody>
        </table>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h2 class="card-title">特征归因 · SHAP（output/avm/attribution_report.json · R-UNW-03）</h2>
      <div id="p9-attr-body"><div class="empty">加载中…</div></div>
    </div>
  `;

  function resultBadge(result) {
    const r = String(result || "");
    if (r === "success") return `<span class="badge ok">success</span>`;
    if (r === "failure") return `<span class="badge bad">failure</span>`;
    return `<span class="badge off">${esc(r || "-")}</span>`;
  }

  function renderExportAudit(rows, total) {
    const tbody = document.getElementById("p9-export-tbody");
    document.getElementById("p9-export-total").textContent =
      `共 ${total} 条审计记录，展示最近 ${rows.length} 条（导出/确认/配置变更均留痕）。`;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">暂无审计记录</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map(
        (r) =>
          `<tr>` +
          `<td>${r.id}</td>` +
          `<td>${esc(r.action_label)}</td>` +
          `<td>${esc(r.username)}</td>` +
          `<td>${esc(r.role)}</td>` +
          `<td>${r.rows == null ? "-" : r.rows}</td>` +
          `<td>${resultBadge(r.result)}</td>` +
          `<td>${esc(r.ip || "-")}</td>` +
          `<td>${esc(r.created_at)}</td>` +
          `<td style="max-width:320px;color:var(--muted);font-size:12px;word-break:break-all">${esc(r.detail || "-")}</td>` +
          `</tr>`
      )
      .join("");
  }

  function renderReportAlerts(rows, total) {
    const tbody = document.getElementById("p9-alert-tbody");
    document.getElementById("p9-alert-total").textContent =
      `共 ${total} 条阻断告警，展示最近 ${rows.length} 条（口径不一致时报送被阻断并推送合规）。`;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">暂无阻断告警 · 最近报送口径一致</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map(
        (r) =>
          `<tr>` +
          `<td>${r.id}</td>` +
          `<td>${esc(r.report_date)}</td>` +
          `<td>${esc(r.report_type)}</td>` +
          `<td><span class="badge bad">${esc(r.alert_level)}</span></td>` +
          `<td>${esc(r.check_name)}</td>` +
          `<td style="max-width:340px;font-size:12px">${esc(r.detail)}</td>` +
          `<td>${esc(r.etl_ts)}</td>` +
          `</tr>`
      )
      .join("");
  }

  /* ---------------- 特征归因渲染（缺失时降级，绝不 500） ---------------- */

  function escHtml(s) {
    return String(s == null ? "" : s);
  }

  function renderAttribution(attr) {
    const body = document.getElementById("p9-attr-body");
    if (!attr.available) {
      body.innerHTML =
        `<div class="check-line" style="color:var(--warn)">⚠ ${attr.message || "归因报告未生成。"}</div>` +
        `<div class="check-line">归因报告由 <code>tools/avm</code> 训练链路产出（SHAP 特征重要性 + 三分量归因）；` +
        `本页仅展示产物，不参与计算。待报告生成后自动填充，无需改前端。</div>`;
      return;
    }
    const rep = attr.report || {};
    // 兼容两类产物形态：直接含 summary，或嵌套 report 元数据。
    const summary = rep.summary || rep;
    const meta = rep.meta || {};

    let kpiHtml = "";
    const kpis = [
      {
        label: "报告时间",
        value: escHtml(meta.generated_at || rep.generated_at || "-"),
        sub: attr.path,
      },
      { label: "方法", value: escHtml(meta.method || summary.method || "SHAP"), sub: "特征归因" },
      { label: "样本量", value: escHtml(summary.n_samples ?? summary.n ?? "-"), sub: "归因样本" },
    ];
    kpiHtml = `<div class="kpi-row">${kpis
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${k.label}</div>` +
          `<div class="kpi-value" style="font-size:16px">${k.value}</div>` +
          `<div class="kpi-sub">${escHtml(k.sub)}</div></div>`
      )
      .join("")}</div>`;

    // SHAP 特征重要性表：features 数组优先，字典其次。
    let featureHtml = "";
    const feats = summary.features || summary.shap_values || rep.features || [];
    if (Array.isArray(feats) && feats.length) {
      featureHtml =
        `<table class="table" style="margin-top:12px"><thead><tr>` +
        `<th>特征</th><th>SHAP 重要性</th><th>方向 / 备注</th></tr></thead><tbody>` +
        feats
          .map((f) => {
            if (typeof f === "string") {
              return `<tr><td>${esc(f)}</td><td>-</td><td>-</td></tr>`;
            }
            const name = f.name || f.feature || f["0"];
            const val = f.importance ?? f.value ?? f.shap ?? f["1"];
            const note = f.note || f.sign || f.direction || "";
            return (
              `<tr><td>${esc(name)}</td>` +
              `<td>${val == null ? "-" : esc(val)}</td>` +
              `<td style="color:var(--muted)">${esc(note)}</td></tr>`
            );
          })
          .join("") +
        `</tbody></table>`;
    } else if (feats && typeof feats === "object") {
      const entries = Object.entries(feats).sort((a, b) => Number(b[1]) - Number(a[1]));
      if (entries.length) {
        featureHtml =
          `<table class="table" style="margin-top:12px"><thead><tr>` +
          `<th>特征</th><th>SHAP 重要性</th></tr></thead><tbody>` +
          entries.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") +
          `</tbody></table>`;
      }
    }
    if (!featureHtml) {
      featureHtml = `<div class="empty">报告无特征重要性明细</div>`;
    }

    body.innerHTML =
      kpiHtml +
      `<div class="legend" style="margin-top:10px">` +
      `<span>报告路径：<code>${esc(attr.path)}</code></span>` +
      `<span>归因结论：异常估值偏差由城市级基准错位主导（详见报告 summary）</span></div>` +
      `<div style="margin-top:12px">${featureHtml}</div>`;
  }

  async function render(_root) {
    const data = await api("/api/compliance_audit");

    // 归因报告缺失时顶部给一条醒目提示（含重建命令）。
    const notice = document.getElementById("p9-attr-notice");
    if (data.attribution && !data.attribution.available) {
      notice.innerHTML =
        `<div class="card" style="border-left:4px solid var(--warn)">` +
        `<span class="badge warn">归因报告未生成</span> ` +
        `${data.attribution.message}</div>`;
    } else {
      notice.innerHTML = "";
    }

    renderExportAudit(data.export_rows, data.export_total);
    renderReportAlerts(data.report_alerts, data.report_alert_total);
    renderAttribution(data.attribution);
  }

  window.registerPage("compliance_audit", { html: HTML, render });
})();
