/* global window */
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

  const HTML = `
    <h1 class="page-title">五级分类迁徙矩阵</h1>
    <div id="mig-notice"></div>
    <div class="filter-bar">
      <label>期初日期
        <select id="mig-from"></select>
      </label>
      <label>期末日期
        <select id="mig-to"></select>
      </label>
      <span id="mig-range" class="filter-total"></span>
    </div>
    <div id="mig-kpi" class="kpi-row"></div>
    <div class="card" style="margin-top:16px">
      <h2 class="card-title">迁徙矩阵 · 行 = 期初分类，列 = 期末分类</h2>
      <div id="mig-matrix"></div>
      <div class="legend">
        <span><i style="background:#e5e7eb"></i>对角线 · 未迁徙</span>
        <span><i style="background:#fecaca"></i>下迁 · 分类变差（颜色越深占比越高）</span>
        <span><i style="background:#bbf7d0"></i>上迁 · 分类改善</span>
        <span>单元格：笔数 / 行内占比（分母 = 期初该类总笔数）</span>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2 class="card-title">Roll Rate 下迁率趋势（各期初分类 → 更差分类的笔数占比）</h2>
      <div id="mig-roll" class="chart"></div>
      <div id="mig-roll-table"></div>
    </div>
  `;

  /* ---------------- 折线图（本页独有，主框架只有柱状图） ---------------- */

  function lineChart(container, labels, series, height) {
    if (!labels.length) {
      container.innerHTML = `<div class="empty">暂无可比较的相邻期间</div>`;
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
        svg += `<title>${esc(s.name)} ${esc(p.label)}：下迁 ${p.down}/${p.base} = ${(p.rate * 100).toFixed(2)}%</title></circle>`;
      });
    }
    svg += "</svg>";
    const legend = series
      .map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`)
      .join("");
    container.innerHTML =
      svg +
      `<div class="legend">${legend}<span>「损失」为最差一级，无更差可迁，故不入图</span></div>`;
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
    let html = `<table class="table"><thead><tr><th style="min-width:96px">期初 ＼ 期末</th>`;
    for (const c of cls) {
      html += `<th style="text-align:center;color:${CLASS_COLORS[c]}">${esc(c)}</th>`;
    }
    html += `<th style="text-align:center">期初合计</th></tr></thead><tbody>`;

    cls.forEach((from, i) => {
      const rowTotal = data.row_totals[i].count;
      html += `<tr><td style="font-weight:600;color:${CLASS_COLORS[from]}">${esc(from)}</td>`;
      cls.forEach((to, j) => {
        const c = data.matrix[i][j];
        const dim = c.count ? "" : "color:#c7cdd6";
        html +=
          `<td style="text-align:center;${cellStyle(i, j, c.pct)};${dim}" title="${esc(from)} → ${esc(to)}：${c.count} 笔，余额 ${fmtMoney(c.balance)}">` +
          `<div style="font-weight:${i === j ? 600 : 700}">${c.count}</div>` +
          `<div style="font-size:11px;opacity:.75">${c.count ? fmtPct(c.pct) : "-"}</div></td>`;
      });
      html += `<td style="text-align:center;font-weight:600">${rowTotal}</td></tr>`;
    });

    html += `<tr><td style="font-weight:600">期末合计</td>`;
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
      { label: "可比贷款笔数", value: s.matched, sub: "两期均存在（内联口径）" },
      { label: "未迁徙", value: s.stay, sub: "分类保持不变" },
      {
        label: "下迁（变差）",
        value: s.down,
        sub: `${fmtPct(s.down_pct)} · 余额 ${fmtMoney(s.down_balance)}`,
        color: "#b91c1c",
      },
      { label: "上迁（改善）", value: s.up, sub: fmtPct(s.up_pct), color: "#15803d" },
      {
        label: "新增 / 退出",
        value: `${s.new_loans} / ${s.exited_loans}`,
        sub: "期间进出台账，不计入矩阵",
      },
    ];
    el.innerHTML = items
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${k.label}</div>` +
          `<div class="kpi-value"${k.color ? ` style="color:${k.color}"` : ""}>${k.value}</div>` +
          `<div class="kpi-sub">${k.sub}</div></div>`
      )
      .join("");
  }

  function renderRoll(chartEl, tableEl, roll) {
    const labels = roll.periods.map((p) => p.label);
    const series = roll.series
      // 「损失」下迁率恒为 0（已是最差一级），画进去只是一条贴底直线，干扰读图。
      .filter((s) => s.risk_class !== "损失")
      .map((s) => ({
        name: s.risk_class,
        color: CLASS_COLORS[s.risk_class] || "#2563eb",
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
    let html = `<table class="table" style="margin-top:12px"><thead><tr><th>期初分类</th>`;
    for (const lb of labels) html += `<th style="text-align:center">${esc(lb)}</th>`;
    html += `</tr></thead><tbody>`;
    for (const s of roll.series) {
      html += `<tr><td style="color:${CLASS_COLORS[s.risk_class]};font-weight:600">${esc(s.risk_class)}</td>`;
      for (const p of s.points) {
        const worst = s.risk_class === "损失";
        html +=
          `<td style="text-align:center${worst ? ";color:#9ca3af" : ""}" title="下迁 ${p.down} / 期初 ${p.base}">` +
          `${worst ? "—" : fmtPct(p.rate)}<div style="font-size:11px;color:#9ca3af">${p.down}/${p.base}</div></td>`;
      }
      html += `</tr>`;
    }
    html += `<tr><td style="font-weight:700">整体</td>`;
    for (const p of roll.overall) {
      html += `<td style="text-align:center;font-weight:600">${fmtPct(p.rate)}<div style="font-size:11px;color:#9ca3af">${p.down}/${p.base}</div></td>`;
    }
    html += `</tr></tbody></table>`;
    tableEl.innerHTML = html;
  }

  function fillDateSelect(el, dates, value) {
    el.innerHTML = dates
      .map(
        (d) =>
          `<option value="${d.snap_date}">${d.snap_date}${d.is_demo ? "（演示）" : ""} · ${d.loan_count} 笔</option>`
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

    const notice = root.querySelector("#mig-notice");
    const kpiEl = root.querySelector("#mig-kpi");
    const matrixEl = root.querySelector("#mig-matrix");
    const rollEl = root.querySelector("#mig-roll");
    const rollTableEl = root.querySelector("#mig-roll-table");
    const fromEl = root.querySelector("#mig-from");
    const toEl = root.querySelector("#mig-to");

    // 演示回填必须如实标注，不能让 DA 把造出来的历史当真实迁徙来下结论。
    notice.innerHTML = (data.demo_dates || []).length
      ? `<div class="card" style="margin-bottom:14px;border-left:4px solid var(--warn)">` +
        `<span class="badge warn">演示数据</span> ` +
        `以下快照日期为<b>演示回填</b>（基于当前明细按估值回溯重算分类，非真实历史）：` +
        `${data.demo_dates.map(esc).join("、")}。` +
        `最新一期为 dws_risk_class 真实快照；真实历史由 ` +
        `<code>p3_migration.py --snapshot</code> 每日累积。</div>`
      : "";

    if (data.message || !data.matrix) {
      fromEl.innerHTML = toEl.innerHTML = "";
      kpiEl.innerHTML = "";
      matrixEl.innerHTML = `<div class="empty">${esc(data.message || "暂无快照数据")}</div>`;
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
      `观察期 ${data.date_from} → ${data.date_to}（跨 ${days} 天）`;

    renderKpi(kpiEl, data.summary);
    renderMatrix(matrixEl, data);
    renderRoll(rollEl, rollTableEl, data.roll_rate);
  }

  window.registerPage("migration", { html: HTML, render });
})();
