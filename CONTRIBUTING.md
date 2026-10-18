# 开发规范（Contributing）

本仓库已完成产品全周期文档（阶段 0–6）与核心工程实现（估值 → 风控 → 报送 → 前端闭环），当前处于维护与演示阶段。下列规范用于统一协作方式，降低评审与集成成本。

## 1. 环境准备

```bash
# 1) 本地钩子（Python）——先选择一个已装好依赖的 Python（目标 3.10；仓库不随 git 分发 .venv）
pip install pre-commit==4.6.1   # 与 environment.yml / requirements-lite.txt 钉版一致
pre-commit install
pre-commit install --hook-type commit-msg   # 启用提交信息校验

# 2) 提交信息与前端校验（Node）
npm install
```

> 不装也行：CI 会在 PR 中再次校验提交信息和代码风格（双重保障）。

> **git blame**：纯格式化之类的大批量机械改动会把 blame 结果冲掉，这类提交已记入
> `.git-blame-ignore-revs`。本地执行一次即可永久跳过：
>
> ```bash
> git config blame.ignoreRevsFile .git-blame-ignore-revs
> ```

## 2. 提交信息规范（Conventional Commits）

提交信息格式：

```
<type>(<scope>): <subject>
```

- **type**：`feat` / `fix` / `docs` / `style` / `refactor` / `perf` / `test` / `build` / `ci` / `chore` / `revert`
- **scope**（可选）：模块或阶段，填写时须命中 `commitlint.config.mjs` 的 `scope-enum` 白名单，如
  `avm`、`risk`、`ltv`、`spatial`、`alerting`、`reporting`、`crawler`、`orchestrator`、`etl`、`cdc`、
  `lake`、`frontend`、`llm`、`persona`、`airflow`、`ops`、`docs`、`adr`、`evidence`、`trace`
  （完整列表见 `commitlint.config.mjs`）
- **subject**：祈使句、简洁，不超过 100 字符，不以句号结尾

示例：

```
feat(avm): 接入 HistGBR + 空间近邻特征月度重估任务
fix(ltv): 修正 LTV 红线比对中余额口径不一致
docs(product/04): 补充设计评审纪要
```

不符合规范的提交会被本地钩子与 CI 同时拦截。

## 3. 分支模型（建议）

| 分支              | 用途                                                            |
| ----------------- | --------------------------------------------------------------- |
| `main` / `master` | 受保护主干，仅通过 PR 合并                                      |
| `dev`             | 集成分支（CI / Commitlint / CodeQL 均监听 main / master / dev） |
| `feature/*`       | 功能开发                                                        |
| `fix/*`           | 缺陷修复                                                        |
| `release/v*`      | 发版（打 tag 触发 Deploy 流水线）                               |

建议开启的分支保护（仓库 Settings → Branches）：

- 保护 `main` / `master` / `dev`：禁止直接 push
- 要求 PR 通过 **CI** 与 **Commitlint** 两个检查
- 要求 PR 至少 1 个审批（review）
- 合并前要求分支为最新（up to date）

## 4. PR 流程

1. 从 `dev` 切出 `feature/*` 或 `fix/*`
2. 提交遵循 Conventional Commits
3. 推送并开 PR，填写 PR 模板（关联文档 / 阶段、测试说明）
4. 通过 CI（pre-commit 全量校验）与 Commitlint（提交信息）检查
5. 评审通过后合并；需要发版时打 `v*` tag

## 5. 语言检查（双栈已启用）

`.pre-commit-config.yaml` 中 Python 与 Node/TS 双栈钩子均已启用：Python 用
`ruff`（v0.16.0，--fix，配置见 `pyproject.toml`，含 `ruff-format`）；Node/TS 用
`eslint`（--fix）与 `prettier`（--check），ESLint 配置见 `eslint.config.mjs`。
本地提交时会自动运行，CI 的 pre-commit 全量校验也会再次执行。

## 6. 合规要求

项目涉及金融数据，**严禁**提交真实个人金融信息与未脱敏数据；仅使用合成 / 模拟样本。

## 7. 变更可追溯性（必读）

产品演进的追溯契约见 [docs/TRACEABILITY.md](docs/TRACEABILITY.md)。任何变更落入下列情形时，PR 须同时提供对应追溯载体，否则不予合并：

| 变更类型 | 必须提供 |
| -------- | -------- |
| 架构 / 选型 / 范围变化（引入或移除组件、替换技术路径） | 一篇 ADR（`docs/adr/`，模板见 `docs/adr/README.md`） |
| 阈值 / 规则 / 模型 / Prompt 变化 | ADR（如属决策）+ 更新 `docs/evidence/registry.json` 对应条目 |
| 产生新的性能或规模数字 | `docs/evidence/registry.json` 新条目（含 `source_artifact` 与 `reproducibility`） |
| 面向外部的数字出现在 README / 文档 | 该数字须能在 `registry.json` 找到同名 `metric_id` |

- 数字一律标注可信度：`verified`（可由仓库复算）/ `recorded`（历史报告，产物未分发）/ `unavailable`（未测量）。**不得把 `recorded` 写成 `verified`。**
- 本地可一键自检：`python tools/dev/trace_check.py`（链接 / ADR 字段 / 证据登记 / 文档一致性 / 术语黑名单）。
- 生成证据快照：`python tools/dev/gen_evidence.py`；校验漂移：`python tools/dev/gen_evidence.py --check`。
