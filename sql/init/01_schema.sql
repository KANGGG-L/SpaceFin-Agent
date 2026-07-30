-- ============================================================================
-- SpaceFin Agent · L0 业务源库 schema（Sprint 0）
-- ----------------------------------------------------------------------------
-- 定位：模拟金融机构核心业务系统（生产对应物：MySQL/Oracle 核心库）。
--       本文件由 docker-compose 在 MySQL 首次初始化时自动执行
--       （挂载至 /docker-entrypoint-initdb.d）。
-- 合规：所有数据均为合成样本，不含任何真实个人金融信息。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS spacefin
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE spacefin;

-- ----------------------------------------------------------------------------
-- 客户表（合成借款人画像）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer (
  customer_id    INT            NOT NULL                COMMENT '客户号(合成)',
  credit_score   DOUBLE         NULL                    COMMENT '征信评分(合成, 均值680/标准差60)',
  income_monthly DECIMAL(12, 2) NULL                    COMMENT '月收入(合成, 元)',
  debt_ratio     DECIMAL(4, 2)  NULL                    COMMENT '负债收入比(合成, 0-1)',
  created_at     TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  PRIMARY KEY (customer_id)
) ENGINE = InnoDB COMMENT = '客户(借款人)主档 · 合成数据';

-- ----------------------------------------------------------------------------
-- 抵押物表（合成房产 + 空间特征）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collateral (
  collateral_id             INT            NOT NULL     COMMENT '抵押物号(合成)',
  property_addr             VARCHAR(128)   NULL         COMMENT '房产地址(合成, 仅占位)',
  lat                       DOUBLE         NULL         COMMENT '纬度(合成)',
  lng                       DOUBLE         NULL         COMMENT '经度(合成)',
  area                      DOUBLE         NULL         COMMENT '建筑面积(平方米)',
  age                       DOUBLE         NULL         COMMENT '房龄(年)',
  true_market_price         DECIMAL(14, 2) NULL         COMMENT '真实市场价(合成, 供 AVM 验证用)',
  poi_density               DOUBLE         NULL         COMMENT '周边 POI 密度(合成, 0-1)',
  commute_min               DOUBLE         NULL         COMMENT '通勤时长(分钟)',
  is_high_risk_zone         TINYINT        NULL         COMMENT '是否落入高危区 1/0',
  spatial_feat_missing_pct  DECIMAL(4, 2)  NULL         COMMENT '空间特征缺失率(0-1)',
  PRIMARY KEY (collateral_id)
) ENGINE = InnoDB COMMENT = '抵押物(房产)主档 + 空间特征 · 合成数据';

-- ----------------------------------------------------------------------------
-- 贷款表（关联客户与抵押物）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loan (
  loan_id          INT            NOT NULL              COMMENT '贷款号(合成)',
  customer_id      INT            NOT NULL              COMMENT '客户号',
  collateral_id    INT            NOT NULL              COMMENT '抵押物号',
  loan_amount      DECIMAL(14, 2) NULL                  COMMENT '放款金额(元)',
  balance          DECIMAL(14, 2) NULL                  COMMENT '贷款余额(元)',
  interest_rate    DECIMAL(5, 2)  NULL                  COMMENT '年利率(%)',
  risk_class       VARCHAR(8)     NOT NULL              COMMENT '五级分类',
  origination_date DATE           NULL                  COMMENT '放款日期',
  created_at       TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  PRIMARY KEY (loan_id),
  CONSTRAINT fk_loan_customer   FOREIGN KEY (customer_id)   REFERENCES customer (customer_id),
  CONSTRAINT fk_loan_collateral FOREIGN KEY (collateral_id) REFERENCES collateral (collateral_id),
  CONSTRAINT chk_risk_class CHECK (risk_class IN ('正常', '关注', '次级', '可疑', '损失')),
  KEY idx_loan_customer   (customer_id),
  KEY idx_loan_collateral (collateral_id),
  KEY idx_loan_risk_class (risk_class)
) ENGINE = InnoDB COMMENT = '贷款台账 · 合成数据';
