import js from "@eslint/js";

export default [
  js.configs.recommended,
  {
    // 第三方前端库（本地自托管，如 tools/frontend/static/vendor/leaflet）跳过 lint：
    // 它们来自上游发行版，按文件本身的质量基线交付，跑 no-undef 只会制造噪音。
    ignores: ["commitlint.config.mjs", "eslint.config.mjs", "tools/frontend/static/vendor/**"],
  },
  {
    languageOptions: {
      // 本仓库是 vanilla JS + 全局脚本（无打包器、无模块系统）：浏览器全局与 i18n.js
      // 挂到 window 上的函数都是真全局。不在这里声明，no-undef 会在每个页面文件上
      // 误报上千条（改配置前全仓库约 1200 条），噪音大到没人愿意看 lint 结果。
      globals: {
        // 浏览器 / DOM：手写而不 import "globals" 包——它只是 eslint 的传递依赖，
        // 未在 package.json 声明，直接 import 会在依赖树变动时突然崩。
        window: "readonly",
        document: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
        navigator: "readonly",
        clearTimeout: "readonly",
        Event: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        Blob: "readonly",
        CSS: "readonly",
        Node: "readonly",
        alert: "readonly",
        fetch: "readonly",
        location: "readonly",
        history: "readonly",
        // 高精度计时 / 下一帧调度（P6 运行按钮最小可见时长、地图折叠重绘）。
        performance: "readonly",
        requestAnimationFrame: "readonly",
        // 自托管第三方库（vendor/leaflet）挂的全局；vendor 目录本身已跳过 lint。
        L: "readonly",
        // i18n.js 挂载的全局（见 tools/frontend/static/i18n.js 末尾的 window.* 赋值）。
        // setLang / getLang 只挂在 window.I18N 上，页面不得裸调用，故不列入。
        t: "readonly",
        registerI18n: "readonly",
        applyI18nText: "readonly",
        buildLangSwitcher: "readonly",
        setNamespace: "readonly",
        // app.js 顶层声明的全局函数 / 常量（非模块脚本进入全局词法作用域，
        // 页面插件文件可访问；见 app.js 的 showPage、CITY_CODES、INDUSTRY_CODES、
        // POLICY_ACTIONS 等）。
        showPage: "readonly",
        showToast: "readonly",
        CITY_CODES: "readonly",
        INDUSTRY_CODES: "readonly",
        POLICY_ACTIONS: "readonly",
      },
    },
    rules: {
      // argsIgnorePattern 只管函数参数；catch (_) 里的绑定要单独配，
      // 否则「有意忽略异常」会被当成未使用变量报一堆警告。
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }],
      "no-console": "off",
    },
  },
];
