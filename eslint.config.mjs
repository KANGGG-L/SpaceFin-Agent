import js from "@eslint/js";

export default [
  js.configs.recommended,
  {
    ignores: ["commitlint.config.mjs", "eslint.config.mjs"],
  },
  {
    rules: {
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "no-console": "off",
    },
  },
];
