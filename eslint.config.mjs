import js from "@eslint/js";

export default [
  js.configs.recommended,
  {
    // 第三方前端库（本地自托管，如 tools/frontend/static/vendor/leaflet）跳过 lint：
    // 它们来自上游发行版，按文件本身的质量基线交付，跑 no-undef 只会制造噪音。
    ignores: ["commitlint.config.mjs", "eslint.config.mjs", "tools/frontend/static/vendor/**"],
  },
  {
    rules: {
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "no-console": "off",
    },
  },
];
