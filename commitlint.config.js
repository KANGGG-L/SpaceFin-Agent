module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    // 允许的提交类型（与项目阶段/职责对应）
    "type-enum": [
      2,
      "always",
      [
        "feat", // 新功能
        "fix", // 缺陷修复
        "docs", // 文档（产品/技术/PoC 文档）
        "style", // 格式调整（不影响代码含义）
        "refactor", // 重构
        "perf", // 性能优化
        "test", // 测试
        "build", // 构建 / 依赖
        "ci", // CI/CD
        "chore", // 杂项
        "revert", // 回滚
      ],
    ],
    "subject-case": [0],
    "subject-full-stop": [0, "never"],
    "header-max-length": [2, "always", 100],
  },
};
