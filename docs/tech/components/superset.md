# 组件技术说明 · Superset BI 看板（L5 展示 / BI 层）

> **状态**：✅ 已接入 + 投产加固（Superset 4.1.2 已起、连 Doris ADS，3 图表+示例仪表盘已建；PG 元数据 + PII 脱敏视图 + 四角色 RBAC + 审计钩子已落地）
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

## 4. 部署（docker-compose，已接入）

`docker-compose.yml` 已定义 `superset` service（端口 `8088`，仅绑 `127.0.0.1`，镜像固定
`apache/superset:4.1.2`）。因官方镜像未预装 PyMySQL，已用 `deploy/superset/Dockerfile`
派生镜像补 `pymysql`（Doris 走 MySQL 协议）。容器内 `127.0.0.1` 指向自身，连宿主 Doris 经
`host.docker.internal`（compose `extra_hosts` 已设）。

首次启动 + 导入看板资产：

```bash
docker compose up -d --build superset
docker compose exec superset superset db upgrade
docker compose exec superset superset init
docker compose exec superset superset fab create-admin \
    --username admin --firstname Superset --lastname Admin \
    --email admin@example.com --password superset_dev_only
# 健康检查
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/health   # 期望 200
# 导入数据源 + 数据集 + 3 图表 + 仪表盘
python deploy/superset/setup_superset.py
```

导入步骤、图表清单与验证记录见 [`deploy/superset/README.md`](../../deploy/superset/README.md)。

> 元数据默认用容器内 SQLite（`/app/superset_home`），生产应改为独立 PostgreSQL 并固定镜像版本、强密码。
> 连接 Doris 的 URI（容器视角）：`mysql+pymysql://root@host.docker.internal:9030/ads`；
> 宿主等价：`mysql+pymysql://root@127.0.0.1:9030/ads`。

## 5. 合规（已落地，投产加固 2026-08-07）

> ✅ 下列合规约束已在 `feat/superset-prod-hardening` 落地，与驾驶舱 `tools/frontend`
> 同一套 RBAC / PII 脱敏 / 审计通道打通，消除「驾驶舱合规、Superset 裸数」破防。

- **PII 脱敏视图**：Superset 只注册 Doris 脱敏视图（见 `sql/doris/02_superset_pii_views.sql`），
  不注册裸明细基表；敏感列按 `data_classification.mask_value` 同口径脱敏
  （`CONCAT('c****', RIGHT(col,4))`），如 `v_ltv_alerts_masked.customer_id`。
- **RBAC 四角色**：复用 Superset 原生角色模型（Admin/Risk/DA/Postloan），经容器内
  `security_manager` 映射数据集/看板权限（Admin 全量；Risk 全 5 视图；DA 限聚合+趋势；
  Postloan 限贷后脱敏明细），并关闭 Alpha 的 SQL Lab 写权限。落地见 `setup_roles.py`。
- **审计闭环**：查询/导出经 `superset_config.py` 的 `after_request` 钩子写
  `spacefin.ads_export_audit`（与驾驶舱同一张表、同字段），合并审计通道。
- 看板分享须带权限边界；投产前仍建议完成法律审查（同 [anjuke-crawler.md](anjuke-crawler.md) §6）。

## 6. 诚实标注

**已接入 + 投产加固（2026-08-07）**：Superset 4.1.2 已真机拉起，`/health` 返回 200；已连
Doris ADS 并建 3 图表（资产质量概览 / 五级分类分布 / AVM 精度趋势）+ 示例仪表盘
「房产金融风险概览」。投产加固（分支 `feat/superset-prod-hardening`）进一步完成：
- 元数据库改 **PostgreSQL**（`superset-metadata-db` service），固定 `SUPERSET_SECRET_KEY`、
  管理员强密码（`SUPERSET_PASSWORD`）经 `.env` 注入；
- **PII 脱敏视图**（`v_*`）作为 Superset 唯一入口，敏感列同口径脱敏；
- **RBAC 四角色**映射（Admin/Risk/DA/Postloan）+ 关闭 Alpha SQL Lab 写；
- **审计钩子**写 `spacefin.ads_export_audit`，与驾驶舱合并审计。

验证要点：
- 数据源「Doris ADS」创建成功，5 个脱敏视图数据集 + 3 图表 + 1 仪表盘经 API 落库；
- `pytest deploy/superset/test_superset_compliance.py` 真机断言：脱敏视图列形态
  （`c****{后4位}`）、视图存在、审计钩子落 `ads_export_audit`（全绿）；
- 容器内对 Doris 直跑三图对应 SQL 均返回真实数据（连接与口径已验证）。

待人工确认 / 已知约束：
- 通过脚本化 `api/v1/chart/data`（嵌套 `datasource`）调用在 4.1.2 触发
  `QueryContextFactory.create() missing datasource` 的服务端已知 quirk，与 UI 经 slice 解析
  datasource 的路径不同；建议浏览器打开 `http://127.0.0.1:8088` 用 admin 登录人工确认渲染。
- 本部署未启用 `/api/v1/security/roles/` REST 端点，RBAC 经容器内 `security_manager`
  （`setup_roles.py`）落地，不走 REST。
- 真实外部依赖（G1 真实数据源 / G3 算法备案 / H4 试点行）见阶段 6 文档，不属本组件范围。
