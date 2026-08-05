"""G2 · 导出数据分类分级与脱敏单测（零 DB / 零 HTTP）。

覆盖：
1. COLUMN_LEVELS 已从 PRESET_SOURCES 播种（业务库表 -> 其登记 data_level）；
2. PII 白名单列（customer.customer_id / customer.customer_name）升到 PII 级；
3. level_of 回退到整表分级、未知列返回 None；
4. mask_value 对 PII 走 c****{后4位}，非 PII 原样，None 透传。
"""

import data_classification as dc


def test_column_levels_seeded_from_preset():
    # 带落地表的源按 PRESET_SOURCES 的 data_level 播种整表分级（如公开源 anjuke_sale）。
    assert dc.level_of("crawl_housing_sale", "*") == dc.LEVEL_PUBLIC
    assert dc.level_of("ods_cdc_log", "*") == dc.LEVEL_SENSITIVE
    # PII 白名单列（业务库 biz_mysql 虽 target_table=None，但 customer_id/customer_name
    # 通过显式白名单强制 PII 级）已登记。
    assert ("customer", "customer_id") in dc.COLUMN_LEVELS
    assert dc.level_of("customer", "customer_id") == dc.LEVEL_PII


def test_pii_columns_marked_pii():
    assert dc.level_of("customer", "customer_id") == dc.LEVEL_PII
    assert dc.level_of("customer", "customer_name") == dc.LEVEL_PII
    assert dc.is_pii("customer", "customer_id") is True
    assert dc.is_pii("customer", "loan_id") is False


def test_level_of_falls_back_and_unknown_none():
    # customer 表未挂整表 PII 回退位：非白名单列返回 None（调用方按明文处理）。
    assert dc.level_of("customer", "some_other_col") is None
    # 完全无关的表.列返回 None。
    assert dc.level_of("nonexistent", "x") is None


def test_mask_value_pii_keeps_last4():
    assert dc.mask_value("PII", "C1234567890") == "c****7890"
    assert dc.mask_value("PII", "abc123") == "c****c123"  # 留后 4 位 c123


def test_mask_value_non_pii_passthrough_and_none():
    assert dc.mask_value("内部", "plain-text") == "plain-text"
    assert dc.mask_value("PII", None) is None
    assert dc.mask_value("PII", "ab") == "c****"  # 短于 4 位兜底
