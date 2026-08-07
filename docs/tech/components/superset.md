# 组件技术说明 · Superset BI 看板（L5 展示 / BI 层）

> **状态**：✅ 已接入（Superset 4.1.2 已起、连 Doris ADS，3 图表+示例仪表盘已建）
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

## 5. 合规（接入前必须解决）

- 复用现有 RBAC 与 PII 脱敏通道（`tools/frontend/data_classification.py` + `ads_export_audit`），Superset 查询 likewise 受控；
- 看板分享需带权限边界，避免把敏感明细直接暴露给无权限角色；
- 投产前完成法律审查（同 [anjuke-crawler.md](anjuke-crawler.md) §6）。

## 6. 诚实标注

**已接入（2026-08-07）**：Superset 4.1.2 已真机拉起，`/health` 返回 200；已连 Doris ADS 并
建 3 个图表（资产质量概览 / 五级分类分布 / AVM 精度趋势）+ 示例仪表盘「房产金融风险概览」
（见 `deploy/superset/README.md`）。验证要点：
- 数据源「Doris ADS」创建成功，3 数据集 + 3 图表 + 1 仪表盘经 API 落库；
- 在 Superset 容器内对 Doris 直跑三图对应 SQL 均返回真实数据（连接与口径已验证）。

待人工确认 / 已知约束：
- 通过脚本化 `api/v1/chart/data`（嵌套 `datasource`）调用在 4.1.2 触发
  `QueryContextFactory.create() missing datasource` 的服务端已知 quirk，与 UI 经 slice 解析
  datasource 的路径不同；建议浏览器打开 `http://127.0.0.1:8088` 用 admin 登录人工确认渲染。
- 看板**未**接既有 RBAC/PII 脱敏（驾驶舱侧已内建）；生产前须复用
  `tools/frontend/data_classification.py` + `ads_export_audit` 约束，避免敏感明细裸曝（见 §5）。
- 元数据库为 SQLite、管理员密码为 dev 值，投产前须改 PostgreSQL + 强密码 + 固定 `SUPERSET_SECRET_KEY`。
