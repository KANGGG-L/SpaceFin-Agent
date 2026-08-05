/* global document, fetch, alert, URLSearchParams, window, console */
/* S5 前端驾驶舱 · 单页交互逻辑（vanilla JS，无框架无 CDN）。
 * RBAC 由服务端强制校验，这里仅根据 /api/me 返回的 pages/can_confirm/can_export
 * 裁剪导航与操作按钮（第二层防御，主要是 UX 层面）。 */

const state = { user: null, currentPage: "dashboard", alertsPage: 1 };

const CLASS_COLORS = {
  正常: "#2e9e5b",
  关注: "#d9a520",
  次级: "#e07b1f",
  可疑: "#d14a3a",
  损失: "#b02a2a",
};

/* ---------------- 工具函数 ---------------- */

function fmtMoney(v) {
  if (v == null) return "-";
  const n = Number(v);
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + " 万";
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

async function api(path, opts = {}) {
  const init = {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  };
  const res = await fetch(path, init);
  if (res.status === 401) {
    state.user = null;
    showLogin();
    throw new Error("unauthorized");
  }
  if (res.status === 403) {
    alert("当前角色无此操作权限");
    throw new Error("forbidden");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ---------------- SVG 柱状图 ---------------- */

function barChart(container, data, opts) {
  const { value, label, sub, format, color } = opts;
  const height = opts.height || 230;
  const width = container.clientWidth || 640;
  const padT = 26;
  const padB = 52;
  const plotH = height - padT - padB;
  const plotW = width - 8;
  const max = Math.max(1, ...data.map((d) => Number(d[value]) || 0));
  const slot = plotW / data.length;
  const barW = Math.max(8, Math.min(46, slot * 0.56));
  const fmt = format || ((x) => x);

  let svg = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">`;
  for (let i = 0; i <= 4; i++) {
    const y = padT + plotH - (plotH * i) / 4;
    svg += `<line x1="4" y1="${y}" x2="${width - 4}" y2="${y}" stroke="#eef2f7"/>`;
  }
  data.forEach((d, i) => {
    const v = Number(d[value]) || 0;
    const h = (v / max) * plotH;
    const x = 4 + slot * i + (slot - barW) / 2;
    const y = padT + plotH - h;
    const fill = color ? color(d) : "#2563eb";
    svg += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="3" fill="${fill}"/>`;
    svg += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle" font-size="11" fill="#374151">${fmt(v)}</text>`;
    const lx = 4 + slot * i + slot / 2;
    svg += `<text x="${lx.toFixed(1)}" y="${height - 28}" text-anchor="middle" font-size="11" fill="#6b7280">${esc(d[label])}</text>`;
    if (sub != null) {
      svg += `<text x="${lx.toFixed(1)}" y="${height - 10}" text-anchor="middle" font-size="10" fill="#9ca3af">${esc(d[sub])}</text>`;
    }
  });
  svg += "</svg>";
  container.innerHTML = svg;
}

function hbarList(container, items, opts) {
  const { value, label, format, color } = opts;
  const max = Math.max(1, ...items.map((d) => Number(d[value]) || 0));
  const fmt = format || ((d) => d[value]);
  let html = "";
  for (const d of items) {
    const w = ((Number(d[value]) || 0) / max) * 100;
    html +=
      `<div class="hbar-row">` +
      `<div class="hbar-label">${esc(d[label])}</div>` +
      `<div class="hbar-track"><div class="hbar-fill ${color ? color(d) : ""}" style="width:${w.toFixed(1)}%"></div></div>` +
      `<div class="hbar-num">${esc(fmt(d))}</div>` +
      `</div>`;
  }
  container.innerHTML = html;
}

/* ---------------- 登录 / 会话 ---------------- */

function showLogin() {
  document.getElementById("login-view").classList.remove("hidden");
  document.getElementById("app-view").classList.add("hidden");
}

async function showApp() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("app-view").classList.remove("hidden");
  document.getElementById("user-label").textContent = state.user.user;
  document.getElementById("user-role").textContent = state.user.role_label;
  // 插件页必须先加载并建好 section，renderNav/showPage 才能正确切换。
  await loadPlugins(state.user.pages || []);
  renderNav();
  const visible = (state.user.pages || []).map((p) => p.id);
  if (!visible.includes(state.currentPage)) state.currentPage = visible[0] || "dashboard";
  showPage(state.currentPage);
}

function renderNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = state.user.pages
    .map(
      (p) =>
        `<a data-page="${p.id}" class="${p.id === state.currentPage ? "active" : ""}">${esc(p.label)}</a>`
    )
    .join("");
  nav.querySelectorAll("a").forEach((a) => {
    a.addEventListener("click", () => showPage(a.dataset.page));
  });
}

/* ---------------- 页面路由 ---------------- */

function showPage(id) {
  state.currentPage = id;
  document.querySelectorAll(".page").forEach((el) => {
    el.classList.toggle("hidden", el.id !== "page-" + id);
  });
  document.querySelectorAll("#nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === id);
  });
  if (id === "dashboard") renderDashboard();
  if (id === "alerts") renderAlerts();
  if (id === "report") renderReport();
  if (PLUGIN_PAGES[id]) mountPlugin(id);
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
// 供插件页复用主框架的工具函数，避免每个页面重复实现。
window.spf = { api, esc, fmtMoney, fmtPct, fmtLtv, barChart, hbarList, CLASS_COLORS };

function loadPluginScript(src) {
  return new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    // 单个页面加载失败不阻塞其它页面，导航项保留但内容为空。
    s.onerror = () => {
      console.error("[spf] 页面模块加载失败:", src);
      resolve();
    };
    document.body.appendChild(s);
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
    _mounted.add(id);
  }
  if (def.render) {
    try {
      await def.render(el);
    } catch (e) {
      console.error("[spf] 页面渲染失败:", id, e);
      el.innerHTML = `<div class="empty">加载失败：${esc(String(e.message || e))}</div>`;
    }
  }
}

/* ---------------- 驾驶舱 ---------------- */

async function renderDashboard() {
  const data = await api("/api/dashboard");
  const kpi = data.kpi;
  const kpis = [
    { label: "贷款笔数", value: kpi.loan_count, sub: "dws_risk_class 明细" },
    { label: "总敞口（余额）", value: fmtMoney(kpi.total_balance), sub: "全部贷款" },
    { label: "预警贷款", value: kpi.alert_loans, sub: "LTV 超红线" },
    { label: "低置信笔数", value: kpi.low_confidence, sub: "空间特征缺失" },
    { label: "高危区贷款", value: kpi.high_risk_zone_loans, sub: "空间画像标记" },
  ];
  document.getElementById("kpi-cards").innerHTML = kpis
    .map(
      (k) =>
        `<div class="kpi"><div class="kpi-label">${k.label}</div>` +
        `<div class="kpi-value">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`
    )
    .join("");

  barChart(document.getElementById("five-class-chart"), data.five_class, {
    value: "balance_total",
    label: "risk_class",
    sub: (d) => `${d.loan_count}笔 · ${fmtPct(d.balance_pct)}`,
    format: fmtMoney,
    color: (d) => CLASS_COLORS[d.risk_class] || "#2563eb",
    height: 250,
  });

  barChart(document.getElementById("ltv-hist-chart"), data.ltv_hist, {
    value: "count",
    label: "bucket",
    format: (x) => x,
    color: (d) => ltvColor(d.bucket),
    height: 250,
  });
  document
    .getElementById("ltv-hist-chart")
    .insertAdjacentHTML(
      "beforeend",
      `<div class="legend"><span><i style="background:#b91c1c"></i>LTV&gt;0.85 红线内</span>` +
        `<span><i style="background:#b45309"></i>0.80-0.85 临界</span>` +
        `<span><i style="background:#2563eb"></i>安全区间</span></div>`
    );

  hbarList(document.getElementById("city-dist"), data.city_dist, {
    value: "loan_count",
    label: "city",
    format: (d) => `${d.loan_count} 笔${d.high_risk_loans ? " · 高危 " + d.high_risk_loans : ""}`,
    color: (d) => (d.high_risk_loans > 0 ? "risk" : ""),
  });

  renderAlertOverview(data.alert_breakdown);
}

function ltvColor(bucket) {
  if (bucket.startsWith(">") || bucket.startsWith("0.85") || bucket.startsWith("0.90"))
    return "#b91c1c";
  if (bucket.startsWith("0.80")) return "#b45309";
  if (bucket === "缺失") return "#9ca3af";
  return "#2563eb";
}

function renderAlertOverview(ab) {
  const srcs = [
    { key: "offline", title: "离线 T+1 预警（ads_ltv_alerts）", total: ab.offline_total },
    { key: "stream", title: "实时预警（ads_stream_ltv_alerts）", total: ab.stream_total },
  ];
  let html = "";
  for (const src of srcs) {
    const rows = Object.entries(ab[src.key])
      .filter(([k]) => k !== "total")
      .sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...rows.map(([, c]) => c));
    html += `<div class="alert-src"><h3>${src.title} · 共 ${src.total} 条</h3><div class="alert-bars">`;
    for (const [cls, cnt] of rows) {
      html +=
        `<div class="alert-bar-row"><span>${esc(cls)}</span>` +
        `<div class="alert-bar-track"><div class="alert-bar-fill" style="width:${((cnt / max) * 100).toFixed(1)}%;background:${CLASS_COLORS[cls] || "#d14a3a"}"></div></div>` +
        `<span class="alert-bar-num">${cnt} 条</span></div>`;
    }
    html += `</div></div>`;
  }
  document.getElementById("alert-overview").innerHTML = html;
}

/* ---------------- LTV 预警列表 ---------------- */

/* 两档预警等级标签（与 tools/alerting/main.py 的 alert_level_label 口径一致）：
 * strong→强预警级、warn→警示级、缺失/NULL→「—」。 */
function alertLevelLabel(lv) {
  const level = (lv == null ? "" : String(lv)).toLowerCase();
  if (level === "strong") return `<span class="badge bad">强预警级</span>`;
  if (level === "warn") return `<span class="badge warn">警示级</span>`;
  return "—";
}

function buildAlertQuery(page) {
  const q = new URLSearchParams();
  const v = (id) => document.getElementById(id).value;
  if (v("f-risk-class")) q.set("risk_class", v("f-risk-class"));
  if (v("f-source")) q.set("source", v("f-source"));
  if (v("f-ltv-min")) q.set("ltv_min", v("f-ltv-min"));
  if (v("f-ltv-max")) q.set("ltv_max", v("f-ltv-max"));
  if (v("f-date-from")) q.set("date_from", v("f-date-from"));
  if (v("f-date-to")) q.set("date_to", v("f-date-to"));
  q.set("page", String(page || state.alertsPage));
  return q.toString();
}

async function renderAlerts() {
  const qs = buildAlertQuery();
  const data = await api("/api/alerts?" + qs);
  document.getElementById("export-btn").href = "/api/alerts/export?" + qs;
  document.getElementById("alert-total").textContent = `共 ${data.total} 条预警`;

  const tbody = document.querySelector("#alert-table tbody");
  if (!data.rows.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="empty">无符合条件的数据</td></tr>`;
  } else {
    tbody.innerHTML = data.rows
      .map((r) => {
        const conf = r.confirmed
          ? `<span class="confirmed-tag">✓ ${esc(r.confirmed.confirmed_by)}</span>`
          : state.user.can_confirm
            ? `<button class="btn btn-sm btn-ghost btn-confirm" data-loan="${r.loan_id}" data-date="${r.alert_date}" data-src="${r.src}">确认</button>`
            : `<span class="badge off">未确认</span>`;
        return (
          `<tr>` +
          `<td>${r.src === "offline" ? "离线 T+1" : "实时"}</td>` +
          `<td>${r.loan_id}</td>` +
          `<td>${r.customer_id ?? "-"}</td>` +
          `<td><b>${fmtLtv(r.ltv)}</b></td>` +
          `<td>${fmtMoney(r.loan_balance)}</td>` +
          `<td>${fmtMoney(r.market_valuation)}</td>` +
          `<td><span class="badge" style="background:${CLASS_COLORS[r.risk_class]}22;color:${CLASS_COLORS[r.risk_class]}">${esc(r.risk_class)}</span></td>` +
          `<td>${r.is_high_risk_zone ? `<span class="badge bad">高危区</span>` : "-"}</td>` +
          `<td>${alertLevelLabel(r.alert_level)}</td>` +
          `<td>${r.alert_date}</td>` +
          `<td>${esc(r.property_addr)}</td>` +
          `<td>${conf}</td>` +
          `<td></td>` +
          `</tr>`
        );
      })
      .join("");
  }

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  state.alertsPage = data.page;
  document.getElementById("page-info").textContent = `第 ${data.page}/${totalPages} 页`;
  document.getElementById("prev-page").disabled = data.page <= 1;
  document.getElementById("next-page").disabled = data.page >= totalPages;
}

async function confirmAlert(loanId, alertDate, source) {
  await api("/api/alerts/confirm", {
    method: "POST",
    body: JSON.stringify({ loan_id: loanId, alert_date: alertDate, source }),
  });
  renderAlerts();
}

/* ---------------- 1104 报送 ---------------- */

async function renderReport() {
  const dates = (await api("/api/report/dates")).dates;
  const sel = document.getElementById("report-date");
  const prev = sel.value;
  sel.innerHTML = dates.map((d) => `<option>${d}</option>`).join("");
  if (prev && dates.includes(prev)) sel.value = prev;

  const date = sel.value;
  if (!date) {
    document.getElementById("report-status").textContent = "";
    document.getElementById("report-table").querySelector("tbody").innerHTML =
      `<tr><td colspan="5" class="empty">尚无报送数据</td></tr>`;
    return;
  }
  const data = await api("/api/report?date=" + encodeURIComponent(date));

  const statusEl = document.getElementById("report-status");
  if (data.consistent) {
    statusEl.className = "badge ok";
    statusEl.textContent = "口径一致 · 可报送";
  } else {
    statusEl.className = "badge bad";
    statusEl.textContent = "口径不一致 · 已阻断";
  }

  const tbody = document.querySelector("#report-table tbody");
  tbody.innerHTML = data.rows
    .map(
      (r) =>
        `<tr style="${r.is_total ? "font-weight:700;background:#f8fafc" : ""}">` +
        `<td>${r.is_total ? "合计" : esc(r.risk_class)}</td>` +
        `<td>${r.loan_count}</td>` +
        `<td>${fmtMoney(r.balance_total)}</td>` +
        `<td>${fmtPct(r.balance_pct)}</td>` +
        `<td>${r.etl_ts || "-"}</td></tr>`
    )
    .join("");

  const checkEl = document.getElementById("report-check");
  if (data.consistent) {
    checkEl.innerHTML = `<div class="check-line">与 dws_risk_class 明细聚合一致（笔数/余额均在容差内）</div>`;
  } else {
    checkEl.innerHTML =
      `<div class="check-line">以下口径与 dws_risk_class 明细聚合不一致（校验逻辑同 tools/reporting/main.py）：</div>` +
      `<div class="mismatch-list">${data.mismatches
        .map((m) => `<div class="mismatch-item">· ${esc(m)}</div>`)
        .join("")}</div>`;
  }

  const alertEl = document.getElementById("report-alerts");
  alertEl.innerHTML = data.alerts.length
    ? data.alerts
        .map(
          (a) =>
            `<div class="check-line">#${a.id} [${a.alert_level}] ${esc(a.detail)} @ ${a.etl_ts}</div>`
        )
        .join("")
    : `<div class="check-line">无阻断告警（ads_report_alert 为空）</div>`;
}

/* ---------------- 初始化 ---------------- */

async function init() {
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
      err.textContent = "账号或密码错误";
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

  // 确认按钮用事件委托，避免整表重绑。
  document.querySelector("#alert-table tbody").addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-confirm");
    if (btn) confirmAlert(btn.dataset.loan, btn.dataset.date, btn.dataset.src);
  });

  const me = await api("/api/me");
  if (me.logged_in) {
    state.user = me;
    await showApp();
  } else {
    showLogin();
  }
}

init();
