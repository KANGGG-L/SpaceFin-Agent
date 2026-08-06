/* global window, URLSearchParams, document, L, setTimeout */
/* P5 空间风险画像（插件页）。
 *
 * 为什么地图是手写 SVG 而不是 Leaflet/ECharts：
 * 1. 全站零依赖、离线可跑，引 CDN 会在内网直接白屏；
 * 2. 本页只需要「点位相对位置 + 数值着色」，不需要底图瓦片、缩放、路网；
 * 3. 132 个网格点用等距圆柱投影（经度乘 cos(纬度) 收缩）线性映射到视口即可，
 *    在广东尺度上形变 <1%，与 tools/spatial 的距离口径一致。
 * 代价是没有行政区底图——图上只有点，所以必须画经纬网格线 + 比例尺，
 * 否则读图人无法判断点位到底在哪、隔多远。
 */
(function () {
  const { api, esc, fmtMoney, fmtPct, fmtLtv, CLASS_COLORS } = window.spf;

  const state = {
    city: "",
    zoneType: "price", // 默认只看价格面网格：LTV 网格是坐标补全前的历史结果（上海坐标），
    // 与广东网格混在一张图上会把视野拉到跨省尺度，广东的点全挤成一团。
    layer: "risk",
    showLoans: false,
    data: null,
    detail: null,
  };

  const LAYERS = {
    risk: { label: "高危区判定", unit: "" },
    poi: { label: "POI 密度（挂牌密度代理）", unit: " 个/km²" },
    commute: { label: "通勤时长（直线近似）", unit: " 分钟" },
    dev: { label: "价格偏离度（vs 城市中位）", unit: "" },
  };

  const RULE_LABEL = {
    price_low: "价格洼地（中位单价 ≤ 城市中位×75%，样本≥20）",
    ltv_high: "LTV 集中（网格 LTV 中位 >0.85，样本≥3）",
  };

  /* ---------------- 颜色 ---------------- */

  function hex2rgb(h) {
    return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  }

  function lerpColor(a, b, t) {
    const A = hex2rgb(a);
    const B = hex2rgb(b);
    const c = A.map((v, i) => Math.round(v + (B[i] - v) * Math.max(0, Math.min(1, t))));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  // 各图层的取值口径：value 取哪一列、怎么映射成颜色。
  // 单独抽出来是因为地图、图例、tooltip 三处必须用同一套映射，否则图例会骗人。
  function layerValue(z, layer) {
    if (layer === "poi") return z.poi_density_avg;
    if (layer === "commute") return z.commute_min_avg;
    if (layer === "dev") return z.price_dev_vs_city;
    return z.is_high_risk_zone;
  }

  function makeScale(zones, layer) {
    if (layer === "risk") {
      return {
        color: (z) => (z.is_high_risk_zone ? "#b91c1c" : "#2563eb"),
        stops: [
          { color: "#b91c1c", label: "高危区" },
          { color: "#2563eb", label: "正常区块" },
        ],
      };
    }
    const vals = zones.map((z) => layerValue(z, layer)).filter((v) => v != null);
    if (!vals.length) {
      return { color: () => "#d1d5db", stops: [{ color: "#d1d5db", label: "无数据" }] };
    }
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    if (layer === "dev") {
      // 发散色阶，中点固定为 0：负偏离（洼地）是风险侧，用红；正偏离用蓝。
      const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
      const col = (v) =>
        v == null
          ? "#d1d5db"
          : v < 0
            ? lerpColor("#e5e7eb", "#b91c1c", -v / m)
            : lerpColor("#e5e7eb", "#1d4ed8", v / m);
      return {
        color: (z) => col(layerValue(z, layer)),
        stops: [
          { color: col(-m), label: fmtPct(-m) + "（洼地）" },
          { color: col(-m / 2), label: fmtPct(-m / 2) },
          { color: col(0), label: "0" },
          { color: col(m / 2), label: fmtPct(m / 2) },
          { color: col(m), label: fmtPct(m) + "（高地）" },
        ],
      };
    }
    const from = layer === "poi" ? "#e0f2fe" : "#fef3c7";
    const to = layer === "poi" ? "#075985" : "#b91c1c";
    const span = hi - lo || 1;
    const col = (v) => (v == null ? "#d1d5db" : lerpColor(from, to, (v - lo) / span));
    const fmt = (v) => v.toFixed(1) + LAYERS[layer].unit;
    return {
      color: (z) => col(layerValue(z, layer)),
      stops: [0, 0.25, 0.5, 0.75, 1].map((t) => ({
        color: col(lo + span * t),
        label: fmt(lo + span * t),
      })),
    };
  }

  /* ---------------- 各区块渲染 ---------------- */

  function renderKpi(el, d) {
    const k = d.kpi;
    const cards = [
      { label: "网格总数", value: k.zone_count, sub: "ads_spatial_zone（当前筛选）" },
      { label: "高危网格", value: k.high_risk_zone_count, sub: "规则命中：price_low / ltv_high" },
      {
        label: "覆盖挂牌样本",
        value: k.sample_count.toLocaleString("zh-CN"),
        sub: "网格内有坐标的挂牌行",
      },
      { label: "高危区贷款", value: k.high_risk_loan_count, sub: "dws_risk_class 引擎标记" },
      {
        label: "高危区不良率",
        value: k.high_risk_npl_rate_balance == null ? "-" : fmtPct(k.high_risk_npl_rate_balance),
        sub: "余额口径 · 次级+可疑+损失",
      },
    ];
    el.querySelector("#p5-kpi").innerHTML = cards
      .map(
        (c) =>
          `<div class="kpi"><div class="kpi-label">${esc(c.label)}</div>` +
          `<div class="kpi-value">${esc(String(c.value))}</div>` +
          `<div class="kpi-sub">${esc(c.sub)}</div></div>`
      )
      .join("");
  }

  let _map = null;
  let _zoneLayer = null;
  let _loanLayer = null;

  function renderMap(el, fit) {
    if (fit === undefined) fit = true;
    const d = state.data;
    const zones = (d.zones || []).filter((z) => z.center_lng != null && z.center_lat != null);
    const loans = state.showLoans
      ? (d.collaterals || []).filter((l) => l.lat != null && l.lng != null)
      : [];
    const container = el.querySelector("#p5-leaflet");

    if (!zones.length && !loans.length) {
      if (_map) {
        _zoneLayer.clearLayers();
        _loanLayer.clearLayers();
      }
      el.querySelector("#p5-legend").innerHTML = "";
      return;
    }

    // 若页面被 SPA 重新渲染过，旧容器已脱离 DOM，需销毁重建，否则 Leaflet 操作失效。
    if (_map && (!_map.getContainer || !document.body.contains(_map.getContainer()))) {
      try {
        _map.remove();
      } catch (e) {
        void e; /* 旧容器已脱离 DOM，remove 失败可忽略 */
      }
      _map = _zoneLayer = _loanLayer = null;
    }
    if (!_map) {
      _map = L.map(container, { preferCanvas: true }).setView([23.1, 113.3], 9);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
      }).addTo(_map);
      L.control.scale({ imperial: false }).addTo(_map);
      _zoneLayer = L.layerGroup().addTo(_map);
      _loanLayer = L.layerGroup().addTo(_map);
    }

    _zoneLayer.clearLayers();
    _loanLayer.clearLayers();

    const scale = makeScale(zones, state.layer);
    const maxSample = Math.max(1, ...zones.map((z) => z.sample_count || 0));
    const radius = (z) => 4 + 11 * Math.sqrt((z.sample_count || 0) / maxSample);
    const bounds = [];

    for (const z of zones) {
      const lat = z.center_lat,
        lng = z.center_lng;
      bounds.push([lat, lng]);
      const r = radius(z);
      const hi = z.is_high_risk_zone;
      const m = L.circleMarker([lat, lng], {
        radius: r,
        color: hi ? "#7f1d1d" : "#94a3b8",
        weight: hi ? 1.6 : 0.8,
        fillColor: scale.color(z),
        fillOpacity: hi ? 0.8 : 0.6,
      });
      m.bindTooltip(tooltipHtml(z), { sticky: true, direction: "top", opacity: 0.95 });
      m.on("click", () => openZone(el, z.zone_id));
      _zoneLayer.addLayer(m);
      if (hi) {
        L.circleMarker([lat, lng], {
          radius: r + 3.5,
          color: "#b91c1c",
          weight: 1,
          fill: false,
          opacity: 0.45,
          dashArray: "2 2",
        }).addTo(_zoneLayer);
      }
    }

    if (state.showLoans) {
      for (const l of loans) {
        bounds.push([l.lat, l.lng]);
        const m = L.circleMarker([l.lat, l.lng], {
          radius: 4,
          color: "#fff",
          weight: 0.8,
          fillColor: CLASS_COLORS[l.risk_class] || "#6b7280",
          fillOpacity: 0.75,
        });
        m.bindTooltip(loanTooltipHtml(l), { sticky: true, opacity: 0.95 });
        _loanLayer.addLayer(m);
      }
    }

    if (fit && bounds.length) {
      _map.fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
    }
    setTimeout(() => _map.invalidateSize(), 30);

    let legend =
      `<span style="color:#374151;font-weight:600">${esc(LAYERS[state.layer].label)}：</span>` +
      scale.stops
        .map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`)
        .join("");
    legend +=
      `<span style="margin-left:12px"><i style="background:none;border:1px dashed #b91c1c"></i>高危网格（虚线圈）</span>` +
      `<span>点面积 ∝ 网格样本量（最大 ${maxSample}）</span>`;
    if (state.showLoans) {
      legend +=
        `<span style="margin-left:12px;color:#374151">抵押物（圆点）：</span>` +
        Object.keys(CLASS_COLORS)
          .map((c) => `<span><i style="background:${CLASS_COLORS[c]}"></i>${esc(c)}</span>`)
          .join("");
    }
    el.querySelector("#p5-legend").innerHTML = legend;
  }

  function renderHighRiskTable(el) {
    const rows = state.data.high_risk_zones;
    const tb = el.querySelector("#p5-hr-table tbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="7" class="empty">当前筛选下无高危网格</td></tr>`;
      return;
    }
    tb.innerHTML = rows
      .map(
        (z) =>
          `<tr data-zid="${esc(z.zone_id)}" style="cursor:pointer">` +
          `<td>${esc(z.city_name)}</td>` +
          `<td style="font-family:monospace;font-size:12px">${esc(z.zone_id)}</td>` +
          `<td>${z.median_unit_price == null ? "-" : Number(z.median_unit_price).toLocaleString("zh-CN") + " 元/㎡"}</td>` +
          `<td>${z.median_ltv == null ? "-" : fmtLtv(z.median_ltv)}</td>` +
          `<td style="color:#b91c1c;font-weight:600">${z.price_dev_vs_city == null ? "-" : fmtPct(z.price_dev_vs_city)}</td>` +
          `<td><span class="badge bad">${esc(z.high_risk_rule || "-")}</span></td>` +
          `<td>${z.sample_count}</td>` +
          `</tr>`
      )
      .join("");
  }

  function renderCityLayers(el) {
    const layer = state.layer === "risk" ? "poi" : state.layer;
    const key =
      layer === "commute"
        ? "commute_min_avg"
        : layer === "dev"
          ? "price_deviation_avg"
          : "poi_density_avg";
    const items = state.data.city_layers.filter((c) => c[key] != null);
    const box = el.querySelector("#p5-city-layers");
    if (!items.length) {
      box.innerHTML = `<div class="empty">无城市维度统计</div>`;
      return;
    }
    const max = Math.max(...items.map((c) => Math.abs(Number(c[key]))));
    const unit = layer === "commute" ? " 分钟" : layer === "dev" ? "" : " 个/km²";
    box.innerHTML = items
      .map((c) => {
        const v = Number(c[key]);
        const w = (Math.abs(v) / (max || 1)) * 100;
        const txt = layer === "dev" ? fmtPct(v) : v.toFixed(1) + unit;
        return (
          `<div class="hbar-row"><div class="hbar-label">${esc(c.name)}</div>` +
          `<div class="hbar-track"><div class="hbar-fill${v < 0 ? " risk" : ""}" style="width:${w.toFixed(1)}%"></div></div>` +
          `<div class="hbar-num">${esc(txt)} · ${c.listing_count} 行</div></div>`
        );
      })
      .join("");
    el.querySelector("#p5-city-layers-title").textContent =
      layer === "commute"
        ? "城市平均通勤时长（直线近似，低估 1.4~1.6×）"
        : layer === "dev"
          ? "城市平均价格偏离度（挂牌行 vs 5km 邻域中位）"
          : "城市平均 POI 密度（挂牌密度代理，个/km²）";
  }

  function grp(g, cls) {
    return (
      `<td>${g.loan_count}</td>` +
      `<td>${fmtMoney(g.balance)}</td>` +
      `<td>${g.npl_count}</td>` +
      `<td>${fmtMoney(g.npl_balance)}</td>` +
      `<td style="font-weight:700;color:${cls}">${g.npl_rate_count == null ? "-" : fmtPct(g.npl_rate_count)}</td>` +
      `<td style="font-weight:700;color:${cls}">${g.npl_rate_balance == null ? "-" : fmtPct(g.npl_rate_balance)}</td>` +
      `<td>${g.avg_ltv == null ? "-" : fmtLtv(g.avg_ltv)}</td>` +
      `<td>${g.alert_count}</td>`
    );
  }

  function renderNpl(el) {
    const n = state.data.npl;
    const a = state.data.attribution;
    let html =
      `<table class="table"><thead><tr>` +
      `<th>分组</th><th>笔数</th><th>余额敞口</th><th>不良笔数</th><th>不良余额</th>` +
      `<th>不良率(笔数)</th><th>不良率(余额)</th><th>平均 LTV</th><th>预警笔数</th></tr></thead><tbody>` +
      `<tr><td><span class="badge bad">高危区</span></td>${grp(n.high, "#b91c1c")}</tr>` +
      `<tr><td><span class="badge ok">非高危区</span></td>${grp(n.normal, "#15803d")}</tr>` +
      `<tr style="font-weight:700;background:#f8fafc"><td>全量</td>${grp(n.total, "#374151")}</tr>` +
      `</tbody></table>`;

    // 集中度倍数：>1 说明高危区确实更差，<1 说明这个标记当前没有区分力。
    const liftTxt = (v) => (v == null ? "-" : v.toFixed(2) + "×");
    const verdict =
      n.lift_balance == null
        ? "当前筛选下高危区无贷款样本，无法比较。"
        : n.lift_balance >= 1.2
          ? "高危区不良集中度显著高于非高危区，空间惩罚项有区分力。"
          : n.lift_balance <= 0.9
            ? "高危区不良集中度反而低于非高危区——该标记当前<b>不具备区分力</b>，不应据此加严授信。"
            : "高危区与非高危区不良水平接近，区分力不明显。";
    html +=
      `<div class="check-line">集中度倍数（高危 ÷ 非高危）：笔数口径 <b>${liftTxt(n.lift_count)}</b>，` +
      `余额口径 <b>${liftTxt(n.lift_balance)}</b>。${verdict}</div>`;
    if (n.small_sample) {
      html +=
        `<div class="check-line" style="color:#b45309">⚠ 小样本提示：高危区仅 ${n.high.loan_count} 笔，` +
        `单笔迁徙即可让不良率跳动 ${(100 / Math.max(1, n.high.loan_count)).toFixed(1)}pct，结论只能作为方向性参考。</div>`;
    }

    // 口径交叉核对：本页最容易被误读的地方，必须显式摊开。
    html +=
      `<div style="margin-top:14px;padding:12px 14px;border-radius:8px;background:${a.consistent ? "#f0fdf4" : "#fff7ed"};border:1px solid ${a.consistent ? "#bbf7d0" : "#fed7aa"}">` +
      `<div style="font-weight:600;margin-bottom:6px">口径交叉核对：引擎标记 vs 空间网格归属` +
      `<span class="badge ${a.consistent ? "ok" : "warn"}" style="margin-left:8px">${a.consistent ? "一致" : "不一致"}</span></div>` +
      `<div class="check-line" style="margin-top:0">` +
      `引擎口径（dws_risk_class.is_high_risk_zone，来自业务库 collateral 合成字段）标记 <b>${a.flagged_by_engine}</b> 笔；` +
      `空间口径（按 tools/spatial 同款 0.02° 网格重新归属 ${a.with_coord} 笔有坐标抵押物）落入任一网格 <b>${a.in_any_zone_grid}</b> 笔、` +
      `落入高危网格 <b>${a.in_high_risk_grid}</b> 笔，dws_spatial_feature.zone_id 非空 <b>${a.with_zone_id}</b> 笔。</div>` +
      `<div class="check-line">抵押物到最近网格中心距离：最小 ${a.nearest_zone_km_min ?? "-"} km、中位 ${a.nearest_zone_km_median ?? "-"} km，5km 内仅 ${a.within_5km} 笔。</div>` +
      (a.consistent
        ? ""
        : `<div class="check-line" style="color:#b45309">⚠ 两个口径对不上：价格面网格只覆盖有坐标的挂牌聚集区（9 个城市），` +
          `而抵押物散布在全省 21 城，绝大多数落在任何网格之外。因此上表的「高危区」分组来自业务库合成标记，` +
          `<b>并非 S3 空间画像的网格归属结论</b>，不能当作空间证据使用；需先补齐抵押物周边挂牌坐标覆盖。</div>`) +
      `</div>`;

    el.querySelector("#p5-npl").innerHTML = html;
  }

  function renderDetail(el) {
    const box = el.querySelector("#p5-detail");
    const d = state.detail;
    if (!d) {
      box.innerHTML = `<div class="check-line">点击地图上的网格点或下方高危区列表，查看该网格明细。</div>`;
      return;
    }
    const z = d.zone;
    let html =
      `<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:baseline">` +
      `<div style="font-weight:700;font-family:monospace">${esc(z.zone_id)}</div>` +
      `<div>${esc(z.city_name)} · ${z.zone_type === "price" ? "价格面网格" : "LTV 网格"}</div>` +
      `<div>${z.is_high_risk_zone ? `<span class="badge bad">高危 · ${esc(RULE_LABEL[z.high_risk_rule] || z.high_risk_rule || "")}</span>` : `<span class="badge ok">正常</span>`}</div>` +
      `<div class="hbar-num">中心 ${z.center_lng?.toFixed(4)}°E, ${z.center_lat?.toFixed(4)}°N · 样本 ${z.sample_count} · 构建日 ${esc(z.build_date || "-")}</div>` +
      `</div>`;

    html +=
      `<table class="table" style="margin-top:10px"><thead><tr><th>实体类型</th><th>数量</th>` +
      `<th>POI 密度均值</th><th>通勤均值</th><th>价格偏差均值</th><th>特征缺失率均值</th></tr></thead><tbody>` +
      (d.entities.length
        ? d.entities
            .map(
              (e) =>
                `<tr><td>${esc(e.entity_type)}</td><td>${e.count}</td>` +
                `<td>${e.poi_density_avg == null ? "-" : e.poi_density_avg.toFixed(2) + " 个/km²"}</td>` +
                `<td>${e.commute_min_avg == null ? "-" : e.commute_min_avg.toFixed(1) + " 分钟"}</td>` +
                `<td>${e.price_deviation_avg == null ? "-" : fmtPct(e.price_deviation_avg)}</td>` +
                `<td>${e.missing_pct_avg == null ? "-" : e.missing_pct_avg.toFixed(1) + "%"}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="6" class="empty">该网格无实体特征（LTV 网格为坐标补全前的历史构建结果）</td></tr>`) +
      `</tbody></table>`;

    const hist = d.deviation_hist.filter((h) => h.count > 0);
    if (hist.length) {
      const max = Math.max(...hist.map((h) => h.count));
      html +=
        `<div class="card-title" style="margin-top:12px">网格内挂牌价格偏差分布（vs 5km 邻域中位）</div>` +
        hist
          .map(
            (h) =>
              `<div class="hbar-row"><div class="hbar-label">${esc(h.bucket)}</div>` +
              `<div class="hbar-track"><div class="hbar-fill${h.bucket.startsWith("<") || h.bucket.startsWith("-") ? " risk" : ""}" style="width:${((h.count / max) * 100).toFixed(1)}%"></div></div>` +
              `<div class="hbar-num">${h.count} 行</div></div>`
          )
          .join("");
    }

    html += `<div class="card-title" style="margin-top:12px">5km 内贷款敞口（共 ${d.nearby_total} 笔）</div>`;
    html += d.nearby_loans.length
      ? `<table class="table"><thead><tr><th>贷款号</th><th>距离</th><th>五级</th><th>LTV</th><th>余额</th><th>引擎高危标记</th><th>抵押物地址</th></tr></thead><tbody>` +
        d.nearby_loans
          .map(
            (l) =>
              `<tr><td>${l.loan_id}</td><td>${l.distance_km} km</td>` +
              `<td><span class="badge" style="background:${CLASS_COLORS[l.risk_class]}22;color:${CLASS_COLORS[l.risk_class]}">${esc(l.risk_class)}</span></td>` +
              `<td>${fmtLtv(l.ltv)}</td><td>${fmtMoney(l.balance)}</td>` +
              `<td>${l.is_high_risk_zone ? `<span class="badge bad">是</span>` : "-"}</td>` +
              `<td>${esc(l.property_addr)}</td></tr>`
          )
          .join("") +
        `</tbody></table>`
      : `<div class="check-line">该网格 5km 内没有抵押物——即便判定为高危，当前也无实际敞口需要处置。</div>`;

    box.innerHTML = html;
  }

  function renderCaveats(el) {
    el.querySelector("#p5-caveats").innerHTML = state.data.caveats
      .map((c) => `<div class="check-line">· ${esc(c)}</div>`)
      .join("");
  }

  /* ---------------- tooltip ---------------- */

  function tooltipHtml(z) {
    const rows = [
      ["城市", z.city_name],
      ["网格", z.zone_id],
      ["样本量", z.sample_count],
      [
        "中位单价",
        z.median_unit_price == null
          ? "-"
          : Number(z.median_unit_price).toLocaleString("zh-CN") + " 元/㎡",
      ],
      ["中位 LTV", z.median_ltv == null ? "-" : fmtLtv(z.median_ltv)],
      ["价格偏离度", z.price_dev_vs_city == null ? "-" : fmtPct(z.price_dev_vs_city)],
      ["POI 密度均值", z.poi_density_avg == null ? "-" : z.poi_density_avg.toFixed(2) + " 个/km²"],
      ["通勤均值", z.commute_min_avg == null ? "-" : z.commute_min_avg.toFixed(1) + " 分钟"],
      ["判定", z.is_high_risk_zone ? "高危 · " + (z.high_risk_rule || "") : "正常"],
    ];
    return rows
      .map(
        ([k, v]) =>
          `<div style="display:flex;gap:10px"><span style="color:#9ca3af;min-width:76px">${esc(k)}</span><span>${esc(String(v))}</span></div>`
      )
      .join("");
  }

  function loanTooltipHtml(l) {
    const rows = [
      ["贷款号", l.loan_id],
      ["抵押物", l.collateral_id],
      ["城市", l.city_name],
      ["五级分类", l.risk_class],
      ["LTV", fmtLtv(l.ltv)],
      ["余额", fmtMoney(l.balance)],
      ["引擎高危标记", l.is_high_risk_zone ? "是" : "否"],
      ["地址", l.property_addr],
    ];
    return rows
      .map(
        ([k, v]) =>
          `<div style="display:flex;gap:10px"><span style="color:#9ca3af;min-width:76px">${esc(k)}</span><span>${esc(String(v))}</span></div>`
      )
      .join("");
  }

  /* ---------------- 加载与事件 ---------------- */

  async function load(el) {
    const q = new URLSearchParams();
    if (state.city) q.set("city", state.city);
    if (state.zoneType) q.set("zone_type", state.zoneType);
    state.data = await api("/api/spatial?" + q.toString());

    const sel = el.querySelector("#p5-city");
    if (!sel.dataset.filled) {
      sel.innerHTML =
        `<option value="">全部城市</option>` +
        state.data.cities
          .map(
            (c) =>
              `<option value="${esc(c.code)}">${esc(c.name)}（网格 ${c.zone_count}/高危 ${c.high_risk_zones}/贷款 ${c.loan_count}）</option>`
          )
          .join("");
      sel.dataset.filled = "1";
      sel.value = state.city;
    }
    el.querySelector("#p5-build-date").textContent = state.data.build_date
      ? `空间画像构建日：${state.data.build_date}`
      : "";

    renderKpi(el, state.data);
    renderMap(el);
    renderHighRiskTable(el);
    renderCityLayers(el);
    renderNpl(el);
    renderCaveats(el);
    renderDetail(el);
  }

  async function openZone(el, zoneId) {
    state.detail = await api("/api/spatial/zone?zone_id=" + encodeURIComponent(zoneId));
    renderDetail(el);
    el.querySelector("#p5-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function bind(el) {
    el.querySelector("#p5-city").addEventListener("change", (e) => {
      state.city = e.target.value;
      state.detail = null;
      load(el);
    });
    el.querySelector("#p5-zone-type").addEventListener("change", (e) => {
      state.zoneType = e.target.value;
      state.detail = null;
      load(el);
    });
    // 图层切换纯前端重绘：同一批点换着色维度，不用再往返一次后端。
    el.querySelector("#p5-layer").addEventListener("change", (e) => {
      state.layer = e.target.value;
      renderMap(el);
      renderCityLayers(el);
    });
    el.querySelector("#p5-show-loans").addEventListener("change", (e) => {
      state.showLoans = e.target.checked;
      renderMap(el);
    });

    el.querySelector("#p5-hr-table").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-zid]");
      if (tr) openZone(el, tr.dataset.zid);
    });
  }

  const HTML = `
<h2 class="page-title">空间风险画像 <span style="font-size:13px;color:#6b7280;font-weight:400">通勤 / 高危区 / POI 图层 + 高危区 NPL 集中度</span></h2>

<div class="filter-bar">
  <label>城市<select id="p5-city"></select></label>
  <label>网格类型<select id="p5-zone-type">
    <option value="price">价格面网格（广东 DWD）</option>
    <option value="ltv">LTV 集中网格（历史构建）</option>
    <option value="">全部</option>
  </select></label>
  <label>图层<select id="p5-layer">
    <option value="risk">高危区判定</option>
    <option value="poi">POI 密度（挂牌密度代理）</option>
    <option value="commute">通勤时长（直线近似）</option>
    <option value="dev">价格偏离度</option>
  </select></label>
  <label style="flex-direction:row;align-items:center;gap:6px">
    <input type="checkbox" id="p5-show-loans" style="min-width:auto"> 叠加抵押物点位
  </label>
  <div class="filter-total" id="p5-build-date"></div>
</div>

<div class="kpi-row" id="p5-kpi"></div>

<div class="card" style="margin-top:16px">
  <div class="card-title">空间分布图（Leaflet 互动地图 · OpenStreetMap 底图）</div>
  <div id="p5-map">
    <div id="p5-leaflet" style="height:560px;width:100%"></div>
  </div>
  <div class="legend" id="p5-legend"></div>
  <div class="check-line">点面积表示网格挂牌样本量；点位为网格内样本的中心坐标，非行政区划边界。悬停看明细，点击下钻。</div>
</div>

<div class="card" style="margin-top:16px">
  <div class="card-title">网格下钻明细</div>
  <div id="p5-detail"></div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title">高危区列表（命中规则 + 判定依据）</div>
    <table class="table" id="p5-hr-table">
      <thead><tr><th>城市</th><th>网格</th><th>中位单价</th><th>中位 LTV</th><th>价格偏离度</th><th>命中规则</th><th>样本</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="card">
    <div class="card-title" id="p5-city-layers-title">城市图层统计</div>
    <div class="hbar-list" id="p5-city-layers"></div>
  </div>
</div>

<div class="card" style="margin-top:16px">
  <div class="card-title">高危区 NPL 集中度（不良 = 次级 + 可疑 + 损失）</div>
  <div id="p5-npl"></div>
</div>

<div class="card" style="margin-top:16px">
  <div class="card-title">数据口径与已知局限（源：docs/tech/components/spatial-feature.md）</div>
  <div id="p5-caveats"></div>
</div>
`;

  let bound = false;
  window.registerPage("spatial", {
    html: HTML,
    render: async (el) => {
      if (!bound) {
        bind(el);
        bound = true;
      }
      await load(el);
    },
  });
})();
