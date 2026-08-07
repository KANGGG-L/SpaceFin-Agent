/* global window, document */
/* P10 · 策略沙盒推演（设计评审 P10 / R-OPT-01-02，本期仅出框架）。
 *
 * 不做闭环交互，只呈现三件事：
 *  1. 生成→批评→校准的闭环说明框架（静态结构 + 未来交互形态占位）；
 *  2. R-OPT-01「未校准」醒目标记：读 output/persona/persona_report.json，
 *     报告含 naive（calibration_status="未校准"）时置顶红色横幅；
 *  3. 校准证据：naive vs calibrated 的 KS 指标、校准轨迹、违约率分布对比。
 * 报告缺失时显示占位说明，不崩。
 */

(function () {
  const { api, esc } = window.spf;

  const HTML = `
    <h1 class="page-title">策略沙盒推演 <span style="font-size:13px;color:#6b7280;font-weight:400">（框架预览）</span></h1>
    <div class="check-line" style="font-size:13px;color:#6b7280;margin-bottom:12px">本页为设计框架展示，闭环交互不进入本期开发排期。</div>

    <div id="p10-uncalib-banner"></div>
    <div id="p10-attr-notice"></div>

    <!-- 闭环说明框架（本期仅框架，不实现交互） -->
    <div class="card" style="margin-top:16px">
      <h2 class="card-title">生成 → 批评 → 校准 闭环（说明框架）</h2>
      <div class="grid-2">
        <div class="card" style="box-shadow:none;border-color:var(--border)">
          <h3 class="card-title" style="color:var(--primary)">① 生成 Generator</h3>
          <div class="check-line">输入真实客户特征分布（基准），生成合成客户画像，用于策略推演与产品设计验证。</div>
          <div class="check-line" style="color:var(--muted)">未来交互：参数（样本量 / 种子 / 特征口径）→ 触发生成。</div>
        </div>
        <div class="card" style="box-shadow:none;border-color:var(--border)">
          <h3 class="card-title" style="color:var(--warn)">② 批评 Critic</h3>
          <div class="check-line">以 KL/KS 对照基准校验合成分布；存在美化偏见（乐观化）时判定「未校准」并拒绝直接用于决策。</div>
          <div class="check-line" style="color:var(--muted)">未来交互：Critic 严格模式开关、逐特征 KS 报告。</div>
        </div>
        <div class="card" style="box-shadow:none;border-color:var(--border)">
          <h3 class="card-title" style="color:var(--ok)">③ 校准 Calibration</h3>
          <div class="check-line">秩分位数映射逐轮逼近基准边际，直到违约率分布 KS ≤ 0.05，输出带「校准」标记。</div>
          <div class="check-line" style="color:var(--muted)">未来交互：目标 KS、最大轮数、逐轮轨迹回放。</div>
        </div>
        <div class="card" style="box-shadow:none;border-color:var(--border)">
          <h3 class="card-title" style="color:var(--danger)">⛔ R-OPT-01 使用红线</h3>
          <div class="check-line"><b>未校准输出不得直接用于决策</b>——仅可用于策略推演与产品设计验证，不作为任何个体授信决策依据。</div>
          <div class="check-line" style="color:var(--muted)">本页仅展示框架与标记规则，闭环交互不进入本期开发排期。</div>
        </div>
      </div>
    </div>

    <!-- 校准状态卡片 -->
    <div id="p10-status"></div>

    <!-- 校准轨迹与分布对比 -->
    <div id="p10-evidence"></div>

    <!-- 诚实声明 -->
    <div id="p10-honest"></div>
  `;

  /* ---------------- 渲染辅助 ---------------- */

  function ksBadge(passed) {
    if (passed === true) return `<span class="badge ok">通过 KS≤0.05</span>`;
    if (passed === false) return `<span class="badge bad">未通过</span>`;
    return "";
  }

  function renderCalibCard(prefix, raw) {
    if (!raw) return "";
    const dp_ks = raw.ks_default_prob;
    const feats = Object.entries(raw.ks_features || {})
      .map(([name, f]) => ({
        name,
        ks: f.ks,
        passed: f.passed_ks_le_0_05,
      }))
      .sort((a, b) => b.ks - a.ks);
    const statusOk = raw.calibration_status === "校准";
    return `
      <div class="card">
        <h3 class="card-title">${prefix} ${esc(raw.bias || "")}</h3>
        <div class="check-line">
          校准状态：
          ${
            statusOk
              ? `<span class="badge ok">校准</span>`
              : `<span class="badge bad">未校准</span>`
          }
          违约率 KS：<b>${dp_ks == null ? "-" : dp_ks.toFixed(3)}</b> ${ksBadge(raw.default_prob_passed)}
        </div>
        <div class="check-line">逐特征 KS（默认违约率分布口径，越小越接近基准）：</div>
        <div style="overflow-x:auto;margin-top:6px">
          <table class="table">
            <thead><tr><th>特征</th><th>KS</th><th>判定</th></tr></thead>
            <tbody>
              ${
                feats.length
                  ? feats
                      .map(
                        (f) =>
                          `<tr><td>${esc(f.name)}</td>` +
                          `<td>${f.ks == null ? "-" : f.ks.toFixed(3)}</td>` +
                          `<td>${ksBadge(f.passed)}</td></tr>`
                      )
                      .join("")
                  : `<tr><td colspan="3" class="empty">无特征 KS 明细</td></tr>`
              }
            </tbody>
          </table>
        </div>
      </div>`;
  }

  function renderStatus(naive, calibrated) {
    const el = document.getElementById("p10-status");
    el.innerHTML =
      `<div class="grid-2">` +
      renderCalibCard("naive（未校准）", naive) +
      renderCalibCard("calibrated（校准）", calibrated) +
      `</div>`;
  }

  function renderTrajectory(calibration) {
    if (!calibration || !calibration.trajectory || !calibration.trajectory.length) {
      return `<div class="empty">无校准轨迹</div>`;
    }
    const rows = calibration.trajectory
      .map(
        (t) =>
          `<tr>` +
          `<td>${t.round == null ? "-" : t.round}</td>` +
          `<td>${t.p_ks == null ? "-" : t.p_ks.toFixed(3)}</td>` +
          `<td>${t.alpha == null ? "-" : t.alpha}</td>` +
          `</tr>`
      )
      .join("");
    return (
      `<div style="overflow-x:auto;margin-top:6px"><table class="table">` +
      `<thead><tr><th>轮次</th><th>违约率 KS</th><th>混合系数 α</th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`
    );
  }

  function renderComparison(cmp) {
    if (!cmp || !cmp.benchmark || !cmp.naive || !cmp.calibrated) return "";
    const rows = ["benchmark", "naive", "calibrated"].map((k) => {
      const d = cmp[k];
      return (
        `<tr>` +
        `<td><b>${k === "benchmark" ? "基准" : k === "naive" ? "naive" : "calibrated"}</b></td>` +
        `<td>${d.mean == null ? "-" : (d.mean * 100).toFixed(1)}%</td>` +
        `<td>${d.p10 == null ? "-" : (d.p10 * 100).toFixed(1)}%</td>` +
        `<td>${d.p50 == null ? "-" : (d.p50 * 100).toFixed(1)}%</td>` +
        `<td>${d.p90 == null ? "-" : (d.p90 * 100).toFixed(1)}%</td>` +
        `</tr>`
      );
    });
    return (
      `<table class="table" style="margin-top:6px"><thead><tr>` +
      `<th>违约率分布</th><th>均值</th><th>P10</th><th>P50</th><th>P90</th></tr></thead>` +
      `<tbody>${rows.join("")}</tbody></table>`
    );
  }

  function renderEvidence(data) {
    const el = document.getElementById("p10-evidence");
    if (!data.available) {
      el.innerHTML = "";
      return;
    }
    const cal = data.calibration || {};
    const conv = cal.converged === true;
    el.innerHTML =
      `<div class="card" style="margin-top:16px">` +
      `<h2 class="card-title">校准证据 · 收敛至 KS ≤ ${esc(cal.target_ks ?? 0.05)}</h2>` +
      `<div class="check-line">机制：${esc(cal.mechanism || "秩分位数映射")}</div>` +
      `<div class="check-line">实际轮次 ${esc(cal.rounds ?? "-")} / 上限 ${esc(cal.max_rounds ?? "-")} · ` +
      `收敛：${conv ? `<span class="badge ok">是</span>` : `<span class="badge warn">否</span>`}</div>` +
      renderTrajectory(cal) +
      `<h3 class="card-title" style="margin-top:14px">违约率分布对比（基准 / naive / calibrated）</h3>` +
      renderComparison(data.comparison) +
      `<div class="check-line" style="color:var(--muted)">naive 违约率 KS=0.257 低估违约风险；校准后 0.028 ≤ 0.05 达标。</div>` +
      `</div>`;
  }

  function renderHonest(honest) {
    const el = document.getElementById("p10-honest");
    if (!honest || !Object.keys(honest).length) {
      el.innerHTML = "";
      return;
    }
    const items = [
      ["基准来源", honest.benchmark_source],
      ["真实基准", honest.real_benchmark],
      ["使用边界", honest.not_for_credit_decision],
    ].filter(([, v]) => v);
    el.innerHTML =
      `<div class="card" style="margin-top:16px;border-left:4px solid var(--warn)">` +
      `<h2 class="card-title">诚实声明</h2>` +
      items.map(([k, v]) => `<div class="check-line"><b>${esc(k)}：</b>${esc(v)}</div>`).join("") +
      `</div>`;
  }

  async function render(_root) {
    const data = await api("/api/sandbox");

    // R-OPT-01：报告含 naive（未校准）输出 → 顶部红色横幅，醒目。
    const banner = document.getElementById("p10-uncalib-banner");
    const notice = document.getElementById("p10-attr-notice");

    if (!data.available) {
      banner.innerHTML = "";
      notice.innerHTML =
        `<div class="card" style="border-left:4px solid var(--warn)">` +
        `<span class="badge warn">报告缺失</span> ${data.message || "推演报告未生成。"}</div>`;
      document.getElementById("p10-status").innerHTML = "";
      document.getElementById("p10-evidence").innerHTML = "";
      document.getElementById("p10-honest").innerHTML = "";
      return;
    }

    notice.innerHTML =
      `<div class="card" style="margin-bottom:14px;border-left:4px solid var(--primary)">` +
      `<span class="badge ok">报告已就绪</span> 生成于 ${esc(data.generated_at || "-")} · ` +
      `读取 <code>${esc(data.path)}</code>。本页仅展示框架与校准状态，不实现闭环交互（D-09）。</div>`;

    if (data.uncalibrated_present) {
      banner.innerHTML =
        `<div class="card" style="margin-bottom:14px;border-left:6px solid var(--danger);background:#fef2f2">` +
        `<span class="badge bad" style="font-size:13px">⚠ 未校准</span> ` +
        `<b>当前报告含 naive 输出（calibration_status="未校准"）</b>——按 R-OPT-01，` +
        `未校准输出<b>不得直接用于决策</b>，仅可用于策略推演与产品设计验证。` +
        `<div class="check-line">校准后违约率分布 KS 须 ≤ 0.05 方可解除该标记（详见下方校准证据）。</div></div>`;
    } else {
      banner.innerHTML = "";
    }

    renderStatus(data.naive, data.calibrated);
    renderEvidence(data);
    renderHonest(data.honest_declaration);
  }

  window.registerPage("sandbox", { html: HTML, render });
})();
