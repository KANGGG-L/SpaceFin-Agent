"""
解析子包：把列表页 HTML 抽成结构化房源记录。

- numeric   17 字段纯数值 schema（喂给 L3 AVM 估值）
- advanced  18 字段增强 schema（额外含户型串 / 车位描述）
"""

from .advanced import ADVANCED_HEADERS, parse_advanced_housing_data, save_advanced_csv
from .numeric import SCHEMA_HEADERS, parse_numeric_schema_housing, save_numeric_schema_csv

__all__ = [
    "parse_numeric_schema_housing",
    "save_numeric_schema_csv",
    "SCHEMA_HEADERS",
    "parse_advanced_housing_data",
    "save_advanced_csv",
    "ADVANCED_HEADERS",
]
