# 组件技术说明 · Superset BI 看板（L5 展示 / BI 层）

> **状态**：📋 规划中（考虑接入）
> **能力地图层级**：L5 展示
> **引入原则**：按需接入。与既有 `tools/frontend` 零依赖驾驶舱互补——驾驶舱是固化的 RBAC 决策视图，Superset 提供自助式探索性 BI。

---

## 1. 为何引入

既有 S5 前端驾驶舱是**固化**的决策视图（10 个页面、RBAC 四角色、审计脱敏内建），适合日常监控与演示。但它不擅长**临时、自由的探索性分析**（如分析师想自己拖一个「各城市 AVM 误差分布」对比图）。

Superset 补这块能力：

- **自助 BI**：分析师自建图表 / 看板，无需改前端代码；
- **统一数据源**：直接连 Doris `ADS` 层（MySQL 协议，端口 9030）与 MySQL 业务源库（3306），与驾驶舱读同一份数据，口径一致；
- **与驾驶舱互补**：驾驶舱 = 固化、受控、可审计；Superset = 灵活、探索。两者并存，不互相替代。

## 2. 数据源

| 数据源 | 连接方式 | 用途 |
|--------|----------|------|
| Doris `ADS` 层 | SQLAlchemy `mysql+pymysql://...:9030/ads` | 资产质量、五级分类、AVM 精度、预警等聚合指标 |
| MySQL 业务源库 | SQLAlchemy `mysql+pymysql://...:3306/spacefin` | 客户 / 抵押物 / 贷款明细 |

> Doris 兼容 MySQL 协议，Superset 用 `mysql+pymysql` 驱动即可接入，无需独立插件。

## 3. 与既有前端驾驶舱的关系

```
                 ┌─────────────────────────────┐
   数据层        │  Doris(ADS) / MySQL(spacefin) │
                 └──────────────┬──────────────┘
                        ┌───────┴────────┐
                  读同一份数据       读同一份数据
                        │                │
                  ┌─────▼──────┐   ┌──────▼───────┐
                  │ S5 驾驶舱  │   │  Superset BI │
                  │ 固化 RBAC │   │  自助探索    │
                  │ 审计/脱敏 │   │  (规划中)    │
                  └────────────┘   └──────────────┘
```

- 驾驶舱：固定 10 页，RBAC 强控，导出走 `ads_export_audit` 审计 + PII 脱敏。
- Superset：看板自由搭建；**生产接入时必须复用同一套 RBAC / 脱敏 / 审计约束**，否则会出现「驾驶舱合规、Superset 裸数」的合规破防。

## 4. 部署（docker-compose，规划中）

`docker-compose.yml` 已预留 `superset` service（端口 `8088`，仅绑 `127.0.0.1`）。首次启动需初始化元数据库：

```bash
docker compose up -d superset
docker compose exec superset superset db upgrade
docker compose exec superset superset init
docker compose exec superset superset fab create-admin \
    --username admin --firstname Superset --lastname Admin \
    --email admin@example.com --password <强密码>
```

> 元数据默认用容器内 SQLite（`/app/superset_home`），生产应改为独立 PostgreSQL 并固定镜像版本。

## 5. 合规（接入前必须解决）

- 复用现有 RBAC 与 PII 脱敏通道（`tools/frontend/data_classification.py` + `ads_export_audit`），Superset 查询 likewise 受控；
- 看板分享需带权限边界，避免把敏感明细直接暴露给无权限角色；
- 投产前完成法律审查（同 [anjuke-crawler.md](anjuke-crawler.md) §6）。

## 6. 诚实标注

当前**仅规划，未接入**：`docker-compose.yml` 仅有 service 占位，无看板定义、未接 RBAC/脱敏。接入后此处更新为 ✅ 并补充看板清单。
