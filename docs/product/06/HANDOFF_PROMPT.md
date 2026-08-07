# SpaceFin-Agent — 接手 Prompt（Handoff Prompt）

> 用途：把这份 prompt 整体复制给「下一个接手的工程师或 agent」，即可在零上下文的情况下接续本项目。
> 最后更新：2026-08-07（接入 I-04 倒排索引审计检索 + Superset BI 看板并收口）。验证基线：`dev`（合并 `feat/inverted-index-superset` 后，已推 origin）。

---

## 0. 你是谁 / 项目定位（先读这段，避免误判标准）

你接手的是 **SpaceFin-Agent**：**实质是校招作品集（portfolio）**，但按**最终交付版本（final deliverable）**标准完整构建与验收——凡能凭代码 / 合成数据达成的技术能力均已落地，不降级为「作品集范围外」。

- **验收标准 = 技术 AC + 可复现性 + 测试 + 文档自洽**，不是商业 SLA / 真实生产可用性。
- 仅「真实外部资源依赖项」（真实数据源、合规审批、真实试点行意向）**诚实声明为未达成（delivery-gap）**，不要把它当成 bug 去「修复」——那超出可凭代码交付的范围。
- 全链路 workflow 已把文档记录的所有可行 gap 收口完毕，G2/G4/G5/G8/G9/G10 已交付。你看到的状态**应当是 8/8 AC 全绿、645 测试通过（2026-08-07 全仓实测，含 I-04 倒排索引 3 项真机测试；2026-08-05 收口记录为 603/604，差异为计数口径）、D/RD/C 清单填满**。如果不是，先回到 §3 核对，再决定是不是你环境的问题。

---

## 1. 关键事实（必须相信，除非你亲眼证伪）

1. **AC-01~08 全部达成**（技术口径，见 `docs/product/03/README.md` §6）：
   - AC-01 CDC ≤10s（position 已持久化到 `ods_cdc_position`）
   - AC-02 五级一致性 ≥99.9%
   - AC-03 LTV 两级预警（WARN_LINE 双阈值）
   - AC-04 低置信度路由（标度统一 + 阈值 75 + 与空间网格解耦；端到端 101 低置信 / 预警 29→19）
   - AC-05 1104 报送模块
   - AC-06 PII 脱敏（最终交付版本口径）
   - AC-07 AVM 精度@覆盖：45% 覆盖 MAPE **9.88%** ≤10%
   - AC-08 报表一致性 100%
2. **AC-07 可复现结论（重要，曾有争议）**：
   - 规范配置 = `canonical r11` / 28 特征 / `HistGradientBoostingRegressor` + `quantile(0.45)` loss + `EB` 平滑。
   - 提交代码加 `--loss quantile --quantile 0.45 --smooth-mode eb` → **9.88%**（28 特征复现）。
   - 曾有 product-b 误称「10.49% 需要 91 特征实验」——**这是错的**（那是默认配置的结果）。**不要做特征移植**，28 特征 canonical 配置即达 9.88%。
   - 全量 MAPE 14.59%，oracle 下界 12.65%，baseline 20.64%。
3. **C-02 特征归因**：用 `sklearn.inspection.permutation_importance`（**不是 SHAP**）。原因：shap 未安装，且 HistGBR quantile 没有 `feature_importances_`。所有文档/前端文案已统一为 "permutation importance"，**不要再写 SHAP**。Top-5：`comm_mean 11.31 / comm_median 10.31 / city_code 7.12 / floor_total 2.43 / area 1.04`。
4. **测试**：15 个模块，当前全仓实测 **645 passed + 1 skipped**（2026-08-07；含 I-04 倒排索引 3 项真机测试）。跑法见 §4。
5. **前端**：零依赖方案，`tools/frontend`，端口 **8500**，RBAC **四角色**（admin/risk/da/postloan）。页面自动发现：`tools/frontend/pages/__init__.py` 的 `_discover()` 扫描 `pN_*.py`。P9（合规审计，角色 admin/risk）读 `ads_export_audit`/`ads_report_alert`/`attribution_report.json`；P10（沙盒，角色 admin/risk/da）读 `persona_report.json`，对 naive KS 0.257 显示「未校准」横幅。
6. **已推远端**：各 feature 分支已推 origin；`feat/inverted-index-superset` 已推 origin，合并入 `dev` 后 `dev` 推 origin。
7. **I-04 倒排索引审计检索 + Superset BI 看板（`feat/inverted-index-superset`，2026-08-07）**：Doris 中文倒排索引毫秒级敏感词检索（pytest 3 passed，检索 4–7ms）；Superset 4.1.2 已起（`/health` 200）、连 Doris ADS、经 API 落库 3 图表+1 仪表盘。两者均已真机验证；Superset 投产前须复用 RBAC/PII 脱敏约束（诚实声明，见 superset.md §5/§6），元数据库改 PostgreSQL+强密码+固定 `SUPERSET_SECRET_KEY` 为生产前置项。

---

## 2. 已知的「交付缺口」——真实外部资源依赖（诚实声明，勿当 bug）

文档（阶段 6 复盘 `docs/product/06/README.md` §4）里的 G1/G3/H4 为真实外部资源依赖，作品集/最终版范围内**不应**动手（G2/G4/G5/G8/G9/G10 已交付）：
- G1 真实数据源接入（当前用 demo / mock / 合成种子回填）
- G3 真实合规审批流 / 算法备案
- H4 真实试点行 / 机构试用意向
- 真实生产高可用 / 监控属 G8 框架可补部分（已交付 `/metrics` + `manage.sh health`），多副本 HA 等仍外部依赖

**判据**：如果一项缺口的修复需要「外部真实资源 / 第三方审批 / 真实 PII 通道」，它就是真实外部依赖（delivery-gap），在文档里标记即可，不要改代码去假装解决。

---

## 3. 剩余的文档级待办（D1–D3 + C-class 漂移，仅文档编辑，可选）

这些是**探索报告与代码不一致**或**阶段文档缺失**，纯文档修改，不影响运行：

- **D1**：创建 `docs/product/06/README.md`（阶段6 上线复盘）。
- **D2**：调和 阶段5 文档的 status marker 与正文内容不一致。
- **D3**：Open Questions Q1/Q2/Q3 仍为「未确认」——如已可判定，补结论；否则保留并标注依赖。
- **C-class 文档漂移**（代码已实现，探索报告写错了，应改文档）：
  - LTV 两级预警 / `WARN_LINE` 双阈值 → 代码已做，报告说没做，改报告。
  - RBAC 四角色 → 代码已做，报告说三角色/两角色，改报告。
  - missing-rate 注释 25%→实际 75% → 改注释/报告。
  - AC-01 position 持久化 → 代码已实现（`ods_cdc_position`），报告说未持久化，改报告。

> 上面这些不是必须做的 release blocker，但做掉能让评审不被误导。可走一轮轻量 product+fix，或直接编辑。

---

## 4. 环境与常用命令（接手第一件事：复现基线）

```bash
# 仓库
cd /home/azureuser/SpaceFin-Agent
git status                      # 应干净；当前分支应 = develop @ 79d0cd2

# Python 环境（conda，env 名 spark）
source ~/miniforge/bin/activate spark

# 跑测试（15 模块，期望 645 passed + 1 skipped）
python -m pytest -q
# I-04 倒排索引真机验证（需 Doris FE 9030 在线）：
python -m pytest tools/compliance/test_inverted_search.py -v   # 期望 3 passed

# AVM 复现 9.88%
cd tools/avm
python train.py --loss quantile --quantile 0.45 --smooth-mode eb   # canonical r11 / 28 特征

# 前端
# 启动：tools/frontend（端口 8500）；验证：
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8500/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8500/p9_compliance_audit
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8500/p10_sandbox

# Superset BI 看板验证（需 docker 已起 spacefin-superset）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/health   # 期望 200
python deploy/superset/setup_superset.py   # 幂等导入数据源+数据集+3 图表+1 仪表盘
```

### systemd 用户级服务（改了代码要 reload）
> 注意是**用户级** systemd，不是系统级。改完相关代码后重启以加载新代码：
```bash
systemctl --user restart spacefin-cdc.service
systemctl --user restart spacefin-cdc-consumer.service
systemctl --user restart spacefin-frontend.service
```
验证 CDC：看 `ods_cdc_position` 持久化 + consumer offset 在前进。

### Docker 技术栈（MySQL / Redis / Airflow / crawler 镜像）
- 已在跑。Airflow DAG：`airflow/dags/guangdong_daily_crawl.py`（含 lake_sync 任务）。
- MinIO 同步：`tools/lake/minio_sync.py`（SigV4 GET/PUT/DELETE + list/delete/clear_prefix）。

---

## 5. 工作流约定（避免重复踩坑）

- **共享工作树风险**：本机多 agent 共用一个 git 工作树，别的 agent 的 `checkout`/`merge` 会改当前分支。提交前**先确认分支**，用**明确的文件列表** `git add <files>`，**绝不用 `git add -A` / `git add .`**，绝不 `reset`/`revert` 共享分支。
- **pre-commit 钩子**：commitlint 需要 `npx`，而 `npx` 在 `~/node24/bin`。提交前：
  ```bash
  export PATH="$HOME/node24/bin:$PATH"
  ```
- **ruff-format 钩子**会重排文件 → 提交被拒后重新 `git add` 再提交即可。
- **分支策略**：功能在 feature 分支开发，merge 进 develop。之前 workflow 用的分支名类似 `feature/xxx`、`spacefin-gap-closure`、`spacefin-close2`。

---

## 6. 全链路 workflow 编排模板（如果要再做一轮收口）

之前两轮用的模式：`TeamCreate` → `TaskCreate`（拆 product/dev/review/fix）→ 派发 Agent（subagent_type 按角色）→ `TaskUpdate` 指派 → review 找问题 → fix 收敛 → `shutdown_request`。

角色职责：
- **product**：对 `docs/product/03~05` 的 AC / D / RD / C 清单负责，写验收记录，判定外部资源依赖（delivery-gap）。
- **dev**：实现功能 + 写真实断言测试（`tests/`），不写 mock 糊弄。
- **review**：对照 AC 逐条核，找文档/代码漂移、测试越界（如跨目录 `from conftest import`）、非幂等建表等。
- **fix**：收敛 review 提出的 P 级问题，改完回测。

---

## 7. 一句话给接手者

「这是按最终交付版本标准验收的校招作品集：8/8 AC 绿、当前全仓 560 测试过、文档已收口。别把真实外部资源依赖（delivery-gap）当 bug；别动 AC-07 的 28 特征 canonical 配置；G2/G4/G5/G8/G9/G10 已交付，G1/G3/H4 为外部依赖声明。先 `git status` + 跑测试复现基线，再决定干什么。」

---

## 附：最近关键 merge / commit 锚点（便于回溯）

- `ee26320` docs(readme): 范围说明对齐最终交付版本口径（develop HEAD）
- `fbac3ee` feat(final): merge G2/G4/G5/G8 build-out（最终版补齐）
- `3836df3` P9/P10 页面 + 静态 js
- `f2d625e` `tools/avm/attribution.py` permutation_importance 归因
- `79d0cd2` chore(fix-close): 审核意见 P2-1~P2-3/P3 修复收口（历史锚点）
- `601d05b` feature/lake-sync-dag（minio_sync + DAG）
- `28a2ea6` feature/qg-quota-conservation（orchestrator 节流）
- 内存索引：`/home/azureuser/.codebuddy/projects/home-azureuser/memory/MEMORY.md`（project_spacefin_* 系列条目）
