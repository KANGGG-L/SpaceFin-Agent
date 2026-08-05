-- SpaceFin 实时 LTV 预警联动（Workflow B：Kafka + Flink 实时接入）。
--
-- 链路：Kafka topic spacefin.cdc.log（tools/stream/producer.py 转发 ODS 变更）
--   → Flink 过滤 loan 事件 → 与 dws_risk_class 做查找 join（取离线 AVM 估值）
--   → LTV = 事件新余额 / 估值 → 越线(>0.85，与 tools/risk/config.py 红线一致)
--   → 写 ads_stream_ltv_alerts（实时告警 inbox）。
--
-- 设计说明：
--   - 只推送不闭环（规划 L1 语义）：余额变更 → 实时算出越线告警推给下游，
--     不做删除/状态机回写，保证 Flink 作业无状态、可随意重启重放。
--   - 估值维度用 JDBC 查找 join（PROCTIME 时间属性），取自离线批的 AVM 结果；
--     这正是「余额一变 → 立刻用最新估值看是否越线」的联动语义。
--   - 重复消费幂等：inbox 表以 event_id 为主键，重放只是再次覆盖同一行。
--   - 凭据为仓库 .env 的 dev-only 值（spacefin_dev_only），仅限本机演练；
--     生产接入应换成最小权限账号并走 secret 管理。

SET 'pipeline.name' = 'spacefin-ltv-realtime';

-- 事件源：producer 平铺后的 loan 事件（顶层含 loan_id/balance 等）。
CREATE TABLE cdc_events (
  event_id BIGINT,
  table_name STRING,
  event_type STRING,
  cdc_ts STRING,
  biz_date STRING,
  loan_id INT,
  customer_id INT,
  collateral_id INT,
  balance DECIMAL(14,2),
  interest_rate DECIMAL(5,2),
  risk_class STRING,
  event_time AS PROCTIME()
) WITH (
  'connector' = 'kafka',
  'topic' = 'spacefin.cdc.log',
  'properties.bootstrap.servers' = 'kafka:9093',
  'properties.group.id' = 'flink-ltv-realtime',
  -- 关键修复（2026-08-05 故障根因）：Flink Kafka source 在无事件时会长时间不发送任何
  -- 请求，客户端/broker 两侧默认 connections.max.idle.ms=9min 会把连接断开；此后该
  -- 连接不会自动重建（实测作业仍 RUNNING 但 source 静默停摆）。这里把客户端侧拉长到
  -- 24h，broker 侧在 docker-compose 里同步配置 KAFKA_CONNECTIONS_MAX_IDLE_MS。
  'properties.connections.max.idle.ms' = '86400000',
  'scan.startup.mode' = 'latest-offset',
  'format' = 'json',
  'json.ignore-parse-errors' = 'true'
);

-- 估值维度：dws_risk_class 由离线批维护，这里只取估值/客户/抵押物侧列。
CREATE TABLE dws_dim (
  loan_id INT,
  customer_id INT,
  collateral_id INT,
  market_valuation DECIMAL(14,2),
  is_high_risk_zone INT,
  PRIMARY KEY (loan_id) NOT ENFORCED
) WITH (
  'connector' = 'jdbc',
  'url' = 'jdbc:mysql://spacefin-mysql:3306/spacefin_crawler',
  'table-name' = 'dws_risk_class',
  'username' = 'root',
  'password' = 'spacefin_dev_only',
  'lookup.cache.max-rows' = '2000',
  'lookup.cache.ttl' = '10s'
);

-- 实时告警 inbox：与 ads_stream_ltv_alerts 表逐列对应（tools/stream/init_db.py 建表）。
CREATE TABLE stream_alert_sink (
  event_id BIGINT,
  loan_id INT,
  customer_id INT,
  collateral_id INT,
  loan_balance DECIMAL(14,2),
  market_valuation DECIMAL(14,2),
  ltv DECIMAL(8,4),
  risk_class STRING,
  is_high_risk_zone INT,
  alert_date DATE
) WITH (
  'connector' = 'jdbc',
  'url' = 'jdbc:mysql://spacefin-mysql:3306/spacefin_crawler',
  'table-name' = 'ads_stream_ltv_alerts',
  'username' = 'root',
  'password' = 'spacefin_dev_only'
);

INSERT INTO stream_alert_sink
SELECT
  e.event_id,
  e.loan_id,
  d.customer_id,
  d.collateral_id,
  e.balance,
  d.market_valuation,
  CAST(e.balance / d.market_valuation AS DECIMAL(8,4)),
  CASE
    WHEN e.balance / d.market_valuation <= 0.60 THEN '正常'
    WHEN e.balance / d.market_valuation <= 0.75 THEN '关注'
    WHEN e.balance / d.market_valuation <= 0.85 THEN '次级'
    WHEN e.balance / d.market_valuation <= 1.00 THEN '可疑'
    ELSE '损失'
  END,
  d.is_high_risk_zone,
  CAST(e.biz_date AS DATE)
FROM cdc_events e
LEFT JOIN dws_dim FOR SYSTEM_TIME AS OF e.event_time d
  ON e.loan_id = d.loan_id
WHERE e.table_name = 'loan'
  AND e.event_type IN ('INSERT', 'UPDATE')
  AND d.market_valuation IS NOT NULL
  AND d.market_valuation > 0
  AND (e.balance / d.market_valuation) > 0.85;
