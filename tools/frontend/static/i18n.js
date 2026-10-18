/* i18n.js —— SpaceFin 前端多语言框架（vanilla JS，无依赖）
 *
 * 用法约定：
 *  - t(key)：返回当前语言下 key 的译文；key 即 zh-CN 原文，zh-CN 或无译文时原样返回 key。
 *    静态测试会对 JS 源码里的中文字面量做正则断言，因此页面里一律写 t("中文原文")，
 *    译文只存在于注册的字典里，绝不动原文。
 *  - registerI18n("en" | "zh-HK", { "中文原文": "译文", ... }, ns?)：注册语言字典。
 *    ns 省略时落 "global"（公共术语层：城市/行业/动作等跨页复用词，app.js 注册）；
 *    页面模块传自己的页面 id（如 "policy"），互不覆盖——否则各页同 key 不同译法时
 *    后加载的页会静默吞掉先加载的页的译文（last-write-wins）。
 *  - setNamespace(ns)：切页时由 app.js 的 showPage 调用，t() 先查当前 ns 再回退 global。
 *  - setLang(code)：切换语言并写入 localStorage（key: spacefin-lang），触发
 *    window.__onLangChange(code)（app.js 用它重渲染当前页）。
 *  - applyI18nText(root)：遍历 [data-i18n] 元素，只替换「直接文本节点」（保留子元素，
 *    供 <label>…<select>…</select></label> 这类混合内容使用）；同时支持
 *    [data-i18n-ph]（placeholder）、[data-i18n-title]（title）属性翻译。
 *  - buildLangSwitcher()：生成语言切换下拉，注入到任意挂载点。
 */

(function () {
  "use strict";

  var LANGS = [
    { code: "zh-CN", label: "简体中文" },
    { code: "en", label: "English" },
    { code: "zh-HK", label: "繁體中文" },
  ];

  // 按页命名空间隔离：dicts[lang][ns][key]。页面各自的 registerI18n(..., ns) 互不覆盖，
  // 跨页复用的公共术语（城市/行业/动作等）由 app.js 注册进 global，各页不再重复注册。
  var dicts = { "zh-CN": { global: {} }, en: { global: {} }, "zh-HK": { global: {} } };
  var currentNs = "global";
  var lang = "zh-CN";
  try {
    var saved = localStorage.getItem("spacefin-lang");
    if (saved && dicts[saved]) lang = saved;
  } catch (_) {
    // localStorage 不可用（隐私模式 / 被禁用）：读不到就用默认语言，无需处理。
  }

  document.documentElement.lang = lang;
  window.__LANG__ = lang;

  // 占位符整句翻译：t("共 {n} 条", { n: 5 })。
  // 中文里同一个「条」在不同句子里英文可能是 items / more / with 0 hits，把句子拆成
  // 片段翻译必然译错，所以整句进字典、变量用 {name} 占位。不传 vars 时与旧行为一致。
  // 查找顺序：当前页 ns → global 公共层 → 原样返回 key。
  //
  // 复数占位：{n|单数|复数} —— 按 n 是否等于 1 选形，只替换名词本身，数字仍由 {n} 渲染。
  // 例："LTV Alert Overview ({n} {n|record|records})" → "… (1 record)" / "… (5 records)"。
  // 选它而不是在调用点写 n === 1 ? … : … 或独立的 plural(n, …) 辅助函数，是因为：
  //  1. 名词在译文句子内部，只有把选形能力放进占位符，译文才不用被拆成片段拼接
  //     （拆片段正是本项目明令禁止的译错根源）；
  //  2. 调用点只多传一个 { n: count }，不含任何条件判断，单复数知识全留在字典里；
  //  3. 存量「片段型」词条（t(" 笔") → " loans"，数字拼在 t() 外面）也能用同一套语法
  //     收敛，不必先改写成整句。
  // 中文无副作用：zh-CN 无词条（原样返回 key）、zh-HK 词条里不写这种占位，
  // 两种中文下命中的是与改动前完全相同的分支，不会凭空多出空格或词。
  var PLURAL_RE = /\{(\w+)(?:\|([^|}]*)\|([^|}]*))?\}/g;

  // "1" / "1.0" / "1,000" → 单数；"0"、"2"、"1,200" → 复数。非数字一律落复数。
  function isSingular(v) {
    var n = Number(String(v).replace(/[,\s\u00a0]/g, ""));
    return Number.isFinite(n) ? Math.abs(n) === 1 : false;
  }

  function t(key, vars) {
    var langDict = dicts[lang] || {};
    var nsDict = langDict[currentNs] || {};
    var gDict = langDict.global || {};
    var s = nsDict[key] != null ? nsDict[key] : gDict[key] != null ? gDict[key] : key;
    var v = vars || {};
    return String(s).replace(PLURAL_RE, function (m, name, sing, plur) {
      if (sing === undefined) {
        // 普通占位漏传变量：保留 {name} 原样（zh-CN 渲染不受影响），但 console.warn
        // 提示调用点漏传——与复数占位（静默落复数形）的不对称在此说明。
        if (!Object.prototype.hasOwnProperty.call(v, name)) {
          try { console.warn("[i18n] 占位符变量未传: {" + name + "} in " + key); } catch (_) { /* console 被禁用时静默 */ }
          return m;
        }
        return String(v[name]);
      }
      // 复数形占位：没传 n 时落复数，绝不把占位语法原样漏进 DOM。
      if (!Object.prototype.hasOwnProperty.call(v, name)) return plur;
      return isSingular(v[name]) ? sing : plur;
    });
  }

  function registerI18n(code, dict, ns) {
    ns = ns || "global"; // ns 省略时落 global：存量两参调用行为不变（公共术语层）
    if (!dicts[code]) dicts[code] = {};
    if (!dicts[code][ns]) dicts[code][ns] = {};
    for (var k in dict) {
      if (Object.prototype.hasOwnProperty.call(dict, k)) dicts[code][ns][k] = dict[k];
    }
  }

  // 切页时由 app.js 的 showPage(id) 调用；页面模块加载时只注册字典、不切 ns，
  // 保证「注册发生在加载期、消费发生在渲染期」的时序下 t() 永远查对命名空间。
  function setNamespace(ns) {
    currentNs = ns || "global";
  }

  function getLang() {
    return lang;
  }

  function setLang(code) {
    if (!dicts[code]) return;
    lang = code;
    try {
      localStorage.setItem("spacefin-lang", code);
    } catch (_) {
      // localStorage 不可用：语言只在本次会话生效，下次回落默认语言，无需处理。
    }
    document.documentElement.lang = code;
    window.__LANG__ = code;
    if (typeof window.__onLangChange === "function") {
      try {
        window.__onLangChange(code);
      } catch (e) {
        console.error("[i18n] __onLangChange 执行失败:", e);
      }
    }
  }

  // 字典是否登记过这个 key。不能直接拿 tr === key 判断「没命中」：
  // 简繁同形的词（「城市」→「城市」、「操作」→「操作」）译文与 key 逐字相同，
  // 但确实是登记过的译文，必须照常写回 DOM——否则从 en 切到 zh-HK 时，
  // 这类元素会被误判成「未命中」而早退，把 en 的旧译文原样留在页面上。
  function hasTranslation(key) {
    var langDict = dicts[lang] || {};
    var nsDict = langDict[currentNs] || {};
    var gDict = langDict.global || {};
    return nsDict[key] != null || gDict[key] != null;
  }

  /* 替换 [data-i18n] 元素的直接文本节点：只动文本，不碰子元素。
   * 注意：zh-CN 是基准语言，t(key) 恒等于 key；若元素先前已被切到 en/zh-HK
   * 改写，切回 zh-CN 时必须重置为原文，故 zh-CN 下无条件执行替换。
   * 早退条件：字典（当前页 ns + global 两级）里都没有这个 key——此时保留
   * DOM 原文，绝不能用未命中的 key 覆写已渲染的译文。 */
  function applyTextToElement(el, key) {
    var tr = t(key);
    if (!hasTranslation(key) && lang !== "zh-CN") return;
    var hasChildEl = el.querySelector("*") != null;
    if (!hasChildEl) {
      if (el.tagName === "OPTION") {
        // <option value="正常">正常</option>：值保留原文（提交后端），只换显示文本。
        el.textContent = tr;
      } else {
        el.textContent = tr;
      }
      return;
    }
    var replaced = false;
    Array.prototype.forEach.call(el.childNodes, function (node) {
      if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) {
        node.nodeValue = tr;
        replaced = true;
      }
    });
    if (!replaced) {
      el.insertBefore(document.createTextNode(tr), el.firstChild);
    }
  }

  function applyI18nText(root) {
    var r = root || document;
    Array.prototype.forEach.call(r.querySelectorAll("[data-i18n]"), function (el) {
      var key = el.getAttribute("data-i18n") || "";
      if (!key) return;
      applyTextToElement(el, key);
    });
    // ph/title/aria 三个属性分支与文本分支同一套早退规则：
    // zh-CN 下无条件用原文回写（否则上一语言的译文会残留在属性里），
    // 其它语言只在字典登记过该 key 时回写。不能用 tr !== key 判断——
    // 简繁同形 / zh-CN 基准语言的译文与 key 逐字相同，会被误判成未命中。
    Array.prototype.forEach.call(r.querySelectorAll("[data-i18n-ph]"), function (el) {
      var key = el.getAttribute("data-i18n-ph") || "";
      if (!key) return;
      if (lang === "zh-CN" || hasTranslation(key)) el.setAttribute("placeholder", t(key));
    });
    Array.prototype.forEach.call(r.querySelectorAll("[data-i18n-title]"), function (el) {
      var key = el.getAttribute("data-i18n-title") || "";
      if (!key) return;
      if (lang === "zh-CN" || hasTranslation(key)) el.setAttribute("title", t(key));
    });
    Array.prototype.forEach.call(r.querySelectorAll("[data-i18n-aria]"), function (el) {
      var key = el.getAttribute("data-i18n-aria") || "";
      if (!key) return;
      if (lang === "zh-CN" || hasTranslation(key)) el.setAttribute("aria-label", t(key));
    });
    // 日期类输入控件（date/datetime-local/month/time）的展示格式与占位符随元素 lang 走。
    // 显式写入当前语言，保证原生日期格式跟随应用语言而非浏览器默认区域——
    // 否则切到 zh-HK/zh-CN 仍显示 mm/dd/yyyy 等西式格式，违反 i18n。
    Array.prototype.forEach.call(
      r.querySelectorAll('input[type="date"],input[type="datetime-local"],input[type="month"],input[type="time"]'),
      function (el) {
        el.setAttribute("lang", lang);
        syncDateDisplay(el);
      }
    );
  }

  // 把 ISO yyyy-mm-dd 按当前语言格式化为本地化显示串（如 zh-CN 2026/08/30，
  // en 08/30/2026 等）。供 .date-input-wrap 里的 date picker → text display 同步。
  function formatDateDisplay(iso, langCode) {
    if (!iso) return "";
    var parts = iso.split("-");
    if (parts.length !== 3) return iso;
    var y = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    var d = parseInt(parts[2], 10);
    var date = new Date(y, m - 1, d);
    if (isNaN(date.getTime())) return iso;
    try {
      return new Intl.DateTimeFormat(langCode, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(date);
    } catch (_e) {
      return iso;
    }
  }

  // 同步 .date-input-wrap 内 date picker 的 ISO 值到可见 text display。
  function syncDateDisplay(picker) {
    if (!picker || picker.type !== "date") return;
    var wrap = picker.closest(".date-input-wrap");
    if (!wrap) return;
    var display = wrap.querySelector("input.date-display");
    if (!display) return;
    display.value = formatDateDisplay(picker.value, lang);
  }

  // 日期选择器变更时同步本地化显示。
  document.addEventListener("change", function (e) {
    if (e.target && e.target.tagName === "INPUT" && e.target.type === "date") {
      syncDateDisplay(e.target);
    }
  });

  function buildLangSwitcher() {
    var sel = document.createElement("select");
    sel.className = "lang-switcher";
    sel.setAttribute("aria-label", "Language / 语言");
    LANGS.forEach(function (l) {
      var o = document.createElement("option");
      o.value = l.code;
      o.textContent = l.label;
      if (l.code === lang) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener("change", function () {
      setLang(sel.value);
      Array.prototype.forEach.call(document.querySelectorAll(".lang-switcher"), function (s) {
        if (s !== sel) s.value = sel.value;
      });
    });
    return sel;
  }

  window.I18N = {
    LANGS: LANGS,
    t: t,
    registerI18n: registerI18n,
    setNamespace: setNamespace,
    getLang: getLang,
    setLang: setLang,
    applyI18nText: applyI18nText,
    buildLangSwitcher: buildLangSwitcher,
  };
  window.t = t;
  window.registerI18n = registerI18n;
  window.setNamespace = setNamespace;
  window.applyI18nText = applyI18nText;
  window.buildLangSwitcher = buildLangSwitcher;
})();
