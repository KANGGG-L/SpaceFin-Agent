"""加载 tools/stream 的平铺模块。测试从这里取被测对象。

producer.py / init_db.py 内部 `import config`（tools/risk），且仓库里存在多个同名
config/store（tools/risk 等）。沿用 tools/risk/tests/riskmods.py 与 tools/cdc/tests/
cdcmods.py 的约定：用唯一模块名 streammods 加载，规避跨目录 conftest 撞名。

隔离原则：本目录不连任何数据库、不连 Kafka/Flink。producer 的 KafkaProducer 由测试
monkeypatch 成录制型假 producer；init_db 的 pymysql 连接换成录制型假连接。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_DIR = os.path.dirname(TESTS_DIR)
RISK_DIR = os.path.join(os.path.dirname(STREAM_DIR), "risk")

_FLAT_NAMES = ("config", "store", "valuation", "producer", "init_db", "main")


def _load_file(unique_name: str, path: str):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load():
    for name in _FLAT_NAMES:
        sys.modules.pop(name, None)
    for d in (RISK_DIR, STREAM_DIR):
        while d in sys.path:
            sys.path.remove(d)
        sys.path.insert(0, d)

    producer = _load_file("cdc_stream_producer", os.path.join(STREAM_DIR, "producer.py"))
    init_db = _load_file("cdc_stream_init_db", os.path.join(STREAM_DIR, "init_db.py"))
    return producer, init_db


producer, init_db = _load()
