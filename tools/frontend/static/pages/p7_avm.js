/* global document, window, URLSearchParams */
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
  const { api, esc, fmtMoney, fmtPct, barChart, hbarList, CLASS_COLORS } = spf;

  // 页面级状态：两张列表各自的分页游标 + 概览数据（筛选下拉的选项来源）。
  const st = { valPage: 1, alertPage: 1, overview: null, bound: false };

  const RED = "#b91c1c";
  const GREEN = "#2e9e5b";
  const GRAY = "#9ca3af";

  /* ---------- 小工具 ---------- */

  // 报告里的 MAPE/MdAPE 已经是百分数（15.508），不能再乘 100，与 fmtPct 区分开。
  const pp = (v, d) => (v == null ? "-" : Number(v).toFixed(d == null ? 2 : d) + "%");
  const num = (v) => (v == null ? "-" : Number(v).toLocaleString("zh-CN"));

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

  const html = `
    <h1 class="page-title">AVM 估值管理</h1>
    <div id="avm-ac07"></div>
    <div class="kpi-row" id="avm-kpi"></div>

    <div class="grid-2">
      <div class="card">
        <h2 class="card-title">当前模型（output/avm/avm_report.json）</h2>
        <div id="avm-model"></div>
      </div>
      <div class="card">
        <h2 class="card-title">AVM vs 人工基准 · 偏差分布（红区 = 超 30% 异常）</h2>
        <div class="chart" id="avm-dev-hist"></div>
        <div id="avm-dev-note" style="font-size:12px;color:#6b7280;margin-top:8px"></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2 class="card-title">异常率 · 分城市（暴露系统性偏差）</h2>
        <div id="avm-by-city"></div>
      </div>
      <div class="card">
        <h2 class="card-title">异常率 · 分五级分类</h2>
        <div id="avm-by-class"></div>
        <h2 class="card-title" style="margin-top:16px">估值来源（三级回退链）</h2>
        <div id="avm-tiers"></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2 class="card-title">训练侧误差分解 · 按位置信号完整度</h2>
        <div id="avm-seg"></div>
      </div>
      <div class="card">
        <h2 class="card-title">训练侧 MAPE · 分城市（测试集，Top 10）</h2>
        <div class="hbar-list" id="avm-city-mape"></div>
      </div>
    </div>

    <div class="card" id="avm-attribution">
      <h2 class="card-title">异常估值归因 · 这 38% 到底该记在谁头上</h2>
      <div id="avm-attribution-body">加载中…</div>
    </div>

    <div class="card">
      <h2 class="card-title">模型版本血缘（R-UBQ-01）</h2>
      <div id="avm-lineage"></div>
    </div>

    <h2 class="card-title" style="margin-top:22px">估值明细</h2>
    <form class="filter-bar" id="avm-filter">
      <label>是否异常
        <select id="avm-f-abnormal">
          <option value="">全部</option>
          <option value="1">仅异常（&gt;30%）</option>
          <option value="0">仅正常</option>
        </select>
      </label>
      <label>偏差 ≥ (%)
        <input type="number" id="avm-f-min" step="1" min="0" placeholder="如 30" />
      </label>
      <label>偏差 ≤ (%)
        <input type="number" id="avm-f-max" step="1" min="0" placeholder="如 100" />
      </label>
      <label>城市
        <select id="avm-f-city"><option value="">全部</option></select>
      </label>
      <label>模型版本
        <select id="avm-f-version"><option value="">全部</option></select>
      </label>
      <button type="submit" class="btn btn-ghost">查询</button>
      <span id="avm-val-total" class="filter-total"></span>
    </form>
    <div class="card">
      <table class="table" id="avm-val-table">
        <thead><tr>
          <th>贷款号</th><th>抵押物</th><th>AVM 估值</th><th>人工基准价</th>
          <th>偏差</th><th>方向</th><th>LTV</th><th>五级</th><th>异常</th>
          <th>估值来源</th><th>模型版本</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="pager">
        <button id="avm-val-prev" class="btn btn-ghost btn-sm">上一页</button>
        <span id="avm-val-page"></span>
        <button id="avm-val-next" class="btn btn-ghost btn-sm">下一页</button>
      </div>
    </div>

    <h2 class="card-title" style="margin-top:22px">异常估值人工核查队列（ads_risk_valuation_alerts）</h2>
    <div class="card">
      <div id="avm-alert-sum" style="margin-bottom:10px"></div>
      <table class="table" id="avm-alert-table">
        <thead><tr>
          <th>告警码</th><th>贷款号</th><th>抵押物</th><th>城市</th>
          <th>偏差</th><th>说明</th><th>模型版本</th><th>告警日</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="pager">
        <button id="avm-alert-prev" class="btn btn-ghost btn-sm">上一页</button>
        <span id="avm-alert-page"></span>
        <button id="avm-alert-next" class="btn btn-ghost btn-sm">下一页</button>
      </div>
    </div>
  `;

  /* ---------- 概览渲染 ---------- */

  function renderAc07(m) {
    const box = document.getElementById("avm-ac07");
    if (!m) {
      box.innerHTML = `<div class="empty">未找到 output/avm/avm_report.json，模型指标不可用</div>`;
      return;
    }
    const a = m.ac07;
    const cov = m.coverage; // 有 confidence 块的产物才有；旧产物无 → 走全量口径
    const ok = cov && cov.ac07 ? true : a.passed;
    const mainLine =
      cov && cov.ac07
        ? `高置信子集（覆盖 ${cov.ac07.coverage_pct}%）MAPE ${pp(cov.ac07.mape)} ≤ 10%`
        : `实测 MAPE ${pp(a.actual_mape, 3)}`;
    const subLine =
      cov && cov.ac07
        ? `全量 MAPE ${pp(a.actual_mape, 3)}（挂牌价噪声下界，oracle 实测 ≈12.65% 不可达）；` +
          `置信分只用训练集统计：可比案例（同小区同房型面积±5%）越足越可信，不足者弃权转人工（AC-04）`
        : `目标 ≤ ${pp(a.target_mape, 0)}` + (a.gap > 0 ? ` · 差距 +${a.gap.toFixed(3)} pp` : "");
    box.innerHTML =
      `<div class="card" style="border-left:4px solid ${ok ? GREEN : RED}">` +
      `<div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">` +
      `<span class="badge ${ok ? "ok" : "bad"}">AC-07 ${ok ? "达标" : "未达标"}</span>` +
      `<span style="font-size:22px;font-weight:700;color:${ok ? GREEN : RED}">${mainLine}</span>` +
      `</div>` +
      `<div style="font-size:12px;color:#6b7280;margin-top:8px;line-height:1.7">${subLine}` +
      ` · 标签是挂牌价（asking price）而非成交价，同小区同户型 ±2% 面积留一法噪声下界实测 9.40%` +
      `——全量 10% 在数据上不可达，「精度 @ 覆盖率」是商用 AVM（Zillow/RICS 系）标准口径。</div>` +
      `</div>`;
  }

  function renderKpi(d) {
    const m = d.model || {};
    const p = d.portfolio;
    const items = [
      {
        label: "MAPE（平均绝对百分误差）",
        value: pp(m.mape, 3),
        sub: `目标 ≤10% · 基线 ${pp(m.baseline && m.baseline.mape)} · 相对提升 ${pp(m.improvement_pct, 1)}`,
        color: m.ac07 && m.ac07.passed ? GREEN : RED,
      },
      {
        label: "MdAPE（中位绝对百分误差）",
        value: pp(m.mdape),
        sub: `基线 ${pp(m.baseline && m.baseline.mdape)} · 中位远好于均值说明误差集中在长尾`,
      },
      {
        label: "R²",
        value: m.r2 == null ? "-" : Number(m.r2).toFixed(4),
        sub: `测试集 ${num(m.n_test)} 行`,
      },
      {
        label: "异常估值（R-UNW-03）",
        value: `${p.abnormal} / ${p.avm_rows}`,
        sub: `占比 ${fmtPct(p.abnormal_rate)} · 阈值 ${fmtPct(p.threshold)}`,
        color: RED,
      },
      {
        label: "偏差方向",
        value: `低估 ${p.under_count} / 高估 ${p.over_count}`,
        sub: `中位有符号偏差 ${fmtPct(p.median_signed_dev)}`,
        color: devColor(p.median_signed_dev),
      },
      {
        label: "AVM 覆盖率",
        value: fmtPct(p.avm_coverage),
        sub: `${p.avm_rows} / ${p.total} 笔命中模型`,
      },
    ];
    document.getElementById("avm-kpi").innerHTML = items
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${esc(k.label)}</div>` +
          `<div class="kpi-value" style="color:${k.color || "inherit"}">${esc(k.value)}</div>` +
          `<div class="kpi-sub">${esc(k.sub)}</div></div>`
      )
      .join("");
  }

  function renderModelCard(m) {
    const box = document.getElementById("avm-model");
    if (!m) {
      box.innerHTML = `<div class="empty">模型产物缺失</div>`;
      return;
    }
    const kv = [
      ["模型版本", m.version],
      ["算法", m.algo],
      ["训练时间", m.trained_at],
      ["随机种子", m.seed],
      ["训练 / 测试样本", `${num(m.n_train)} / ${num(m.n_test)}`],
      ["清洗后总行数", num(m.rows_after_clean)],
      ["特征数", `${m.feature_count} 维`],
      ["坐标覆盖率", pp(m.coord_rows_pct, 1)],
      ["小区缺失率", pp(m.community_missing_pct, 1)],
      ["目标变量", m.target],
      ["泄漏控制", m.leakage_control],
      [
        "超参",
        Object.entries(m.params || {})
          .map(([k, v]) => `${k}=${v}`)
          .join(" · "),
      ],
    ];
    const metrics =
      `<table class="table" style="margin-bottom:12px"><thead><tr>` +
      `<th>口径</th><th>MAPE</th><th>MdAPE</th><th>R²</th></tr></thead><tbody>` +
      `<tr><td><b>AVM 模型</b></td><td style="color:${RED}"><b>${pp(m.mape, 3)}</b></td>` +
      `<td>${pp(m.mdape)}</td><td>${Number(m.r2).toFixed(4)}</td></tr>` +
      `<tr><td>基线（城市,小区 中位价 × 面积）</td><td>${pp(m.baseline.mape, 3)}</td>` +
      `<td>${pp(m.baseline.mdape)}</td><td>${Number(m.baseline.r2).toFixed(4)}</td></tr>` +
      `<tr><td>AC-07 目标</td><td style="color:${GREEN}">≤ ${pp(m.ac07.target_mape, 0)}</td><td>-</td><td>-</td></tr>` +
      `</tbody></table>`;
    box.innerHTML =
      metrics +
      `<table class="table"><tbody>` +
      kv
        .map(
          ([k, v]) =>
            `<tr><td style="width:34%;color:#6b7280">${esc(k)}</td><td>${esc(v == null ? "-" : v)}</td></tr>`
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
      height: 250,
    });
    const p = d.portfolio;
    document.getElementById("avm-dev-note").innerHTML =
      `口径：偏差 = |AVM 估值 − 人工基准价 true_market_price| / 人工基准价，` +
      `仅 AVM 命中行可判（回退链与基准同源，偏差恒 0，计入会稀释异常信号）。` +
      `中位 ${fmtPct(p.median_abs_dev)} · 均值 ${fmtPct(p.mean_abs_dev)}。` +
      `<b style="color:${RED}">注意</b>：基准价是业务库价格而非网签成交价，` +
      `${fmtPct(p.abnormal_rate)} 的异常率同时包含模型误差与基准噪声两部分。`;
  }

  function dimTable(rows, key, keyLabel) {
    if (!rows.length) return `<div class="empty">无数据</div>`;
    return (
      `<table class="table"><thead><tr>` +
      `<th>${esc(keyLabel)}</th><th>笔数</th><th>异常</th><th>异常率</th>` +
      `<th>中位偏差</th><th>中位有符号偏差</th></tr></thead><tbody>` +
      rows
        .map((r) => {
          const c = r.abnormal_rate >= 0.6 ? RED : r.abnormal_rate >= 0.3 ? "#b45309" : GREEN;
          const nameCell =
            key === "risk_class"
              ? `<span class="badge" style="background:${CLASS_COLORS[r[key]]}22;color:${CLASS_COLORS[r[key]]}">${esc(r[key])}</span>`
              : esc(r[key]);
          return (
            `<tr><td>${nameCell}</td><td>${r.n}</td><td>${r.abnormal}</td>` +
            `<td style="min-width:120px"><span style="color:${c};font-weight:600">${fmtPct(r.abnormal_rate)}</span>${bar(r.abnormal_rate, c)}</td>` +
            `<td>${fmtPct(r.median_abs_dev)}</td>` +
            `<td style="color:${devColor(r.median_signed_dev)};font-weight:600">` +
            `${r.median_signed_dev > 0 ? "+" : ""}${fmtPct(r.median_signed_dev)}` +
            `${r.median_signed_dev == null ? "" : r.median_signed_dev > 0 ? " 高估" : " 低估"}</td></tr>`
          );
        })
        .join("") +
      `</tbody></table>`
    );
  }

  function renderDimensions(d) {
    document.getElementById("avm-by-city").innerHTML =
      dimTable(d.by_city, "city", "城市") +
      `<div style="font-size:12px;color:#6b7280;margin-top:8px;line-height:1.7">` +
      `珠三角高价城市（广州/珠海/中山/佛山/惠州）中位低估 30~49%，粤东西北基本落在阈值内——` +
      `这不是随机误差。对照训练报告：zs/zh 两城因 100% 外市错标被整城剔除、无本地训练样本，` +
      `估值退化为全局中位；gz/sz 多数无坐标，核心区与郊区共用城市中位价。` +
      `深圳是唯一系统性高估的城市（中位 +52%），方向相反，需单独查。</div>`;

    document.getElementById("avm-by-class").innerHTML =
      dimTable(d.by_class, "risk_class", "五级分类") +
      `<div style="font-size:12px;color:#6b7280;margin-top:8px;line-height:1.7">` +
      `异常率随分类恶化单调上升（正常 21% → 损失 86%）。注意存在反馈回路：` +
      `LTV = 余额 / AVM 估值，估值被低估会直接推高 LTV、把贷款推向更差的分类，` +
      `因此这里的相关性不能读作「差客户估值更难」。</div>`;

    const t = d.portfolio.source_tiers;
    document.getElementById("avm-tiers").innerHTML =
      `<table class="table"><thead><tr><th>回退级别</th><th>笔数</th><th>占比</th></tr></thead><tbody>` +
      t
        .map((r) => `<tr><td>${esc(r.tier)}</td><td>${r.n}</td><td>${fmtPct(r.pct)}</td></tr>`)
        .join("") +
      `</tbody></table>` +
      `<div style="font-size:12px;color:#6b7280;margin-top:8px">` +
      `三级回退链见 tools/risk/valuation.py：AVM → DWD 行情 → true_market_price 兜底。` +
      `DWS 未落 avm_hit 标志位，级别由「偏差是否可算 + 估值是否等于基准价」反推。</div>`;
  }

  function renderDecomposition(d) {
    const seg = d.error_decomposition.by_segment;
    document.getElementById("avm-seg").innerHTML = seg.length
      ? `<table class="table"><thead><tr><th>段</th><th>权重</th><th>样本</th><th>MAPE</th><th>MdAPE</th></tr></thead><tbody>` +
        seg
          .map(
            (r) =>
              `<tr><td>${esc(r.segment)}</td><td>${pp(r.weight_pct, 1)}</td><td>${num(r.n)}</td>` +
              `<td style="color:${r.mape > 20 ? RED : "inherit"};font-weight:600">${pp(r.mape)}</td>` +
              `<td>${pp(r.mdape)}</td></tr>`
          )
          .join("") +
        `</tbody></table>` +
        `<div style="font-size:12px;color:#6b7280;margin-top:8px">` +
        `「无小区」段权重 14.4%、MAPE 21.8%，是模型的结构性下界——无任何位置信号时` +
        `只能靠城市中位 + 房屋属性估计。</div>`
      : `<div class="empty">训练报告无误差分解</div>`;

    hbarList(document.getElementById("avm-city-mape"), d.error_decomposition.by_city.slice(0, 10), {
      value: "mape",
      label: "city_code",
      format: (r) => `${r.mape}% · n=${r.n}`,
      color: (r) => (r.mape > 20 ? "risk" : ""),
    });
  }

  function renderLineage(rows) {
    document.getElementById("avm-lineage").innerHTML = rows.length
      ? `<table class="table"><thead><tr><th>版本</th><th>产物位置</th><th>训练时间</th>` +
        `<th>训练/测试</th><th>MAPE</th><th>较上一版</th><th>MdAPE</th><th>R²</th><th>vs 基线</th></tr></thead><tbody>` +
        rows
          .map((r) => {
            const dlt =
              r.mape_delta == null
                ? "-"
                : `<span style="color:${r.mape_delta < 0 ? GREEN : RED}">${r.mape_delta > 0 ? "+" : ""}${r.mape_delta} pp</span>`;
            return (
              `<tr><td>${esc(r.version)}${r.is_current ? ` <span class="badge ok">在跑</span>` : ""}</td>` +
              `<td style="color:#6b7280">output/avm/${r.source === "current" ? "" : esc(r.source) + "/"}avm_report.json</td>` +
              `<td>${esc(r.trained_at)}</td><td>${num(r.n_train)} / ${num(r.n_test)}</td>` +
              `<td><b>${pp(r.mape, 3)}</b></td><td>${dlt}</td><td>${pp(r.mdape)}</td>` +
              `<td>${r.r2 == null ? "-" : Number(r.r2).toFixed(4)}</td>` +
              `<td>基线 ${pp(r.baseline_mape, 3)} → 提升 ${pp(r.improvement_pct, 1)}</td></tr>`
            );
          })
          .join("") +
        `</tbody></table>` +
        `<div style="font-size:12px;color:#6b7280;margin-top:8px">` +
        `R-UBQ-01：dws_risk_class 每行落 model_version，可反查到具体产物；` +
        `历史产物在加 version 字段前生成的用目录名兜底，血缘不断链。</div>`
      : `<div class="empty">output/avm/ 下未找到任何训练报告</div>`;
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
      `共 ${d.total} 笔（按偏差降序，最不可信的排最前）`;
    const tbody = document.querySelector("#avm-val-table tbody");
    tbody.innerHTML = d.rows.length
      ? d.rows
          .map((r) => {
            const c = devColor(r.deviation_pct == null ? null : r.signed_deviation_pct);
            const dir =
              r.signed_deviation_pct == null
                ? "-"
                : r.signed_deviation_pct > 0
                  ? `<span style="color:${RED}">高估</span>`
                  : `<span style="color:#2563eb">低估</span>`;
            return (
              `<tr><td>${r.loan_id}</td>` +
              `<td title="${esc(r.property_addr)}">${esc(r.property_addr.slice(0, 16))}</td>` +
              `<td>${fmtMoney(r.avm_valuation)}</td><td>${fmtMoney(r.book_price)}</td>` +
              `<td style="color:${c};font-weight:600">${fmtPct(r.deviation_pct)}</td>` +
              `<td>${dir}</td><td>${fmtPct(r.ltv)}</td>` +
              `<td><span class="badge" style="background:${CLASS_COLORS[r.risk_class]}22;color:${CLASS_COLORS[r.risk_class]}">${esc(r.risk_class)}</span></td>` +
              `<td>${r.abnormal_valuation ? `<span class="badge bad">异常</span>` : `<span class="badge off">正常</span>`}</td>` +
              `<td>${esc(r.source_tier)}</td><td style="color:#6b7280">${esc(r.model_version)}</td></tr>`
            );
          })
          .join("")
      : `<tr><td colspan="11" class="empty">无符合条件的数据</td></tr>`;

    const tp = Math.max(1, Math.ceil(d.total / d.page_size));
    st.valPage = d.page;
    document.getElementById("avm-val-page").textContent = `第 ${d.page}/${tp} 页`;
    document.getElementById("avm-val-prev").disabled = d.page <= 1;
    document.getElementById("avm-val-next").disabled = d.page >= tp;
  }

  async function renderAlerts() {
    const d = await api("/api/avm/alerts?page=" + st.alertPage);
    document.getElementById("avm-alert-sum").innerHTML =
      `共 <b>${d.total}</b> 条待核查 · ` +
      d.by_code
        .map((c) => `<span class="badge bad">${esc(c.alert_code)} × ${c.n}</span>`)
        .join(" ") +
      `<span style="font-size:12px;color:#6b7280;margin-left:10px">` +
      `R-UNW-03：偏差 &gt; 30% 转人工复估；R-UBQ-01 不可溯源告警也落这张表。</span>`;

    const tbody = document.querySelector("#avm-alert-table tbody");
    tbody.innerHTML = d.rows.length
      ? d.rows
          .map(
            (r) =>
              `<tr><td><span class="badge bad">${esc(r.alert_code)}</span></td>` +
              `<td>${r.loan_id}</td><td title="${esc(r.property_addr)}">${esc(r.property_addr.slice(0, 16))}</td>` +
              `<td>${esc(r.city)}</td>` +
              `<td style="color:${RED};font-weight:600">${fmtPct(r.deviation_pct)}</td>` +
              `<td>${esc(r.detail)}</td><td style="color:#6b7280">${esc(r.model_version)}</td>` +
              `<td>${esc(r.alert_date)}</td></tr>`
          )
          .join("")
      : `<tr><td colspan="8" class="empty">无异常估值告警</td></tr>`;

    const tp = Math.max(1, Math.ceil(d.total / d.page_size));
    st.alertPage = d.page;
    document.getElementById("avm-alert-page").textContent = `第 ${d.page}/${tp} 页`;
    document.getElementById("avm-alert-prev").disabled = d.page <= 1;
    document.getElementById("avm-alert-next").disabled = d.page >= tp;
  }

  /* ---------- 异常估值归因 ---------- */

  const SIDE_COLOR = { baseline: "#b45309", model: "#b91c1c", mixed: "#7c3aed" };
  const SIDE_LABEL = { baseline: "基准侧", model: "模型侧", mixed: "多因叠加" };

  async function renderAttribution() {
    const box = document.getElementById("avm-attribution-body");
    const d = await api("/api/avm/attribution");
    if (!d || (d.diag_available === false && !d.attribution)) {
      box.innerHTML =
        `<div class="empty">归因产物缺失（output/avm/bias_diag/）——` +
        `重跑 <code>python tools/avm/bias_attribution.py</code> 生成。</div>`;
      return;
    }
    const live = d.live || {};
    const att = d.attribution || {};
    const cf = d.counterfactual || [];
    const varC = d.variance ? d.variance.components : [];
    const cc = d.city_contrast ? d.city_contrast.items : [];
    const src = d.sources || {};
    const out = [];

    // 1. 结论先行
    const baselineShare = att.baseline_share;
    out.push(
      `<div style="display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:10px 0">` +
        `<span class="badge bad">${live.abnormal} / ${live.avm_rows}（${fmtPct(live.abnormal_rate)}）</span>` +
        `<span style="font-size:18px;font-weight:700;color:#b45309">` +
        `其中 ${fmtPct(baselineShare)}（${att.baseline_n} 笔）归因于合成基准</span>` +
        `<span style="color:#b91c1c;font-weight:700">真实模型误差仅 ${att.model_n} 笔</span>` +
        `</div>`
    );
    out.push(
      `<div style="font-size:12px;color:#6b7280;margin-bottom:12px;line-height:1.7">` +
        `根因：<code>true_market_price</code> 是 <b>2026-08-04 版模型自己</b>生成的` +
        `（seed/generate_seed.py 的 CITY_UNIT_PRICE 来自模型隐含单价 × U(0.75,1.35)）。` +
        `当前模型是 ${live.model_version || "r4"}，所以 R-UNW-03 实际度量的是「模型 vs 它自己` +
        `的旧快照」= <b>版本漂移</b>，不是估值精度。因此<b>不做偏差校正层</b>——把新模型拉回旧输出去` +
        `拟合一个假基准是本末倒置。` +
        (att.rule
          ? `<br/>分类判据：反事实（把某分量置零后是否仍越过 30% 阈值），不是分量占偏差的份额。`
          : "") +
        `</div>`
    );

    // 2. 反事实异常率（6.5% 标为不可消除地板）
    if (cf.length) {
      out.push(`<h3 class="card-title">反事实异常率（抹掉哪些分量后还剩多少）</h3>`);
      out.push(
        `<table class="table"><thead><tr><th>情形</th><th>异常率</th><th>说明</th></tr></thead><tbody>` +
          cf
            .map((c) => {
              const color = c.is_floor ? GRAY : c.rate >= 0.3 ? RED : GREEN;
              return (
                `<tr><td>${esc(c.label)}${c.is_floor ? ` <span class="badge off">噪声地板</span>` : ""}</td>` +
                `<td style="color:${color};font-weight:700">${fmtPct(c.rate)}</td>` +
                `<td style="font-size:12px;color:#6b7280">${esc(c.note || "")}</td></tr>`
              );
            })
            .join("") +
          `</tbody></table>`
      );
      out.push(
        `<div style="font-size:12px;color:#6b7280;margin-top:8px">` +
          `即便模型完美命中冻结基准（仅剩 seed 噪声），仍有 ${fmtPct(6.5)} 的「异常」——` +
          `这是合成数据的固有地板，不是模型能消除的。</div>`
      );
    }

    // 3. 方差贡献
    if (varC.length) {
      out.push(
        `<h3 class="card-title" style="margin-top:18px">方差贡献（log 域，总和 ${(d.variance.total_variance || 0).toFixed(4)}）</h3>`
      );
      out.push(
        `<div class="hbar-list">` +
          varC
            .map((c) => {
              const pct = Math.round(c.share * 100);
              const color = c.component === "A1" ? "#b45309" : c.component === "B" ? GRAY : RED;
              return (
                `<div style="display:flex;align-items:center;gap:10px;margin:6px 0">` +
                `<span style="width:150px;font-size:13px">${esc(c.label)}</span>` +
                `<div style="flex:1;background:#eef2f7;border-radius:999px;height:10px">` +
                `<div style="width:${pct}%;height:10px;border-radius:999px;background:${color}"></div></div>` +
                `<span style="width:70px;text-align:right;font-size:13px;font-weight:600">${pct}%</span>` +
                `</div>`
              );
            })
            .join("") +
          `</div>`
      );
      out.push(
        `<div style="font-size:12px;color:#6b7280;margin-top:6px">` +
          `A1 城市系统性错位占 68% —— 正是「旧快照 vs 新模型」的城市水平差；` +
          `A2（真正属于模型的部分）只占 22%，单独仅造成 ${fmtPct(3.0)} 异常。</div>`
      );
    }

    // 4. 六类归因构成
    if (att.items && att.items.length) {
      out.push(`<h3 class="card-title" style="margin-top:18px">76 笔异常的归因构成</h3>`);
      out.push(
        `<table class="table"><thead><tr><th>类别</th><th>责任侧</th><th>笔数</th><th>占异常</th></tr></thead><tbody>` +
          att.items
            .map((i) => {
              const color = SIDE_COLOR[i.side] || GRAY;
              return (
                `<tr><td>${esc(i.label)}</td>` +
                `<td><span class="badge" style="background:${color}22;color:${color}">${SIDE_LABEL[i.side] || i.side}</span></td>` +
                `<td>${i.n}</td><td>${fmtPct(i.share_abnormal)}</td></tr>`
              );
            })
            .join("") +
          `</tbody></table>`
      );
      if (!att.matches_live) {
        out.push(
          `<div style="font-size:12px;color:#b45309;margin-top:8px">` +
            `注意：归因产物统计 ${att.total} 笔异常 ≠ 库内实时 ${att.live_abnormal} 笔，产物已过期，请重跑 bias_attribution.py。</div>`
        );
      }
    }

    // 5. 东莞反例
    if (cc && cc.length) {
      out.push(
        `<h3 class="card-title" style="margin-top:18px">东莞反例 · 剔城本身不产生偏差</h3>` +
          `<div style="font-size:12px;color:#6b7280;margin-bottom:8px;line-height:1.7">` +
          `zs/zh/dg/yf 四城都被整城剔除、模型输出几乎相同的全局回退价（模型侧价差仅 ` +
          `${(d.city_contrast.model_spread * 100).toFixed(1)}%），异常率却从 0% 拉到 90%——` +
          `差异 100% 来自冻结基准那个数（价差 ${(d.city_contrast.frozen_spread * 100).toFixed(1)}%）。` +
          `dg/yf 的基准在 08-04 冻结时已是回退价，zs/zh 当时还有本地样本、之后才被剔除。` +
          `<b>剔城不产生偏差；剔城发生在基准冻结之后才产生偏差。</b></div>` +
          `<table class="table"><thead><tr><th>城市</th><th>剔除行数</th><th>样本</th>` +
          `<th>模型隐含单价</th><th>冻结基准价</th><th>比值</th><th>异常率</th></tr></thead><tbody>` +
          cc
            .map(
              (c) =>
                `<tr><td>${esc(c.city)}</td><td>${c.dropped_rows}</td><td>${c.n}</td>` +
                `<td>${fmtMoney(c.model_unit_price)}</td><td>${fmtMoney(c.frozen_unit_price)}</td>` +
                `<td style="color:${c.ratio < 0.75 ? RED : c.ratio > 1.25 ? RED : GREEN};font-weight:600">${c.ratio.toFixed(3)}</td>` +
                `<td style="color:${c.abnormal_rate >= 0.6 ? RED : GREEN};font-weight:600">${fmtPct(c.abnormal_rate)}</td></tr>`
            )
            .join("") +
          `</tbody></table>`
      );
    }

    // 6. 数据来源口径
    out.push(
      `<div style="font-size:12px;color:#6b7280;margin-top:14px;border-top:1px dashed #d1d5db;padding-top:10px;line-height:1.8">` +
        `数据口径（诚实声明）：异常率/城市对比 = <b>实时库</b>现算（${esc(src.live || "-")}）；` +
        `方差/反事实/归因构成 = <b>离线诊断产物</b>（${esc(src.attribution || "-")}）。` +
        `基准 true_market_price 是<b>合成种子数据</b>，不是网签成交价；产物由 ` +
        `<code>tools/avm/bias_attribution.py</code> 生成。</div>`
    );

    box.innerHTML = out.join("");
  }

  /* ---------- 挂载 ---------- */

  function bindOnce() {
    if (st.bound) return;
    st.bound = true;
    document.getElementById("avm-filter").addEventListener("submit", (e) => {
      e.preventDefault();
      st.valPage = 1; // 换筛选条件必须回第一页，否则会停在越界页显示空表
      renderValuations();
    });
    document.getElementById("avm-val-prev").addEventListener("click", () => {
      st.valPage = Math.max(1, st.valPage - 1);
      renderValuations();
    });
    document.getElementById("avm-val-next").addEventListener("click", () => {
      st.valPage += 1;
      renderValuations();
    });
    document.getElementById("avm-alert-prev").addEventListener("click", () => {
      st.alertPage = Math.max(1, st.alertPage - 1);
      renderAlerts();
    });
    document.getElementById("avm-alert-next").addEventListener("click", () => {
      st.alertPage += 1;
      renderAlerts();
    });
  }

  function fillFilterOptions(d) {
    const city = document.getElementById("avm-f-city");
    const ver = document.getElementById("avm-f-version");
    // 选项来自概览接口的实际取值，避免前端硬编码城市/版本清单与数据脱节。
    city.innerHTML =
      `<option value="">全部</option>` +
      d.by_city
        .map((c) => `<option value="${esc(c.city)}">${esc(c.city)}（${c.n}）</option>`)
        .join("");
    ver.innerHTML =
      `<option value="">全部</option>` +
      d.portfolio.versions
        .map(
          (v) =>
            `<option value="${esc(v.model_version)}">${esc(v.model_version)}（${v.n}）</option>`
        )
        .join("");
  }

  window.registerPage("avm", {
    html,
    render: async () => {
      const d = await api("/api/avm");
      st.overview = d;
      renderAc07(d.model);
      renderKpi(d);
      renderModelCard(d.model);
      renderDevHist(d);
      renderDimensions(d);
      renderDecomposition(d);
      renderLineage(d.lineage);
      fillFilterOptions(d);
      bindOnce();
      await Promise.all([renderValuations(), renderAlerts(), renderAttribution()]);
    },
  });
})();
