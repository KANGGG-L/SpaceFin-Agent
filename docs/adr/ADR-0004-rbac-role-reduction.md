# ADR-0004：RBAC 由 PRD 的 5 角色收敛为 admin / postloan 两角色

## 状态
已采纳

## 迭代
第 4 轮迭代

## 背景（Context）
- PRD §7.3 定义 5 类角色：数据分析师(DA)、风控策略经理、贷后资产保全、数据工程师(DE)、合规官。
- MVP 前端实现时，原 `admin` / `risk` / `da` 三个角色权限**完全一致**，区分无实际意义。
- 落地注明确：MVP 前端实现 2 角色，其余为后续扩展。
- **观察数据**：前端实际落地 3 个角色（`admin` / `risk` / `da`），且三者权限**完全一致**；PRD 原列 5 类角色。

## 决策（Decision）
- `tools/frontend/app.py` 的 `USERS` 仅保留 `admin` 与 `postloan` 两个角色。
- 原 `risk` / `da` 权限并入 `admin`（内部管理，融合风控 / 分析 / 管理员）；`postloan` 为受限角色。
- 页面可见性由 `PAGE_VISIBILITY` 控制：`postloan` 不可见 1104 报送页（返回 403）。

## 备选方案（Alternatives）
- 按 PRD 落地 5 角色：否决——MVP 前端 DA/DE/合规官与 admin 权限基本重合，区分无收益且增大维护面。
- 保留 `risk` / `da` 独立角色：否决——三者权限完全一致，收敛为单一内部管理角色。
- 不做任何角色隔离：否决——合规要求「贷后不可见报送页」，最少隔离需保留。

## 影响（Consequences）
**正面**
- 角色模型简化，演示与测试面收窄；保留关键隔离（postloan 见报送页 403）。
- 与 README 演示账号口径一致。
- **改变后数据**：角色数 3 → **2**（`rbac_roles`）；`postloan` 访问 1104 报送页返回 **403**；前端导航 **9** 页（`frontend_nav_pages`）。

**代价**
- 与 PRD §7.3 的 5 角色表述存在差异（以落地注说明）；DE / 合规官职责暂由 1104 阻断告警 + 导出审计兜底，由 admin 代行。

## 证据（Evidence）
- `docs/product/03/README.md` :: `### 7.3 权限模型（RBAC）`（PRD §7.3 五角色表）；`docs/product/03/README.md` :: `落地注：MVP 前端实现 **2 角色**`（落地注：融合原 risk/da）。
- `tools/frontend/app.py` :: `_ROLE_LABELS = {`（`_ROLE_LABELS`：admin / postloan；注释说明原 admin/risk/da 融合）。
- `tools/frontend/app.py` :: `USERS = {`（`USERS` 由 `_ROLE_LABELS` 生成）；`tools/frontend/app.py` :: `PAGE_VISIBILITY = {`。
- `README.md` :: `演示账号（RBAC 两角色`、`README.md` :: `贷后资产保全 | 仅看 LTV 预警`（演示账号：两角色 admin / postloan，凭据 dev-only）。

## 取证来源（Provenance）
`documented`——PRD 角色表与代码实现均可直接核对，落地注记录了收敛范围。

## 回退（Rollback）
- 恢复 `tools/frontend/app.py` 的 `_ROLE_LABELS` 与 `PAGE_VISIBILITY`（`USERS` 由 `_ROLE_LABELS` 派生）即可重新引入 risk / da 等角色及其页面可见性。
- 回退需同步更新 README 演示账号口径与 `SF_PWD_<ROLE>` 环境变量约定。

## 关联（Related）
- [ADR-0007-evidence-grading-and-artifact-exclusion.md](ADR-0007-evidence-grading-and-artifact-exclusion.md)
- [docs/product/03/README.md](../product/03/README.md)
- [docs/tech/components/frontend.md](../tech/components/frontend.md)
- [tools/frontend/README.md](../../tools/frontend/README.md)
