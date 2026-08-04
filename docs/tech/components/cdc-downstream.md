# 组件技术说明 · CDC 下游消费链（ODS 变更 → DWS/ADS 增量同步）

> **状态**：✅ 已实施（2026-08-05 落地，S1 收尾）
> **能力地图层级**：L0 数据底座 — 变更数据资产化与下游联动
> **所属系统**：tools/cdc（消费者）+ tools/risk（重算）+ tools/pipeline（统一入口）

---

## 1. 它解决什么问题

I-01 的 binlog CDC（`tools/cdc/main.py`）只做到「业务变更 1 分钟内落进 ODS」，ODS 之后是断的：
改一笔 loan，`ods_cdc_log` 里能查到事件，但 `dws_risk_class` / `ads_ltv_alerts` 还是旧值，
风险视图与业务库长期漂移，必须靠人手跑一次全量重算才对得上。

本组件把这一段接起来：**消费 ODS 变更 → 推导受影响贷款 → 只重算这几笔 → 刷新 DWS/ADS**。

## 2. 链路与分工

```
MySQL binlog ──tools/cdc/main.py──▶ ODS ──tools/cdc/consumer.py──▶ DWS/ADS
 (spacefin)     [spacefin-cdc]      ods_cdc_log    [spacefin-cdc-consumer]   (spacefin_crawler)
                                    data_lake/cdc/ods/
```

| 环节 | 载体 | 职责 |
|---|---|---|
| 采集变更 | `tools/cdc/main.py`，systemd `spacefin-cdc` | binlog → ODS，**贴源不解释**（before/after 全量 JSON） |
| 消费变更 | `tools/cdc/consumer.py`，systemd `spacefin-cdc-consumer` | ODS → DWS/ADS，**解释语义、驱动重算** |
| 重算落库 | `tools/risk/store.py` | 全量与增量共用的读取/计算/落库语义 |
| 统一编排 | `tools/pipeline/run_pipeline.py` | ETL → geocode → CDC 消费 → 风险重算 一条命令 |

**为什么拆成两个常驻服务**：binlog 读取必须单点（`server_id` 唯一、位点连续）。若把下游重算耦进
读取进程，一次重算变慢就会反压 binlog 消费、拉长位点滞后；拆开后下游可独立重启、重放、补跑，
而上游位点不受影响。

## 3. 消费语义

### 3.1 水位（offset）

- 表 `spacefin_crawler.ods_cdc_consumer_offset(consumer, last_id, updated_at)`。
- 以 `ods_cdc_log.id`（自增）而非 `cdc_ts` 做水位：**同秒多事件用时间戳会漏消费**。
- 按 `id ASC` 拉取 + 单向推进水位 = 至少一次（at-least-once）语义。重复消费无害，因为重算幂等
  （`upsert_dws` 按 loan_id 覆盖）。
- `--from-offset N` 可忽略水位表从头重放，用于对账。

### 3.2 影响面推导（增量的关键）

| 变更表 | 受影响贷款 | 理由 |
|---|---|---|
| `loan` | 该 `loan_id` | 余额/利率变了，LTV 与五级分类直接变 |
| `collateral` | 挂在该抵押物上的**全部**贷款 | 估值是 LTV 的分母，一改全改 |
| `customer` | 该客户名下**全部**贷款 | 当前风险口径未直接用客户特征，仍重算以保持视图新鲜 |

主档变更走一次 `SELECT loan_id FROM loan WHERE collateral_id IN (...)` 反查，**不逐事件打 DB**。

### 3.3 DELETE 与冲突

- `loan` DELETE → 清掉 DWS 明细与当日预警，避免下游看到幽灵敞口。
- **删除优先于重算**：同一批里若某笔先 UPDATE 后 DELETE，最终态是不存在，不能再 UPSERT 回去
  （`plan_impact` 里 `recalc -= deleted`）。
- `collateral`/`customer` DELETE → 其名下贷款按「无抵押物」保守重算（`risk_engine` 已处理
  `collateral=None`，归「损失」）。

### 3.4 写库边界（防止增量误伤全量）

| 操作 | 做法 | 为什么不能图省事 |
|---|---|---|
| DWS 明细 | `ON DUPLICATE KEY UPDATE` 按 loan_id 幂等覆盖 | — |
| 预警 | 只删「本批 loan_id **且** 当日」再插 | 全表 DELETE 会把当日其它贷款的预警一起抹掉 |
| 五级汇总 | 从 `dws_risk_class` 现状 **SQL 聚合**刷新 | 占比分母是全量余额，只更新本批会让占比失真 |

## 4. 业务日期口径（踩过的坑）

`stat_date` / `alert_date` 一律按 **Asia/Shanghai**（`tools/risk/config.py: BUSINESS_TZ`、
`business_date()`），不用 `time.strftime` 的本地时区。

原因：本机三方时区不一致——宿主是 **UTC**，MySQL 容器是 **+08:00**，Airflow DAG 的 `{{ ds }}`
按 `LOCAL_TZ=Asia/Shanghai` 生成。若用本地时区，每天 **16:00–24:00 UTC** 这 8 小时内跑的批次会把
「业务上的第二天」标成第一天，同一批数据被拆进两个 `stat_date`，五级分类占比的分母随之错乱。
可用 `SPACEFIN_BUSINESS_TZ` 覆盖。

## 5. 运行方式

### 5.1 常驻（实时，systemd）

```bash
cp deploy/systemd/spacefin-cdc-consumer.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now spacefin-cdc-consumer
```

轮询间隔默认 20s，可在 `~/.config/spacefin/cdc.env` 里设 `SPACEFIN_CDC_CONSUMER_INTERVAL`。

### 5.2 批处理（Airflow / 手动）

DAG `guangdong_daily_crawl` 尾部新增两个任务：

```
... → geocode_backfill_finalize → cdc_consume → risk_recalc
```

`cdc_consume` 在常驻服务之外**再跑一次**，是批处理时点的对齐保证：不依赖常驻进程是否健康，
DAG 自己确保上下游一致。`risk_recalc` 必须排在其后——CDC 只覆盖「有变更的贷款」，而 ETL 刷新的
行情（DWD）影响**全部**贷款的估值，只有全量过一遍 LTV 才跟得上新行情。

### 5.3 统一入口

```bash
# 完整链：ETL → geocode → CDC 消费 → 风险重算
tools/orchestrator/.venv/bin/python tools/pipeline/run_pipeline.py --date 2026-08-05

# 只刷下游（业务库改动后快速同步）
tools/orchestrator/.venv/bin/python tools/pipeline/run_pipeline.py --only cdc,risk

# 演练
tools/orchestrator/.venv/bin/python tools/pipeline/run_pipeline.py --dry-run
```

阶段顺序由 `STEP_ORDER` 固化，`--only` 给的书写顺序会被忽略——顺序是数据依赖，不该由调用方决定。

## 6. 验收记录（2026-08-05）

| 项 | 结果 |
|---|---|
| 改一笔 loan → ODS 可见 | **3s**（`ods_cdc_log` + `data_lake/cdc/ods/loan/dt=*/`） |
| 改一笔 loan → DWS/ADS 同步（常驻服务全自动） | **20s**（要求 ≤60s） |
| 是否全量重算 | 否，`recalc_loans=1`（全库 200 笔） |
| 增量与全量结果互证 | 一致（均为 2 条预警、`total_balance` 相同） |

实测样例：`loan 30001` 余额 `1219.41 → 6800.00`，LTV `0.1740 → 0.9701`，越过红线 0.85 自动进入
`ads_ltv_alerts`，`ads_risk_class` 汇总同步刷新。

## 7. 已知局限

- **数据鸿沟未消除**：`spacefin` 种子是上海合成地址（`合成地址-N`, lat≈31），与广东 DWD 不对齐，
  故 `dwd_hits=0`、`avm_hits=0`，估值仍回退 `true_market_price`。要让 DWD/AVM 行情估值生效，
  须先把种子换成广东城市地址。这不是消费链的缺陷，是种子数据问题。
  AVM 接入见 `tools/risk/valuation.py`：`property_addr` 含广东城市名即命中（三级回退
  AVM → DWD 中位 → true_market_price），合成地址无城市码时正确落回兜底。
- **AVM 指标**：sale DWD 上 MAPE 16.63%（基线 22.57%，较初版 19.49% 再降；title 回填小区 +
  外市清洗 2038 行），未达 10% 目标——剩余瓶颈是 16.9% 行无楼盘名可解析且无坐标、sz 全表
  无坐标、残留外市污染段（详见 `tools/avm/README.md` 误差分解）。
- **L2 空间特征已接入**：`dws_spatial_feature`（S3）对抵押物按「有效值才覆盖」更新
  poi_density/commute_min/is_high_risk_zone——落在空间网格外（如种子上海坐标）维持占位值，
  不误判低置信；模块说明见 [spatial-feature.md](spatial-feature.md)。
- **DWD 单价词典每批全量加载**：44k 行聚合在秒级，暂未按增量拆分。若 DWD 涨到百万级需改为缓存。
- **at-least-once 而非 exactly-once**：依赖重算幂等消化重复。跨库事务不在 MVP 范围。
