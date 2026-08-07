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
  district         VARCHAR(32)     NULL     COMMENT '区/县',
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
  last_seen_date   DATE            NOT NULL COMMENT '最近爬取日期（每次更新）',
  days_on_market   INT             NOT NULL COMMENT '市场留存天数=last_seen-first_seen',
  geocode_status   VARCHAR(16)     NULL     COMMENT '坐标补全状态 pending/hit/miss（补全从 ETL 解耦，见 docs/tech/components/crawler-etl.md）',
  source           VARCHAR(16)     NULL     COMMENT '数据源 qg/free/render',
  etl_ts           TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'ETL写入时间',
  PRIMARY KEY (url_key)
) ENGINE = InnoDB COMMENT = '安居客/58 出售房源 DWD 主表 · 每房源最新状态+市场留存';

-- ----------------------------------------------------------------------------
-- 出租（rent/fangyuan）房源 DWD 主表
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_housing_rent (
  url_key          VARCHAR(64)     NOT NULL COMMENT '规范化房源ID（跨日去重键）',
  url              VARCHAR(1024)   NOT NULL COMMENT '原始URL（实测最长982字符，不建索引）',
  title            VARCHAR(255)    NULL     COMMENT '标题',
  community        VARCHAR(255)    NULL     COMMENT '小区名',
  district         VARCHAR(32)     NULL     COMMENT '区/县',
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
  last_seen_date   DATE            NOT NULL COMMENT '最近爬取日期（每次更新）',
  days_on_market   INT             NOT NULL COMMENT '市场留存天数=last_seen-first_seen',
  geocode_status   VARCHAR(16)     NULL     COMMENT '坐标补全状态 pending/hit/miss（补全从 ETL 解耦）',
  source           VARCHAR(16)     NULL     COMMENT '数据源 qg/free/render',
  etl_ts           TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'ETL写入时间',
  PRIMARY KEY (url_key)
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
-- 专用账号（root 仅用于初始化；日常读写走该账号，只授权本库）
-- 密码由 etl.py 从 .env MYSQL_APP_PASSWORD 读取注入。
-- ----------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'spacefin_crawler_app'@'%' IDENTIFIED BY 'PLACEHOLDER_CHANGED_BY_ETL';
GRANT SELECT, INSERT, UPDATE, DELETE ON spacefin_crawler.* TO 'spacefin_crawler_app'@'%';
FLUSH PRIVILEGES;
