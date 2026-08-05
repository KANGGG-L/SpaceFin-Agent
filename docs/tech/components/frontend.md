# 组件技术说明 · S5 前端驾驶舱（L5 展示层）

> **状态**：✅ 已实施（2026-08-05 落地，feature/s5-frontend，含 P1/P3/P5/P6/P7 插件页；同日收口补 P9/P10，达成设计评审 D-01 十页全覆盖）
> **能力地图层级**：L5 展示层 — 资产质量监控驾驶舱（PRD §7.3 语义）
> **所属系统**：tools/frontend（数据源 spacefin_crawler.ads_* + spacefin.collateral）

---

## 1. 它解决什么问题

风险引擎 / 报送 / 预警各链路的产物都落在 MySQL 的 ADS 表里，但「有数」不等于「看得见、动得了」：
贷后保全要每天看 LTV 预警并对处置做**确认留痕**，合规要看 1104 报送与口径一致性，风控与数据分析要
下钻空间画像、惩罚项配置、迁徙矩阵与 AVM 估值。S5 驾驶舱把这些**只读展示 + 少量留痕写操作**收敛到
一个零依赖的 Web 应用，并在此之上做 RBAC 访问控制。

本组件做四件事：
1. **只读展示**：内置三页（资产质量驾驶舱 / LTV 预警列表 / 1104 报送）+ 7 个插件页面（P1/P3/P5/P6/P7/P9/P10，覆盖设计评审 10 页清单）；
2. **RBAC**：登录 + 服务端每个 API 强制角色校验（未登录 401 / 角色不符 403），前端只按 `/api/me` 裁剪导航；
3. **预警确认留痕**：风控/管理员「确认」写 `ads_alert_confirm`（不改预警链路既有表）；
4. **操作审计**：导出/确认写 `ads_export_audit`（who/role/when/what/result/ip，对应 TC-06「审计日志已记录」）。

## 2. 技术选型（为什么是 stdlib，而不是 Streamlit/FastAPI）

| 方案 | 结论 | 理由 |
|------|------|------|
| **stdlib `http.server` + PyMySQL** | ✅ 采用 | 全仓唯一 Python 环境 `tools/orchestrator/.venv`（= conda spark 3.10）已带 PyMySQL 2.2.8；**零新增依赖**，不往共享 conda 环境塞几十个包，不污染 spark/调度等既有流水线 |
| Streamlit | ❌ 不采用 | 依赖树大（altair/pandas/plotly…），且服务模型不利于自控 RBAC 与会话 |
| FastAPI + 前端 | ❌ 不采用 | 需新增 uvicorn/fastapi/pydantic 全家桶；200 行级数据 + 少量页面，不值得引入 ASGI 常驻服务 |
| Node/React 构建链 | ❌ 不采用 | 明确要求避免重型构建链；前端用原生 JS + 内联 SVG 图表，**无 CDN、无构建、离线可用** |

## 3. 架构与分工

```
tools/frontend/
├── app.py                 # HTTP 服务：路由 + 会话 + RBAC（统一 _require 校验）
├── db.py                  # 数据访问层：直读 MySQL（复用 tools/risk/config 连接参数与业务日口径）
├── pages/                 # 插件页面注册表：每页一个 pN_xxx.py，自动发现、按序加载
│   ├── __init__.py        # _discover() 扫描 pN_*.py 收集 PAGE 声明，构建 (method,path)→handler 路由表
│   ├── p1_datasource.py   # P1 数据底座接入配置（admin/da）
│   ├── p3_migration.py    # P3 五级分类迁徙矩阵（admin/risk/da）
│   ├── p5_spatial.py      # P5 空间风险画像（admin/risk/da）
│   ├── p6_policy.py       # P6 空间惩罚项配置（admin/risk/da）
│   ├── p7_avm.py          # P7 AVM 估值管理（admin/risk/da）
│   ├── p9_compliance_audit.py  # P9 合规审计/特征归因（admin/risk，读 ads_export_audit/ads_report_alert/output/avm/attribution_report.json）
│   └── p10_sandbox.py     # P10 策略沙盒推演（admin/risk/da，仅框架 + R-OPT-01 未校准标记，读 output/persona/persona_report.json）
└── static/
    ├── index.html         # 登录视图 + 三个内置页面的 section 骨架
    ├── app.js             # 前端逻辑：登录/导航/内置三页渲染 + 插件 JS 动态加载
    ├── style.css          # 全部样式（无外部资源）
    └── pages/*.js         # 插件页面的前端模块（与 pages/*.py 同名对应）
```

各层职责：

- **app.py**：`ThreadingHTTPServer` 单处理器承载全部路由；RBAC 在 API 分发前统一校验
  （`_require(roles)` → 401/403），页面模块内部不必再判角色；静态文件经 `realpath` 校验防目录穿越。
  会话用内存字典 + `secrets.token_hex(16)`，cookie 带 `HttpOnly; SameSite=Lax`（JS 不可读，防 XSS 窃取会话），
  TTL 12 小时。
- **db.py**：直读 MySQL（`spacefin_crawler` 房产库 + `spacefin` 业务库）。连接参数 / 业务日口径
  **复用 `tools/risk/config.py` 的 `load_env` / `crawl_params` / `business_params`**，与风险引擎、
  报送、预警链路同源；每请求短连接（autocommit），200 行级数据毫秒级返回；建表 DDL（前端自用表）走 root。
- **pages/ 插件机制**：一个页面 = 一个模块文件，新增页面只需在 `pages/` 放一个 `pN_xxx.py`，
  **无需改动 app.py / index.html / app.js 任何一行**。单个模块 import 失败只记录不抛出，一个页面写坏
  不应导致整个驾驶舱起不来（见 `pages/__init__.py`）。

**页面模块契约**（每个插件页必须导出 `PAGE` dict）：

```python
PAGE = {
    "id":     "migration",              # 唯一标识，前端 section id = page-{id}
    "label":  "五级分类迁徙矩阵",        # 导航栏显示名
    "roles":  {"admin", "risk", "da"},  # 可见角色；服务端强制校验，非仅前端隐藏
    "order":  30,                       # 导航排序，小的在前
    "js":     "p3_migration.js",        # static/pages/ 下的前端模块文件名
    "routes": {("GET", "/api/migration"): handler},
}
```

handler 签名统一为 `handler(ctx) -> dict | (int, dict)`；`ctx` 只暴露 `query / body / user / ip`，
隔离 HTTP 细节。写操作审计所需的 ip 由框架统一下发（`RouteCtx.ip`），避免逐页自取漏写。

## 4. 页面清单与数据映射

### 内置页面（渲染逻辑在 app.js）

| 页面 | 数据表（`spacefin_crawler`） | 指标 / 图表形态 |
|------|------------------------------|-----------------|
| 资产质量驾驶舱 | `dws_risk_class`、`ads_risk_class`、`ads_ltv_alerts`、`ads_stream_ltv_alerts`、`spacefin.collateral` | KPI 卡片（笔数/总敞口/预警/低置信/高危区）；五级分类分布柱状图（余额/笔数/占比，口径=ads_risk_class T+1 汇总）；LTV 直方图（0.85 红线染色，桶宽按红线加密）；城市贷款分布 + 高危区笔数（地址解析城市，CITY_MAP 前缀匹配）；离线/实时预警概览 |
| LTV 预警列表 | `ads_ltv_alerts` ∪ `ads_stream_ltv_alerts` + `spacefin.collateral` + `ads_alert_confirm` | 合并分页列表（LTV/估值/余额/抵押物地址/高危区标记/来源/确认状态）；风险类/LTV 区间/日期/来源筛选；风控「确认」；导出 CSV（客户号脱敏只留后 4 位） |
| 1104 报送 | `ads_1104_g11`、`dws_risk_class`、`ads_report_alert` | G11 五级 + 合计表；口径一致性实时校验（以 dws 明细聚合为裁判，逻辑同 `tools/reporting/main.py`）；阻断告警历史 |

### 插件页面（P1/P3/P5/P6/P7/P9/P10）

| 页面 | 模块 | 数据表 | 说明 |
|------|------|--------|------|
| 数据底座接入配置 | `p1_datasource.py`（admin/da） | `ods_cdc_log`、`ods_cdc_position`、`ods_cdc_consumer_offset`、`ads_cdc_alert`、`spacefin.loan/customer` | CDC 增量链路状态：位点、消费进度、CDC 告警、源库活跃度（对应 tools/cdc 与 ops-audit 链路） |
| 五级分类迁徙矩阵 | `p3_migration.py`（admin/risk/da） | `dws_risk_class_snapshot`（+ `dws_risk_class` 落快照） | 期初→期末 5×5 迁徙矩阵 + 各类别下迁率（Roll Rate）趋势；快照表由本模块幂等建表，`--snapshot` 落真实快照、`--backfill` 演示回填（is_demo=1 非真实历史）、`--purge-demo` 清理 |
| 空间风险画像 | `p5_spatial.py`（admin/risk/da） | `ads_spatial_zone`、`dws_spatial_feature`、`dws_risk_class`、`spacefin.collateral` | 高危区总览（zone 列表 + 命中规则 + 建区日期）、单区下钻（实体构成/价格偏离）、区内贷款风险分布 |
| 空间惩罚项配置 | `p6_policy.py`（admin/risk/da） | `ads_spatial_zone`、`dws_spatial_feature`、`dws_risk_class`、`spacefin.collateral` | 空间惩罚规则 CRUD + 试算预览（改规则后先预览再保存）+ 删除；基于 zone 网格生成（如 LTV 上限、低置信判定） |
| AVM 估值管理 | `p7_avm.py`（admin/risk/da） | `dws_risk_class`、`spacefin.collateral`、`ads_risk_valuation_alerts` | AVM 估值覆盖与精度、异常估值清单（`ads_risk_valuation_alerts`，按 alert_code 归类）、三分量归因展示（对应 AC-07 / R-UNW-03 口径） |
| 合规审计 / 特征归因 | `p9_compliance_audit.py`（admin/risk） | `ads_export_audit`、`ads_report_alert`（level=block）、`output/avm/attribution_report.json` | 三块：PII 导出脱敏留痕（TC-06，who/role/when/what/result/ip）、报送阻断告警（AC-05/AC-08「已阻断」态）、SHAP 特征归因报告（产物缺失时降级提示不 500） |
| 策略沙盒推演 | `p10_sandbox.py`（admin/risk/da） | `output/persona/persona_report.json` | 生成→批评→校准闭环说明框架（D-09 本期仅框架，不实现闭环交互）+ R-OPT-01「未校准」醒目标记（报告含 naive 输出时置顶红色横幅）+ KS/校准轨迹/分布对比证据 |

> 插件页的完整读写语义以其模块 docstring 与 README 为权威；本文只列数据表，不展开页面内部算法。

## 5. RBAC 角色矩阵（登录 + 服务端强制校验）

内置账号共 **4 个角色**（PRD §7.3 的 MVP 子集；凭据 dev-only 写死在 `app.py` 的 `USERS`，不落库）：

| 角色 | 账号 | 说明 |
|------|------|------|
| 系统管理员 | `admin` | 全部页面 + 确认/导出 |
| 风控策略经理 | `risk` | 全部页面 + 确认/导出 |
| 数据分析师 | `da` | 只读 + 导出（不可确认） |
| 贷后资产保全 | `postloan` | 驾驶舱 + 预警列表（不可见 1104 报送页，403） |

| 页面 / 操作 | admin | risk | da | postloan |
|------------|:-----:|:----:|:--:|:--------:|
| 资产质量驾驶舱 | ✓ | ✓ | ✓ | ✓ |
| LTV 预警列表（只读） | ✓ | ✓ | ✓ | ✓ |
| 预警确认 | ✓ | ✓ | — | — |
| 预警导出 CSV | ✓ | ✓ | ✓ | — |
| 1104 报送页 | ✓ | ✓ | ✓ | ✗ 不可见（403） |
| 合规审计/特征归因（P9） | ✓ | ✓ | ✗ 不可见（403） | ✗ 不可见（403） |
| 策略沙盒推演（P10） | ✓ | ✓ | ✓ | ✗ 不可见（403） |
| 插件页 P1/P3/P5/P6/P7 | 全部 | 除 P1 外 | 除 P1 外 | — |

> 权限在服务端每个 API 前强制校验（401 未登录 / 403 角色不符）；前端只根据 `/api/me` 裁剪导航与按钮，
> 属第二层防御。会话 cookie 带 `HttpOnly`，JS 不可读。

## 6. 数据源与一致性

- 数据**直读 MySQL**（`spacefin_crawler` 库的 ADS 表 + `spacefin` 业务库的 `collateral`），不经过 HTTP 中间层；
  连接参数 / 业务日口径复用 `tools/risk/config.py`，与风险引擎、报送、预警链路**同源**（同库同口径）。
- 五级分类顺序、LTV 红线（0.85）、口径容差（余额 0.01）与 `config.py` / `tools/reporting` 保持一致；
  1104 校验状态为页面实时复算（dws 明细聚合 vs `ads_1104_g11`），与报送 CLI 的阻断结论互相印证。
- 前端自用表 `ads_alert_confirm`（预警确认留痕，UNIQUE `(loan_id, alert_date, src)`，重复确认幂等）与
  `ads_export_audit`（操作审计：who/role/when/what/result/ip）由 app 启动时用 root 凭证幂等建表；
  只改前端自己的表，**不改动预警链路既有表**。建表权限不足时降级为无确认状态而不报错（确认/审计表
  缺失时静默降级，不阻断业务动作）。

## 7. 运行方式

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

- 默认端口 **8500**（已避开 MySQL 3306 / Redis 6379 / Airflow 8080 / Flink 8081 / Doris 9030 /
  Kafka 9092 / MinIO 9000 等占用）。
- 默认仅监听 `127.0.0.1`；如需局域网访问加 `--host 0.0.0.0`（开发环境自行评估暴露面）。
- 启动时幂等建 `ads_alert_confirm` 与 `ads_export_audit` 表（root + 房产库）。
- 插件页附带 CLI（如 `python tools/frontend/pages/p3_migration.py --snapshot`）用于落快照等管理操作，
  独立于 Web 服务运行。

## 8. API 一览（内置路由）

| 方法 | 路径 | 权限 |
|------|------|------|
| POST | `/api/login` `/api/logout` | 公开 |
| GET | `/api/me` | 公开 |
| GET | `/api/dashboard` | 登录 |
| GET | `/api/alerts` | 登录 |
| POST | `/api/alerts/confirm` | admin / risk |
| GET | `/api/alerts/export` | admin / risk / da |
| GET | `/api/report` `/api/report/dates` | admin / risk / da |

插件路由（`/api/datasource*`、`/api/migration`、`/api/spatial*`、`/api/policy*`、`/api/avm*`、`/api/compliance_audit`、`/api/sandbox`）由各页面模块
自行声明，权限 = 该页 `PAGE["roles"]`，服务端统一校验。

## 9. 已知局限

- **凭据 dev-only 写死**：账号/密码在 `app.py` 的 `USERS` 字典中，上线前必须接统一认证（外部 IDP /
  SSO），当前不落库、无密码策略。
- **会话为内存态**：`_sessions` 存进程内存，重启即全部失效；多实例部署需引入共享会话存储。
- **页面级可扩展但角色静态**：新增页面无需改框架代码，但角色集合与权限矩阵写在 `app.py` / 各页
  `PAGE["roles"]`，改角色需动代码，无运行时管理界面。
- **展示层不替代风控链路**：预警「确认」只写前端自用确认表 `ads_alert_confirm`，不回调/修改
  `ads_ltv_alerts` 等预警链路表；驾驶舱展示的实时口径取决于对应 ADS 表是否已由上游链路（risk /
  kafka-flink / reporting）产出。
