/* P3 · 五级分类迁徙矩阵（US-02 / R-STA-01，主用户 DA）。
 *
 * 视觉与交互全部复用主框架：工具函数取自 window.spf，样式沿用 style.css 已有 class
 * （card / filter-bar / kpi-row / table / badge / legend / empty），
 * 只有矩阵热力底色和折线图是本页独有，用行内 style 表达，不动全局 CSS。
 */

(function () {
  const { api, esc, fmtMoney, fmtPct, CLASS_COLORS } = window.spf;

  // 选中的期初/期末。存在闭包里而不是 DOM 上：切走再切回来时能保持上次的选择。
  const sel = { from: null, to: null };

  /* ---------------- 多语言字典（key = zh-CN 原文，zh-CN 默认返回原文） ----------------
   * 静态测试对 JS 源码做中文字面量断言，因此源码里一律写 t("中文原文")，译文只存字典。
   * 五级分类名（正常/关注/次级/可疑/损失）是后端数据键，取值/匹配仍用中文原文，
   * 展示层经 t() 翻译（公共译文已上提 app.js global）；「整体」是本页自增的展示序列名。
   */
  registerI18n("en", {
    期初日期: "Start Date",
    期末日期: "End Date",
    "迁徙矩阵 · 行 = 期初分类，列 = 期末分类":
      "Migration Matrix · row = starting class, column = ending class",
    "对角线 · 未迁徙": "Diagonal · no migration",
    "下迁 · 分类变差（颜色越深占比越高）": "Downgrade · class worsened (darker = higher share)",
    "上迁 · 分类改善": "Upgrade · class improved",
    "单元格：笔数 / 行内占比（分母 = 期初该类总笔数）":
      "Cell: count / share of row (denominator = total of that starting class)",
    "下迁率趋势（Roll Rate，各期初分类 → 更差分类的笔数占比）":
      "Downgrade Rate Trend (Roll Rate, share of loans migrating from each starting class to a worse class)",
    暂无可比较的相邻期间: "No comparable adjacent periods",
    "：下迁 ": ": downgrade ",
    "灰虚线 = 「损失」（最差一级，无更差可迁，下迁率恒为 0，仅作参考）":
      "Grey dashed = “Loss” (worst class, no worse class to migrate to, rate is always 0, reference only)",
    "期初 ＼ 期末": "Start ＼ End",
    期初合计: "Starting Total",
    期末合计: "Ending Total",
    " 笔，余额 ": "{n| loan| loans}, balance ",
    可比贷款笔数: "Comparable Loans",
    未迁徙: "No Migration",
    "下迁（变差）": "Downgrade (worsened)",
    "上迁（改善）": "Upgrade (improved)",
    " · 余额 ": " · balance ",
    期初分类: "Starting Class",
    下迁: "downgraded",
    期初: "starting",
    整体: "Overall",
    // ---- API message 整句注册（data.message_key 查表，CLI 命令名语言中立）----
    "快照不足两期，无法计算迁徙。请先执行 python tools/frontend/pages/p3_migration.py --reconstruct-from-ads （按 ads_risk_class 真实类笔数重建历史）或 --backfill（演示）":
      "Fewer than two snapshots: migration cannot be computed. Run python tools/frontend/pages/p3_migration.py --reconstruct-from-ads (rebuild history from real ads_risk_class counts) or --backfill (demo) first",
    暂无快照数据: "No snapshot data yet",
    观察期: "Observation period",
    "（跨": " (over",
    "天）": "{n| day| days})",
  }, "migration");
  registerI18n("zh-HK", {
    期初日期: "期初日期",
    期末日期: "期末日期",
    "迁徙矩阵 · 行 = 期初分类，列 = 期末分类": "遷徙矩陣 · 行 = 期初分類，列 = 期末分類",
    "对角线 · 未迁徙": "對角線 · 未遷徙",
    "下迁 · 分类变差（颜色越深占比越高）": "下遷 · 分類變差（顏色越深佔比越高）",
    "上迁 · 分类改善": "上遷 · 分類改善",
    "单元格：笔数 / 行内占比（分母 = 期初该类总笔数）":
      "儲存格：筆數 / 列內佔比（分母 = 期初該類總筆數）",
    "下迁率趋势（Roll Rate，各期初分类 → 更差分类的笔数占比）":
      "下遷率趨勢（Roll Rate，各期初分類 → 更差分類的筆數佔比）",
    暂无可比较的相邻期间: "暫無可比較的相鄰期間",
    "：下迁 ": "：下遷 ",
    "灰虚线 = 「损失」（最差一级，无更差可迁，下迁率恒为 0，仅作参考）":
      "灰虛線 = 「損失」（最差一級，無更差可遷，下遷率恆為 0，僅供參考）",
    "期初 ＼ 期末": "期初 ＼ 期末",
    期初合计: "期初合計",
    期末合计: "期末合計",
    " 笔，余额 ": " 筆，餘額 ",
    可比贷款笔数: "可比貸款筆數",
    未迁徙: "未遷徙",
    "下迁（变差）": "下遷（變差）",
    "上迁（改善）": "上遷（改善）",
    " · 余额 ": " · 餘額 ",
    期初分类: "期初分類",
    下迁: "下遷",
    期初: "期初",
    整体: "整體",
    // ---- API message 整句注册（与 en 块一一对应）----
    "快照不足两期，无法计算迁徙。请先执行 python tools/frontend/pages/p3_migration.py --reconstruct-from-ads （按 ads_risk_class 真实类笔数重建历史）或 --backfill（演示）":
      "快照不足兩期，無法計算遷徙。請先執行 python tools/frontend/pages/p3_migration.py --reconstruct-from-ads （按 ads_risk_class 真實類筆數重建歷史）或 --backfill（演示）",
    暂无快照数据: "暫無快照資料",
    观察期: "觀察期",
    "（跨": "（跨",
    "天）": " 天）",
  }, "migration");

  function html() {
    return `
    <h1 class="page-title">${t("迁徙与降级追踪")}</h1>
    <div class="filter-bar">
      <label data-i18n="期初日期">期初日期
        <select id="mig-from"></select>
      </label>
      <label data-i18n="期末日期">期末日期
        <select id="mig-to"></select>
      </label>
      <span id="mig-range" class="filter-total"></span>
    </div>
    <div id="mig-kpi" class="kpi-row"></div>
    <div class="card" style="margin-top:16px">
      <h2 class="card-title">${t("迁徙矩阵 · 行 = 期初分类，列 = 期末分类")}</h2>
      <div id="mig-matrix"></div>
      <div class="legend">
        <span><i style="background:#e5e7eb"></i>${t("对角线 · 未迁徙")}</span>
        <span><i style="background:#fecaca"></i>${t("下迁 · 分类变差（颜色越深占比越高）")}</span>
        <span><i style="background:#bbf7d0"></i>${t("上迁 · 分类改善")}</span>
        <span>${t("单元格：笔数 / 行内占比（分母 = 期初该类总笔数）")}</span>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2 class="card-title">${t("下迁率趋势（Roll Rate，各期初分类 → 更差分类的笔数占比）")}</h2>
      <div id="mig-roll" class="chart"></div>
      <div id="mig-roll-table"></div>
    </div>
  `;
  }

  /* ---------------- 折线图（本页独有，主框架只有柱状图） ---------------- */

  function lineChart(container, labels, series, height) {
    if (!labels.length) {
      container.innerHTML = `<div class="empty">${t("暂无可比较的相邻期间")}</div>`;
      return;
    }
    const width = container.clientWidth || 720;
    const h = height || 270;
    const padL = 52;
    const padR = 14;
    const padT = 16;
    const padB = 48;
    const plotW = width - padL - padR;
    const plotH = h - padT - padB;
    // 下迁率通常是个位数百分比，最大值直接当上界会让折线贴顶；留 20% 余量并设 5% 下限。
    const maxV = Math.max(0.05, ...series.flatMap((s) => s.points.map((p) => p.rate))) * 1.2;
    const xAt = (i) =>
      labels.length > 1 ? padL + (plotW * i) / (labels.length - 1) : padL + plotW / 2;
    const yAt = (v) => padT + plotH - (v / maxV) * plotH;

    let svg = `<svg viewBox="0 0 ${width} ${h}" xmlns="http://www.w3.org/2000/svg">`;
    for (let i = 0; i <= 4; i++) {
      const v = (maxV * i) / 4;
      const y = yAt(v);
      svg += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${width - padR}" y2="${y.toFixed(1)}" stroke="#eef2f7"/>`;
      svg += `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="#9ca3af">${(v * 100).toFixed(1)}%</text>`;
    }
    labels.forEach((lb, i) => {
      svg += `<text x="${xAt(i).toFixed(1)}" y="${h - 26}" text-anchor="middle" font-size="10" fill="#6b7280">${esc(lb)}</text>`;
    });
    for (const s of series) {
      const pts = s.points
        .map((p, i) => `${xAt(i).toFixed(1)},${yAt(p.rate).toFixed(1)}`)
        .join(" ");
      svg += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/>`;
      s.points.forEach((p, i) => {
        svg += `<circle cx="${xAt(i).toFixed(1)}" cy="${yAt(p.rate).toFixed(1)}" r="3" fill="${s.color}">`;
        svg += `<title>${esc(t(s.name))} ${esc(p.label)}${t("：下迁 ")}${p.down}/${p.base} = ${(p.rate * 100).toFixed(2)}%</title></circle>`;
      });
    }
    svg += "</svg>";
    const legend = series
      .map((s) => `<span><i style="background:${s.color}"></i>${esc(t(s.name))}</span>`)
      .join("");
    container.innerHTML =
      svg +
      `<div class="legend">${legend}<span style="color:#9ca3af">${t("灰虚线 = 「损失」（最差一级，无更差可迁，下迁率恒为 0，仅作参考）")}</span></div>`;
  }

  /* ---------------- 矩阵热力表 ---------------- */

  function cellStyle(i, j, pct) {
    if (!pct) return "";
    if (i === j) return "background:#eef2f7";
    // 占比映射到透明度：0 笔不着色，占比越高越深，最深 0.85 保证文字仍可读。
    const a = Math.min(0.85, 0.18 + pct * 0.9).toFixed(2);
    // 下迁（列序号更大 = 更差）用红，醒目；上迁用绿。
    return j > i ? `background:rgba(220,38,38,${a})` : `background:rgba(22,163,74,${a})`;
  }

  function renderMatrix(el, data) {
    const cls = data.classes;
    let html = `<table class="table"><thead><tr><th style="min-width:96px">${t("期初 ＼ 期末")}</th>`;
    for (const c of cls) {
      html += `<th style="text-align:center;color:${CLASS_COLORS[c]}">${esc(t(c))}</th>`;
    }
    html += `<th style="text-align:center">${t("期初合计")}</th></tr></thead><tbody>`;

    cls.forEach((from, i) => {
      const rowTotal = data.row_totals[i].count;
      html += `<tr><td style="font-weight:600;color:${CLASS_COLORS[from]}">${esc(t(from))}</td>`;
      cls.forEach((to, j) => {
        const c = data.matrix[i][j];
        const dim = c.count ? "" : "color:#c7cdd6";
        html +=
          `<td style="text-align:center;${cellStyle(i, j, c.pct)};${dim}" title="${esc(t(from))} → ${esc(t(to))}${t("：")}${c.count}${t(" 笔，余额 ", { n: c.count })}${fmtMoney(c.balance)}">` +
          `<div style="font-weight:${i === j ? 600 : 700}">${c.count}</div>` +
          `<div style="font-size:11px;opacity:.75">${c.count ? fmtPct(c.pct) : "-"}</div></td>`;
      });
      html += `<td style="text-align:center;font-weight:600">${rowTotal}</td></tr>`;
    });

    html += `<tr><td style="font-weight:600">${t("期末合计")}</td>`;
    for (const t of data.col_totals) {
      html += `<td style="text-align:center;font-weight:600">${t.count}</td>`;
    }
    html += `<td style="text-align:center;font-weight:700">${data.summary.matched}</td></tr>`;
    html += `</tbody></table>`;
    el.innerHTML = html;
  }

  /* ---------------- 渲染主流程 ---------------- */

  function renderKpi(el, s) {
    const items = [
      { label: t("可比贷款笔数"), value: s.matched },
      { label: t("未迁徙"), value: s.stay },
      {
        label: t("下迁（变差）"),
        value: s.down,
        sub: `${fmtPct(s.down_pct)}${t(" · 余额 ")}${fmtMoney(s.down_balance)}`,
        color: "#b91c1c",
      },
      { label: t("上迁（改善）"), value: s.up, sub: fmtPct(s.up_pct), color: "#15803d" },
    ];
    el.innerHTML = items
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${k.label}</div>` +
          `<div class="kpi-value"${k.color ? ` style="color:${k.color}"` : ""}>${k.value}</div>` +
          `<div class="kpi-sub">${k.sub || ""}</div></div>`
      )
      .join("");
  }

  function renderRoll(chartEl, tableEl, roll) {
    const labels = roll.periods.map((p) => p.label);
    // 五类全部入图；「损失」是最差一级、无更差可迁，下迁率恒为 0，
    // 画成灰色虚线当 0% 参考线——保留数据可见性，又不与真实趋势线混淆。
    const series = roll.series.map((s) => ({
      name: s.risk_class,
      color:
        s.risk_class === "损失"
          ? "#9ca3af" // 灰：参考线，不代表真实波动
          : CLASS_COLORS[s.risk_class] || "#2563eb",
      dash: s.risk_class === "损失" ? "4 4" : undefined,
      width: s.risk_class === "损失" ? 1.5 : 2,
      points: s.points,
    }));
    series.push({
      name: "整体",
      color: "#374151",
      dash: "5 4",
      width: 2.5,
      points: roll.overall,
    });
    lineChart(chartEl, labels, series, 280);

    if (!labels.length) {
      tableEl.innerHTML = "";
      return;
    }
    let html = `<table class="table" style="margin-top:12px"><thead><tr><th>${t("期初分类")}</th>`;
    for (const lb of labels) html += `<th style="text-align:center">${esc(lb)}</th>`;
    html += `</tr></thead><tbody>`;
    for (const s of roll.series) {
      html += `<tr><td style="color:${CLASS_COLORS[s.risk_class]};font-weight:600">${esc(t(s.risk_class))}</td>`;
      const worst = s.risk_class === "损失";
      for (const p of s.points) {
        // 直观性：多数周期下迁为 0，干看全是 0.00% 看不出变化。
        // 非零下迁高亮（分类色浅底 + 加粗），0% 弱化为灰字，让真实变化一眼可辨。
        const hit = !worst && p.rate > 0;
        const cellStyle = worst
          ? "text-align:center;color:#9ca3af"
          : hit
            ? `text-align:center;background:${CLASS_COLORS[s.risk_class]}1a;font-weight:700;color:${CLASS_COLORS[s.risk_class]}`
            : "text-align:center;color:#9ca3af";
        html +=
          `<td style="${cellStyle}" title="${t("下迁")} ${p.down} / ${t("期初")} ${p.base}">` +
          `${worst ? "—" : hit ? fmtPct(p.rate) + " ▲" : fmtPct(p.rate)}` +
          `<div style="font-size:11px;color:${hit ? CLASS_COLORS[s.risk_class] : "#9ca3af"}">${p.down}/${p.base}</div></td>`;
      }
      html += `</tr>`;
    }
    html += `<tr><td style="font-weight:700">${t("整体")}</td>`;
    for (const p of roll.overall) {
      // 整体行同样高亮非零下迁，弱化 0%。
      const hit = p.rate > 0;
      const cellStyle = hit
        ? "text-align:center;background:#3741511a;font-weight:700;color:#374151"
        : "text-align:center;font-weight:600;color:#9ca3af";
      html += `<td style="${cellStyle}">${hit ? fmtPct(p.rate) + " ▲" : fmtPct(p.rate)}<div style="font-size:11px;color:${hit ? "#374151" : "#9ca3af"}">${p.down}/${p.base}</div></td>`;
    }
    html += `</tr></tbody></table>`;
    tableEl.innerHTML = html;
  }

  function fillDateSelect(el, dates, value) {
    el.innerHTML = dates
      .map(
        (d) =>
          `<option value="${d.snap_date}">${d.snap_date} · ` +
          `${d.loan_count} ${t("笔", { n: d.loan_count })}</option>`
      )
      .join("");
    if (value) el.value = value;
  }

  async function render(root) {
    const data = await api(
      "/api/migration" +
        (sel.from && sel.to
          ? `?date_from=${encodeURIComponent(sel.from)}&date_to=${encodeURIComponent(sel.to)}`
          : "")
    );

    const kpiEl = root.querySelector("#mig-kpi");
    const matrixEl = root.querySelector("#mig-matrix");
    const rollEl = root.querySelector("#mig-roll");
    const rollTableEl = root.querySelector("#mig-roll-table");
    const fromEl = root.querySelector("#mig-from");
    const toEl = root.querySelector("#mig-to");

    if (data.message || !data.matrix) {
      fromEl.innerHTML = toEl.innerHTML = "";
      kpiEl.innerHTML = "";
      matrixEl.innerHTML = `<div class="empty">${esc(data.message_key ? t(data.message_key) : t(data.message || "暂无快照数据"))}</div>`;
      rollEl.innerHTML = "";
      rollTableEl.innerHTML = "";
      root.querySelector("#mig-range").textContent = "";
      return;
    }

    sel.from = data.date_from;
    sel.to = data.date_to;
    fillDateSelect(fromEl, data.dates, sel.from);
    fillDateSelect(toEl, data.dates, sel.to);
    // 用 onXxx 赋值而非 addEventListener：render 每次切页都会跑，赋值天然幂等不会叠加监听。
    fromEl.onchange = () => {
      sel.from = fromEl.value;
      render(root);
    };
    toEl.onchange = () => {
      sel.to = toEl.value;
      render(root);
    };

    const days = Math.round((new Date(data.date_to) - new Date(data.date_from)) / 86400000);
    root.querySelector("#mig-range").textContent =
      `${t("观察期")} ${data.date_from} → ${data.date_to}${t("（跨")} ${days}${t("天）", { n: days })}`;

    renderKpi(kpiEl, data.summary);
    renderMatrix(matrixEl, data);
    renderRoll(rollEl, rollTableEl, data.roll_rate);
  }

  window.registerPage("migration", { html, render });
})();
