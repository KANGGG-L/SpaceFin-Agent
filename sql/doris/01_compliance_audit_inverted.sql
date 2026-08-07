-- ============================================================================
-- SpaceFin Agent · Doris 倒排索引合规审计检索（I-04，L5 合规）
-- ----------------------------------------------------------------------------
-- 定位：在 Doris ADS 层建一张合规审计表，并在审计文本列上建中文倒排索引
--       （USING INVERTED, chinese parser），使合规/风控部门可毫秒级定位
--       "包装流水"、"过桥资金" 等敏感词，满足 I-04 接口契约。
-- 执行：用 MySQL 协议客户端连 Doris FE(9030) 执行本文件，例如：
--       mysql -h127.0.0.1 -P9030 -uroot -e "SOURCE sql/doris/01_compliance_audit_inverted.sql"
-- 兼容：Doris 2.0+（中文分词 parser=chinese 自 2.0 起支持）。
-- 合规：样例行均为合成文本，不含任何真实个人金融信息。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS ads;

-- ----------------------------------------------------------------------------
-- 合规审计表（审计文本为倒排索引检索目标）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ads.ads_compliance_audit (
  id          BIGINT       NOT NULL COMMENT '审计记录ID(合成, 显式赋值)',
  audit_type  VARCHAR(32)  NOT NULL COMMENT '审计类型: loan_application/collection_flow/early_warning/export',
  audit_text  STRING       NOT NULL COMMENT '审计文本(倒排索引检索目标, 含敏感词)',
  biz_id      VARCHAR(64)  NULL     COMMENT '关联业务单号(脱敏占位, 合成)',
  operator    VARCHAR(32)  NULL     COMMENT '操作人(合成)',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '审计时间'
) ENGINE = OLAP
UNIQUE KEY(id)
DISTRIBUTED BY HASH(id) BUCKETS 1
PROPERTIES (
  "replication_num" = "1",
  "enable_unique_key_merge_on_write" = "true"
);

-- ----------------------------------------------------------------------------
-- 样例行（含敏感词，用于演示 I-04 毫秒级定位）
-- ----------------------------------------------------------------------------
INSERT INTO ads.ads_compliance_audit (id, audit_type, audit_text, biz_id, operator, created_at) VALUES
  (1, 'loan_application', '客户A申请经营贷，资料中存在包装流水嫌疑，近6个月流水与申报收入明显背离，建议人工复核。', 'LOAN-20260801-001', 'risk_01', '2026-08-01 09:12:00'),
  (2, 'collection_flow',  '贷后检查发现该笔通过过桥资金垫资过账，还款来源不真实，触发反欺诈预警。', 'LOAN-20260801-002', 'postloan_01', '2026-08-01 10:30:00'),
  (3, 'loan_application', '收入证明为虚假收入证明，单位公章经核验为PS生成，已驳回申请。', 'LOAN-20260801-003', 'risk_02', '2026-08-01 11:05:00'),
  (4, 'early_warning',    '抵押物估值波动在正常区间，无异常，五级分类维持正常类。', 'LOAN-20260801-004', 'risk_01', '2026-08-01 14:20:00'),
  (5, 'collection_flow',  '资金流向多层账户拆分，疑似利用包装流水掩盖真实用途，移交合规审查。', 'LOAN-20260802-001', 'postloan_02', '2026-08-02 09:48:00'),
  (6, 'export',           '数据分析师导出资产质量报表，已脱敏，导出行为记入 ads_export_audit。', 'EXPORT-20260802-001', 'da_01', '2026-08-02 15:10:00');

-- ----------------------------------------------------------------------------
-- 在审计文本列上建 Doris 倒排索引（中文分词）
--   建完索引需 BUILD INDEX 使其在存量数据上生效。
-- ----------------------------------------------------------------------------
ALTER TABLE ads.ads_compliance_audit
  ADD INDEX IF NOT EXISTS idx_audit_text(audit_text) USING INVERTED PROPERTIES("parser" = "chinese");

BUILD INDEX idx_audit_text ON ads.ads_compliance_audit;

-- ----------------------------------------------------------------------------
-- 检索示例（I-04 契约形态：WHERE col MATCH 'keyword'）
--   SELECT * FROM ads.ads_compliance_audit WHERE audit_text MATCH '包装流水';
-- ----------------------------------------------------------------------------
