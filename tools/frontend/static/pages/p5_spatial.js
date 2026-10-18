/* P5 空间风险画像（插件页）。
 *
 * 地图：本页需要可交互地图（缩放 / 平移 / 点击网格下钻），因此使用随部署包
 * 分发的本地 vendor/leaflet（非 CDN，展示机离线也能加载），而非手写 SVG。
 * 交互能力来自 Leaflet；底图瓦片来自可配置源（默认 OSM 公网，可由
 * SPATIAL_TILE_URL 环境变量覆盖为本地或内网瓦片）。132 个网格点 + 抵押物点
 * 叠加在 Leaflet 画布上，颜色按数值着色，与 tools/spatial 的距离口径一致。
 * 代价：相比「纯 SVG 无底图」方案，本页依赖 Leaflet 库与瓦片源——
 * 前提是「可交互」优先于「零依赖离线」，故「全站零依赖」对地图页不成立。
 */
(function () {
  const { api, esc, fmtMoney, fmtPct, fmtLtv, CLASS_COLORS } = window.spf;

  /* ---------------- 多语言字典（key = zh-CN 原文，译文只存这里） ----------------
   * 数据键（city_name / risk_class / zone_id 等）保持中文原文用于匹配后端，
   * 仅展示层经 t() 翻译；t() 无译文时原样返回 key。
   */
  registerI18n("en", {
    "{city}（{grid} 网格 / {hr} 高危 / {loan} 贷款）":
      "{city} (Grid {grid} / High-Risk {hr} / Loans {loan})",
    网格类型: "Grid Type",
    价格面网格: "Price-surface Grid",
    "LTV 集中网格": "LTV Concentration Grid",
    全部城市: "All Cities",
    图层: "Layer",
    高危网格判定: "High-Risk Grid Classification",
    "POI 密度（挂牌密度代理）": "POI Density (listing-density proxy)",
    "通勤时长（直线近似）": "Commute Time (straight-line approx.)",
    价格偏离度: "Price Deviation",
    叠加抵押物点位: "Overlay collateral points",
    空间分布图: "Spatial Distribution Map",
    网格下钻明细: "Grid Drill-down Details",
    高危网格列表: "High-Risk Grid List",
    中位单价: "Median Unit Price",
    "片区抵押 LTV 中位": "Zone Median Mortgage LTV",
    样本: "Samples",
    城市图层统计: "City Layer Stats",
    "高危网格不良（NPL）集中度": "NPL Concentration in High-Risk Grids",
    "价格偏离度（vs 城市中位）": "Price Deviation (vs city median)",
    " 个/km²": " /km²",
    " 分钟": " min",
    "价格洼地（中位单价 ≤ 城市中位×75%，样本≥20）":
      "Price Trough (median unit price ≤ 75% of city median, n≥20)",
    "LTV 集中（网格 LTV 中位 >0.85，样本≥3）": "LTV Concentration (zone median LTV >0.85, n≥3)",
    正常区块: "Normal Zone",
    "（洼地）": " (trough)",
    "（高地）": " (highland)",
    网格总数: "Total Grids",
    覆盖挂牌样本: "Covered Listing Samples",
    位于高危网格不良率: "NPL Rate in High-Risk Grids",
    "高危标记圈（高危网格外描边）": "High-risk marker ring (outline of high-risk grids)",
    "抵押物（圆点）：": "Collateral (dots):",
    当前筛选下无高危网格: "No high-risk grids under the current filter",
    "网格编号：": "Grid ID: ",
    " 元/㎡": " /㎡",
    无城市维度统计: "No city-level statistics",
    城市平均通勤时长: "Avg Commute Time by City",
    城市平均价格偏离度: "Avg Price Deviation by City",
    "城市平均 POI 密度": "Avg POI Density by City",
    分组: "Group",
    余额敞口: "Balance Exposure",
    不良笔数: "NPL Count",
    不良余额: "NPL Balance",
    "不良率(笔数)": "NPL Rate (count)",
    "不良率(余额)": "NPL Rate (balance)",
    "平均 LTV": "Avg LTV",
    预警笔数: "Alert Count",
    非高危网格: "Non-High-Risk Grid",
    全量: "Total",
    "解读：本页「高危」判定来自挂牌价格面信号（价格洼地 / LTV 集中），并非由历史不良表现推得。因此高危网格的当前不良率未必更高——它提示的是该片区抵押物未来变现时的折价风险，宜与预警列表联动跟踪。":
      "Note: “High-risk” on this page is determined by listing price-surface signals (price trough / LTV concentration), not by historical NPL performance. So high-risk grids do not necessarily show a higher current NPL rate — it flags the discount risk when collateral in that zone is later liquidated, and should be tracked together with the alert list.",
    内部追溯用网格编号: "Internal-trace grid ID",
    编号: "ID",
    "高危 · ": "High-risk · ",
    中心: "Center",
    构建日: "Built",
    实体类型: "Entity Type",
    数量: "Count",
    "POI 密度均值": "Avg POI Density",
    通勤均值: "Avg Commute",
    价格偏差均值: "Avg Price Deviation",
    特征缺失率均值: "Avg Missing-Feature Rate",
    "该网格无实体特征（LTV 网格为坐标补全前的历史构建结果）":
      "This grid has no entity features (LTV grids are historical builds before coordinate backfill)",
    "网格内挂牌价格偏差分布（vs 5km 邻域中位）":
      "Intra-Grid Listing Price Deviation Distribution (vs 5km neighborhood median)",
    // ---- API 错误句整句注册（ValueError 原句即 key，app.js api() 统一查表）----
    "zone_type 只能是 price / ltv": "zone_type must be price or ltv",
    "缺少参数 zone_id": "Missing parameter: zone_id",
    "未知城市码：{city}": "Unknown city code: {city}",
    "网格不存在：{zone_id}": "Zone not found: {zone_id}",
    "（{n} 笔）": "({n} {n|loan|loans})",
    "在售楼盘": "Listings for sale",
    "小区": "Communities",
    "5km 内贷款敞口（共 {n} 笔）": "Loans within 5km ({n} in total)",
    距离: "Distance",
    "高危网格（引擎判定）": "High-Risk Grid (engine verdict)",
    "该网格 5km 内没有抵押物——即便判定为高危，当前也无实际敞口需要处置。":
      "No collateral within 5km of this grid — even if flagged high-risk, there is currently no real exposure to dispose of.",
    网格位置: "Grid Position",
    样本量: "Sample Size",
    地址: "Address",
    "空间画像构建日：": "Spatial profile build date: ",
    "底图瓦片加载失败或当前缩放层级无瓦片（可能离线/无瓦片服务）；空间网格与抵押物点位不受影响。":
      "Base-map tiles failed to load or no tiles exist at this zoom level (possibly offline / no tile service); spatial grids and collateral points are unaffected.",
    // T7 P5 → P6 网格联动
    "拿去推演": "Use in Policy Deduction",
    "把该网格带入策略推演中心的作用域（zone 粒度）":
      "Bring this grid into the Policy Deduction Center as the scope (zone granularity)",
  }, "spatial");
  registerI18n("zh-HK", {
    "{city}（{grid} 网格 / {hr} 高危 / {loan} 贷款）":
      "{city}（{grid} 網格 / {hr} 高危 / {loan} 貸款）",
    网格类型: "網格類型",
    价格面网格: "價格面網格",
    "LTV 集中网格": "LTV 集中網格",
    全部城市: "全部城市",
    图层: "圖層",
    高危网格判定: "高危網格判定",
    "POI 密度（挂牌密度代理）": "POI 密度（掛牌密度代理）",
    "通勤时长（直线近似）": "通勤時長（直線近似）",
    价格偏离度: "價格偏離度",
    叠加抵押物点位: "疊加抵押物點位",
    空间分布图: "空間分布圖",
    网格下钻明细: "網格下鑽明細",
    高危网格列表: "高危網格列表",
    中位单价: "中位單價",
    "片区抵押 LTV 中位": "片區抵押 LTV 中位",
    样本: "樣本",
    城市图层统计: "城市圖層統計",
    "高危网格不良（NPL）集中度": "高危網格不良（NPL）集中度",
    "价格偏离度（vs 城市中位）": "價格偏離度（vs 城市中位）",
    " 个/km²": " 個/km²",
    " 分钟": " 分鐘",
    "价格洼地（中位单价 ≤ 城市中位×75%，样本≥20）": "價格窪地（中位單價 ≤ 城市中位×75%，樣本≥20）",
    "LTV 集中（网格 LTV 中位 >0.85，样本≥3）": "LTV 集中（網格 LTV 中位 >0.85，樣本≥3）",
    正常区块: "正常區塊",
    "（洼地）": "（窪地）",
    "（高地）": "（高地）",
    网格总数: "網格總數",
    覆盖挂牌样本: "覆蓋掛牌樣本",
    位于高危网格不良率: "位於高危網格不良率",
    "高危标记圈（高危网格外描边）": "高危標記圈（高危網格外描邊）",
    "抵押物（圆点）：": "抵押物（圓點）：",
    当前筛选下无高危网格: "目前篩選下無高危網格",
    "网格编号：": "網格編號：",
    " 元/㎡": " 元/㎡",
    无城市维度统计: "無城市維度統計",
    城市平均通勤时长: "城市平均通勤時長",
    城市平均价格偏离度: "城市平均價格偏離度",
    "城市平均 POI 密度": "城市平均 POI 密度",
    分组: "分組",
    余额敞口: "餘額曝險",
    不良笔数: "不良筆數",
    不良余额: "不良餘額",
    "不良率(笔数)": "不良率(筆數)",
    "不良率(余额)": "不良率(餘額)",
    "平均 LTV": "平均 LTV",
    预警笔数: "預警筆數",
    非高危网格: "非高危網格",
    全量: "全量",
    "解读：本页「高危」判定来自挂牌价格面信号（价格洼地 / LTV 集中），并非由历史不良表现推得。因此高危网格的当前不良率未必更高——它提示的是该片区抵押物未来变现时的折价风险，宜与预警列表联动跟踪。":
      "解讀：本頁「高危」判定來自掛牌價格面訊號（價格窪地 / LTV 集中），並非由歷史不良表現推得。因此高危網格的當前不良率未必更高——它提示的是該片區抵押物未來變現時的折價風險，宜與預警列表聯動追蹤。",
    内部追溯用网格编号: "內部追溯用網格編號",
    编号: "編號",
    "高危 · ": "高危 · ",
    中心: "中心",
    构建日: "構建日",
    实体类型: "實體類型",
    数量: "數量",
    "POI 密度均值": "POI 密度均值",
    通勤均值: "通勤均值",
    价格偏差均值: "價格偏差均值",
    特征缺失率均值: "特徵缺失率均值",
    "该网格无实体特征（LTV 网格为坐标补全前的历史构建结果）":
      "該網格無實體特徵（LTV 網格為座標補全前的歷史構建結果）",
    "网格内挂牌价格偏差分布（vs 5km 邻域中位）": "網格內掛牌價格偏差分布（vs 5km 鄰域中位）",
    // ---- API 错误句整句注册（与 en 块一一对应）----
    "zone_type 只能是 price / ltv": "zone_type 只能是 price / ltv",
    "缺少参数 zone_id": "缺少參數 zone_id",
    "未知城市码：{city}": "未知城市碼：{city}",
    "网格不存在：{zone_id}": "網格不存在：{zone_id}",
    "（{n} 笔）": "（{n} 筆）",
    "在售楼盘": "在售樓盤",
    "小区": "小區",
    "5km 内贷款敞口（共 {n} 笔）": "5km 內貸款曝險（共 {n} 筆）",
    距离: "距離",
    "高危网格（引擎判定）": "高危網格（引擎判定）",
    "该网格 5km 内没有抵押物——即便判定为高危，当前也无实际敞口需要处置。":
      "該網格 5km 內沒有抵押物——即便判定為高危，目前也無實際曝險需要處置。",
    网格位置: "網格位置",
    样本量: "樣本量",
    地址: "地址",
    "空间画像构建日：": "空間畫像構建日：",
    "底图瓦片加载失败或当前缩放层级无瓦片（可能离线/无瓦片服务）；空间网格与抵押物点位不受影响。":
      "底圖瓦片載入失敗或目前縮放層級無瓦片（可能離線/無瓦片服務）；空間網格與抵押物點位不受影響。",
    // T7 P5 → P6 网格联动
    "拿去推演": "拿去推演",
    "把该网格带入策略推演中心的作用域（zone 粒度）":
      "把該網格帶入策略推演中心的作用域（zone 粒度）",
  }, "spatial");

  const state = {
    city: "",
    zoneType: "price", // 默认只看价格面网格：LTV 网格是坐标补全前的历史结果（上海坐标），
    // 与广东网格混在一张图上会把视野拉到跨省尺度，广东的点全挤成一团。
    layer: "risk",
    // 默认叠加抵押物/货款坐标散点：让「通过坐标的分布」首屏即见，与 P6 空间地图
    // 默认展示数据点的思路一致；用户仍可手动关闭（#p5-show-loans 开关）。
    showLoans: true,
    data: null,
    detail: null,
  };

  const LAYERS = {
    risk: { label: "高危网格判定", unit: "" },
    poi: { label: "POI 密度（挂牌密度代理）", unit: " 个/km²" },
    commute: { label: "通勤时长（直线近似）", unit: " 分钟" },
    dev: { label: "价格偏离度（vs 城市中位）", unit: "" },
  };

  const RULE_LABEL = {
    price_low: {
      short: "价格洼地",
      full: "价格洼地（中位单价 ≤ 城市中位×75%，样本≥20）",
    },
    ltv_high: {
      short: "LTV 集中",
      full: "LTV 集中（网格 LTV 中位 >0.85，样本≥3）",
    },
  };

  // 下钻明细「实体类型」列：后端枚举值直出会是英文内部标识，统一映射 + t()
  const ENTITY_TYPE_LABELS = {
    listing: "在售楼盘",
    community: "小区",
    collateral: "抵押物",
  };

  // 「片区抵押 LTV 中位」的展示口径：有值才显示，并带上支撑它的抵押笔数——
  // 一个中位数是 3 笔还是 30 笔撑起来的，风控经理判断可信度时必须看得到。
  // ltv_sample_count 只对价格面片区回填；LTV 型片区的 sample_count 本身就是
  // LTV 观测数，故按 zone_type 兜底，避免把「3 笔」显示成「0 笔」。
  function zoneLtvText(z) {
    if (z.median_ltv == null) return "-";
    const n = z.ltv_sample_count ?? (z.zone_type === "ltv" ? z.sample_count : 0);
    return n ? `${fmtLtv(z.median_ltv)}${t("（{n} 笔）", { n })}` : fmtLtv(z.median_ltv);
  }

  // 数字千分位随界面语言：en 用 en-US 分组，繁中 zh-HK，简中 zh-CN
  const numLocale = () =>
    window.__LANG__ === "en" ? "en-US" : window.__LANG__ === "zh-HK" ? "zh-HK" : "zh-CN";

  /* ---------------- 颜色 ---------------- */
  /* 网格中心坐标：优先用后端回填的区块中心；中心缺失（LTV 网格为坐标补全前的历史
     构建结果）时从 zone_id 尾部解出网格角点。 */
  function zoneCoord(z) {
    if (z.center_lng != null && z.center_lat != null) {
      return { lng: Number(z.center_lng), lat: Number(z.center_lat) };
    }
    const m = /(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\s*$/.exec(z.zone_id || "");
    return m ? { lng: Number(m[1]), lat: Number(m[2]) } : null;
  }

  /* 网格的人类可读短名：中心短坐标（3 位小数，约百米级）。坐标口径只有这一处，
     列表 / 地图 tooltip / 下钻详情都从这里取值，保证同一网格在任何位置写法一致。
     zone_id（如 price-gz-113.82-23.28）只放 title 供追溯，不作为主文案。
     短名不带城市：高危网格列表自带「城市」列，内嵌一次会把城市名写两遍。 */
  function zoneShortLabel(z) {
    const c = zoneCoord(z);
    return c
      ? `${t("网格")} ${c.lng.toFixed(3)}°E ${c.lat.toFixed(3)}°N`
      : `${t("网格")} ${z.zone_id || "-"}`;
  }

  /* 地图 tooltip 与下钻详情没有独立的城市字段，这里补上城市前缀。 */
  function zoneFullLabel(z) {
    return `${t(z.city_name || t("未标注"))} · ${zoneShortLabel(z)}`;
  }

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
          { color: "#b91c1c", label: t("高危网格") },
          { color: "#2563eb", label: t("正常区块") },
        ],
      };
    }
    const vals = zones.map((z) => layerValue(z, layer)).filter((v) => v != null);
    if (!vals.length) {
      return { color: () => "#d1d5db", stops: [{ color: "#d1d5db", label: t("无数据") }] };
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
          { color: col(-m), label: fmtPct(-m) + t("（洼地）") },
          { color: col(-m / 2), label: fmtPct(-m / 2) },
          { color: col(0), label: "0" },
          { color: col(m / 2), label: fmtPct(m / 2) },
          { color: col(m), label: fmtPct(m) + t("（高地）") },
        ],
      };
    }
    const from = layer === "poi" ? "#e0f2fe" : "#fef3c7";
    const to = layer === "poi" ? "#075985" : "#b91c1c";
    const span = hi - lo || 1;
    const col = (v) => (v == null ? "#d1d5db" : lerpColor(from, to, (v - lo) / span));
    const fmt = (v) => v.toFixed(1) + t(LAYERS[layer].unit);
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
      { label: t("网格总数"), value: k.zone_count },
      { label: t("高危网格"), value: k.high_risk_zone_count },
      {
        label: t("覆盖挂牌样本"),
        value: k.sample_count.toLocaleString(numLocale()),
      },
      { label: t("位于高危网格贷款"), value: k.high_risk_loan_count },
      {
        label: t("位于高危网格不良率"),
        value: k.high_risk_npl_rate_balance == null ? "-" : fmtPct(k.high_risk_npl_rate_balance),
      },
    ];
    el.querySelector("#p5-kpi").innerHTML = cards
      .map(
        (c) =>
          `<div class="kpi"><div class="kpi-label">${esc(c.label)}</div>` +
          `<div class="kpi-value">${esc(String(c.value))}</div></div>`
      )
      .join("");
  }

  let _map = null;
  let _zoneLayer = null;
  let _loanLayer = null;
  let _tileErrShown = false;

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
      _tileErrShown = false;
    }
    if (!_map) {
      // 封顶缩放：地图不设 maxZoom 时滚轮可放大到瓦片层级之外（>19），整片空白、无法查看。
      _map = L.map(container, { preferCanvas: true, minZoom: 4, maxZoom: 18 }).setView(
        [23.1, 113.3],
        9
      );
      const tileLayer = L.tileLayer(
        state.data && state.data.tile_url
          ? state.data.tile_url
          : "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {
          maxZoom: 19,
          maxNativeZoom: 12,
          attribution: "&copy; OpenStreetMap contributors",
          // 瓦片 404（离线/低层级）时用浅灰底占位，避免裂图破坏整片视觉。
          errorTileUrl:
            "data:image/svg+xml;charset=utf-8," +
            encodeURIComponent(
              '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#eef2f6"/></svg>'
            ),
        }
      ).addTo(_map);
      // 底图缺失时给出非阻断提示，且不遮挡地图交互与数据层（网格点/抵押物 marker）。
      // 按「批」而非按单张瓦片判定：tileerror 只累加计数，Leaflet 在当前视野的瓦片全部
      // 结束加载（成功或失败）时触发 load，那时才决定显示还是撤销。否则一批里偶发一张
      // 404 就误报，更要命的是提示一经出现就永不撤销——一次瞬时重启会让用户此后一直
      // 看到「加载失败」的假状态（即使后续批次全 200）。
      let okCount = 0;
      let errCount = 0;
      tileLayer.on("tileload", (e) => {
        // 出错瓦片的 src 会被换成 errorTileUrl（data: 占位图），它自身能解码、也会再触发
        // 一次 tileload —— 不计入这条判据就把「整片失败」误判成「有成功」，提示永不出现。
        const src = (e.tile && e.tile.getAttribute("src")) || "";
        if (src.startsWith("data:")) return;
        okCount++;
      });
      tileLayer.on("tileerror", () => {
        errCount++;
      });
      tileLayer.on("load", () => {
        // 一批有三种结果：有成功瓦片（撤销提示 + 放开去重，这才是自愈）/ 整批无一成功且有
        // 失败（只提示一次）/ ok 与 err 都是 0 的空转批 —— 出错瓦片的 data: 占位图回填后
        // Leaflet 会再触发一次 load，此时若按「非失败批」一律撤销，提示会在出现的同一刻
        // 被自己撤掉，所以空转批必须保持原状。
        const ok = okCount;
        const err = errCount;
        okCount = 0;
        errCount = 0;
        if (ok > 0) {
          _tileErrShown = false;
          const stale = document.querySelector(".p5-tile-overlay");
          if (stale) stale.remove();
          return;
        }
        if (!(ok === 0 && err > 0) || _tileErrShown) return;
        _tileErrShown = true;
        const host = document.getElementById("p5-leaflet");
        if (!host || !host.parentNode) return;
        const overlay = document.createElement("div");
        overlay.className = "p5-tile-overlay";
        overlay.textContent = t(
          "底图瓦片加载失败或当前缩放层级无瓦片（可能离线/无瓦片服务）；空间网格与抵押物点位不受影响。"
        );
        Object.assign(overlay.style, {
          position: "absolute",
          left: "8px",
          bottom: "8px",
          maxWidth: "320px",
          padding: "8px 12px",
          fontSize: "12px",
          lineHeight: "1.5",
          color: "#334155",
          background: "rgba(255,255,255,0.92)",
          border: "1px solid #cbd5e1",
          borderRadius: "8px",
          pointerEvents: "none",
          zIndex: 500,
        });
        host.parentNode.style.position = host.parentNode.style.position || "relative";
        host.parentNode.appendChild(overlay);
      });
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
          interactive: false,
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
      `<span style="color:#374151;font-weight:600">${esc(t(LAYERS[state.layer].label))}${t("：")}</span>` +
      scale.stops
        .map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`)
        .join("");
    legend += `<span style="margin-left:12px"><i style="background:none;border:1px dashed #b91c1c"></i>${t("高危标记圈（高危网格外描边）")}</span>`;
    if (state.showLoans) {
      legend +=
        `<span style="margin-left:12px;color:#374151">${t("抵押物（圆点）：")}</span>` +
        Object.keys(CLASS_COLORS)
          .map((c) => `<span><i style="background:${CLASS_COLORS[c]}"></i>${esc(t(c))}</span>`)
          .join("");
    }
    el.querySelector("#p5-legend").innerHTML = legend;
  }

  function renderHighRiskTable(el) {
    const rows = state.data.high_risk_zones;
    const tb = el.querySelector("#p5-hr-table tbody");
    const table = el.querySelector("#p5-hr-table");
    // 整列无值时隐藏「片区抵押 LTV 中位」，避免给用户一排无意义的 "-"。
    const hasLtv = rows.some((z) => z.median_ltv != null);
    const ltvTh = hasLtv ? `<th class="num">${t("片区抵押 LTV 中位")}</th>` : "";
    table.querySelector("thead tr").innerHTML =
      `<th>${t("城市")}</th><th>${t("网格位置")}</th><th class="num">${t("中位单价")}</th>${ltvTh}` +
      `<th class="num">${t("价格偏离度")}</th><th>${t("命中规则")}</th><th class="num">${t("样本")}</th>`;
    const cols = hasLtv ? 7 : 6;
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="${cols}" class="empty">${t("当前筛选下无高危网格")}</td></tr>`;
      return;
    }
    tb.innerHTML = rows
      .map(
        (z) =>
          `<tr data-zid="${esc(z.zone_id)}" style="cursor:pointer">` +
          `<td>${esc(t(z.city_name))}</td>` +
          `<td title="${t("网格编号：")}${esc(z.zone_id)}">${esc(zoneShortLabel(z))}</td>` +
          `<td class="num">${z.median_unit_price == null ? "-" : Number(z.median_unit_price).toLocaleString(numLocale()) + t(" 元/㎡")}</td>` +
          (hasLtv ? `<td class="num">${zoneLtvText(z)}</td>` : "") +
          `<td class="num" style="color:#b91c1c;font-weight:600">${z.price_dev_vs_city == null ? "-" : fmtPct(z.price_dev_vs_city)}</td>` +
          `<td><span class="badge bad" title="${esc(t(RULE_LABEL[z.high_risk_rule] ? RULE_LABEL[z.high_risk_rule].full : ""))}">${RULE_LABEL[z.high_risk_rule] ? esc(t(RULE_LABEL[z.high_risk_rule].short)) : esc(z.high_risk_rule || "-")}</span></td>` +
          `<td class="num">${z.sample_count}</td>` +
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
    const items = state.data.city_layers
      .filter((c) => c[key] != null)
      // 按当前图层的数值排序（价格偏离度则最负的排最前=偏离最大），保证列表一眼可读。
      .sort((a, b) => {
        const va = Number(a[key]);
        const vb = Number(b[key]);
        return layer === "dev" ? va - vb : vb - va;
      });
    const box = el.querySelector("#p5-city-layers");
    if (!items.length) {
      box.innerHTML = `<div class="empty">${t("无城市维度统计")}</div>`;
      return;
    }
    const max = Math.max(...items.map((c) => Math.abs(Number(c[key]))));
    const unit = layer === "commute" ? t(" 分钟") : layer === "dev" ? "" : t(" 个/km²");
    box.innerHTML = items
      .map((c) => {
        const v = Number(c[key]);
        const w = (Math.abs(v) / (max || 1)) * 100;
        const txt = layer === "dev" ? fmtPct(v) : v.toFixed(1) + unit;
        // 颜色与地图图例（makeScale）同口径：poi 低浅蓝→高深青、commute 低浅黄→高红、
        // dev 负(洼地)→红、正(高地)→蓝。按相对全局 max 归一——此前仅 v<0 判红、
        // 其余默认蓝，POI/通勤全为正数时整列同色无区分度。
        let bg = "";
        if (layer === "poi") bg = lerpColor("#e0f2fe", "#075985", v / (max || 1));
        else if (layer === "commute") bg = lerpColor("#fef3c7", "#b91c1c", v / (max || 1));
        else if (layer === "dev")
          bg =
            v < 0
              ? lerpColor("#e5e7eb", "#b91c1c", -v / (max || 1))
              : lerpColor("#e5e7eb", "#1d4ed8", v / (max || 1));
        return (
          `<div class="hbar-row"><div class="hbar-label">${esc(t(c.name))}</div>` +
          `<div class="hbar-track"><div class="hbar-fill" style="width:${w.toFixed(1)}%${bg ? `;background:${bg}` : ""}"></div></div>` +
          `<div class="hbar-num">${esc(txt)} · ${c.listing_count} ${t("行")}</div></div>`
        );
      })
      .join("");
    el.querySelector("#p5-city-layers-title").textContent =
      layer === "commute"
        ? t("城市平均通勤时长")
        : layer === "dev"
          ? t("城市平均价格偏离度")
          : t("城市平均 POI 密度");
  }

  function grp(g, cls) {
    return (
      `<td class="num">${g.loan_count}</td>` +
      `<td class="num">${fmtMoney(g.balance)}</td>` +
      `<td class="num">${g.npl_count}</td>` +
      `<td class="num">${fmtMoney(g.npl_balance)}</td>` +
      `<td class="num" style="font-weight:700;color:${cls}">${g.npl_rate_count == null ? "-" : fmtPct(g.npl_rate_count)}</td>` +
      `<td class="num" style="font-weight:700;color:${cls}">${g.npl_rate_balance == null ? "-" : fmtPct(g.npl_rate_balance)}</td>` +
      `<td class="num">${g.avg_ltv == null ? "-" : fmtLtv(g.avg_ltv)}</td>` +
      `<td class="num">${g.alert_count}</td>`
    );
  }

  function renderNpl(el) {
    const n = state.data.npl;
    let html =
      `<table class="table"><thead><tr>` +
      `<th>${t("分组")}</th><th class="num">${t("笔数")}</th><th class="num">${t("余额敞口")}</th><th class="num">${t("不良笔数")}</th><th class="num">${t("不良余额")}</th>` +
      `<th class="num">${t("不良率(笔数)")}</th><th class="num">${t("不良率(余额)")}</th><th class="num">${t("平均 LTV")}</th><th class="num">${t("预警笔数")}</th></tr></thead><tbody>` +
      `<tr><td><span class="badge bad">${t("高危网格")}</span></td>${grp(n.high, "#b91c1c")}</tr>` +
      `<tr><td><span class="badge ok">${t("非高危网格")}</span></td>${grp(n.normal, "#15803d")}</tr>` +
      `<tr style="font-weight:700;background:#f8fafc"><td>${t("全量")}</td>${grp(n.total, "#374151")}</tr>` +
      `</tbody></table>`;

    el.querySelector("#p5-npl").innerHTML =
      html +
      `<div class="check-line" style="margin-top:8px;color:#6b7280">` +
      t(
        "解读：本页「高危」判定来自挂牌价格面信号（价格洼地 / LTV 集中），并非由历史不良表现推得。因此高危网格的当前不良率未必更高——它提示的是该片区抵押物未来变现时的折价风险，宜与预警列表联动跟踪。"
      ) +
      `</div>`;
  }

  /* 偏差分桶的近似中心值（%）：'<-30%'→-35、'-30~-20%'→-25、'0~10%'→5、'>30%'→35。
     deviation_hist 的 bucket 是有序区间串，供「网格内挂牌价格偏差分布」按偏差强度做渐变着色。 */
  function bucketCenter(bucket) {
    const range = /(-?\d+)\s*~\s*(-?\d+)/.exec(bucket || "");
    if (range) return (Number(range[1]) + Number(range[2])) / 2;
    const lt = /<(-?\d+)/.exec(bucket || "");
    if (lt) return Number(lt[1]) - 5;
    const gt = />(-?\d+)/.exec(bucket || "");
    if (gt) return Number(gt[1]) + 5;
    return 0;
  }

  function renderDetail(el) {
    const box = el.querySelector("#p5-detail");
    const d = state.detail;
    if (!d) {
      box.innerHTML = "";
      return;
    }
    const z = d.zone;
    let html =
      `<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:baseline">` +
      `<div style="font-weight:700">${esc(zoneFullLabel(z))}</div>` +
      `<div>${z.zone_type === "price" ? t("价格面网格") : t("LTV 网格")}<span class="hbar-num" style="margin-left:8px" title="${t("内部追溯用网格编号")}">${t("编号")} ${esc(z.zone_id)}</span></div>` +
      `<div>${z.is_high_risk_zone ? `<span class="badge bad">${t("高危 · ")}${esc(t(RULE_LABEL[z.high_risk_rule] ? RULE_LABEL[z.high_risk_rule].full : z.high_risk_rule || ""))}</span>` : `<span class="badge ok">${t("正常")}</span>`}</div>` +
      `<div class="hbar-num">${t("中心")} ${z.center_lng?.toFixed(4)}°E, ${z.center_lat?.toFixed(4)}°N · ${t("样本")} ${z.sample_count} · ${t("构建日")} ${esc(z.build_date || "-")}</div>` +
      `</div>`;

    html +=
      `<table class="table" style="margin-top:10px"><thead><tr><th>${t("实体类型")}</th><th class="num">${t("数量")}</th>` +
      `<th class="num">${t("POI 密度均值")}</th><th class="num">${t("通勤均值")}</th><th class="num">${t("价格偏差均值")}</th><th class="num">${t("特征缺失率均值")}</th></tr></thead><tbody>` +
      (d.entities.length
        ? d.entities
            .map(
              (e) =>
                `<tr><td>${esc(t(ENTITY_TYPE_LABELS[e.entity_type] || e.entity_type))}</td><td class="num">${e.count}</td>` +
                `<td class="num">${e.poi_density_avg == null ? "-" : e.poi_density_avg.toFixed(2) + t(" 个/km²")}</td>` +
                `<td class="num">${e.commute_min_avg == null ? "-" : e.commute_min_avg.toFixed(1) + t(" 分钟")}</td>` +
                `<td class="num">${e.price_deviation_avg == null ? "-" : fmtPct(e.price_deviation_avg)}</td>` +
                `<td class="num">${e.missing_pct_avg == null ? "-" : e.missing_pct_avg.toFixed(1) + "%"}</td></tr>`
            )
            .join("")
        : `<tr><td colspan="6" class="empty">${t("该网格无实体特征（LTV 网格为坐标补全前的历史构建结果）")}</td></tr>`) +
      `</tbody></table>`;

    const hist = d.deviation_hist.filter((h) => h.count > 0);
    if (hist.length) {
      const max = Math.max(...hist.map((h) => h.count));
      // 按偏差中心值做发散渐变（与地图 dev 图层同口径）：负偏差(低估/洼地)→红、
      // 正偏差(高估)→蓝、0 附近→浅灰。替代原先「< 或 - 前缀→红、其余→蓝」的二值色——
      // 同侧颜色此前无深浅变化，看不出偏差幅度。
      const centers = hist.map((h) => bucketCenter(h.bucket));
      const maxAbs = Math.max(...centers.map((v) => Math.abs(v)), 1);
      html +=
        `<div class="card-title" style="margin-top:12px">${t("网格内挂牌价格偏差分布（vs 5km 邻域中位）")}</div>` +
        hist
          .map((h, i) => {
            const c = centers[i];
            const bg =
              c < 0
                ? lerpColor("#e5e7eb", "#b91c1c", -c / maxAbs)
                : c > 0
                  ? lerpColor("#e5e7eb", "#1d4ed8", c / maxAbs)
                  : "#e5e7eb";
            return (
              `<div class="hbar-row"><div class="hbar-label">${esc(h.bucket)}</div>` +
              `<div class="hbar-track"><div class="hbar-fill" style="width:${((h.count / max) * 100).toFixed(1)}%;background:${bg}"></div></div>` +
              `<div class="hbar-num">${h.count} ${t("行")}</div></div>`
            );
          })
          .join("");
    }

    html += `<div class="card-title" style="margin-top:12px">${t("5km 内贷款敞口（共 {n} 笔）", { n: d.nearby_total })}</div>`;
    html += d.nearby_loans.length
      ? `<table class="table"><thead><tr><th>${t("贷款号")}</th><th class="num">${t("距离")}</th><th>${t("五级")}</th><th class="num">LTV</th><th class="num">${t("余额")}</th><th>${t("高危网格（引擎判定）")}</th><th>${t("抵押物地址")}</th></tr></thead><tbody>` +
        d.nearby_loans
          .map(
            (l) =>
              `<tr><td>${l.loan_id}</td><td class="num">${l.distance_km} km</td>` +
              `<td><span class="badge" style="background:${CLASS_COLORS[l.risk_class]}22;color:${CLASS_COLORS[l.risk_class]}">${esc(t(l.risk_class))}</span></td>` +
              `<td class="num">${fmtLtv(l.ltv)}</td><td class="num">${fmtMoney(l.balance)}</td>` +
              `<td>${l.is_high_risk_zone ? `<span class="badge bad">${t("是")}</span>` : "-"}</td>` +
              `<td>${esc(l.property_addr === "未标注" ? t("未标注") : l.property_addr)}</td></tr>`
          )
          .join("") +
        `</tbody></table>`
      : `<div class="check-line">${t("该网格 5km 内没有抵押物——即便判定为高危，当前也无实际敞口需要处置。")}</div>`;

    // T7 P5 → P6 网格联动入口：把该网格交给策略推演中心做作用域（zone 粒度）。
    // 交接走 sessionStorage 键（spf_policy_scope_prefill，值为 zone_id），
    // P6 挂载时消费即删——SPA 内 showPage 切页，不经 URL。
    html +=
      `<div style="margin-top:14px;display:flex;align-items:center;gap:8px">` +
      `<button type="button" class="btn btn-primary btn-sm" id="p5-to-policy" data-zone="${esc(z.zone_id)}"` +
      ` title="${t("把该网格带入策略推演中心的作用域（zone 粒度）")}">${t("拿去推演")}</button>` +
      `</div>`;

    box.innerHTML = html;
  }

  /* ---------------- tooltip ---------------- */

  function tooltipHtml(z) {
    const rows = [
      [t("网格位置"), zoneFullLabel(z)],
      [t("样本量"), z.sample_count],
      [
        t("中位单价"),
        z.median_unit_price == null
          ? "-"
          : Number(z.median_unit_price).toLocaleString(numLocale()) + t(" 元/㎡"),
      ],
      [t("片区抵押 LTV 中位"), zoneLtvText(z)],
      [t("价格偏离度"), z.price_dev_vs_city == null ? "-" : fmtPct(z.price_dev_vs_city)],
      [
        t("POI 密度均值"),
        z.poi_density_avg == null ? "-" : z.poi_density_avg.toFixed(2) + t(" 个/km²"),
      ],
      [t("通勤均值"), z.commute_min_avg == null ? "-" : z.commute_min_avg.toFixed(1) + t(" 分钟")],
      [
        t("判定"),
        z.is_high_risk_zone
          ? t("高危 · ") + t(RULE_LABEL[z.high_risk_rule] ? RULE_LABEL[z.high_risk_rule].full : z.high_risk_rule || "")
          : t("正常"),
      ],
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
      [t("贷款号"), l.loan_id],
      [t("抵押物"), l.collateral_id],
      [t("城市"), t(l.city_name)],
      [t("五级分类"), t(l.risk_class)],
      ["LTV", fmtLtv(l.ltv)],
      [t("余额"), fmtMoney(l.balance)],
      [t("高危网格（引擎判定）"), l.is_high_risk_zone ? t("是") : t("否")],
      [t("地址"), l.property_addr === "未标注" ? t("未标注") : l.property_addr],
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

    // 城市下拉每次都重填。原先有 dataset.filled 守卫（只填一次），但切语言会
    // 重新挂载本页调用 load()，选项文本就会停在切换前的语言上。重填后按
    // 「DOM 当前值优先、失效则回落 state.city」恢复选中，不打断未提交的选择。
    const sel = el.querySelector("#p5-city");
    const curCity = sel.value;
    sel.innerHTML =
      `<option value="">${t("全部城市")}</option>` +
      state.data.cities
        .map(
          (c) =>
            `<option value="${esc(c.code)}">${esc(
              t("{city}（{grid} 网格 / {hr} 高危 / {loan} 贷款）", {
                city: t(c.name),
                grid: c.zone_count,
                hr: c.high_risk_zones,
                loan: c.loan_count,
              })
            )}</option>`
        )
        .join("");
    if (Array.prototype.some.call(sel.options, (o) => o.value === curCity)) sel.value = curCity;
    else sel.value = state.city;
    el.querySelector("#p5-build-date").textContent = state.data.build_date
      ? `${t("空间画像构建日：")}${state.data.build_date}`
      : "";

    renderKpi(el, state.data);
    renderMap(el);
    renderHighRiskTable(el);
    renderCityLayers(el);
    renderNpl(el);
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

    // T7：「拿去推演」→ 写 sessionStorage 交接键并切到策略推演中心（P6）。
    // 事件挂在 #p5-detail 上用委托：renderDetail 每次选中网格都会整块重画。
    el.querySelector("#p5-detail").addEventListener("click", (e) => {
      const btn = e.target.closest("#p5-to-policy");
      if (!btn) return;
      try {
        sessionStorage.setItem("spf_policy_scope_prefill", btn.dataset.zone);
      } catch (err) {
        void err; // sessionStorage 不可用（隐私模式等）：按钮退化为纯切页
      }
      showPage("policy");
    });
  }

  const HTML = function () {
    // 开关初值与 state 同步：散点默认开启时，勾选框也要显示为已勾选，避免「散点在、
    // 开关却看起来没勾」的状态矛盾（HTML 无 checked 属性时初态即未勾选）。
    const showLoansChecked = state.showLoans ? " checked" : "";
    return `
<h1 class="page-title">${t("空间风险画像")}</h1>

<div class="filter-bar">
  <label>${t("城市")}<select id="p5-city"></select></label>
  <label>${t("网格类型")}<select id="p5-zone-type">
    <option value="price">${t("价格面网格")}</option>
    <option value="ltv">${t("LTV 集中网格")}</option>
    <option value="">${t("全部")}</option>
  </select></label>
  <label>${t("图层")}<select id="p5-layer">
    <option value="risk">${t("高危网格判定")}</option>
    <option value="poi">${t("POI 密度（挂牌密度代理）")}</option>
    <option value="commute">${t("通勤时长（直线近似）")}</option>
    <option value="dev">${t("价格偏离度")}</option>
  </select></label>
  <label style="flex-direction:row;align-items:center;gap:6px">
    <input type="checkbox" id="p5-show-loans" style="min-width:auto"${showLoansChecked}> ${t("叠加抵押物点位")}
  </label>
  <div class="filter-total" id="p5-build-date"></div>
</div>

<div class="kpi-row" id="p5-kpi"></div>

<div class="card" style="margin-top:16px">
  <div class="card-title">${t("空间分布图")}</div>
  <div id="p5-map">
    <div id="p5-leaflet" style="height:560px;width:100%;background:#eef2f6"></div>
  </div>
  <div class="legend" id="p5-legend"></div>
</div>

<div class="card" style="margin-top:16px">
  <div class="card-title">${t("网格下钻明细")}</div>
  <div id="p5-detail"></div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title">${t("高危网格列表")}</div>
    <table class="table" id="p5-hr-table">
      <thead><tr><th>${t("城市")}</th><th>${t("网格位置")}</th><th class="num">${t("中位单价")}</th><th class="num">${t("片区抵押 LTV 中位")}</th><th class="num">${t("价格偏离度")}</th><th>${t("命中规则")}</th><th class="num">${t("样本")}</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="card">
    <div class="card-title" id="p5-city-layers-title">${t("城市图层统计")}</div>
    <div class="hbar-list" id="p5-city-layers"></div>
  </div>
</div>

<div class="card" style="margin-top:16px">
  <div class="card-title">${t("高危网格不良（NPL）集中度")}</div>
  <div id="p5-npl"></div>
</div>
`;
  };

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
