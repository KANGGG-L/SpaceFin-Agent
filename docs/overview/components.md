# 组件与目录逐一介绍

> 目标：把仓库里每个模块「是谁、干什么、入口在哪、怎么验证它」讲清楚。分层视角见 [architecture.md](architecture.md)，本文按目录逐个展开。

## `tools/` 模块速查表

| 模块               | 一句话职责                                                     | 主要入口                                   | 测试位置（`test_*.py` 数） |
| ------------------ | -------------------------------------------------------------- | ------------------------------------------ | -------------------------- |
| `frontend`         | 零依赖 Web 驾驶舱（展示 + RBAC + 确认/导出留痕）               | `app.py`（端口 8500）                      | `tests/`（48）             |
| `risk`             | 风险引擎：估值 → LTV → 五级分类 → 写 DWS/ADS                   | `main.py`（`--date` / `--write-db`）       | `tests/`（6）              |
| `avm`              | 自动估值模型：训练 + 推理 + 误差归因                           | `train.py` / `predict.py` / `gen_attribution.py` | `tests/`（4）        |
| `spatial`          | L2 空间特征：价格面 / 高危区 / POI 密度 / 通勤近似             | `main.py --once`                           | `tests/`（3）              |
| `alerting`         | LTV 预警 T+1 推送（去重 + 失败重试状态机）                     | `main.py --date`                           | `tests/`（5）              |
| `reporting`        | 1104 G11 监管报送 + 三出口口径校验（不一致即阻断）             | `main.py --date`                           | `tests/`（2）              |
| `cdc`              | binlog 变更捕获（ODS）与下游增量重放（DWS/ADS）                | `main.py` / `consumer.py` / `bootstrap_ods.py` | `tests/`（3）        |
| `orchestrator`     | 容器编排采集系统：master 主备 + worker + ETL + 质量闸门 + 观测账本 | `master.py` / `etl.py` / `start_all.sh` | `tests/`（19）             |
| `anjuke_crawler`   | 安居客单页爬虫库：抓取 / 解析 / 地理编码（离线 fixture 可复现） | `main.py`（`parse`/`geocode`/`crawl` 子命令） | `tests/`（12）         |
| `lake`             | Doris + MinIO 湖仓同步（MySQL → ODS → 分层加工 → Parquet → 对账） | `sync.py`                              | `tests/`（1）              |
| `compliance`       | Doris 中文倒排索引敏感词检索（I-04，毫秒级）                   | `inverted_search.py`                       | 模块内 `test_inverted_search.py`（1） |
| `persona`          | 合成行为仿真 Generator → Critic → 校准闭环（H3 验证）          | `main.py`                                  | `tests/`（4）              |
| `pipeline`         | 统一数据链入口：把 7 个步骤按依赖顺序收口成一条命令            | `run_pipeline.py --date`                   | 无独立测试（薄编排层）     |
| `ops`              | 资源管家：容器 + systemd 服务 + 内存水位三级启停               | `manage.sh`                                | 无（shell 工具）           |
| `dev`              | 7 天风险演进演示：逐日回填 + 验收 SQL 检查                     | `backfill_7d.py` / `verify.py`             | 无（自带验收清单）         |

> 全仓 `test_*.py` 共 109 个（tools/ 内 108 + `deploy/superset/` 1 个）；四个核心模块（frontend / risk / alerting / avm）合计 63 个文件、858 个 `def test_` 用例。

## 核心引擎模块细讲

### `tools/risk/` — 风险引擎（业务心脏）

- **职责**：对每笔贷款重算「抵押物估值 → LTV → 五级分类」，产出预警与汇总。
- **入口**：`python tools/risk/main.py --date <业务日> --write-db`（`--dry-run` 只算不写）。
- **关键设计**：估值三级回退（AVM 命中 → DWD 行情命中 → 业务库 `true_market_price` 兜底，命中率记入 `risk_report.json`）；LTV 两档线警示 0.75 / 强预警 0.85 均可配置，边界「严格大于才触发」；低置信标记（空间特征缺失率 >75）；`config.py` 是全链路共享的连接参数 / 业务口径来源（前端、报送、预警都复用它，避免口径漂移）。
- **产出表**：`dws_risk_class`（逐笔明细）、`ads_ltv_alerts`（预警）、`ads_risk_valuation_alerts`（估值异常核查）、`ads_risk_class`（五级汇总）。

### `tools/avm/` — 自动估值模型

- **职责**：用机器学习估住宅单位面积价格，替代人工评估与「小区中位价 × 面积」的粗糙口径。
- **入口**：`train.py`（训练，产出 `output/avm/model.joblib` + `avm_report.json`）；`predict.py`（推理接口，供风险引擎调用）；`gen_attribution.py`（幂等生成 P7 归因产物）。
- **关键设计**：HistGradientBoostingRegressor 单模型 + 坐标最近邻空间特征（`nn_med_k3/k8/k20/k50`），目标变量取 `log(单价)`；回退语义与训练一致（小区 → 城市 → 全局中位价）；样本不足门槛明确退出并提示「降级人工」；模型产物 `output/avm/model.joblib`（约 22MB）**不随仓库分发**（`output/` 被 `.gitignore` 排除），使用前需本机 `tools/avm/train.py` 重训。
- **精度口径**：45% 覆盖率下 MAPE 9.94%（≤10% 达标），全量 15.00%，基线 20.6%。

### `tools/spatial/` — 空间特征

- **入口**：`python tools/spatial/main.py --once [--date] [--dry-run]`。
- **关键设计**：单机 cKDTree 近似替代 Apache Sedona（架构等价性由 `sedona_demo.py` 演示）；高危区 A=价格洼地（区块中位单价 ≤ 城市中位 ×0.75 且样本 ≥20）、B=LTV 集中区；POI 以挂牌密度代理、通勤以直线距离近似——两处降级都是**已声明的工程简化**，不是隐藏缺陷。
- **产出**：`ads_spatial_zone`（区块画像）、`dws_spatial_feature`（逐实体特征）。

### `tools/alerting/` + `tools/reporting/` — 应用出口

- **alerting**：读当日 `ads_ltv_alerts`，按「T+1 去重 + 失败重试状态机」推送；driver 可组合（站内收件箱 `site_inbox` / JSONL 文件 / 可选 `postloan_http` webhook）。`audit_final_failed.py` 做终态失败审计（DAG 的 G4 步）。
- **reporting**：把 `ads_risk_class` 重排成 1104 G11 模板落 `ads_1104_g11` 并导出 CSV/JSON；**三出口校验**（模板 vs 明细 SQL 聚合 vs 汇总自比）任一项对不上就阻断报送并写 `ads_report_alert`——「宁可拒报，不把口径不一致的数字送出去」。`g11_report.py` 是历史兼容转发入口。

### `tools/cdc/` — 变更捕获双进程

- `main.py`：读 `spacefin` 库 binlog → 写 ODS（`ods_cdc_log` + Parquet 贴源），位点持久化、断点续跑，内置位点停滞 / 消费积压告警。
- `consumer.py`：解释 ODS 变更 → 按影响面推导（collateral 变更会牵动其全部贷款）增量重放 DWS/ADS，幂等可重跑。
- `bootstrap_ods.py`：初始全量快照补数（demo 数据由批处理直灌、不经 binlog，故 ODS 默认空是预期）。

## `tools/frontend/` — 驾驶舱（独立展开）

- **技术选型**：stdlib `http.server` + PyMySQL，零新增依赖；前端原生 JS + 内联 SVG，无构建链。选型对比（为何不用 Streamlit/FastAPI）见 [tools/frontend/README.md](../../tools/frontend/README.md)。
- **插件化页面**：`pages/` 下「一个页面 = 一个模块文件」，导出 `PAGE` dict（id/label/roles/order/js/routes）即自动注册，路由与 RBAC 服务端强制校验。当前 3 个内置页（驾驶舱 / 预警列表 / 1104 报送）+ 6 个插件页（P1 数据底座、P3 迁徙追踪、P5 空间画像、P6 策略推演中心、P7 AVM、P9 合规审计），P10 策略沙盒模块已并入 P6（按设计评审口径为九页）。
- **RBAC**：两角色 `admin` / `postloan`（贷后不可见报送页，403）；dev 模式内置弱口令，生产走 `SF_PWD_*` 环境变量或随机强口令持久化。
- **AI 能力**：`sandbox_agent.py` 接入 LangChain Agent（OpenAI 兼容 `/chat/completions`，模型可配，见 [AI 链路说明](../tech/ai-decompose-and-anthropic.md)）做「输入假设 → 推演」，无 key / 依赖缺失 / 调用失败一律降级为确定性规则推演；`ai_guard.py` 做注入防护与白名单约束。
- **数据分类分级**：`data_classification.py` 列级元数据 + 导出按级强控，PII 脱敏（客户号 `c****后4位`）+ `ads_export_audit` 审计留痕。

## 采集侧：`tools/orchestrator/` + `tools/anjuke_crawler/`

- **anjuke_crawler**（库）：`fetch/`（curl_cffi 伪装 Chrome + 代理轮换 + stealth）、`parse/`（列表页/数值/租房解析，附真实 HTML fixture 离线可复现）、`geocoder.py`（离线地理编码）、`distributed/worker.py`（队列消费节点）、`k8s_manifests/`（K8s 备选路径清单，未部署）。
- **orchestrator**（编排）：`master.py` 主备选举（Redis 抢锁）+ 42 任务状态机（21 城 × sale/fangyuan）+ 每城 IP 预算 + 双代理池管理 + HTTP API（:5100）；`etl.py` 按观测日目录（`raw/ds=<日>/`）增量清洗入库；`census.py` 观测账本（「今天没看到 ≠ 已下架」的完备性证据）；`market_state.py` 在架/消失/下架/流转率判定；`crawl_quality_gate.py` 采集质量闸门；`slack_notify.py` DAG 失败四角色告警；`host_render_service.py` 宿主 Chrome 渲染（:8899）。
- 采集横向扩展由「master 主备 + 多 worker + Redis 队列」提供；K8s 路径仅为清单备选。

## 其余目录

| 目录/文件                      | 内容                                                                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `sql/init/`                    | `01_schema.sql`（spacefin 业务库建表）+ `02_seed.sql`（合成 seed 产物，容器首次初始化自动导入）                                     |
| `sql/doris/`                   | `01_compliance_audit_inverted.sql`（倒排索引表）、`02_superset_pii_views.sql`（PII 脱敏视图）                                        |
| `seed/`                        | `generate_seed.py`：固定种子 + 固定基准日，重复运行逐字节一致；说明见 [seed/README.md](../../seed/README.md)                          |
| `airflow/`                     | DAG `guangdong_daily_crawl.py`（每日 00:30：拉起采集栈 → 盯完成 → ETL/闸门/排空/关栈/审计收尾）+ 容器化部署 compose；手册见 [airflow/README.md](../../airflow/README.md) |
| `deploy/doris-minio/`          | Doris FE/BE + MinIO 的 docker-compose（BI 分析副本，核心链不依赖）                                                                   |
| `deploy/superset/`             | Superset 镜像构建、初始化脚本（角色/图表/示例仪表盘）、审计写入与合规测试                                                            |
| `deploy/systemd/`              | 用户级 systemd 单元：CDC、CDC 消费者、前端、宿主渲染（VPS 常驻托管）                                                                 |
| `deploy/vps/`                  | [DEPLOYMENT.md](../../deploy/vps/DEPLOYMENT.md)：Linux VPS 新机部署实录（含精简版范围说明）                                          |
| `.github/workflows/`           | `ci.yml`（pre-commit 全量校验，Python 3.10 + Node 24）、`codeql.yml`（代码扫描）、`commitlint.yml`（提交信息规范）、`deploy.yml`（打 Release） |
| 根目录                         | `Makefile`（`make help` 列全部目标）、`docker-compose.yml`（MySQL + 可选 Superset）、`dev.sh` / `dev_server.py`（热重载开发）、`environment.yml` / `requirements*.txt`（Python 环境）、`vercel.json`（前端静态托管 + API 反代） |

## 已移除的组件（精简版）

Kafka + Flink 实时流层（原 `tools/stream/`、`deploy/kafka-flink/`）已从本分支删除，实时预警改为 T+1 批处理推送（`tools/alerting/`，Airflow 排程）。驾驶舱与文档中凡出现「实时链路已移除」字样的页面/说明，均与此口径一致。
