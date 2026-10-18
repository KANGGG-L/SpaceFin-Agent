-- ============================================================================
-- SpaceFin Agent · 采集数据 ETL DWD 主表 schema
-- ----------------------------------------------------------------------------
-- 定位：安居客/58 采集房源数据 DWD 主表（独立库 spacefin_crawler，与 L0 业务
--       源库 spacefin 彻底隔离）。每房源一行最新状态 + 市场留存语义。
-- 加载：由 etl.py 幂等执行（CREATE DATABASE/TABLE IF NOT EXISTS），
--       无需手动 SQL、不依赖容器 init 机制。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS spacefin_crawler
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE spacefin_crawler;

-- ----------------------------------------------------------------------------
-- 出售（sale）房源 DWD 主表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_housing_sale (
  url_key          VARCHAR(64)     NOT NULL COMMENT '规范化房源ID（跨日去重键）',
  url              VARCHAR(1024)   NOT NULL COMMENT '原始URL（实测最长982字符，不建索引）',
  title            VARCHAR(255)    NULL     COMMENT '标题',
  community        VARCHAR(255)    NULL     COMMENT '小区名',
  district         VARCHAR(32)     NULL     COMMENT '城市码（非区县）：广东 21 城存短码，省外存 URL 子域原文；由 etl 按 URL 解析写入',
  bedrooms         INT             NULL     COMMENT '室',
  halls            INT             NULL     COMMENT '厅',
  bathrooms        INT             NULL     COMMENT '卫',
  area_sqm         DECIMAL(10, 2)  NULL     COMMENT '面积(㎡)',
  direction        VARCHAR(16)     NULL     COMMENT '朝向',
  floor            VARCHAR(32)     NULL     COMMENT '楼层',
  building_year    INT             NULL     COMMENT '建成年份',
  building_age     INT             NULL     COMMENT '楼龄(年)',
  parking_count    INT             NULL     COMMENT '车位数',
  total_price_wan  DECIMAL(12, 2)  NULL     COMMENT '总价(万)',
  unit_price_yuan  INT             NULL     COMMENT '单价(元/㎡)',
  latitude         DECIMAL(10, 6)  NULL     COMMENT '纬度(ETL阶段geocode补)',
  longitude        DECIMAL(10, 6)  NULL     COMMENT '经度(ETL阶段geocode补)',
  first_seen_date  DATE            NOT NULL COMMENT '首次爬取日期（保留不覆盖）',
  last_seen_date   DATE            NOT NULL COMMENT '最近一次观测到在架的观测日（= 该批 raw 的 ds，非 ETL 重跑日）',
  last_seen_page   SMALLINT        NULL     COMMENT '该次观测所在列表页码；NULL=观测无页码证据，闸门按未知处理',
  days_on_market   INT             NOT NULL COMMENT '市场留存天数=last_seen-first_seen',
  geocode_status   VARCHAR(16)     NULL     COMMENT '坐标补全状态 pending/hit/miss（补全从 ETL 解耦，见 docs/tech/components/crawler-etl.md）',
  market_status    VARCHAR(16)     NOT NULL DEFAULT 'unknown' COMMENT 'active|absent|unknown|removed；DWD 只是缓存，权威在 dwd_listing_market_state',
  removed_date     DATE            NULL     COMMENT 'decide_removal 确认下架的观测日',
  unit_sig         CHAR(12)        NULL     COMMENT '小区+户型+面积+楼层段+价格段哈希，识别重挂（不合并主键）',
  source           VARCHAR(16)     NULL     COMMENT '数据源 qg/free/render',
  etl_ts           TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'ETL写入时间',
  PRIMARY KEY (url_key),
  KEY idx_status_last_seen (market_status, last_seen_date)
) ENGINE = InnoDB COMMENT = '安居客/58 出售房源 DWD 主表 · 每房源最新状态+市场留存';

-- ----------------------------------------------------------------------------
-- 出租（rent/fangyuan）房源 DWD 主表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_housing_rent (
  url_key          VARCHAR(64)     NOT NULL COMMENT '规范化房源ID（跨日去重键）',
  url              VARCHAR(1024)   NOT NULL COMMENT '原始URL（实测最长982字符，不建索引）',
  title            VARCHAR(255)    NULL     COMMENT '标题',
  community        VARCHAR(255)    NULL     COMMENT '小区名',
  district         VARCHAR(32)     NULL     COMMENT '城市码（非区县）：广东 21 城存短码，省外存 URL 子域原文；由 etl 按 URL 解析写入',
  bedrooms         INT             NULL     COMMENT '室',
  halls            INT             NULL     COMMENT '厅',
  bathrooms        INT             NULL     COMMENT '卫',
  area_sqm         DECIMAL(10, 2)  NULL     COMMENT '面积(㎡)',
  direction        VARCHAR(16)     NULL     COMMENT '朝向',
  floor            VARCHAR(32)     NULL     COMMENT '楼层/地铁线',
  monthly_rent_yuan INT            NULL     COMMENT '月租金(元/月)',
  rent_type        VARCHAR(16)     NULL     COMMENT '租期类型(整租/合租)',
  latitude         DECIMAL(10, 6)  NULL     COMMENT '纬度(ETL阶段geocode补)',
  longitude        DECIMAL(10, 6)  NULL     COMMENT '经度(ETL阶段geocode补)',
  first_seen_date  DATE            NOT NULL COMMENT '首次爬取日期（保留不覆盖）',
  last_seen_date   DATE            NOT NULL COMMENT '最近一次观测到在架的观测日（= 该批 raw 的 ds，非 ETL 重跑日）',
  last_seen_page   SMALLINT        NULL     COMMENT '该次观测所在列表页码；NULL=观测无页码证据，闸门按未知处理',
  days_on_market   INT             NOT NULL COMMENT '市场留存天数=last_seen-first_seen',
  geocode_status   VARCHAR(16)     NULL     COMMENT '坐标补全状态 pending/hit/miss（补全从 ETL 解耦）',
  market_status    VARCHAR(16)     NOT NULL DEFAULT 'unknown' COMMENT 'active|absent|unknown|removed；DWD 只是缓存，权威在 dwd_listing_market_state',
  removed_date     DATE            NULL     COMMENT 'decide_removal 确认下架的观测日',
  unit_sig         CHAR(12)        NULL     COMMENT '小区+户型+面积+楼层段+价格段哈希，识别重挂（不合并主键）',
  source           VARCHAR(16)     NULL     COMMENT '数据源 qg/free/render',
  etl_ts           TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'ETL写入时间',
  PRIMARY KEY (url_key),
  KEY idx_status_last_seen (market_status, last_seen_date)
) ENGINE = InnoDB COMMENT = '安居客 zu 出租房源 DWD 主表 · 每房源最新状态+市场留存';

-- ----------------------------------------------------------------------------
-- 小区坐标词典（跨城重名隔离，单小区只调一次腾讯 geocoder）
-- 状态机：pending=待查 / hit=已解析 / miss=查无（miss 每周允许重查一次）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS community_coords (
  city           VARCHAR(16)     NOT NULL COMMENT '城市代码(如 gz)',
  community      VARCHAR(255)    NOT NULL COMMENT '小区名',
  lat            DECIMAL(10, 6)  NULL     COMMENT '纬度 WGS-84',
  lng            DECIMAL(10, 6)  NULL     COMMENT '经度 WGS-84',
  status         VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT 'hit/miss/pending',
  source         VARCHAR(16)     NULL     COMMENT 'tencent/osm/manual',
  level          INT             NULL     COMMENT '腾讯解析精度(>=10 小区/大厦级才采纳)',
  query_count    INT             NOT NULL DEFAULT 0 COMMENT '累计查询次数',
  last_queried_at TIMESTAMP      NULL     COMMENT '上次查询时间(miss 超 7 天可重查)',
  updated_at     TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (city, community)
) ENGINE = InnoDB COMMENT = '小区坐标词典 · (city,community) 复合主键隔离跨城重名';

-- ----------------------------------------------------------------------------
-- 观测完备性账本（计划 S2）：每观测日每 (城,型,波次,容器) 一行
-- 作用：把「今天扫到了哪几页」变成可查的证据。complete=0 的行不认证任何页码，
--       缺席判定只允许出现在「complete=1 且 first_page <= last_seen_page <= contiguous_pages」
--       的行上——否则就是拿「我没看」当「它不在」。
-- 由 census.py 幂等执行（该工具是这张表的唯一写入方）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_census (
  ds               DATE          NOT NULL COMMENT '观测日（= raw/stats 的 ds 目录名，非 ETL 重跑日）',
  city             VARCHAR(8)    NOT NULL COMMENT '城市码',
  typ              VARCHAR(8)    NOT NULL COMMENT '采集任务类型 sale|fangyuan（非 DWD 表名口径）',
  wave             VARCHAR(16)   NOT NULL COMMENT '波次 floor|rescue|depth|unknown；无观测行用 none',
  worker           VARCHAR(16)   NOT NULL COMMENT '产出该行的容器 WORKER_ID；无观测行用 none',
  scope            VARCHAR(8)    NOT NULL DEFAULT '' COMMENT 'head=从第1页起 / deep=断点续爬的深部',
  pages_planned    VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '该波计划页数（来自 redis 波次快照/任务 Hash）',
  pages_ok         SMALLINT      NOT NULL DEFAULT 0 COMMENT '状态 ok 的去重页数',
  first_page       SMALLINT      NOT NULL DEFAULT 0 COMMENT '本页区间起点（head 行=1）',
  contiguous_pages SMALLINT      NOT NULL DEFAULT 0 COMMENT '从 first_page 起连续 ok 的最深页；也是区间终点',
  finish_reason    VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '任务终态（redis 权威；空=查无终态）',
  rows_emitted     INT           NOT NULL DEFAULT 0 COMMENT '该行 raw 落盘行数（含重复，供 ETL 去重对账）',
  distinct_keys    INT           NOT NULL DEFAULT 0 COMMENT '该行内去重 url_key 数',
  key_digest       CHAR(40)      NOT NULL DEFAULT '' COMMENT 'sha1(sorted url_key)：不存全量键也能比出同日是否同一批',
  complete         TINYINT       NOT NULL DEFAULT 0 COMMENT '1=该 (城,型,波,容器) 观测完备，可作缺席证据',
  complete_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'ok / no_observation / gap@N / shallow / reason:X ...',
  created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ds, city, typ, wave, worker),
  KEY idx_ds_city_complete (ds, city, complete)
) ENGINE = InnoDB COMMENT = '采集观测完备性账本 · 每观测日每(城,型,波,容器)一行，缺席判定的唯一证据源';

-- ----------------------------------------------------------------------------
-- 房源市场状态（计划 S3）：每观测日每 url_key 一行，是「在架/消失/下架」的权威表。
-- DWD 的 market_status/removed_date 只是当日缓存，重放与对账一律以本表为准。
-- eligible=0 的行一律 market_status='unknown'：没有合格观测时「没扫到」不等于「已下架」，
--      缺席计数（absent_streak）绝不推进——这是防假绿的地基。
-- 由 market_state.py 幂等写入（该工具是本表唯一写入方，ETL 不写）。
--
-- ⚠ 本段必须与 `tools/orchestrator/market_state.py` 的 `STATE_DDL` **保持逐列一致**，
--   且 **STATE_DDL 是唯一源**（写入方自带 DDL，照 census.py 的模式：DAG 里 market_state
--   任务自己建表，故容器/新环境不依赖本文件也能自愈）。改任何一列都要先改 STATE_DDL
--   再同步到这里；两处一旦漂移，谁先建表另一个就按缺/多列的形态失败。
--   本段存在的唯一理由：新库用 --init-db 一次成型时也要有这张表。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dwd_listing_market_state (
  ds               DATE          NOT NULL COMMENT '观测日',
  url_key          VARCHAR(64)   NOT NULL COMMENT '房源去重键（url_key.make_url_key）',
  city             VARCHAR(32)   NOT NULL COMMENT '城市归属，与 DWD.district 同口径：广东 21 城存短码，省外存子域原文',
  typ              VARCHAR(16)   NOT NULL COMMENT 'sale|fangyuan（采集任务类型口径）',
  market_status    VARCHAR(16)   NOT NULL COMMENT 'active|absent|unknown|removed',
  seen_today       TINYINT       NOT NULL DEFAULT 0 COMMENT '当日是否被观测到在架',
  eligible         TINYINT       NOT NULL DEFAULT 0 COMMENT '1=当日有资格作缺席证据（该 city-day complete=1 且页码在认证区间且首见日达标）',
  eligible_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '不合格原因：no_certified_pages / no_page_evidence / page_out_of_range / before_cutover',
  absent_streak    SMALLINT      NOT NULL DEFAULT 0 COMMENT '连续合格缺席天数；unknown 日不推进也不清零',
  last_seen_page   SMALLINT      NULL COMMENT '最近一次观测到在架时的列表页码',
  last_observed_date DATE        NULL COMMENT '最近一次观测到在架的观测日',
  first_seen_date  DATE          NULL COMMENT '首次观测日（取自 DWD，供 min_age_days 门槛重放/审计）',
  removed_date     DATE          NULL COMMENT 'decide_removal 确认下架的观测日（首次判定日，纯函数自持）',
  decision_reason  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'seen|streak|not_eligible|confirmed_absent',
  created_at       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (ds, url_key),
  KEY idx_ds_city_typ (ds, city, typ),
  KEY idx_ds_status (ds, market_status)
) ENGINE = InnoDB COMMENT = '房源市场状态派生表 · 每日逐 key 一行、可重放；缺席判定（absent）的唯一权威源';

-- ----------------------------------------------------------------------------
-- 专用账号（root 仅用于初始化；日常读写走该账号，只授权本库）
-- 密码由 etl.py 从 .env MYSQL_APP_PASSWORD 读取注入。
-- ----------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'spacefin_crawler_app'@'%' IDENTIFIED BY 'PLACEHOLDER_CHANGED_BY_ETL';
GRANT SELECT, INSERT, UPDATE, DELETE ON spacefin_crawler.* TO 'spacefin_crawler_app'@'%';
FLUSH PRIVILEGES;
