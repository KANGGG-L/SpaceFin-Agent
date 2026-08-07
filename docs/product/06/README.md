# 阶段 6：上线复盘（Launch Retrospective）

> **状态**：✅ 已完成（2026-08-05）
> **定位**：阶段 6 子交付物。总结目标达成度、上线效果（实测数字）、最终版交付缺口（G1–G8 + H4）与文档收口结论。

---

## 0. 项目定位提醒（先读这段）

SpaceFin-Agent 是**最终交付版本（final deliverable）**，不是 MVP 原型，也不是商业部署系统。

- **验收标准 = 技术 AC + 可复现性 + 测试 + 文档自洽**；凡能凭代码/合成数据交付的能力，均应按最终版补齐，不降级为「作品集范围外」。
- **真实外部资源依赖项**（真实数据源授权、监管算法备案、真实机构试点意向）无法凭代码达成，诚实声明为「未达成（external-dependency）」，**不视为 bug，但列为最终版待办**。
- 本阶段是在 8/8 AC 已全绿、603 测试已通过的前提下，对文档做的一次收口（D1–D3 + C 类漂移修正 + 最终版口径重定）。

---

## 1. 验收结论（AC-01～08 全部达成，技术口径）

| ID | 验收项 | 结果 | 实测数字（2026-08-05） |
|----|--------|------|------------------------|
| AC-01 | CDC 同步至 ODS（≤10s、去重） | ✅ | 独立端到端实测 **0.32s** ≤ 10s，窗口内恰 1 条无误重 |
| AC-02 | 日终五级分类一致率 ≥ 99.9% | ✅ | 合成种子环境抽样 **100%（123/123）** |
| AC-03 | LTV 两档预警（>0.75 警示级 / >0.85 强预警） | ✅ | 全量 **200/200** alert_level 匹配（strong 11 / warn 6） |
| AC-04 | 特征缺失率>75 → 低置信不自动预警 | ✅ | 缺失率>75 的 **10 笔**全部低置信且全部抑制预警，恰好=75 的 91 笔不标记 |
| AC-05 | 1104 口径不一致 → 阻断+告警 | ✅ | 正常报送 exit 0；--simulate-mismatch 演练 **exit 1、3 项**口径不一致告警落 `ads_report_alert` |
| AC-06 | PII 导出脱敏 + 审计日志 | ✅ | **136/136** 客户号脱敏，业务库真实客户号 0 泄漏 |
| AC-07 | AVM 精度@覆盖率：45% 覆盖 MAPE ≤ 10% | ✅ | **9.88%**（MdAPE 6.885%，n=3618）；全量 MAPE 14.59%；基线 20.64%；oracle 下界 12.65% |
| AC-08 | 报表口径一致率 = 100% | ✅ | 两条独立路径比对 **27/27** 一致（100.000%） |

---

## 2. 测试结论

- **自动化测试**：15 模块（新增 `tools/compliance` 倒排索引 I-04 真机测试 3 项），共 **646 项**，其中 **645 passed + 1 skipped**（skipped = 活体 Chrome 冒烟测试，`ANJUKE_TEST_LIVE=1` 显式启用，非缺陷）。2026-08-07 全仓实测 **645 passed + 1 skipped**，较 2026-08-05 基线（560 passed）的新增主要来自分支 `feat/inverted-index-superset` 的 I-04 倒排索引检索与既有模块回归补全，**无失败、无回归**。投产加固分支 `feat/superset-prod-hardening` 新增 `deploy/superset/test_superset_compliance.py`（3 项真机断言：视图脱敏形态 / 视图存在 / 审计钩子落库），服务在线时计入、离线时自动 skip。
- **缺陷分布**：0 P0 / 0 P1 / 5 P2（均已修复并合入 develop）/ 0 P3；本轮（I-04 + Superset）新增 P2-1~P2-3（superset.md §5/§6 合规声明矛盾 / README 虚构「先清后建 TODO」/ `setup_superset.py` 重入 bug）与 P3-1~P3-5，均已在 `feat/inverted-index-superset` 收口（含修复 `ensure_dataset` 422 幂等、真正 `import tools.lake.config` 为单一真相源等）。
- **用例覆盖**：功能 8/8（TC-01～08）+ 边界 6 项（B-01～06）+ 异常 6 项（E-01～06）+ 回归 R-01～06 范围定义齐备；I-04 倒排索引检索新增 3 项真机断言（表/索引存在、敏感词命中、无关词零误报，检索 4–7ms）。

---

## 3. Go / No-Go 决策

> ☑ **Go（技术验收口径）**

理由：功能验收 AC-01～08 全绿、自动化测试 603 passed / 1 skipped、设计/研发/合规护栏清单回填完成、无遗留 P0/P1。G6 渲染已验证、G7 为 PRD Non-goals 已排除；G2/G4/G5/G8 已于 feature/final-buildout（f3c9fde）凭代码补齐并交付；G1/G3/H4 为真实外部资源依赖、诚实声明未达成（非 bug）。技术交付已达标，外部依赖项待真实资源到位后推进。

---

## 4. 最终版交付缺口（G1–G8 + H4）

> 本 repo 为最终交付版本。判定口径：**能凭代码/合成数据交付的能力必须补齐（不再降级为"作品集外"）；仅真实外部资源依赖项诚实声明未达成，列为最终版待办。**
> 状态图例：✅ 已交付 ／ 🟡 框架可补（最终版待办，须代码实现） ／ ⛔ 外部依赖（诚实声明未达成） ／ ➖ 设计排除（Non-goals）。

| # | 交付项 | 状态 | 说明与最终版行动 |
|---|--------|------|------------------|
| G1 | 真实数据源接入 | ⛔ 外部依赖 | 真实业务库 / 广东抵押物地址 / 第三方房产·地图 API 依赖外部机构授权，无法凭代码达成。当前以 demo / mock / 合成种子驱动全链路，**诚实声明未达成**（R-rev-2、Q1/Q2） |
| G2 | 数据授权链路 + 分类分级 | ✅ 已交付 | 分类分级标签 `DATA_LEVELS=["公开","内部","敏感","PII"]` 已在 `tools/frontend/pages/p1_datasource.py`；列级 schema 元数据 + 导出按级别强制脱敏/拦截已落地（`tools/frontend/data_classification.py` + `db.py` 审计复用）。**实现于 feature/final-buildout（commit f3c9fde）**。真实授权审批流属 G1 外部依赖 |
| G3 | 算法备案 / 合规审批 | ⛔ 外部依赖 | 需监管审批，无法凭代码达成。**诚实声明未达成**；特征归因报告（C-02）已就绪可作备案材料 |
| G4 | PII 脱敏通道 | ✅ 已交付 | 脱敏+审计**已交付**：`app.py` `c****{后4位}`、AC-06 实测 136/136 脱敏 0 泄漏、`ads_export_audit` 留痕；通道级说明已补（字段级加密/KMS 为外部依赖，脚本级通道已就绪）。**实现于 feature/final-buildout（commit f3c9fde）** |
| G5 | 亿级坐标分布式性能 | ✅ 已交付 | 单机 cKDTree 已实现（`tools/spatial/main.py`，Sedona 降级）；`deploy/sedona/` 演示脚本已加（`tools/spatial/sedona_demo.py`，合成放大数据跑通分布式 join），证明架构可行。**实现于 feature/final-buildout（commit f3c9fde）**。真实亿级压测达标属外部集群依赖 |
| G6 | Linux 宿主 Chrome 反爬渲染 | ✅ 已交付 | 空壳页识别 66/66 零错判 + `render_smoke_test.sh` 通过 + 2026-08-03 全量跑通（sale 21 城 186,635 行、fangyuan 出数页率 2.82%→33.33%） |
| G7 | 实时反欺诈闭环 | ➖ Non-goals | PRD 明确 Non-goals（L1），仅做预警联动不闭环 |
| G8 | 生产高可用 / 监控 | ✅ 已交付 | `tools/ops/manage.sh` 已有 `status` + 容器 `unless-stopped` 自拉起；`/metrics` 健康端点 + 告警阈值已加（纯代码）。**实现于 feature/final-buildout（commit f3c9fde）**。多副本 HA / 生产监控栈（Prometheus 等）属外部基础设施依赖 |
| G9 | Doris 倒排索引审计检索（I-04，L5 合规） | ✅ 已交付 | `sql/doris/01_compliance_audit_inverted.sql` 建 `ads_compliance_audit` + 中文倒排索引（`USING INVERTED, chinese`）；`tools/compliance/inverted_search.py` 提供毫秒级敏感词检索（连接以 `tools/lake/config.py` 为单一真相源）；pytest 3 项真机全绿（检索 4–7ms、敏感词命中、无关词零误报）。**实现于 feat/inverted-index-superset**。生产需由贷后/申请流水持续写入 `ads_compliance_audit` |
| G10 | Superset BI 看板（L5 展示） | ✅ 已交付 | Superset 4.1.2 已真机拉起（`/health` 200）、连 Doris ADS、经 API 落库 3 图表（资产质量/五级分类/AVM 趋势）+ 1 仪表盘「房产金融风险概览」，与 S5 驾驶舱读同一份数据、口径一致。**投产加固（feat/superset-prod-hardening，2026-08-07）已落地 RBAC/PII/审计**：元数据库改 PostgreSQL、固定 `SUPERSET_SECRET_KEY`、管理员强密码；仅注册 Doris 脱敏视图（`v_*`，PII 同 `mask_value` 口径）；四角色 RBAC（Admin/Risk/DA/Postloan，经 `setup_roles.py` 落地，关闭 Alpha SQL Lab 写）；查询/导出经 `superset_config.py` 钩子写 `spacefin.ads_export_audit`（与驾驶舱同一张审计表）；`deploy/superset/test_superset_compliance.py` 真机断言全绿。 |
| H4 | 试点行 / 机构试用意向（假设 H4） | ⛔ 外部依赖 | 需真实机构签约 / ≥2 家试点行意向，无法凭代码达成。**诚实声明未达成**（阶段 0 / 阶段 4 Q1·A-01） |

---

## 5. 文档收口（本轮）

- **D1**：创建 `docs/product/06/README.md`（本文件，阶段 6 上线复盘）。
- **D2**：阶段 5 `docs/product/05/README.md` 顶部 status marker 与 §8 待办项已更新为「已完成」口径（8/8 AC 绿、603/604 测试、Go 建议 2026-08-05）。
- **D3**：阶段 3 `docs/product/03/README.md` 的 Q1/Q2/Q3 保持「未确认」并标注依赖方——未编造结论；Q3 仅阻塞 P1 沙盒，不阻塞本期 MVP。
- **C 类文档漂移（4 处，代码已实现、旧报告写错）已修正**：
  1. `docs/tech/components/risk-engine.md` §3.1 / §9：LTV 两档预警**已实现**——`LTV_WARN_LINE=0.75`（warn）+ `LTV_RED_LINE=0.85`（strong）均在 `tools/risk/config.py`，`risk_engine.py` 含两分支逻辑，并非只有单档。
  2. 低置信阈值口径：**严格大于** `missing_pct > LOW_CONF_MISSING_PCT(75.0)`，恰好 = 75 不标记（与 AC-04/B-04 边界一致），旧「≥75 含等号 / 缺失率 ≥75」说法已改正。
  3. 陈旧注释：risk_engine.py 模块 docstring 已说明实际阈值 75（旧「>= 25%」为过期注释）。
  4. AC-01 position 持久化（`ods_cdc_position`）代码已实现，旧报告「未持久化」说法已改正。

---

## 6. 一句话总结

「已验收最终交付版本：8/8 AC 绿、603 测试过、文档收口完毕。G1/G3/H4 为真实外部资源依赖、诚实声明未达成（非 bug）；G2/G4/G5/G8 已于 feature/final-buildout（f3c9fde）凭代码补齐并标记为已交付；G6 已验证、G7 为 Non-goals。别动 AC-07 的 28 特征 canonical 配置；C 类漂移与 D1–D3 仅在文档层收口。」
