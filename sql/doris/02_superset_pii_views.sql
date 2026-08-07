-- ============================================================================
-- SpaceFin Agent · Superset 脱敏视图（投产加固，L5 合规）
-- ----------------------------------------------------------------------------
-- 定位：Superset 只注册下列视图/聚合视图，禁止直接注册裸明细基表（customer/
--       loan/dws_risk_class 等），从而复用驾驶舱同一套 PII 脱敏口径，消除
--       「驾驶舱合规、Superset 裸数」破防。脱敏表达式与 tools/frontend/
--       data_classification.py:mask_value 同口径：CONCAT('c****', RIGHT(col,4))。
-- 执行：用 MySQL 协议客户端连 Doris FE(9030) 执行本文件，例如：
--       mysql -h127.0.0.1 -P9030 -uroot -e "SOURCE sql/doris/02_superset_pii_views.sql"
-- 兼容：Doris 2.0+。视图采用 DROP+CREATE 保证幂等（Doris 不支持 CREATE OR REPLACE VIEW）。
-- 合规：样例行均为合成文本/合成主键，不含任何真实个人金融信息。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) 聚合视图：3 个 ADS 聚合表的等价视图，作为 Superset 唯一入口（口径与原表一致，
--    留 PII 扩展位——若未来聚合表引入 PII 列，在此处加掩码即可， Superset 无需改）。
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS ads.v_risk_class;
CREATE VIEW ads.v_risk_class
  AS SELECT stat_date, risk_class, loan_count, balance_total, balance_pct, etl_ts
     FROM ads.ads_risk_class;

DROP VIEW IF EXISTS ads.v_avm_precision_trend;
CREATE VIEW ads.v_avm_precision_trend
  AS SELECT stat_date, model_version, model_mape, baseline_mape, n_test
     FROM ads.ads_avm_precision_trend;

DROP VIEW IF EXISTS ads.v_city_avg_price;
CREATE VIEW ads.v_city_avg_price
  AS SELECT stat_date, city_code, listing_count, avg_unit_price_yuan, median_unit_price_yuan
     FROM ads.ads_city_avg_price;

-- ----------------------------------------------------------------------------
-- 2) 受限视图（admin/risk 可见）：合规审计。audit_text 保留用于 I-04 倒排检索，
--    biz_id/operator 已是合成占位（非真实 PII），原样透传；该视图不向普通角色开放。
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS ads.v_compliance_audit;
CREATE VIEW ads.v_compliance_audit
  AS SELECT id, audit_type, audit_text, biz_id, operator, created_at
     FROM ads.ads_compliance_audit;

-- ----------------------------------------------------------------------------
-- 3) 脱敏视图（admin/risk 可见）：LTV 预警明细含 PII 列 customer_id。
--    按 data_classification.mask_value 同口径脱敏——只留后 4 位（c****{后4位}），
--    其余列（贷款号/抵押物/余额/估值/LTV/分类）保持明文供风控核对。普通角色
--    禁止注册此视图，避免客户号裸曝。
--    注：明细在 ods 库，视图建在同库（避免跨库视图），Superset 注册时 schema=ods。
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS ods.v_ltv_alerts_masked;
CREATE VIEW ods.v_ltv_alerts_masked
  AS SELECT
       id,
       loan_id,
       CONCAT('c****', RIGHT(customer_id, 4)) AS customer_id,
       collateral_id,
       loan_balance,
       market_valuation,
       ltv,
       risk_class,
       is_high_risk_zone,
       alert_date
     FROM ods.ods_ads_ltv_alerts;

-- ----------------------------------------------------------------------------
-- 4) 合规约束（流程级，非 SQL 强制）：Superset 仅允许注册上述视图及聚合表，
--    禁止直接注册 ods_customer / ods_loan / dws_risk_class / ads_compliance_audit
--    (基表) / ads_ltv_alerts(若存在) 等裸明细。角色权限见 setup_superset.py。
-- ----------------------------------------------------------------------------
