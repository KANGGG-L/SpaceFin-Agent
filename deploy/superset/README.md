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

> 重复执行幂等：已存在的数据库/数据集会按名复用，图表/仪表盘重建（先清后建见脚本内 TODO）。

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

## 5. 合规（接入必须遵循，勿破防）

- 复用既有 RBAC 与 PII 脱敏约束：看板**不要**裸曝敏感明细（客户号、身份证、联系方式等）。
- 看板查询与 `tools/frontend/data_classification.py` + `ads_export_audit` 同一套合规口径；
  导出/共享看板须带权限边界。
- 生产建议：元数据库改 PostgreSQL、固定 `SUPERSET_SECRET_KEY`、管理员强密码、行级权限。
