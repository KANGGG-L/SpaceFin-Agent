/* global window, document */
/* P6 空间惩罚项配置（US-01）· 前端模块。
 *
 * 页面逻辑围绕一条主线：**选区域 → 定阈值 → 试算影响 → 确认保存**。
 * 「试算影响」不是装饰——策略配置一旦落库就会进审批链路，风控经理必须先看到
 * 「会波及多少存量、多少笔从通过翻成拒贷」才敢按保存，这是 PRD §5.3 策略沙盒的雏形。
 *
 * 整个文件包在 IIFE 里：插件 js 是普通 <script> 注入（非 ES module），
 * 顶层 const 会挂到全局，与 app.js 的 `const state` 直接重名报错。
 */
(function () {
  const { api, esc, fmtMoney, fmtPct, fmtLtv, CLASS_COLORS } = window.spf;

  // 模块内状态：列表/草案数据缓存 + 最近一次试算结果。
  const st = { data: null, preview: null, suggests: null, bound: false };

  const ACTION_BADGE = { reject: "bad", strict: "warn", notice: "off" };

  /* ---------------- 骨架 ---------------- */

  const html = `
    <h1 class="page-title">空间惩罚项配置</h1>

    <div class="card">
      <h2 class="card-title">规则编辑 · 高危网格 → 加严审批 / 拒贷（US-01）</h2>
      <form id="p6-form" class="filter-bar">
        <label>粒度
          <select id="p6-scope-type">
            <option value="zone">空间网格</option>
            <option value="city">城市</option>
          </select>
        </label>
        <label>作用域
          <select id="p6-scope-value"></select>
        </label>
        <label>惩罚动作
          <select id="p6-action"></select>
        </label>
        <label>LTV 上限
          <input id="p6-ltv-cap" type="number" step="0.01" min="0.01" max="2" placeholder="0.70" />
        </label>
        <label>启用
          <select id="p6-enabled">
            <option value="1">启用</option>
            <option value="0">停用</option>
          </select>
        </label>
        <label style="flex:1 1 240px">配置理由
          <input id="p6-reason" type="text" maxlength="255" placeholder="如：价格洼地网格，抵押物变现折价风险高" />
        </label>
        <button type="submit" class="btn btn-ghost">试算影响</button>
        <button type="button" id="p6-save" class="btn">保存生效</button>
        <button type="button" id="p6-reset" class="btn btn-ghost btn-sm">清空</button>
      </form>
      <div id="p6-form-msg" class="check-line"></div>
    </div>

    <div class="card">
      <h2 class="card-title">命中影响预览 · 保存前先看后果</h2>
      <div id="p6-preview"></div>
    </div>

    <div class="card">
      <h2 class="card-title">已配置规则</h2>
      <table class="table" id="p6-policy-table">
        <thead><tr>
          <th>作用域</th><th>动作</th><th>LTV 上限</th><th>区域内存量</th>
          <th>当前命中</th><th>其中新增</th><th>状态</th><th>配置人</th><th>更新时间</th><th>操作</th>
        </tr></thead>
        <tbody><tr><td colspan="10" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>

    <div class="card">
      <h2 class="card-title">高危网格建议规则 · 一键生成</h2>
      <div class="filter-bar">
        <button type="button" id="p6-gen" class="btn btn-ghost">生成建议草案</button>
        <button type="button" id="p6-adopt-all" class="btn btn-ghost btn-sm">全部采纳（有命中的）</button>
        <span class="filter-total" id="p6-suggest-total"></span>
      </div>
      <div id="p6-suggest"></div>
    </div>
  `;

  /* ---------------- 渲染 ---------------- */

  function el(id) {
    return document.getElementById(id);
  }

  function fillScopeOptions() {
    const type = el("p6-scope-type").value;
    const sel = el("p6-scope-value");
    const cur = sel.value;
    if (type === "city") {
      sel.innerHTML = st.data.cities
        .map(
          (c) =>
            `<option value="${esc(c.city)}">${esc(c.city_name)}（存量 ${c.cover.loans} 笔）</option>`
        )
        .join("");
    } else {
      // 高危网格排在最前并打标——配置动作绝大多数从这里起步。
      sel.innerHTML = st.data.zones
        .map((z) => {
          const tag = z.is_high_risk_zone ? "【高危】" : "";
          return `<option value="${esc(z.zone_id)}">${tag}${esc(z.city_name || z.zone_type)} · ${esc(z.zone_id)}（覆盖 ${z.cover.loans} 笔）</option>`;
        })
        .join("");
    }
    if (cur && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  }

  function readForm() {
    const cap = el("p6-ltv-cap").value;
    return {
      scope_type: el("p6-scope-type").value,
      scope_value: el("p6-scope-value").value,
      action: el("p6-action").value,
      ltv_cap: cap === "" ? null : Number(cap),
      reason: el("p6-reason").value,
      enabled: el("p6-enabled").value === "1",
    };
  }

  function writeForm(rule) {
    el("p6-scope-type").value = rule.scope_type;
    fillScopeOptions();
    el("p6-scope-value").value = rule.scope_value;
    el("p6-action").value = rule.action;
    el("p6-ltv-cap").value = rule.ltv_cap == null ? "" : rule.ltv_cap;
    el("p6-reason").value = rule.reason || "";
    el("p6-enabled").value = rule.enabled === 0 ? "0" : "1";
  }

  function msg(text, bad) {
    const box = el("p6-form-msg");
    box.textContent = text || "";
    box.style.color = bad ? "#b02a2a" : "#2e9e5b";
  }

  function kpi(label, value, sub) {
    return (
      `<div class="kpi"><div class="kpi-label">${esc(label)}</div>` +
      `<div class="kpi-value">${value}</div><div class="kpi-sub">${esc(sub)}</div></div>`
    );
  }

  function renderPreview(p) {
    const box = el("p6-preview");
    if (!p) {
      box.innerHTML = `<div class="empty">选择区域与 LTV 上限后点「试算影响」，这里会显示该规则对存量组合的影响。</div>`;
      return;
    }
    const cap = p.ltv_cap == null ? "整区命中" : fmtLtv(p.ltv_cap);
    let out =
      `<div class="check-line">规则：<b>${esc(p.scope_label)}</b> · ${esc(p.action_label)} · LTV 上限 <b>${cap}</b>` +
      `（全局红线 ${fmtLtv(p.ltv_red_line)}）</div>` +
      `<div class="kpi-row">` +
      kpi("区域内存量", p.in_scope.loans + " 笔", fmtMoney(p.in_scope.balance)) +
      kpi("命中规则", p.hit.loans + " 笔", fmtMoney(p.hit.balance)) +
      kpi("其中新增受限", p.newly_hit.loans + " 笔", "原本通过 → " + p.action_label) +
      kpi("原已超红线", p.already_alert.loans + " 笔", "本就在 LTV 预警内") +
      kpi("命中余额占比", fmtPct(p.hit_balance_pct), "占区域内总余额") +
      kpi("低置信笔数", p.low_confidence + " 笔", "R-UNW-01 建议人工复核") +
      `</div>`;

    if (p.newly_hit.loans > 0) {
      out +=
        `<div class="check-line" style="color:#b02a2a">⚠ 该规则会让 <b>${p.newly_hit.loans}</b> 笔` +
        `（${fmtMoney(p.newly_hit.balance)}）原本通过的存量贷款转为「${esc(p.action_label)}」，请确认后再保存。</div>`;
    } else if (p.hit.loans > 0) {
      out += `<div class="check-line">命中的都是已超红线的存量，规则只提高处置等级，不新增受限笔数。</div>`;
    } else {
      out += `<div class="check-line">该规则当前不命中任何存量贷款（仅对新增申请生效）。</div>`;
    }

    if (p.by_class.length) {
      out +=
        `<div class="hbar-list">` +
        p.by_class
          .map((c) => {
            const max = Math.max(...p.by_class.map((x) => x.loans), 1);
            const w = ((c.loans / max) * 100).toFixed(1);
            const color = CLASS_COLORS[c.risk_class] || "#2563eb";
            return (
              `<div class="hbar-row"><div class="hbar-label">${esc(c.risk_class)}</div>` +
              `<div class="hbar-track"><div class="hbar-fill" style="width:${w}%;background:${color}"></div></div>` +
              `<div class="hbar-num">${c.loans} 笔 · ${fmtMoney(c.balance)}</div></div>`
            );
          })
          .join("") +
        `</div>`;
    }

    if (p.rows.length) {
      out +=
        `<table class="table"><thead><tr><th>贷款号</th><th>LTV</th><th>余额</th><th>五级</th>` +
        `<th>抵押物地址</th><th>状态变化</th></tr></thead><tbody>` +
        p.rows
          .map(
            (r) =>
              `<tr><td>${r.loan_id}</td><td><b>${fmtLtv(r.ltv)}</b></td><td>${fmtMoney(r.balance)}</td>` +
              `<td><span class="badge" style="background:${CLASS_COLORS[r.risk_class]}22;color:${CLASS_COLORS[r.risk_class]}">${esc(r.risk_class)}</span></td>` +
              `<td>${esc(r.addr)}</td>` +
              `<td>${r.change.startsWith("通过") ? `<span class="badge bad">${esc(r.change)}</span>` : `<span class="badge warn">${esc(r.change)}</span>`}` +
              `${r.low_confidence ? ` <span class="badge off">低置信</span>` : ""}</td></tr>`
          )
          .join("") +
        `</tbody></table>`;
      if (p.rows_truncated) {
        out += `<div class="check-line">另有 ${p.rows_truncated} 笔命中未展示（按 LTV 降序仅列前 50）。</div>`;
      }
    }
    box.innerHTML = out;
  }

  function renderPolicies() {
    const tbody = document.querySelector("#p6-policy-table tbody");
    const rows = st.data.policies;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">尚未配置任何空间惩罚项</td></tr>`;
      return;
    }
    const canWrite = st.data.can_write;
    tbody.innerHTML = rows
      .map((p) => {
        const ops = canWrite
          ? `<button class="btn btn-sm btn-ghost p6-edit" data-id="${p.policy_id}">编辑</button> ` +
            `<button class="btn btn-sm btn-danger p6-del" data-id="${p.policy_id}">删除</button>`
          : `<span class="badge off">只读</span>`;
        return (
          `<tr><td>${esc(p.scope_label)}</td>` +
          `<td><span class="badge ${ACTION_BADGE[p.action] || ""}">${esc(p.action_label)}</span></td>` +
          `<td>${p.ltv_cap == null ? "-" : fmtLtv(p.ltv_cap)}</td>` +
          `<td>${p.impact.in_scope.loans} 笔 · ${fmtMoney(p.impact.in_scope.balance)}</td>` +
          `<td><b>${p.impact.hit.loans}</b> 笔 · ${fmtMoney(p.impact.hit.balance)}</td>` +
          `<td>${p.impact.newly_hit.loans} 笔${p.impact.low_confidence ? ` <span class="badge off">低置信 ${p.impact.low_confidence}</span>` : ""}</td>` +
          `<td>${p.enabled ? `<span class="badge ok">生效中</span>` : `<span class="badge off">已停用</span>`}</td>` +
          `<td>${esc(p.updated_by || p.created_by)}</td>` +
          `<td>${esc(p.updated_at || p.created_at || "-")}</td>` +
          `<td>${ops}</td></tr>`
        );
      })
      .join("");
  }

  function renderSuggests() {
    const box = el("p6-suggest");
    if (!st.suggests) {
      box.innerHTML = `<div class="empty">点「生成建议草案」，系统会为 ads_spatial_zone 中 is_high_risk_zone=1 的网格自动拟定规则（仅草案，需确认后落库）。</div>`;
      el("p6-suggest-total").textContent = "";
      return;
    }
    const ds = st.suggests.drafts;
    el("p6-suggest-total").textContent =
      `共 ${ds.length} 个高危网格 · 建议 LTV 上限 ${st.suggests.suggest_ltv_cap}`;
    if (!ds.length) {
      box.innerHTML = `<div class="empty">当前没有高危网格</div>`;
      return;
    }
    const canWrite = st.data.can_write;
    box.innerHTML =
      `<table class="table"><thead><tr><th>网格</th><th>高危成因</th><th>与城市中位偏差</th>` +
      `<th>建议动作</th><th>LTV 上限</th><th>区域内存量</th><th>命中 / 新增</th><th>操作</th></tr></thead><tbody>` +
      ds
        .map((d, i) => {
          const ops = canWrite
            ? `<button class="btn btn-sm btn-ghost p6-adopt" data-i="${i}">${d.exists ? "覆盖采纳" : "采纳"}</button>`
            : `<span class="badge off">只读</span>`;
          return (
            `<tr><td>${esc(d.scope_label)}</td>` +
            `<td><span class="badge bad">${esc(d.high_risk_rule || "-")}</span></td>` +
            `<td>${d.price_dev_vs_city == null ? "-" : fmtPct(d.price_dev_vs_city)}</td>` +
            `<td>${esc(d.action_label)}</td><td>${fmtLtv(d.ltv_cap)}</td>` +
            `<td>${d.impact.in_scope.loans} 笔 · ${fmtMoney(d.impact.in_scope.balance)}</td>` +
            `<td><b>${d.impact.hit.loans}</b> / ${d.impact.newly_hit.loans} 笔</td>` +
            `<td>${ops}</td></tr>`
          );
        })
        .join("") +
      `</tbody></table>`;
  }

  /* ---------------- 交互 ---------------- */

  async function reload() {
    st.data = await api("/api/policy");
    el("p6-action").innerHTML = st.data.actions
      .map((a) => `<option value="${esc(a.value)}">${esc(a.label)}</option>`)
      .join("");
    fillScopeOptions();
    if (!el("p6-ltv-cap").value) el("p6-ltv-cap").value = st.data.thresholds.suggest_ltv_cap;
    // 只读角色（DA）隐藏写按钮；真正的拦截在服务端 handler（403），这里只是 UX 裁剪。
    const canWrite = st.data.can_write;
    el("p6-save").style.display = canWrite ? "" : "none";
    el("p6-adopt-all").style.display = canWrite ? "" : "none";
    renderPolicies();
    renderSuggests();
  }

  async function doPreview() {
    st.preview = await api("/api/policy/preview", {
      method: "POST",
      body: JSON.stringify(readForm()),
    });
    renderPreview(st.preview);
    msg("试算完成，确认影响后再点「保存生效」");
  }

  async function doSave(rule) {
    const res = await api("/api/policy", { method: "POST", body: JSON.stringify(rule) });
    st.preview = res.impact;
    renderPreview(st.preview);
    await reload();
    return res;
  }

  function bind(root) {
    if (st.bound) return;
    st.bound = true;

    el("p6-scope-type").addEventListener("change", fillScopeOptions);

    el("p6-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await doPreview();
      } catch (err) {
        msg(String(err.message || err), true);
      }
    });

    el("p6-save").addEventListener("click", async () => {
      const rule = readForm();
      try {
        const res = await doSave(rule);
        msg(
          `已保存（policy_id=${res.policy_id}）：命中 ${res.impact.hit.loans} 笔，其中新增受限 ${res.impact.newly_hit.loans} 笔`
        );
      } catch (err) {
        msg(String(err.message || err), true);
      }
    });

    el("p6-reset").addEventListener("click", () => {
      el("p6-reason").value = "";
      el("p6-ltv-cap").value = st.data.thresholds.suggest_ltv_cap;
      st.preview = null;
      renderPreview(null);
      msg("");
    });

    el("p6-gen").addEventListener("click", async () => {
      st.suggests = await api("/api/policy/suggest");
      renderSuggests();
    });

    el("p6-adopt-all").addEventListener("click", async () => {
      if (!st.suggests) return;
      // 只采纳「有存量命中」的草案：0 命中的网格配了也只是占位，先不污染规则表。
      const targets = st.suggests.drafts.filter((d) => d.impact.hit.loans > 0);
      if (!targets.length) {
        msg("没有命中存量的高危网格草案，未做任何写入", true);
        return;
      }
      let ok = 0;
      for (const d of targets) {
        try {
          await doSave(d);
          ok += 1;
        } catch {
          /* 单条失败不阻断其余草案，最终计数会体现 */
        }
      }
      st.suggests = await api("/api/policy/suggest");
      renderSuggests();
      msg(`已采纳 ${ok}/${targets.length} 条高危网格建议规则`);
    });

    // 列表与草案表的按钮用事件委托，避免每次重绘重复绑定。
    root.addEventListener("click", async (e) => {
      const edit = e.target.closest(".p6-edit");
      if (edit) {
        const p = st.data.policies.find((x) => x.policy_id === Number(edit.dataset.id));
        if (p) {
          writeForm(p);
          msg(
            `已载入规则 #${p.policy_id}，修改后点「试算影响」再保存（同一区域重复保存为覆盖更新）`
          );
        }
        return;
      }
      const del = e.target.closest(".p6-del");
      if (del) {
        try {
          await api("/api/policy/delete", {
            method: "POST",
            body: JSON.stringify({ policy_id: Number(del.dataset.id) }),
          });
          await reload();
          msg(`已删除规则 #${del.dataset.id}`);
        } catch (err) {
          msg(String(err.message || err), true);
        }
        return;
      }
      const adopt = e.target.closest(".p6-adopt");
      if (adopt) {
        const d = st.suggests.drafts[Number(adopt.dataset.i)];
        try {
          writeForm(d);
          const res = await doSave(d);
          st.suggests = await api("/api/policy/suggest");
          renderSuggests();
          msg(`已采纳建议（policy_id=${res.policy_id}）：${d.scope_label}`);
        } catch (err) {
          msg(String(err.message || err), true);
        }
      }
    });
  }

  window.registerPage("policy", {
    html,
    render: async (root) => {
      await reload();
      bind(root);
      renderPreview(st.preview);
    },
  });
})();
