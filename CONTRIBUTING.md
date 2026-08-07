# 开发规范（Contributing）

本仓库刚完成产品资料筹备、进入开发阶段。下列规范用于统一协作方式，降低评审与集成成本。

## 1. 环境准备

```bash
# 1) 本地钩子（Python）
conda activate spark
pip install pre-commit
pre-commit install
pre-commit install --hook-type commit-msg   # 启用提交信息校验

# 2) 提交信息与前端校验（Node）
npm install
```

> 不装也行：CI 会在 PR 中再次校验提交信息和代码风格（双重保障）。

## 2. 提交信息规范（Conventional Commits）

提交信息格式：

```
<type>(<scope>): <subject>
```

- **type**：`feat` / `fix` / `docs` / `style` / `refactor` / `perf` / `test` / `build` / `ci` / `chore` / `revert`
- **scope**（可选）：模块或阶段，如 `avm`、`ltv`、`1104`、`poc`、`docs`
- **subject**：祈使句、简洁，不超过 100 字符，不以句号结尾

示例：

```
feat(avm): 接入 MGWR+GBDT 月度重估任务
fix(ltv): 修正 LTV 红线比对中余额口径不一致
docs(product/04): 补充设计评审纪要
```

不符合规范的提交会被本地钩子与 CI 同时拦截。

## 3. 分支模型（建议）

| 分支 | 用途 |
|------|------|
| `main` / `master` | 受保护主干，仅通过 PR 合并 |
| `develop` | 集成分支 |
| `feature/*` | 功能开发 |
| `fix/*` | 缺陷修复 |
| `release/v*` | 发版（打 tag 触发 Deploy 流水线） |

建议开启的分支保护（仓库 Settings → Branches）：

- 保护 `main` / `master` / `develop`：禁止直接 push
- 要求 PR 通过 **CI** 与 **Commitlint** 两个检查
- 要求 PR 至少 1 个审批（review）
- 合并前要求分支为最新（up to date）

## 4. PR 流程

1. 从 `develop` 切出 `feature/*` 或 `fix/*`
2. 提交遵循 Conventional Commits
3. 推送并开 PR，填写 PR 模板（关联文档 / 阶段、测试说明）
4. 通过 CI（pre-commit 全量校验）与 Commitlint（提交信息）检查
5. 评审通过后合并；需要发版时打 `v*` tag

## 5. 语言检查（按需启用）

仓库当前为「预留双栈」结构，`.pre-commit-config.yaml` 中已注释好 Python（ruff）与
Node/TS（eslint、prettier）的钩子。待实际模块落地后，取消对应注释并在根目录补充
`pyproject.toml`（ruff 配置）或 `eslint` 配置即可生效。

## 6. 合规要求

项目涉及金融数据，**严禁**提交真实个人金融信息与未脱敏数据；仅使用合成 / 模拟样本。
