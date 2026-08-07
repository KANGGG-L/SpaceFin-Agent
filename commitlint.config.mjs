export default {
  extends: ["@commitlint/config-conventional"],
  // 跳过 merge 提交（形如 "Merge xxx: ..."），避免历史 feature 合并提交触发类型检查
  ignores: [(message) => /^Merge\s/.test(message)],
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
