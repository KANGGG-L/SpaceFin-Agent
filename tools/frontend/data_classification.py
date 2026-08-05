#!/usr/bin/env python
"""S5 导出数据分类分级与脱敏（G2 deliverable）。

仅依赖标准库（stdlib only），便于在导出路径里零成本引入，不污染共享 conda 环境。

设计目标（R-UNW-02 合规）：
- 以「表.列」维度登记每条字段的数据分级（公开/内部/敏感/PII），分级初值来自
  P1 数据源登记（PRESET_SOURCES 的 data_level）——业务库整体属 PII，其上承载的
  客户标识字段（customer_id / customer_name）单独标记为 PII 级。
- 导出时用 level_of(table, col) 取分级，mask_value(level, value) 对 PII 级字段做
  脱敏（c****{后4位}），非 PII 字段保持明文。

为什么做 column 级而不是 table 级：
  table 级把整张客户表标成 PII，但抵押物的 loan_id/collateral_id 并不属于个人敏感信息，
  导出应保留明文以便业务核对（见 app.py::_handle_export 的既有口径）。分级下沉到列，
  才能做到「PII 列强制脱敏、其余明文」的精确控制。
"""

from pages.p1_datasource import PRESET_SOURCES

# PII 白名单：这些列承载个人金融信息，导出强制脱敏，与 PRESET_SOURCES 的 data_level 无关。
PII_COLUMNS = {
    ("customer", "customer_id"),
    ("customer", "customer_name"),
}

# 数据分级常量（与 P1 DATA_LEVELS 对齐）。
LEVEL_PUBLIC = "公开"
LEVEL_INTERNAL = "内部"
LEVEL_SENSITIVE = "敏感"
LEVEL_PII = "PII"

# 分级敏感度顺序；>0 的部分视为「需要管控」，PII 最高。
_LEVEL_RANK = {
    LEVEL_PUBLIC: 0,
    LEVEL_INTERNAL: 1,
    LEVEL_SENSITIVE: 2,
    LEVEL_PII: 3,
}


def _seed_column_levels():
    """从 PRESET_SOURCES 预置行播种 COLUMN_LEVELS。

    PRESET_SOURCES 每项第 6 个字段是 data_level；target_table 是第 9 个（可能为 None）。
    只有带 target_table 的源才登记「表.列」分级——没有落地表（如 Kafka topic）无法
    对应到具体列，跳过；它的分级通过 PII_COLUMNS 这类显式白名单覆盖。
    同一列被多个源登记时取敏感度更高者（业务库 PII 优先于其他）。
    """
    levels = {}
    for row in PRESET_SOURCES:
        data_level = row[5]
        target_table = row[8]
        if not target_table:
            continue
        # 整表分级回退位（每次都尝试登记，更敏感者覆盖）。
        star = (target_table, "*")
        if star not in levels or _LEVEL_RANK.get(data_level, 0) > _LEVEL_RANK.get(levels[star], 0):
            levels[star] = data_level
        # 业务库（PII 源）→ 其下客户相关列单独升级为 PII（见 PII_COLUMNS）。
        for table, col in PII_COLUMNS:
            if table == target_table:
                levels[(table, col)] = LEVEL_PII
    # PII 白名单是显式合规要求，不依赖 PRESET_SOURCES 是否登记了落地表：
    # 业务库 biz_mysql 的 target_table 为 NULL，但其上的 customer_id/customer_name
    # 必须强制 PII 级。仅这两列升级——loan_id/collateral_id 不属于个人敏感信息，
    # 保持明文导出（见 app.py::_handle_export 的既有口径），故不挂整表 PII 回退。
    for table, col in PII_COLUMNS:
        levels[(table, col)] = LEVEL_PII
    return levels


# table.column -> level；导入即播种一次（无 DB 依赖，安全）。
COLUMN_LEVELS = _seed_column_levels()


def level_of(table, col):
    """返回某列的分级；无登记时回退到整表分级，都没有返回 None（调用方按明文处理）。"""
    exact = COLUMN_LEVELS.get((table, col))
    if exact is not None:
        return exact
    return COLUMN_LEVELS.get((table, "*"))


def mask_value(level, value):
    """按分级脱敏一个值。PII 级只留后 4 位（c****{后4位}），其余分级原样返回。

    None / 空值直接透传（空值无可脱敏内容）。
    """
    if level != LEVEL_PII:
        return value
    if value is None:
        return value
    s = str(value)
    if len(s) <= 4:
        return "c****"
    return f"c****{s[-4:]}"


def is_pii(table, col):
    """便捷谓词：列是否处于 PII 级（导出时走 mask_value）。"""
    return level_of(table, col) == LEVEL_PII
