# 组件技术说明 · 采集数据 ETL（安居客爬虫 → MySQL DWD + ODS 数据湖）

> **状态**：✅ 已实施（2026-08-03 决策 + 2026-08-04 落地；geocode 已解耦为独立补全链路）
> **能力地图层级**：L0/L1 数据底座 — 采集数据资产化
> **所属系统**：tools/orchestrator（与爬虫容器编排同仓）

---

## 1. 目标与优先级

ETL 是采集系统（master/worker 容器编排）的收尾环节，由 Airflow DAG `guangdong_daily_crawl` 的
`etl_finalize` 任务触发（`BashOperator` 调 Python 环境里的 etl.py——DAG 经 `_resolve_python_bin()`
优先 `tools/orchestrator/.venv`、容器内不可用时回退容器 `python3`；现 Airflow 为容器化部署，参见
[crawler-airflow.md](airflow.md) §3 / §6-1）。目标优先级（用户确认）：

1. **A · 数据资产化**：把 raw JSONL 沉淀为可查询、可追溯的结构化资产——MySQL DWD 主表存每房源最新状态，ODS 湖 Parquet 存每日观测快照。
2. **B · 跨日去重 + 市场留存**：同一房源不重复，且能回答"房源在市场上挂了多久"（`days_on_market`）。
3. **C · 补全增强**：geocode 补坐标（离线词典 + miss 清单）。
4. **D · 下游消费入口**：产出可被分析/Doris/报表直接消费（接口约定）。

## 2. 已确认决策（19 项，grill-me 2026-08-03）

### 2.1 存储目标

| 层             | 载体                                                                            | 语义                                                                                                 |
| -------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **ODS 数据湖** | `data_lake/housing/dt=YYYY-MM-DD/type={sale\|rent}/city={gz}/part-*.parquet`    | 每日观测集（当日去重后完整字段房源行 + url_key + first/last_seen_date + source），可独立分析、可追溯 |
| **DWD 主表**   | MySQL 独立库 `spacefin_crawler`，表 `crawl_housing_sale` / `crawl_housing_rent` | 每房源一行最新状态 + 市场留存                                                                        |

### 2.2 去重与留存语义

- **url_key** 唯一键（`url_key VARCHAR(64) PRIMARY KEY`），规范化房源 ID：
  - `sale:anjuke:Sxxx`（`prop/view/Sxxx`）、`sale:58:xxx`（`xinfang/huxing/xxx`）、`rent:zu:xxx`（`fangyuan/\d+` 及 `gfangyuan/\d+`）
  - 提取 ID 后剥离 query（同房源不同 query 归一到同一 key）
  - 未知形态回退 `md5(url)`（当前 187,313 行全部有 url，md5 兜底仅防御）
- **跨日去重**：`INSERT ... ON DUPLICATE KEY UPDATE` + **COALESCE 只更新非空/非零字段**
  - `first_seen_date` 保留首次日期（不覆盖）
  - `last_seen_date` = **本批 raw 的观测日**（`raw/ds=<观测日>` 目录名），不再是「本次 ETL 运行日」
  - `last_seen_page` = 该房源本次出现在列表第几页；`NULL` 表示该行没有页码证据（S1 切换前的
    历史行），闸门据此把「刷新」与「ETL 恰好又读了一遍」区分开
  - `days_on_market = last_seen_date - first_seen_date`
  - 可变字段（价格/面积/朝向等）新值有效才更新；解析失败的空值不污染已入库好数据
- **url 字段**：`VARCHAR(1024)`（实测最大 982 字符），**不建索引**，仅 `url_key` 作唯一索引

### 2.3 增量范围与识别（S1 起）

- **目录即观测日**：worker 按 `raw/ds=<观测日>/{city}_{type}_{worker_id}.jsonl` 落盘，观测日来自
  `CRAWL_RUN_ID`（DAG 传 `{{ ds }}`，**不是** wall clock——跨零点重试会被拆成两天）
- **增量只扫 `raw/ds=<本次观测日>/` 一个目录**；`etl_processed.json` 的键就是**相对 raw_dir 的路径**
  - 识别"未处理过 或 mtime+size 变化"的文件（worker 断点续爬会追加写同一文件）
  - 处理完才更新标记；幂等（upsert 幂等 + 湖分区覆盖写 + 标记防重复消费）
  - 旧版扫 raw 顶层扁平 `*.jsonl`：那批跨日追加的文件装着全部历史语料，于是每晚把整批历史
    重读并重写 DWD（实测 2026-08-30：inserted=0 / updated=28,619 / 25,241 行的 `last_seen_date`
    被刷成当天）——`last_seen_date` 从此只是「ETL 重跑日」的函数
- **本观测日没有 ds 目录 → WARN + 0 行，绝不报错退出**：零采集日必须由 G2 闸门判 exit 2，
  而不是让 ETL 崩在半路把整条链路变成「未知失败」
- **`--backfill` = 无视处理标记、重跑目标观测日**（配合 `--date` 指定哪天）；重建全部历史请对
  `ds=*` 目录显式循环，一次一天——每个 ds 的 `last_seen_date` 才仍然是那一天的真话
- **`--legacy-flat-raw`**：只读 S1 切换前的顶层扁平语料，仅供一次性回补；默认永不读
- **`--date` = DAG 执行日（`{{ ds }}`）**，决定扫哪个 ds 目录、湖分区 `dt=` 与 `last_seen_date`；
  `--observe-date` 可在人工回补时把「落湖分区日」与「观测日」拆开

### 2.4 清洗与 geocode

- **轻量清洗 + 保真入库**：数值字段强制类型转换（失败置 None）、负值/0 价格面积置 None、去首尾空白；
  **不做业务阈值清洗**（留给下游 DWS/分析层）
- **geocode**：ETL 落库时通过 `DbGeocoder`（词典存 `community_coords` 表，按 `(city,community)` 隔离跨城重名）对坐标为 null 的行尝试词典命中补坐标；命中即写回，未命中则标记 `geocode_status=pending` 交 `geocode_fill.py` 后续补。**不再产出 `geocode_miss_{date}.json` 文件**——miss 清单走 `community_coords` 表的 status 状态机，由 `geocode_backfill.py` / `geocode_fill.py` 消费。
- **去掉 CSV 输出**：只落 MySQL DWD + ODS 湖，单一数据出口

### 2.5 geocode 解耦（方案 C：状态标记代替物理搬家）

坐标补全**从 ETL 主链路解耦**：ETL 落库时只写 `geocode_status`（pending/hit），坐标缺失也照常入库；补全由独立脚本周期执行，不阻塞主链路。三个脚本职责单一：

- **`etl.py`**：落库时通过 `DbGeocoder` 对坐标为 null 的行尝试词典命中补坐标，命中即写回并标 `geocode_status=hit`，未命中则标 `geocode_status=pending`；**不扫历史 null 行**（历史补全交给 `geocode_backfill.py`）。
- **`tools/orchestrator/geocode_fill.py`**：用腾讯位置服务 geocoder（`/ws/geocoder/v1`，每日 6000 配额）对 `community_coords` 词典里 `status='pending'` 或 miss 超 7 天的小区批量补坐标；查询带 `region=城市名` 消除跨城重名；只采纳 `level>=10`（小区/大厦级）；腾讯返回 GCJ-02，写入前转 WGS-84 统一口径；单小区只调一次 API，断点续跑由 status 驱动。
- **`tools/orchestrator/geocode_backfill.py`**：读 `community_coords` 词典 hit 行 → 批量 UPDATE DWD 中 `geocode_status IN ('pending','miss')` 且坐标为 null 的行；miss 小区标记不重试（7 天可重查）。

**词典表 `community_coords`**（在 `sql/crawl_schema.sql`，库 `spacefin_crawler`）：复合主键 `(city, community)` 隔离跨城重名，状态机 `pending/hit/miss`，并含 `source`/`level`/`query_count`/`last_queried_at` 等字段。

### 2.6 运行环境与建库

- **etl.py 由容器化 Airflow 的 `etl_finalize` 任务执行**（BashOperator 传 `--date {{ ds }}`）；Python 解释器经
  `_resolve_python_bin()` 探测：优先 `tools/orchestrator/.venv`（单一环境 conda `spark` 的别名；容器内为悬空
  软链），不可用时自动回退容器 `python3`（`/home/airflow/.local/bin/python3`，Python 3.12.9，预置
  pymysql/pyarrow 等依赖）
- **建库建表 = etl.py 内嵌 DDL 幂等自建**（`CREATE DATABASE IF NOT EXISTS spacefin_crawler` + `CREATE TABLE IF NOT EXISTS`，DDL 读自 `sql/crawl_schema.sql`）
- **连接账号**：root 仅初始化（建库/建表/`CREATE USER IF NOT EXISTS 'spacefin_crawler_app'` 只授权 `spacefin_crawler.*`）；
  日常读写用专用账号；`.env` 新增 `MYSQL_APP_USER`/`MYSQL_APP_PASSWORD`

### 2.7 etl_report.json 指标集

六块：

1. 输入与去重：当日 raw 行数、处理/跳过文件数、去重后唯一数、重复率
2. 跨日增量：当日新增房源数（新 url_key）、更新房源数（last_seen 更新）、未变化数
3. 累计库存：DWD 主表总行数、**按 city:type 分布**（`stock.city_distribution`，按 district/城市代码分布）
4. 市场留存：days_on_market **均值/中位数/分桶分布**（0-7/8-30/31-90/90+）；实际报告含 `retention` 分桶 + `days_on_market_stats`（mean/median）
5. geocode：现只报告 `miss_communities` 计数（不再落 JSON 文件；miss 清单走 `community_coords` 表 status 状态机）
6. 性能：耗时、吞吐（行/s）、MySQL 写入耗时、湖写入耗时

## 3. 数据模型

### 3.1 DWD 主表（`spacefin_crawler.crawl_housing_sale` / `crawl_housing_rent`）

```sql
CREATE TABLE crawl_housing_sale (
  url_key        VARCHAR(64)  NOT NULL,
  url            VARCHAR(1024) NOT NULL,
  title          VARCHAR(255),
  community      VARCHAR(255),
  district       VARCHAR(32),
  bedrooms       INT,
  halls          INT,
  bathrooms      INT,
  area_sqm       DECIMAL(10,2),
  direction      VARCHAR(16),
  floor          VARCHAR(32),
  building_year  INT,
  building_age   INT,
  parking_count  INT,
  total_price_wan  DECIMAL(12,2),
  unit_price_yuan  INT,
  latitude       DECIMAL(10,6),
  longitude      DECIMAL(10,6),
  first_seen_date  DATE NOT NULL,
  last_seen_date   DATE NOT NULL,
  days_on_market   INT  NOT NULL,
  source         VARCHAR(16),
  etl_ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (url_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`crawl_housing_rent` 同构，字段为 RENT 14 项（title/community/district/bedrooms/halls/bathrooms/
area_sqm/direction/floor/monthly_rent_yuan/rent_type/latitude/longitude/url）。

> 注：sale 表与 rent 表结构一致。除示例 SQL 字段外，两表实际还含：`geocode_status`（补全状态 `pending`/`hit`/`miss`，补全已从 ETL 解耦为独立链路）、`last_seen_page`（本次出现在列表第几页，`NULL`=无页码证据）、`market_status`（`active|absent|unknown|removed`，权威在 `dwd_listing_market_state`，本列仅为缓存）、`removed_date`（首次判 absent/removed 日）、`unit_sig`（turnover 净版配对指纹）、`source`（数据源 qg/free/render），并带 `KEY idx_status_last_seen (market_status, last_seen_date)`；`district` 列为**城市码（非区县）**——广东 21 城存短码，省外行存 URL 子域原文，由 ETL 按 URL 子域重判写入（仅对 2026-08-31 城市归属切换后的新观测生效，历史不回刷）。上面 sale 示例 SQL 为简化展示未列出这些列，实际建表 DDL 以 `sql/crawl_schema.sql` 为准。

### 3.2 ODS 湖 Parquet 分区

`data_lake/housing/dt=YYYY-MM-DD/type={sale|rent}/city={gz}/part-*.parquet`

- 字段 = DWD 同构（完整房源字段）+ url_key + first/last_seen_date + source
- 每日分区为**当日观测集**（当日去重后的房源行，含更新）；重跑覆盖写同日分区（幂等）

## 4. 接口（改造后 etl.py）

```bash
# 增量（Airflow etl_finalize 调用，命令形态摘自 DAG）
python etl.py --date 2026-08-04 \
  --raw-dir output/guangdong/raw \
  --mysql-dsn "mysql+pymysql://spacefin_crawler_app:***@127.0.0.1:3306/spacefin_crawler" \
  --lake-dir data_lake/housing

# 重跑某个观测日（无视处理标记，只那一天）
python etl.py --backfill --date 2026-08-30 --raw-dir output/guangdong/raw \
  --mysql-dsn "mysql+pymysql://spacefin_crawler_app:***@127.0.0.1:3306/spacefin_crawler" \
  --lake-dir data_lake/housing

# 重建全部历史：一次一天，逐 ds 目录循环（不再有一把梭的全量扫描）
for d in output/guangdong/raw/ds=*; do
  python etl.py --backfill --date "${d##*ds=}" --raw-dir output/guangdong/raw \
    --lake-dir data_lake/housing
done
```

## 5. 验收清单（7 条）

1. **url_key.py 单测全过**——覆盖 5 种形态（sale:anjuke / sale:58 / rent:zu / gfangyuan 归一 / md5 兜底），同房源不同 query 归一到同一 key
2. **backfill 成功**——mysql 单容器拉起，`--backfill` 处理 187,313 行：DWD 两表入库行数 = 去重后唯一数（对照 etl_report unique 数）
3. **留存字段正确**——backfill 后 `first_seen_date = last_seen_date = 首次运行日`、`days_on_market = 0`（抽样核对）
4. **湖分区生成**——`data_lake/housing/dt={首次运行日}/...` 分区行数总和 = DWD 入库行数
5. **幂等**——同参数重跑一次，行数/湖文件不变，etl_processed.json 标记全部历史文件已处理
6. **etl_report.json**——六块指标齐全、数值合理（重复率/geocode 命中率按城）
7. **增量模拟**——`raw/ds=<观测日>/` 放模拟新文件，重跑验证只处理该文件；换 `--date` 到别的观测日必须读到 0 文件

## 6. 改造点清单

| 文件                                                    | 改动                                                                                                                                    |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| 新增 `tools/orchestrator/url_key.py`                    | URL 规范化去重键纯函数（可单测）                                                                                                        |
| 新增 `tools/orchestrator/sql/crawl_schema.sql`          | 两张 DWD 表 DDL                                                                                                                         |
| 改造 `tools/orchestrator/etl.py`                        | 增量识别/url_key/COALESCE upsert/Parquet 湖分区/miss 清单/etl_report 六块/`--backfill`+`--date`/`--mysql-dsn`/`--lake-dir`；删 CSV 输出 |
| 新增 `tools/orchestrator/geocode_fill.py`               | 腾讯位置服务 geocoder 补 `community_coords` 词典（每日 6000 配额、`region` 消跨城重名、`level>=10` 采纳、GCJ-02→WGS-84）                |
| 新增 `tools/orchestrator/geocode_backfill.py`           | 读 `community_coords` 词典 hit 行 → 批量 UPDATE DWD `geocode_status IN ('pending','miss')` 且坐标为 null 的行                           |
| `sql/crawl_schema.sql` 新增 `community_coords` 表       | 坐标词典，复合主键 `(city, community)` 隔离跨城重名，状态机 `pending/hit/miss`                                                          |
| `tools/anjuke_crawler/geocoder.py` 新增 `DbGeocoder` 类 | 数据库版词典（替代/补充文件版 `LocalGeocoder`），ETL 落库时查表补坐标                                                                   |
| `.env`                                                  | 新增 `MYSQL_APP_USER`/`MYSQL_APP_PASSWORD`                                                                                              |
| 依赖                                                    | 单一环境 conda `spark` 装 pymysql + pyarrow（见 `tools/orchestrator/requirements.txt`）                                                 |
| Airflow DAG（已接入）                                   | `etl_finalize` 任务 `BashOperator` 调 etl.py（`--date {{ ds }} --raw-dir output/guangdong/raw --lake-dir data_lake/housing`）           |
