"""加载 tools/lake 的平铺模块供测试使用。

命名与加载方式的原因同 tools/reporting/tests/g11mods.py：
- minio_sync.py 模块级 `from config import MINIO`，而 `config` 是与 tools/risk、tools/spatial
  撞名的平铺名——直接 sys.path 插入 + `import minio_sync` 会拿到别的目录的 config。
  因此先用唯一名 lake_config 加载 tools/lake/config.py，临时挂到 sys.modules["config"]，
  再以唯一名 lake_minio_sync 加载 minio_sync.py，加载完恢复原 config 条目。
- 被测模块只暴露给测试，不占平铺名，避免多 tests 目录同时收集时互相干扰。
"""

import importlib.util
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LAKE_DIR = os.path.dirname(TESTS_DIR)


def _load_unique(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load():
    lake_config = _load_unique("lake_config", os.path.join(LAKE_DIR, "config.py"))
    prev_config = sys.modules.get("config")
    sys.modules["config"] = lake_config
    try:
        minio_sync = _load_unique("lake_minio_sync", os.path.join(LAKE_DIR, "minio_sync.py"))
    finally:
        if prev_config is not None:
            sys.modules["config"] = prev_config
        else:
            sys.modules.pop("config", None)
    return minio_sync, lake_config


minio_sync, config = _load()
