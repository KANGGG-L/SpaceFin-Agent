/* global window, document */
/* P1 · 数据底座接入配置（设计评审 R-EVT-01 / 阶段2 L0，面向数据工程师）。
 *
 * 页面回答三件事：接了哪些源、每层数据新不新鲜、CDC 通不通。
 * 视觉与主框架一致：只用 style.css 已有的 .card/.table/.badge/.kpi 等类，
 * 本页特有的（分级徽章配色、血缘流向卡片）走内联样式——插件页不改全局 css，
 * 否则多页并行开发会互相覆盖。
 */

(function () {
  const { api, esc, barChart } = window.spf;

  /* 分级标签配色：敏感度递增。PII 额外加边框描边，让它在一屏表格里第一眼就跳出来
   * （R-UNW-02 的意义就在于「谁都不能假装没看见」）。 */
  const LEVEL_STYLE = {
    公开: "background:#dcfce7;color:#15803d",
    内部: "background:#dbeafe;color:#1d4ed8",
    敏感: "background:#fef3c7;color:#b45309",
    PII: "background:#fee2e2;color:#b91c1c;box-shadow:inset 0 0 0 1px #b91c1c",
  };

  /* 健康状态 → 徽章文案 / 颜色。idle 与 empty 都用中性灰：
   * 它们是「没事发生」，不是「出事了」，用暖色会稀释真正的告警。 */
  const STATUS = {
    ok: { cls: "ok", text: "正常", color: "#15803d" },
    warn: { cls: "warn", text: "延迟", color: "#b45309" },
    bad: { cls: "bad", text: "异常", color: "#b91c1c" },
    idle: { cls: "off", text: "静默", color: "#9ca3af" },
    empty: { cls: "off", text: "空表", color: "#9ca3af" },
  };

  function badge(status) {
    const s = STATUS[status] || STATUS.empty;
    return `<span class="badge ${s.cls}">${s.text}</span>`;
  }

  /* 距今时长：秒 → 人读。数值本身由后端在 SQL 侧算好（宿主 UTC / MySQL +08 时区不一致，
   * 前端拿 Date.now() 去减会恒差 8 小时），这里只负责格式化。 */
  function fmtAge(sec) {
    if (sec == null) return "-";
    if (sec < 0) return "刚刚";
    if (sec < 60) return sec + " 秒前";
    if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
    if (sec < 86400) return (sec / 3600).toFixed(1) + " 小时前";
    return (sec / 86400).toFixed(1) + " 天前";
  }

  function fmtNum(n) {
    return Number(n || 0).toLocaleString("zh-CN");
  }

  /* ---------------- 链路健康度（本页重点） ---------------- */

  function renderLineage(el, layers) {
    const cards = layers.map((L) => {
      const items = L.tables
        .map((t) => {
          const ts = STATUS[t.status] || STATUS.empty;
          return (
            `<div style="border-left:3px solid ${ts.color};padding:6px 0 6px 8px;margin-bottom:6px" ` +
            `title="${esc(t.note)}（时间列 ${esc(t.ts_col)}）">` +
            `<div style="font-family:ui-monospace,Menlo,monospace;font-size:12px">${esc(t.table)}</div>` +
            `<div style="font-size:12px;color:#6b7280">${fmtNum(t.rows)} 行 · ${fmtAge(t.age_seconds)}</div>` +
            `</div>`
          );
        })
        .join("");
      return (
        `<div style="flex:1 1 220px;min-width:210px;border:1px solid #e3e8f0;border-radius:10px;padding:12px">` +
        `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">` +
        `<strong>${esc(L.layer)}</strong>${badge(L.status)}</div>` +
        `<div style="font-size:12px;color:#6b7280;margin-bottom:10px">${esc(L.desc)}</div>` +
        items +
        `<div style="font-size:11px;color:#9ca3af;margin-top:6px">` +
        `延迟阈值 ${fmtAge(L.warn_seconds)} / 异常 ${fmtAge(L.bad_seconds)}</div>` +
        `</div>`
      );
    });
    const arrow =
      '<div style="align-self:center;color:#9ca3af;font-size:18px;flex:0 0 auto">&rarr;</div>';
    el.innerHTML =
      `<div style="display:flex;flex-wrap:wrap;gap:10px">${cards.join(arrow)}</div>` +
      `<div class="check-line">分层阈值按各层的产出节奏定：ODS 对齐 R-EVT-01「CDC 秒级入 ODS」，` +
      `超 5 分钟标延迟、30 分钟标异常；DWD/DWS/ADS 是 T+1 批（Airflow 00:30），留 2 小时跑批余量后按 26 小时判延迟。` +
      `「静默」= 下游水位已追平、无滞留事件，源头本身没有新变更；「空表」= 尚无产出，与断流是两回事。</div>`;
  }

  /* ---------------- CDC 状态 ---------------- */

  function renderCdc(el, cdc) {
    const v = cdc.verdict;
    const head =
      v.level === "bad"
        ? `<span class="badge bad">异常</span>`
        : v.idle
          ? `<span class="badge off">静默</span>`
          : `<span class="badge ok">正常</span>`;

    const lines = [];
    if (v.missed_events) {
      lines.push(
        `<div class="mismatch-item">业务库存在比 ODS 最后一条事件更新的记录（源头 ${fmtAge(
          v.biz_newest_age_seconds
        )} / ODS ${fmtAge(v.ods_age_seconds)}）——确认漏采，先查 spacefin-cdc 进程与 binlog 权限。</div>`
      );
    }
    if (v.max_lag > 1000) {
      lines.push(
        `<div class="mismatch-item">下游消费水位落后 ${fmtNum(v.max_lag)} 条（阈值 1000），检查 spacefin-cdc-consumer。</div>`
      );
    }
    if (v.stall_suspect) {
      lines.push(
        `<div class="check-line" style="color:#b45309">弱信号：binlog 位点已停滞 ${fmtAge(
          v.stale_seconds
        )}，且主库位点领先。${esc(v.master_note)}</div>`
      );
    }
    if (!lines.length) {
      lines.push(
        `<div class="check-line">无积压、无漏采：下游水位已追平 ODS 最大事件号（${fmtNum(
          cdc.max_event_id
        )}）。</div>`
      );
    }

    const pos = cdc.position;
    const posHtml = pos
      ? `<tr><td>binlog 位点</td><td style="font-family:ui-monospace,Menlo,monospace">${esc(
          pos.log_file
        )}:${fmtNum(pos.log_pos)}</td><td>${esc(pos.updated_at)}</td><td>${fmtAge(
          pos.stale_seconds
        )}</td></tr>`
      : `<tr><td>binlog 位点</td><td colspan="3">未持久化（首次启动前）</td></tr>`;
    const masterHtml = cdc.master
      ? `<tr><td>主库位点</td><td style="font-family:ui-monospace,Menlo,monospace">${esc(
          cdc.master.log_file
        )}:${fmtNum(cdc.master.log_pos)}</td><td colspan="2" style="color:#6b7280">含实例全部库表写入，仅作参考</td></tr>`
      : `<tr><td>主库位点</td><td colspan="3" style="color:#6b7280">无权限读取（需 REPLICATION CLIENT）</td></tr>`;

    const consumers = cdc.consumers
      .map(
        (c) =>
          `<tr><td>${esc(c.consumer)}</td><td>水位 ${fmtNum(c.last_id)}</td>` +
          `<td>${c.lag > 0 ? `<span style="color:#b45309">滞后 ${fmtNum(c.lag)} 条</span>` : "已追平"}</td>` +
          `<td>${fmtAge(c.idle_seconds)}</td></tr>`
      )
      .join("");

    el.innerHTML =
      `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">${head}` +
      `<span style="color:#6b7280;font-size:13px">ODS 最后事件 ${fmtAge(v.ods_age_seconds)} · ` +
      `累计 ${fmtNum(cdc.max_event_id)} 个事件</span></div>` +
      lines.join("") +
      `<table class="table" style="margin-top:10px"><tbody>${posHtml}${masterHtml}${consumers}</tbody></table>`;
  }

  /* ---------------- 数据源登记表 ---------------- */

  function toggleBtn(row, field, on, canEdit) {
    const label = on ? "开启" : "关闭";
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
          )}</div><div>${esc(s.name)}</div></td>` +
          `<td>${esc(s.source_type)}</td>` +
          `<td><span class="badge" style="${lv}">${esc(s.data_level)}</span></td>` +
          `<td style="max-width:280px"><div style="font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all">${esc(
            s.endpoint
          )}</div><div style="font-size:12px;color:#6b7280">${esc(s.remark || "")}</div></td>` +
          `<td style="font-size:12px">${esc(s.target_table || "-")}<div style="color:#6b7280">${esc(
            s.service_unit || "-"
          )}</div></td>` +
          `<td>${esc(s.owner)}</td>` +
          `<td>${toggleBtn(s, "cdc_enabled", s.cdc_enabled, data.can_edit)}</td>` +
          `<td>${toggleBtn(s, "enabled", s.enabled, data.can_edit)}</td>` +
          `<td style="font-size:12px;color:#6b7280">${esc(s.updated_at)}</td></tr>`
        );
      })
      .join("");
    el.innerHTML =
      `<table class="table"><thead><tr>` +
      `<th>数据源</th><th>类型</th><th>分类分级</th><th>接入端点 / 说明</th><th>落地表 / 管控单元</th>` +
      `<th>负责人</th><th>CDC</th><th>登记状态</th><th>更新时间</th>` +
      `</tr></thead><tbody>${rows}</tbody></table>` +
      `<div class="check-line" style="color:#b45309">⚠ ${esc(data.switch_note)}</div>` +
      `<div class="check-line">分级口径（R-UNW-02）：` +
      data.levels
        .map(
          (l) => `<span class="badge" style="${LEVEL_STYLE[l]};margin-right:6px">${esc(l)}</span>`
        )
        .join("") +
      `PII / 敏感级的数据导出必须脱敏，导出动作已在 ads_export_audit 留痕。` +
      (data.can_edit ? "" : "当前角色只读，开关仅管理员可改。") +
      `</div>`;
  }

  /* ---------------- 页面装配 ---------------- */

  const HTML =
    `<h2 class="page-title">数据底座接入配置` +
    `<button class="btn btn-ghost btn-sm" id="ds-refresh" style="margin-left:12px">刷新</button>` +
    `<span id="ds-now" style="font-size:12px;color:#6b7280;margin-left:8px"></span></h2>` +
    `<div class="kpi-row" id="ds-kpi"></div>` +
    `<div class="card" style="margin-top:16px"><div class="card-title">链路健康度 · ODS → DWD → DWS → ADS</div>` +
    `<div id="ds-lineage"></div></div>` +
    `<div class="grid-2">` +
    `<div class="card"><div class="card-title">CDC 位点与消费水位</div><div id="ds-cdc"></div></div>` +
    `<div class="card"><div class="card-title">最近 24 小时 CDC 事件量</div><div class="chart" id="ds-trend"></div></div>` +
    `</div>` +
    `<div class="card" style="margin-top:16px"><div class="card-title">数据源登记</div><div id="ds-registry"></div></div>` +
    `<div class="card" style="margin-top:16px"><div class="card-title">CDC 断流告警（ads_cdc_alert）</div>` +
    `<div id="ds-cdc-alerts"></div></div>`;

  async function render(_el) {
    const data = await api("/api/datasource");

    document.getElementById("ds-now").textContent = "数据库时间 " + data.server_now;

    const abnormal = data.layers.reduce(
      (n, L) => n + L.tables.filter((t) => t.status === "warn" || t.status === "bad").length,
      0
    );
    const kpis = [
      { label: "已登记数据源", value: data.sources.length, sub: data.types.join(" / ") },
      {
        label: "启用 CDC",
        value: data.sources.filter((s) => s.cdc_enabled).length,
        sub: "登记配置位，非进程状态",
      },
      {
        label: "PII / 敏感源",
        value: data.sources.filter((s) => s.data_level === "PII" || s.data_level === "敏感").length,
        sub: "导出须脱敏（R-UNW-02）",
      },
      {
        label: "链路异常表",
        value: abnormal,
        sub: abnormal ? "存在延迟/异常分层" : "四层新鲜度均达标",
      },
      {
        label: "消费滞后",
        value: data.cdc.verdict.max_lag,
        sub: "ODS 事件号差（阈值 1000）",
      },
    ];
    document.getElementById("ds-kpi").innerHTML = kpis
      .map(
        (k) =>
          `<div class="kpi"><div class="kpi-label">${esc(k.label)}</div>` +
          `<div class="kpi-value">${esc(k.value)}</div><div class="kpi-sub">${esc(k.sub)}</div></div>`
      )
      .join("");

    renderLineage(document.getElementById("ds-lineage"), data.layers);
    renderCdc(document.getElementById("ds-cdc"), data.cdc);

    // 24 根柱子挤不下 24 个刻度，每 3 小时标一次；空桶用浅灰，断流时段一眼可见。
    const trend = data.trend.map((b, i) => ({ ...b, tick: i % 3 === 0 ? b.hour : "" }));
    barChart(document.getElementById("ds-trend"), trend, {
      value: "count",
      label: "tick",
      format: (x) => (x ? x : ""),
      color: (d) => (d.count > 0 ? "#2563eb" : "#e5e7eb"),
      height: 250,
    });

    renderRegistry(document.getElementById("ds-registry"), data);

    const alertsEl = document.getElementById("ds-cdc-alerts");
    alertsEl.innerHTML = data.alerts.length
      ? `<table class="table"><thead><tr><th>类型</th><th>详情</th><th>时间</th><th>距今</th></tr></thead><tbody>` +
        data.alerts
          .map(
            (a) =>
              `<tr><td><span class="badge bad">${esc(a.alert_type)}</span></td>` +
              `<td>${esc(a.detail)}</td><td>${esc(a.alert_ts)}</td><td>${fmtAge(a.age_seconds)}</td></tr>`
          )
          .join("") +
        `</tbody></table>`
      : `<div class="empty">暂无断流告警（spacefin-cdc 内置监控每 60s 自查一次：位点停滞 &gt;30min 或消费滞后 &gt;1000 条才落库，同类型 30min 去重）</div>`;
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
