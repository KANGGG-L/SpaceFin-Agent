# S5 前端驾驶舱（tools/frontend）

资产质量监控驾驶舱 + LTV 预警列表 + 1104 报送页，含登录与 RBAC（PRD §7.3 语义）。
数据直读 MySQL（`spacefin` 业务库 + `spacefin_crawler` 房产库），只读展示 + 预警确认留痕。

## 技术选型（为什么是 stdlib，而不是 Streamlit/FastAPI）

| 方案 | 结论 | 理由 |
|------|------|------|
| **stdlib `http.server` + PyMySQL** | ✅ 采用 | 全仓唯一 Python 环境 `tools/orchestrator/.venv`（= conda spark 3.10）已带 PyMySQL 2.2.8；**零新增依赖**，不往共享 conda 环境塞几十个包，不污染 spark/调度等既有流水线 |
| Streamlit | ❌ 不采用 | 依赖树大（altair/pandas/plotly…），且服务模型不利于自控 RBAC 与会话 |
| FastAPI + 前端 | ❌ 不采用 | 需新增 uvicorn/fastapi/pydantic 全家桶；200 行级数据 + 3 个页面，不值得引入 ASGI 常驻服务 |
| Node/React 构建链 | ❌ 不采用 | 明确要求避免重型构建链；前端用原生 JS + 内联 SVG 图表，无构建、无 npm 依赖；P5 地图底图瓦片来自外网 OpenStreetMap，需 VPS 可联网访问，内网/断网时降级为点位图 |

## 页面清单与数据映射

| 页面 | 数据表（`spacefin_crawler`） | 指标 / 图表形态 |
|------|------------------------------|-----------------|
| 资产质量驾驶舱 | `dws_risk_class`、`ads_risk_class`、`ads_ltv_alerts`、`ads_stream_ltv_alerts`、`spacefin.collateral` | KPI 卡片（笔数/总敞口/预警/低置信/高危区）；五级分类分布柱状图（余额/笔数/占比，口径=ads_risk_class T+1 汇总）；LTV 直方图（0.85 红线染色）；城市贷款分布 + 高危区笔数（地址解析城市）；离线/实时预警概览 |
| LTV 预警列表 | `ads_ltv_alerts` ∪ `ads_stream_ltv_alerts` + `spacefin.collateral` + `ads_alert_confirm` | 合并分页列表（LTV/估值/余额/抵押物地址/高危区标记/来源/确认状态）；风险类/LTV 区间/日期/来源筛选；风控「确认」；导出 CSV（客户号脱敏） |
| 1104 报送 | `ads_1104_g11`、`dws_risk_class`、`ads_report_alert` | G11 五级 + 合计表；口径一致性实时校验（以 dws 明细聚合为裁判，逻辑同 `tools/reporting/main.py`）；阻断告警历史 |

插件页（pages/ 下，按设计评审 P1~P10 覆盖）：数据底座接入配置（P1）、五级分类迁徙矩阵（P3）、空间风险画像（P5）、空间惩罚项配置（P6）、AVM 估值管理（P7）、合规审计/特征归因（P9）、策略沙盒推演（P10）。页面清单与 RBAC 以各页模块的 `PAGE` 声明为准（自动发现，无需改动 app.py）。

## RBAC 角色矩阵（登录 + 服务端强制校验）

| 页面 / 操作 | admin | risk 风控 | da 数据分析师 | postloan 贷后 |
|------------|:-----:|:---------:|:-------------:|:-------------:|
| 资产质量驾驶舱 | ✓ | ✓ | ✓ | ✓ |
| LTV 预警列表（只读） | ✓ | ✓ | ✓ | ✓ |
| 预警确认 | ✓ | ✓ | — | — |
| 预警导出 CSV | ✓ | ✓ | ✓ | — |
| 1104 报送页 | ✓ | ✓ | ✓ | ✗ 不可见（403） |
| 合规审计/特征归因（P9） | ✓ | ✓ | ✗ 不可见（403） | ✗ 不可见（403） |
| 策略沙盒推演（P10） | ✓ | ✓ | ✓ | ✗ 不可见（403） |
| 其余插件页（P1/P3/P5/P6/P7） | 全部 | 除 P1 外 | 除 P1 外 | — |

> 权限在服务端每个 API 前强制校验（401 未登录 / 403 角色不符）；前端只根据 `/api/me` 裁剪导航与按钮，属第二层防御。会话 cookie 带 `HttpOnly`，JS 不可读。

## 启动 / 停止

```bash
# 启动（后台，日志落 output/frontend/app.log）
mkdir -p output/frontend
nohup tools/orchestrator/.venv/bin/python tools/frontend/app.py \
    --host 127.0.0.1 --port 8500 >> output/frontend/app.log 2>&1 &

# 访问
#   http://127.0.0.1:8500

# 停止
pkill -f "tools/frontend/app.py"
```

- 默认端口 **8500**（已避开 MySQL 3306 / Redis 6379 / Airflow 8080 / Flink 8081 / Doris 9030 / Kafka 9092 / MinIO 9000 等占用）。
- 默认仅监听 `127.0.0.1`；部署到 VPS、暴露到公网时加 `--host 0.0.0.0`（需自行评估暴露面，建议前置 Nginx/反向代理）。
- P5 空间画像页的地图底图瓦片来自外网 OpenStreetMap（`https://{s}.tile.openstreetmap.org/...`），VPS 需可联网访问；内网/断网时地图降级为点位图（点位数据仍在，仅无底图）。
- 首次启动会用 root 凭证幂等建前端自用表 `ads_alert_confirm`（预警确认留痕，不改动预警链路既有表）与 `ads_export_audit`（操作审计：导出/确认留 who/role/when/what/result/ip，对应 TC-06「审计日志已记录」）。

## 默认账号（dev-only，上线前必须接统一认证）

| 账号 | 密码 | 角色 |
|------|------|------|
| `admin` | `admin20020309` | 系统管理员（全部权限） |
| `risk` | `risk20020309` | 风控策略经理（确认/导出） |
| `da` | `da20020309` | 数据分析师（只读 + 导出） |
| `postloan` | `postloan20020309` | 贷后资产保全（不可见报送页） |

> 凭据写死在 `app.py` 的 `USERS` 字典并标注 dev-only，不落库。

## 新增依赖

**无**。全部运行依赖 = PyMySQL（`tools/orchestrator/.venv` 已含）+ Python 标准库。

## 与既有链路的一致性

- 连接参数 / 业务日口径复用 `tools/risk/config.py` 的 `load_env` / `crawl_params` / `business_params`，与风险引擎、报送、预警链路同源；
- 五级分类顺序、LTV 红线（0.85）、口径容差（余额 0.01）与 `config.py` / `tools/reporting` 保持一致；
- 1104 校验状态为页面实时复算（dws 明细聚合 vs `ads_1104_g11`），与报送 CLI 的阻断结论互相印证——当前若显示「口径不一致」，说明 T+1 报送快照与最新明细存在漂移，正是 AC-05 要拦截的场景。
- 1104 页提供处置闭环：「重新校验」只读复算（`POST /api/report/recheck`）；「重建快照」按 dws 明细覆盖重建该日 `ads_1104_g11`（`POST /api/report/rebuild`，admin/risk 权限，写操作前有二次确认，动作留痕 `ads_export_audit`）。重建后口径即一致——这是演示修复漂移的路径，真实报送场景应由 `tools/reporting/main.py` 重跑并保留阻断审计。

## API 一览

| 方法 | 路径 | 权限 |
|------|------|------|
| POST | `/api/login` `/api/logout` | 公开 |
| GET | `/api/me` | 公开 |
| GET | `/api/dashboard` | 登录 |
| GET | `/api/alerts`（支持 `q` 按贷款号/客户号模糊搜索） | 登录 |
| POST | `/api/alerts/confirm` | admin / risk |
| GET | `/api/alerts/export` | admin / risk / da |
| GET | `/api/report` `/api/report/dates` | admin / risk / da |
| POST | `/api/report/recheck` | admin / risk / da（只读重算，返回 `checked_at`） |
| POST | `/api/report/rebuild` | admin / risk（写操作：按 dws 明细重建 G11 快照，落审计） |
| GET | `/api/compliance_audit` | admin / risk |
| GET | `/api/sandbox` | admin / risk / da |

> 插件路由（`/api/datasource*`、`/api/migration`、`/api/spatial*`、`/api/policy*`、`/api/avm*`、`/api/compliance_audit`、`/api/sandbox`）由各页面模块在 `PAGE["routes"]` 自行声明，权限 = 该页 `PAGE["roles"]`，服务端统一校验。
