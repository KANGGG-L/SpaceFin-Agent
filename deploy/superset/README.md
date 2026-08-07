# Superset BI 看板接入（L5 展示 / BI 层）

> 与既有 `tools/frontend` 零依赖驾驶舱互补：驾驶舱=固化 RBAC 决策视图，Superset=自助探索。
> 数据源为 Doris ADS 层（MySQL 协议 9030）。

## 1. 启动（首次）

```bash
# 镜像基于 apache/superset:4.1.2，已补 PyMySQL 驱动（deploy/superset/Dockerfile）
docker compose up -d --build superset
# 初始化元数据库 + 权限 + 管理员（dev 密码 superset_dev_only，投产务必改）
docker compose exec superset superset db upgrade
docker compose exec superset superset init
docker compose exec superset superset fab create-admin \
  --username admin --firstname Superset --lastname Admin \
  --email admin@example.com --password superset_dev_only
# 健康检查
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/health   # 期望 200
```

> 容器内 `127.0.0.1` 指向自身；连宿主 Doris/MySQL 用 `host.docker.internal`
> （compose 已加 `extra_hosts: host.docker.internal:host-gateway`）。
> 对应宿主侧等价 URI：`mysql+pymysql://root@127.0.0.1:9030/ads`。

## 2. 导入看板资产（DB + 数据集 + 3 图表 + 仪表盘）

```bash
# 在宿主（Python 3.10 / conda spark）执行；脚本连 http://127.0.0.1:8088 的 Superset API
python deploy/superset/setup_superset.py
```

脚本会：
1. 连接 Doris ADS（`mysql+pymysql://root@host.docker.internal:9030/ads`）并注册为数据源「Doris ADS」；
2. 注册 3 个数据集：`ads_risk_class` / `ads_avm_precision_trend` / `ads_city_avg_price`；
3. 建 3 个图表：资产质量概览（big_number）、五级分类分布（pie）、AVM 精度趋势（line）；
4. 建示例仪表盘「房产金融风险概览」并关联上述图表。

可配置环境变量：`SUPERSET_URL` / `SUPERSET_ADMIN` / `SUPERSET_PASSWORD` / `SUPERSET_DORIS_URI`。

> 幂等说明：数据库 / 数据集按名复用（已存在则直接复用，不重复创建）；但**图表 / 仪表盘不幂等**——`create_chart` / `create_dashboard` 每次执行都会新建（Superset 允许重名），重跑会累积重复图表与仪表盘。建议一次性执行，或在重跑前于 UI 清理旧图表 / 仪表盘。

## 2.1 投产加固：脱敏视图 + RBAC + 审计

```bash
# 1) 在 Doris 建脱敏视图（仅注册视图，不注册裸明细基表）
mysql -h127.0.0.1 -P9030 -uroot -e "SOURCE sql/doris/02_superset_pii_views.sql"
#    （无 mysql 客户端时可用 deploy/superset/gen_pii_views.py --apply 生成/落地视图）

# 2) 注册视图数据集 + 3 图表 + 1 仪表盘（宿主执行）
python deploy/superset/setup_superset.py

# 3) RBAC 四角色映射（容器内执行；本部署未启用 roles REST 端点，走 security_manager）
docker compose exec superset python /app/setup_roles.py
#    Admin 全量；Risk 全 5 视图；DA 限聚合+趋势；Postloan 限贷后脱敏明细；
#    并关闭 Alpha 的 SQL Lab 写权限。

# 4) 审计钩子默认已通过 SUPERSET_CONFIG_PATH 挂载启用，查询/导出自动写
#    spacefin.ads_export_audit（与驾驶舱同一张审计表）。
```

> 元数据库已改 PostgreSQL（`superset-metadata-db`）、`SUPERSET_SECRET_KEY` 固定、
> 管理员密码读取 `SUPERSET_PASSWORD`，生产须在 `.env` 注入强值（见 `.env.example`）。

## 3. AVM 精度趋势数据

`ads_avm_precision_trend` 由 `deploy/superset/load_avm_precision_trend.py` 从
`output/avm/avm_report.json` 写入一条模型快照。每次 AVM 重训后重跑即可追加，趋势图随之增长：

```bash
python deploy/superset/load_avm_precision_trend.py
```

## 4. 真机验证记录（2026-08-07）

- Superset 容器 `spacefin-superset` 健康：`http://127.0.0.1:8088/health` → **200**。
- 数据源「Doris ADS」创建成功；3 数据集、3 图表、1 仪表盘经 API 创建成功（id 已落库）。
- 在 Superset 容器内对 Doris 直跑三图对应 SQL 均返回真实数据（如五级分类 5 类余额合计、
  AVM 趋势 1 行 model_mape=14.894% / baseline_mape=20.173%）。
- 说明：通过脚本化 `api/v1/chart/data`（嵌套 `datasource {id,type}`）调用在 4.1.2 触发
  `QueryContextFactory.create() missing datasource` 的服务端已知 quirk；该路径与 UI 通过
  slice 解析 datasource 的路径不同。图表/仪表盘已在元数据层创建且底层数据连接已验证，
  **建议浏览器打开 http://127.0.0.1:8088 用 admin 登录人工确认渲染**。

## 5. 合规约束清单（已落地，投产加固 2026-08-07）

下列为投产必须满足的合规约束，已全部落地（见 `sql/doris/02_superset_pii_views.sql`、
`setup_roles.py`、`superset_config.py`）：

- **仅注册脱敏视图 / 聚合表**：Superset 数据集只允许是 `v_*` 视图或聚合表，禁止直接注册
  裸明细基表 `ods_customer` / `ods_loan` / `ods_ads_ltv_alerts` / `dws_risk_class` /
  `ads_compliance_audit`(基表) 等；
- **PII 同口径脱敏**：敏感列按 `data_classification.mask_value` 口径
  `CONCAT('c****', RIGHT(col,4))`（如 `v_ltv_alerts_masked.customer_id`）；
- **RBAC 四角色**：Admin/Risk/DA/Postloan 经 `setup_roles.py` 映射数据集与看板权限，
  普通角色禁止自建数据集（仅 Admin 可建），Alpha 关闭 SQL Lab 写；
- **审计闭环**：查询/导出经 `superset_config.py` 钩子写 `spacefin.ads_export_audit`
  （与驾驶舱同一张表），可通过 `deploy/superset/test_superset_compliance.py` 真机验证。

> 复用既有 RBAC 与 PII 脱敏约束：看板**不**裸曝敏感明细（客户号、身份证、联系方式等）；
> 看板查询与 `tools/frontend/data_classification.py` + `ads_export_audit` 同一套合规口径；
> 导出/共享看板须带权限边界。
