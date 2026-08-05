# 组件技术说明 · Doris + MinIO 湖仓（L0 数据底座）

> **状态**：✅ 已接入（2026-08-05 落地）
> **能力地图层级**：L0 数据底座 — 湖仓分层（MinIO 对象存储 + Doris 分析型 MPP）
> **所属系统**：`deploy/doris-minio`（部署）+ `tools/lake`（接入/同步/对账）

---

## 1. 它解决什么问题

R-tech-1 评估技术栈复杂（Doris+MinIO），开发计划原建议暂缓；但 MVP 阶段决策为
**全量接入**：在硬件受限（4 核 / 15G 内存 / 107G 磁盘）的单机上，用最小节点
（单 MinIO + 单 FE + 单 BE）把「湖」与「仓」真正跑起来，并打通两条数据通路：

1. **MySQL 源 → ODS**：`spacefin` 业务库（loan/collateral/customer）与
   `spacefin_crawler` 房产库（crawl_housing_sale/rent、dws_risk_class、ads_* 等）贴源进 Doris。
2. **数据湖文件 → MinIO → 联邦查询 → 落仓**：`data_lake/housing/*.parquet` 上传 MinIO，
   在 Doris 上用 S3 表函数（TVF）**不落仓直接查**（联邦查询），并可一键物化进 ODS。

分层语义对齐标准数仓：ODS 贴源 → DWD 清洗 → DWS 聚合 → ADS 应用，为后续
AVM 估值 / LTV 预警 / 1104 报送提供可 SQL 直查的分析底座。

## 2. 部署拓扑与端口

`deploy/doris-minio/docker-compose.yml`，官方镜像，全部 `network_mode: host`
（沿用官方 quick-start：Linux 下单 FE 以 `FE_SERVERS=fe1:127.0.0.1:9010` 自举、
只绑定 127.0.0.1，BE 以 `BE_ADDR=127.0.0.1:9050` 注册，跨容器互通无桥接 IP 问题）。

| 服务 | 镜像 | 容器名 | 端口 | 数据目录 |
|------|------|--------|------|---------|
| MinIO | `minio/minio:latest` | `spacefin-minio` | 9000(S3 API) / 9001(控制台) | `/srv/spacefin-lake/minio/data` |
| Doris FE | `apache/doris:fe-2.1.9` | `spacefin-doris-fe` | 8030(HTTP UI) / 9010(edit log) / 9020(rpc) / 9030(MySQL 协议) | `/srv/spacefin-lake/doris/fe-meta` |
| Doris BE | `apache/doris:be-2.1.9` | `spacefin-doris-be` | 8040(BE HTTP/Stream Load) / 9050(heartbeat) / 9060(thrift) / 8060(brpc) | `/srv/spacefin-lake/doris/be-storage` |

端口与既有组件零冲突（MySQL 3306、Redis 6379、Airflow 8080、CDC 无端口）。

### 内存预算（关键）

宿主仅 15G 内存，逐容器设 `mem_limit` 上限，防止 Doris 默认配置（FE 堆 8G、
BE mem_limit = 宿主 80%）把整机打 OOM：

| 容器 | mem_limit | 关键配置 | 实测占用 |
|------|-----------|---------|---------|
| minio | 1g | 默认 | ~96 MB |
| doris-fe | 3g | `conf/fe.conf` JVM `-Xmx2048m -Xms1024m`（镜像 JDK8 走 `JAVA_OPTS`） | ~1.1 GB |
| doris-be | 5g | `conf/be.conf` `mem_limit = 4G` | ~1.3 GB |

数据目录放在仓库外 `/srv/spacefin-lake`（107G 可用磁盘），避免数据文件进入 git 扫描范围。

## 3. 分层方案与表清单

`tools/lake/schema.py` 定义，Doris 单副本（`replication_num=1`）、DUPLICATE KEY 模型、
按首列 HASH 分桶。共 **24 张表**：

| 层 | 库 | 表 | 行数 | 说明 |
|----|----|----|------|------|
| ODS | ods | ods_loan / ods_collateral / ods_customer | 200×3 | spacefin 业务库贴源 |
| ODS | ods | ods_housing_sale / ods_housing_rent | 44369 / 4244 | 房产库贴源（sale 与 MySQL 对账一致） |
| ODS | ods | ods_dws_risk_class / ods_dws_spatial_feature | 200 / 17439 | 上游 DWS 结果贴源 |
| ODS | ods | ods_community_coords / ods_cdc_log / ods_cdc_consumer_offset | 13255 / 2210 / 2 | 坐标、CDC 变更日志贴源 |
| ODS | ods | ods_ads_risk_class / ods_ads_1104_g11 / ods_ads_alert_dispatch / ods_ads_alert_inbox / ods_ads_ltv_alerts / ods_ads_spatial_zone | 5 / 6 / 16 / 16 / 18 / 132 | 上游 ADS 结果贴源 |
| ODS | ods | **ods_housing_sale_lake** | 23270 | 湖 Parquet（2026-08-04 快照）物化落仓 |
| DWD | dwd | dwd_housing_sale | 44349 | 去重（url_key 取最新）+ 剔无效报价/面积 + 补算楼龄 |
| DWD | dwd | dwd_loan_detail | 200 | loan+customer+collateral 三表宽表 |
| DWS | dws | dws_city_price_stats | 21 | 城市级均价/中位价/挂牌量聚合 |
| DWS | dws | dws_risk_class | 200 | 风险五级分类宽表（源自 ods） |
| ADS | ads | ads_risk_class | 5 | 五级分类占比（stat_date=2026-08-05） |
| ADS | ads | ads_city_avg_price | 21 | 城市均价应用表（供驾驶舱） |
| ADS | ads | ads_1104_g11 | 6 | G11 报送口径汇总 |

**DWD 清洗口径**（`DWD_FILL_SQL`，重跑幂等）：
- 同一 `url_key` 只保留 `last_seen_date` 最新一条（安居客同一房源多次更新）；
- 剔除 `unit_price_yuan <= 0` 或 `area_sqm <= 0` 的无效报价（源数据 20 条）；
- `building_age` 缺失时按 `first_seen_date.year - building_year` 推算。

**类型映射注意点**（踩坑记录）：
- Doris `VARCHAR(n)` 的 n 按**字节**计（UTF-8 中文 3 字节/字）。`dws_spatial_feature.entity_id`
  存「城市码|中文房源标题」，最长 94 字节，`VARCHAR(64)` 会整批过滤，需 `VARCHAR(128)`。
- Doris 不允许 STRING 类型作为 key 列，key 列必须定长 VARCHAR。
- 列分隔符用 `\x01`（SOH）而非 `\t`：标题字段偶含 `\t`/换行，`\x01` 在业务文本中不可能出现。

## 4. 数据导入链路

### 4.1 MySQL → ODS（Stream Load）

`tools/lake/sync.py` 读取 MySQL 全表 → 按 ODS 列序生成 `\x01` 分隔 CSV
（NULL 用 Doris 约定 `\N`）→ PUT 到 BE `8040/api/{db}/{table}/_stream_load`
（严格模式 `max_filter_ratio=0`，坏行整批失败）→ 立即 `COUNT(*)` 复核落库行数。

### 4.2 湖 Parquet → MinIO → Doris（联邦查询 + 物化）

`tools/lake/minio_sync.py` 把 `data_lake/housing/dt=<date>/type={sale,rent}/city=*/`
的 Parquet 上传 MinIO 桶 `housing`（按类型扁平化为 `sale/*.parquet`、`rent/*.parquet`，
城市信息保留在文件 `district` 列）。上传实现为 requests 手写 AWS SigV4 签名，
不引入 minio/boto3 依赖。

**联邦查询**（不落仓直查，Doris S3 表函数）：

```sql
SELECT COUNT(*) FROM s3(
  'uri' = 's3://housing/sale/*.parquet',
  's3.access_key' = 'spacefin_minio',
  's3.secret_key' = 'spacefin_minio_dev_only',
  's3.endpoint' = 'http://127.0.0.1:9000',
  's3.region' = 'us-east-1',
  'format' = 'parquet',
  's3.path.style.access' = 'true'
);
```

物化落仓：`INSERT INTO ods.ods_housing_sale_lake SELECT ..., '<date>' AS snapshot_dt FROM s3(...)`。

## 5. 验证结果（2026-08-05 对账报告）

`tools/lake/sync.py` 内置对账，MISMATCH/CHECK 时退出码非 0。关键证据：

| 验证项 | 结果 |
|--------|------|
| 16 张 ODS 表与 MySQL 行数对账 | 全部一致（如 ods_housing_sale 44369=44369、ods_cdc_log 2210=2210） |
| 湖上联邦查询（MinIO Parquet 直查） | `sale/*.parquet` 23270 行 / 21 城，秒级返回 |
| 湖→仓物化 | ods_housing_sale_lake 23270 行，与联邦查询一致 |
| DWD 清洗 | dwd_housing_sale 44369 → 44349（剔 20 条无效报价） |
| DWS 城市聚合 vs MySQL | sw/hui/mz/st/fs Top5 城市挂牌量与均价逐城一致 |
| ADS 应用层 | ads_risk_class 5、ads_city_avg_price 21、ads_1104_g11 6 行 |

## 6. 常用命令

```bash
# 启动/停止/状态
docker compose -f deploy/doris-minio/docker-compose.yml up -d
docker compose -f deploy/doris-minio/docker-compose.yml down   # 保留数据卷
mysql -h127.0.0.1 -P9030 -uroot                                # Doris MySQL 协议入口（宿主机需 mysql 客户端）

# 湖仓同步（tools/lake 下，用 tools/orchestrator/.venv）
python sync.py              # 全量
python sync.py --verify-only
```

## 7. 已知局限

- **单机单副本**：单 FE + 单 BE、`replication_num=1`，无高可用；容器重建需数据目录
  （`/srv/spacefin-lake`）完好，数据在宿主磁盘上而非容器内。
- **内存受限**：FE 堆压到 2G、BE mem_limit 4G；当前 4.4 万行量级绰绰有余，若后续接入
  真实亿级数据需扩容内存或改为多 BE 分布式。
- **全量同步**：sync.py 为 TRUNCATE + 全量重灌，适合开发/日批；未做增量 CDC 消费进 Doris。
- **CDC 追加表对账口径**：ods_cdc_log 等由常驻服务持续追加，对账以**同步时刻**的行数为准
  （`expected` 参数），避免对账时点晚于同步时点造成的假 MISMATCH。
- **标题含换行**：`crawl_housing_sale.title` 有 11 条含字面 `\n`（采集源瑕疵），导入时
  换行退化为空格并告警计数；`\x01` 列分隔符不受影响，数据量占比可忽略。
- **VARCHAR 按字节计**：中文长字段需预留 3 倍长度（见第 3 节踩坑记录）。
